"""UPnP / NAT-PMP for the LAN (built-in ``miniupnpd`` supervisor).

Lets games, consoles, torrent clients and cameras open their own inbound ports
on the gateway instead of the admin writing port-forwards by hand. OFF by
default and tightly scoped:

* the daemon only listens on the client subnet (never on the uplink side);
* ``secure_mode`` (default on) = a device may only map ports to ITSELF;
* a port range + a blocked-port list bound what can be opened;
* the dashboard / SSH / DNS ports are always blocked.

Two situations, because ``miniupnpd`` refuses to work unless the address it
advertises is PUBLIC:

``wan`` (PPPoE on ``ppp0``)
    the box owns the public address — mappings work directly.
``lan`` (box behind the ISP router)
    the public address belongs to the router. With ``relay`` on, every mapping
    a device creates is mirrored to the router with UPnP
    (``upnpc``): router port → this box's uplink address → the device. The
    router must have UPnP enabled, and must not be behind CGNAT.

Kernel side: ``inet quota_upnp`` is app-owned and NEVER flushed while running
(miniupnpd keeps its rules in it). Only its DNAT hook is guarded so a mapping
can never capture the LAN's own outbound traffic (single-NIC setups share the
interface with the uplink). IPv4 only — ``ipv6_disable=yes`` (documented).

Everything that touches the host takes an injectable ``run`` / paths so tests
never need a kernel (``tests/test_upnp.py``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("quota.upnp")

RunCommand = Callable[[list[str]], tuple[int, str]]

TABLE = "quota_upnp"
CHAIN_FORWARD = "miniupnpd"
CHAIN_DNAT = "prerouting_miniupnpd"
CHAIN_SNAT = "postrouting_miniupnpd"
PPP_IFACE = "ppp0"
#: Ports no device may ever open (dashboard + remote admin + DNS/DHCP).
ALWAYS_BLOCKED = (22, 23, 53, 67, 68, 80, 443, 3389)
SETTING_KEY = "upnp_config"
MIN_LIFETIME = 120
RELAY_TTL = 60.0         # seconds between router checks
TABLE_TTL = 60.0         # seconds between kernel-table checks
_IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_PROTOS = ("TCP", "UDP")


# -- settings -----------------------------------------------------------------

@dataclass
class UpnpSettings:
    enabled: bool = False
    #: also answer NAT-PMP / PCP (Apple, some consoles)
    natpmp: bool = True
    #: a device may only map ports to its OWN address
    secure_mode: bool = True
    port_min: int = 1024
    port_max: int = 65535
    #: longest lease a device may request (seconds); miniupnpd re-asks
    max_lifetime: int = 86400
    #: extra ports that can never be mapped (on top of ALWAYS_BLOCKED)
    blocked_ports: list[int] = field(default_factory=list)
    #: LAN mode: mirror mappings to the ISP router with UPnP
    relay: bool = True
    #: LAN mode: public IP of the router, only when it can't be read by UPnP
    ext_ip: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "natpmp": self.natpmp,
            "secure_mode": self.secure_mode,
            "port_min": self.port_min, "port_max": self.port_max,
            "max_lifetime": self.max_lifetime,
            "blocked_ports": list(self.blocked_ports),
            "relay": self.relay, "ext_ip": self.ext_ip,
        }


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_settings(raw: Any) -> UpnpSettings:
    """Sanitize anything stored/received into a safe :class:`UpnpSettings`."""
    data = raw if isinstance(raw, dict) else {}
    port_min = _clamp(data.get("port_min"), 1, 65535, 1024)
    port_max = _clamp(data.get("port_max"), 1, 65535, 65535)
    if port_min > port_max:
        port_min, port_max = port_max, port_min
    blocked: list[int] = []
    for item in data.get("blocked_ports") or []:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 0 < port < 65536 and port not in blocked and port not in ALWAYS_BLOCKED:
            blocked.append(port)
    ext_ip = str(data.get("ext_ip") or "").strip()
    try:
        ext = ipaddress.ip_address(ext_ip) if ext_ip else None
    except ValueError:
        ext = None
    return UpnpSettings(
        enabled=bool(data.get("enabled", False)),
        natpmp=bool(data.get("natpmp", True)),
        secure_mode=bool(data.get("secure_mode", True)),
        port_min=port_min, port_max=port_max,
        max_lifetime=_clamp(data.get("max_lifetime"), MIN_LIFETIME, 604800, 86400),
        blocked_ports=sorted(blocked)[:64],
        relay=bool(data.get("relay", True)),
        ext_ip=str(ext) if ext is not None and ext.version == 4 and ext.is_global else "",
    )


@dataclass(frozen=True)
class UpnpContext:
    """Where the box sits; derived from the live config on every tick."""

    topology: str = "lan"          # "lan" | "wan"
    gateway_ip: str = ""           # LAN-side address (UPnP listens here)
    lan_prefix: int = 24
    uplink_ip: str = ""            # LAN mode: the box's address toward the router
    uplink_iface: str = ""         # LAN mode: NIC facing the router
    ppp_iface: str = PPP_IFACE


# -- pure renderers -------------------------------------------------------------

def _valid_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def is_public_ipv4(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_global


def render_conf(settings: UpnpSettings, ctx: UpnpContext, *, ext_iface: str,
                ext_ip: str, lease_file: Path) -> str:
    """The ``miniupnpd.conf`` text. Raises ValueError on an unusable input."""
    if not _IFACE.fullmatch(ext_iface or ""):
        raise ValueError("invalid external interface")
    if not _valid_ipv4(ctx.gateway_ip):
        raise ValueError("invalid LAN gateway address")
    if ext_ip and not is_public_ipv4(ext_ip):
        raise ValueError("external address is not public")
    prefix = _clamp(ctx.lan_prefix, 8, 30, 24)
    network = ipaddress.ip_interface(f"{ctx.gateway_ip}/{prefix}").network
    lines = [
        "# generated by Netrex - do not edit (overwritten on every change)",
        f"ext_ifname={ext_iface}",
    ]
    if ext_ip:
        lines.append(f"ext_ip={ext_ip}")
    lines += [
        # IP/mask, never the NIC name: on a single-NIC box the NIC also carries
        # the uplink address and the daemon must not listen toward the router.
        f"listening_ip={ctx.gateway_ip}/{prefix}",
        "http_port=0",
        "enable_upnp=yes",
        f"enable_natpmp={'yes' if settings.natpmp else 'no'}",
        f"secure_mode={'yes' if settings.secure_mode else 'no'}",
        "ipv6_disable=yes",
        "system_uptime=yes",
        f"lease_file={lease_file}",
        f"min_lifetime={MIN_LIFETIME}",
        f"max_lifetime={settings.max_lifetime}",
        f"upnp_table_name={TABLE}",
        f"upnp_nat_table_name={TABLE}",
        f"upnp_forward_chain={CHAIN_FORWARD}",
        f"upnp_nat_chain={CHAIN_DNAT}",
        f"upnp_nat_postrouting_chain={CHAIN_SNAT}",
        "friendly_name=Netrex",
        "model_name=Netrex",
    ]
    blocked = sorted(set(ALWAYS_BLOCKED) | set(settings.blocked_ports))
    # deny rules first: miniupnpd applies the first rule that matches.
    for port in blocked:
        lines.append(f"deny {port} {network} 0-65535")
        lines.append(f"deny 0-65535 {network} {port}")
    lines.append(f"allow {settings.port_min}-{settings.port_max} {network} "
                 f"{settings.port_min}-{settings.port_max}")
    lines.append("deny 0-65535 0.0.0.0/0 0-65535")
    return "\n".join(lines) + "\n"


def dnat_guard(topology: str, uplink_ip: str, ppp_iface: str = PPP_IFACE
               ) -> list[str]:
    """Match that decides which packets may reach the mapping rules."""
    if topology == "wan":
        if not _IFACE.fullmatch(ppp_iface or ""):
            raise ValueError("invalid PPP interface")
        return ["iifname", f'"{ppp_iface}"']
    if not _valid_ipv4(uplink_ip):
        raise ValueError("LAN mode needs the uplink address")
    return ["ip", "daddr", uplink_ip]


def render_nft(guard: list[str]) -> str:
    """Whole ``inet quota_upnp`` table (applied only when it does not exist)."""
    return "\n".join([
        f"table inet {TABLE} {{",
        f"  chain forward {{ type filter hook forward priority 0; "
        f"policy accept; jump {CHAIN_FORWARD}; }}",
        f"  chain {CHAIN_FORWARD} {{ }}",
        f"  chain prerouting {{ type nat hook prerouting priority -100; "
        f"policy accept; {' '.join(guard)} jump {CHAIN_DNAT}; }}",
        f"  chain {CHAIN_DNAT} {{ }}",
        f"  chain {CHAIN_SNAT} {{ }}",
        "}", ""])


# -- lease file ------------------------------------------------------------------

@dataclass(frozen=True)
class Mapping:
    proto: str
    ext_port: int
    ip: str
    port: int
    expires: int
    desc: str


def parse_leases(text: str) -> list[Mapping]:
    """``proto:eport:iaddr:iport:expiry:desc`` lines of miniupnpd's lease file."""
    result: list[Mapping] = []
    for line in (text or "").splitlines():
        parts = line.strip().split(":", 5)
        if len(parts) < 5:
            continue
        proto = parts[0].upper()
        if proto not in _PROTOS or not _valid_ipv4(parts[2]):
            continue
        try:
            ext_port, port = int(parts[1]), int(parts[3])
            expires = int(parts[4] or 0)
        except ValueError:
            continue
        if not (0 < ext_port < 65536 and 0 < port < 65536):
            continue
        result.append(Mapping(proto, ext_port, parts[2], port, expires,
                              (parts[5] if len(parts) > 5 else "")[:80]))
    return result


def parse_external_ip(text: str) -> str:
    """``ExternalIPAddress = x`` from ``upnpc -l`` output."""
    match = re.search(r"ExternalIPAddress\s*=\s*(\d{1,3}(?:\.\d{1,3}){3})", text or "")
    return match.group(1) if match else ""


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    except OSError as exc:  # noqa: BLE001
        return 126, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# -- manager ------------------------------------------------------------------------

class UpnpManager:
    """Keeps ``miniupnpd`` + ``inet quota_upnp`` + the router relay converged.

    :meth:`sync` is idempotent and synchronous (callers use
    ``asyncio.to_thread``); it never raises.
    """

    def __init__(self, state_dir: str | Path = "data", *,
                 run: RunCommand | None = None,
                 which: Callable[[str], str | None] | None = None,
                 proc_root: str | Path = "/proc",
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 wall: Callable[[], float] = time.time,
                 install_script: list[str] | None = None,
                 nft_build: Callable[[str], bool] | None = None) -> None:
        self._run = run or _default_run
        self._which = which or shutil.which
        self._proc = Path(proc_root)
        self._mono = monotonic
        self._sleep = sleep
        self._wall = wall
        self._dir = Path(state_dir)
        self.conf_path = self._dir / "miniupnpd.conf"
        self.lease_path = self._dir / "upnp.leases"
        self.pid_path = self._dir / "miniupnpd.pid"
        self._nft_path = self._dir / "upnp.nft"
        self._install_cmd = install_script or [
            "sh", "-c",
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--no-install-recommends miniupnpd-nftables miniupnpc"]
        self._nft_build_check = nft_build
        self._lock = threading.RLock()
        self._conf_hash = ""
        self._guard = ""
        self._table_checked = -1e9
        self._cleaned = False
        self._stock_unit_stopped = False
        self._router_ip = ""
        self._router_checked = -1e9
        self._router_ok = False
        self._relayed: dict[tuple[str, int], int] = {}
        self._relay_errors: dict[tuple[str, int], str] = {}
        self._installing = False
        self._last_error = ""
        self._state = "off"
        self._reason = ""
        self._ext_ip = ""

    # -- helpers --------------------------------------------------------------
    def binary(self) -> str:
        return self._which("miniupnpd") or ""

    def client_binary(self) -> str:
        return self._which("upnpc") or ""

    def _pid(self) -> int:
        try:
            pid = int(self.pid_path.read_text().strip())
            comm = (self._proc / str(pid) / "comm").read_text().strip()
        except (OSError, ValueError):
            return 0
        return pid if comm.startswith("miniupnpd") else 0

    def _nft_build(self) -> bool:
        """True when the daemon was built with the nftables backend."""
        if self._nft_build_check is not None:
            return self._nft_build_check(self.binary())
        try:
            with open(self.binary(), "rb") as stream:
                return b"mnl_socket" in stream.read(4_000_000)
        except OSError:
            return False

    def running(self) -> bool:
        return self._pid() > 0

    def _stop_daemon(self) -> None:
        pid = self._pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = self._mono() + 3.0
            while self._mono() < deadline and (self._proc / str(pid)).exists():
                self._sleep(0.1)
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass
        self._conf_hash = ""

    def _table_exists(self) -> bool:
        code, _ = self._run(["nft", "list", "table", "inet", TABLE])
        return code == 0

    def _ensure_table(self, guard: list[str]) -> bool:
        """Create the table once; later only swap the DNAT guard if it changed.

        The table is looked at again at most every ``TABLE_TTL`` seconds (an
        ``nftables`` restart elsewhere removes it, and with it every mapping:
        the daemon is then restarted so it reloads them from its lease file).
        """
        wanted = " ".join(guard)
        now = self._mono()
        if wanted == self._guard and now - self._table_checked < TABLE_TTL:
            return True
        self._table_checked = now
        if not self._table_exists():
            if self.running():
                self._conf_hash = ""      # force a restart: its rules are gone
            self._dir.mkdir(parents=True, exist_ok=True)
            self._nft_path.write_text(render_nft(guard), encoding="utf-8")
            code, out = self._run(["nft", "-f", str(self._nft_path)])
            if code != 0:
                self._last_error = f"nft table failed: {out.strip()[:160]}"
                return False
            self._guard = wanted
            return True
        code, out = self._run(["nft", "list", "chain", "inet", TABLE, "prerouting"])
        if code == 0 and f"{wanted} jump {CHAIN_DNAT}" in out:
            self._guard = wanted
            return True
        steps = [
            ["nft", "flush", "chain", "inet", TABLE, "prerouting"],
            ["nft", "add", "rule", "inet", TABLE, "prerouting", *guard,
             "jump", CHAIN_DNAT],
        ]
        for step in steps:
            code, out = self._run(step)
            if code != 0:
                self._last_error = f"nft guard failed: {out.strip()[:160]}"
                return False
        self._guard = wanted
        return True

    def _drop_table(self) -> None:
        if self._table_exists():
            self._run(["nft", "delete", "table", "inet", TABLE])
        self._guard = ""
        self._table_checked = -1e9

    def _ppp_address(self, ctx: UpnpContext) -> str:
        if not _IFACE.fullmatch(ctx.ppp_iface or ""):
            return ""
        code, out = self._run(["ip", "-4", "-o", "addr", "show", "dev", ctx.ppp_iface])
        if code != 0:
            return ""
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        return match.group(1) if match else ""

    def _router_external_ip(self, ctx: UpnpContext, *, force: bool = False) -> str:
        """Ask the ISP router (UPnP) for its WAN address; cached."""
        now = self._mono()
        if not force and now - self._router_checked < RELAY_TTL:
            return self._router_ip
        self._router_checked = now
        self._router_ip = ""
        self._router_ok = False
        if not self.client_binary() or not ctx.uplink_ip:
            return ""
        code, out = self._run(["upnpc", "-m", ctx.uplink_ip, "-s"])
        ip = parse_external_ip(out) if code == 0 else ""
        if ip:
            self._router_ok = True
            self._router_ip = ip
        return ip

    # -- relay ------------------------------------------------------------------
    def _relay(self, ctx: UpnpContext, mappings: list[Mapping]) -> None:
        now = int(self._wall())
        wanted: dict[tuple[str, int], Mapping] = {
            (m.proto, m.ext_port): m for m in mappings}
        for key in [k for k in self._relayed if k not in wanted]:
            code, _ = self._run(["upnpc", "-m", ctx.uplink_ip, "-d",
                                 str(key[1]), key[0]])
            log.info("upnp relay: removed %s/%d from the router (rc=%d)",
                     key[0], key[1], code)
            self._relayed.pop(key, None)
            self._relay_errors.pop(key, None)
        for key, mapping in wanted.items():
            remaining = mapping.expires - now if mapping.expires > now else 0
            lease = max(MIN_LIFETIME, remaining) if remaining else 3600
            if key in self._relayed and mapping.expires <= self._relayed[key] + 30:
                continue
            code, out = self._run([
                "upnpc", "-m", ctx.uplink_ip, "-e", f"Netrex {mapping.desc}"[:60],
                "-a", ctx.uplink_ip, str(mapping.ext_port), str(mapping.ext_port),
                mapping.proto, str(lease)])
            if code == 0 and "is redirected to" in out:
                self._relayed[key] = mapping.expires
                self._relay_errors.pop(key, None)
            else:
                self._relay_errors[key] = (out.strip().splitlines() or ["failed"])[-1][:120]
                log.warning("upnp relay: router refused %s/%d: %s", key[0],
                            key[1], self._relay_errors[key])

    def mappings(self) -> list[dict[str, Any]]:
        try:
            text = self.lease_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        now = int(self._wall())
        result = []
        for item in parse_leases(text):
            key = (item.proto, item.ext_port)
            result.append({
                "proto": item.proto, "ext_port": item.ext_port, "ip": item.ip,
                "port": item.port, "desc": item.desc,
                "expires_in": max(0, item.expires - now) if item.expires else 0,
                "relayed": key in self._relayed,
                "relay_error": self._relay_errors.get(key, ""),
            })
        return result

    # -- main entry ---------------------------------------------------------------
    def sync(self, settings: UpnpSettings, ctx: UpnpContext) -> dict[str, Any]:
        with self._lock:
            try:
                self._sync(settings, ctx)
            except Exception as exc:  # noqa: BLE001 — never break the tick
                log.exception("upnp sync failed")
                self._state, self._reason = "error", str(exc)[:160]
            return self.status(settings, ctx)

    def _set(self, state: str, reason: str = "") -> None:
        self._state, self._reason = state, reason

    def _sync(self, settings: UpnpSettings, ctx: UpnpContext) -> None:
        self._last_error = ""
        if not settings.enabled:
            if self._cleaned and self._state == "off":
                return                       # nothing to do, no subprocess per tick
            if self.running():
                self._stop_daemon()
            if self._table_exists():
                self._drop_table()
            self._cleaned = True
            if self._relayed and ctx.uplink_ip and self.client_binary():
                self._relay(ctx, [])
            self._relayed.clear()
            self._set("off")
            return
        self._cleaned = False
        if self._installing:
            self._set("installing")
            return
        if not self.binary():
            self._set("not_installed")
            return
        if not self._nft_build():
            self._set("wrong_backend", "the installed miniupnpd is the iptables "
                      "build - install miniupnpd-nftables")
            return
        wan = ctx.topology == "wan"
        if wan:
            ext_iface = ctx.ppp_iface
            ext_ip = self._ppp_address(ctx)
            if not ext_ip:
                self._stop_if_running("WAN link is down")
                self._set("waiting", "the PPPoE link has no address yet")
                return
            guard_ip = ""
        else:
            if not (_valid_ipv4(ctx.uplink_ip) and _IFACE.fullmatch(ctx.uplink_iface or "")):
                self._stop_if_running("no uplink address")
                self._set("waiting", "the uplink address is not configured")
                return
            ext_iface = ctx.uplink_iface
            ext_ip = settings.ext_ip or self._router_external_ip(ctx)
            guard_ip = ctx.uplink_ip
            if not ext_ip:
                self._stop_if_running("router public address unknown")
                self._set("no_router_upnp" if settings.relay else "need_ext_ip",
                          "the ISP router did not answer UPnP" if settings.relay
                          else "set the router's public IP")
                return
        if not is_public_ipv4(ext_ip):
            self._stop_if_running("no public address")
            self._set("cgnat", f"{ext_ip} is not a public address - port "
                      "mapping cannot work behind carrier-grade NAT")
            return
        self._ext_ip = ext_ip
        guard = dnat_guard(ctx.topology, guard_ip, ctx.ppp_iface)
        if not self._ensure_table(guard):
            self._set("error", self._last_error)
            return
        conf = render_conf(settings, ctx, ext_iface=ext_iface, ext_ip=ext_ip,
                           lease_file=self.lease_path)
        digest = hashlib.sha256(conf.encode()).hexdigest()
        if not self.running() or digest != self._conf_hash:
            if not self._start(conf, digest):
                self._set("error", self._last_error)
                return
        if not wan and settings.relay and self.client_binary():
            self._relay_all(ctx)
        self._set("running")

    def _stop_if_running(self, why: str) -> None:
        if self.running():
            log.warning("upnp: stopping the daemon (%s)", why)
            self._stop_daemon()

    def _relay_all(self, ctx: UpnpContext) -> None:
        try:
            text = self.lease_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        self._relay(ctx, parse_leases(text))

    def _start(self, conf: str, digest: str) -> bool:
        self._dir.mkdir(parents=True, exist_ok=True)
        # The distro unit would fight over the same ports with its own config.
        if not self._stock_unit_stopped:
            code, _ = self._run(["systemctl", "is-active", "--quiet", "miniupnpd"])
            if code == 0:
                self._run(["systemctl", "disable", "--now", "miniupnpd"])
            self._stock_unit_stopped = True
        if self.running():
            self._stop_daemon()
        tmp = self.conf_path.with_suffix(".tmp")
        tmp.write_text(conf, encoding="utf-8")
        os.replace(tmp, self.conf_path)
        code, out = self._run([self.binary(), "-f", str(self.conf_path),
                               "-P", str(self.pid_path)])
        if code != 0:
            self._last_error = f"miniupnpd failed to start: {out.strip()[-160:]}"
            return False
        deadline = self._mono() + 3.0
        while self._mono() < deadline and not self.running():
            self._sleep(0.1)
        if not self.running():
            self._last_error = "miniupnpd exited right after start"
            return False
        self._conf_hash = digest
        log.info("upnp: miniupnpd started (ext %s)", self._ext_ip)
        return True

    # -- admin actions -------------------------------------------------------------
    def clear(self, settings: UpnpSettings, ctx: UpnpContext) -> dict[str, Any]:
        """Forget every mapping (stop, drop the lease file, start again)."""
        with self._lock:
            self._stop_daemon()
            try:
                self.lease_path.unlink()
            except FileNotFoundError:
                pass
            if self._relayed and ctx.uplink_ip and self.client_binary():
                self._relay(ctx, [])
            self._relayed.clear()
            self._relay_errors.clear()
        return self.sync(settings, ctx)

    def remove(self, ctx: UpnpContext, proto: str, port: int) -> bool:
        """Forget ONE mapping.

        ``secure_mode`` stops anyone but the owning device from deleting a
        mapping over UPnP, so the admin's removal edits the lease file and
        restarts the daemon (it reloads every other mapping from that file).
        """
        proto = (proto or "").upper()
        if proto not in _PROTOS or not 0 < int(port) < 65536:
            return False
        with self._lock:
            try:
                lines = self.lease_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return False
            prefix = f"{proto}:{int(port)}:"
            keep = [line for line in lines if not line.upper().startswith(prefix)]
            if len(keep) == len(lines):
                return False
            self._stop_daemon()          # it rewrites nothing on SIGTERM
            tmp = self.lease_path.with_suffix(".tmp")
            tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            os.replace(tmp, self.lease_path)
            return True

    def install(self) -> dict[str, Any]:
        """Install the daemon (+ client) in the background via apt."""
        with self._lock:
            if self._installing or (self.binary() and self.client_binary()):
                return self.status(None, None)
            if not self._which("apt-get"):
                self._last_error = ("one-click install needs apt (Debian/Ubuntu/Kali); "
                                    "install miniupnpd (nftables build) and miniupnpc")
                return self.status(None, None)
            self._installing = True
            self._last_error = ""
        threading.Thread(target=self._install_worker, name="upnp-install",
                         daemon=True).start()
        return self.status(None, None)

    def _install_worker(self) -> None:
        try:
            code, out = self._run(self._install_cmd)
            if code != 0:
                self._last_error = f"install failed: {out.strip()[-160:]}"
            else:
                # the distro starts its own unit with a default config
                self._run(["systemctl", "disable", "--now", "miniupnpd"])
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"install failed: {exc}"
        finally:
            self._installing = False

    def status(self, settings: UpnpSettings | None,
               ctx: UpnpContext | None) -> dict[str, Any]:
        return {
            "state": "installing" if self._installing else self._state,
            "reason": self._reason or self._last_error,
            "installed": bool(self.binary()),
            "client_installed": bool(self.client_binary()),
            "running": self.running(),
            "ext_ip": self._ext_ip if self._state == "running" else "",
            "topology": ctx.topology if ctx else "",
            "mappings": self.mappings() if self._state == "running" else [],
            "install_cmd": "sudo apt install miniupnpd-nftables miniupnpc",
        }

    def stop(self) -> None:
        """Shutdown hook: stop the daemon; keep the lease file so mappings return."""
        with self._lock:
            self._stop_daemon()
