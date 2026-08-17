"""TopologyManager — apply LAN/WAN topology live, entirely from the dashboard.

v18 left the physical topology change to the setup script (``QUOTA_TOPOLOGY``)
and the dashboard only persisted a *preference* that applied on the next
restart. That forced a non-technical admin into a terminal for the exact
operation they most need the panel for. v19 moves the apply into the panel:
the WAN tab collects the PPPoE credentials, calls :meth:`TopologyManager.apply`
which — as root (the app runs under systemd) — rewrites ``config.yaml``, runs
the runtime applier ``scripts/topology.sh`` (NIC + dnsmasq + the PPPoE dial),
persists the DB override, and schedules a detached self-restart.

Two invariants this module exists to guarantee (both were broken in the field):

1. **config.yaml and the DB never disagree.** The script *and* the setting are
   written in the same apply, so the next boot cannot pick one and ignore the
   other (the v18 revert bug: the setup script rewrote config.yaml to ``lan``
   but the DB still held ``topology_source=dashboard + topology=wan``, so the
   app re-forced WAN on every restart).
2. **The LAN reality survives a WAN experiment.** Switching to WAN erases
   ``dhcp.router_ip`` / ``dns_servers`` / ``uplink_subnet`` from the active
   config. The original values are snapshotted into dedicated ``lan_*`` keys
   *before* the switch, so the Revert button restores exactly what was there
   instead of guessing 192.168.1.1.

Credentials are passed to the applier through the ENVIRONMENT, never argv, so
an ISP password cannot be read out of ``ps``.

Everything external is injectable (``run_command``, ``spawn_restart``) so the
tests exercise the full flow with fakes, root-free, on any OS.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

import yaml

from core import config as cfg_mod
from quota import db as _db

log = logging.getLogger("quota.netmgr")

#: The setup-script defaults — the fallback LAN values when no ``lan_*``
#: snapshot exists in config.yaml (a box that was set to WAN by the setup
#: script directly, never through the panel).
DEFAULT_LAN_ROUTER = "192.168.1.1"
DEFAULT_LAN_DNS = ["192.168.1.1", "8.8.8.8"]
DEFAULT_UPLINK_IP = "192.168.1.110"
DEFAULT_LAN_CIDR = 24

#: Map a netmask string -> prefix length (255.255.255.0 -> 24).
_MASK_TO_CIDR = {
    "255.0.0.0": 8, "255.255.0.0": 16, "255.255.255.0": 24,
    "255.255.255.128": 25, "255.255.255.192": 26, "255.255.255.224": 27,
    "255.255.255.240": 28, "255.255.255.248": 29, "255.255.255.252": 30,
}


def _mask_to_cidr(mask: str) -> int:
    if mask in _MASK_TO_CIDR:
        return _MASK_TO_CIDR[mask]
    try:
        return int(ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen)
    except (ValueError, ipaddress.AddressValueError):
        return DEFAULT_LAN_CIDR


def _net_of(ip: str, cidr: int) -> str:
    """Network address of ``ip/cidr`` (192.168.1.110/24 -> 192.168.1.0)."""
    try:
        return str(ipaddress.ip_network(f"{ip}/{cidr}", strict=False).network_address)
    except (ValueError, ipaddress.AddressValueError):
        return "192.168.1.0"


def _parse_ip_addr_out(text: str) -> list[tuple[str, int]]:
    """Parse ``ip -o -4 addr show`` lines -> [(ip, cidr), ...].

    Input lines look like ``2: eth0    inet 192.168.1.110/24 brd ... scope global eth0``.
    """
    out: list[tuple[str, int]] = []
    for line in text.splitlines():
        # find the "inet <addr>/<cidr>" token
        tok = line.split("inet ")
        if len(tok) < 2:
            continue
        addr = tok[1].split(" ", 1)[0]
        ip, sep, prefix = addr.partition("/")
        if not sep:
            continue
        try:
            out.append((ip, int(prefix)))
        except ValueError:
            continue
    return out


class TopologyManager:
    """Owns the runtime LAN/WAN switch. One long-lived instance per process.

    ``config_path`` is the on-disk config.yaml the app loaded (the app may have
    overridden values via ``--port`` etc., but the *file* is the source of truth
    we patch and reload). ``script_path`` is ``scripts/topology.sh`` next to the
    repo. Injectables:

    * ``run_command(cmd, env) -> (returncode, stdout)`` — runs the applier
      (default: a subprocess; tests: a fake).
    * ``spawn_restart() -> None`` — schedules the detached self-restart
      (default: a background ``systemctl restart quota-gateway`` after ~2 s;
      tests: a no-op flag setter).
    * ``addr_cmd() -> str`` — ``ip -o -4 addr show`` output for uplink
      discovery (tests: canned output).
    """

    def __init__(
        self,
        cfg: cfg_mod.Config,
        database: _db.Database,
        config_path: str | os.PathLike[str] | None = None,
        script_path: str | os.PathLike[str] | None = None,
        run_command: Optional[Callable[[list[str], dict[str, str]],
                                       tuple[int, str]]] = None,
        spawn_restart: Optional[Callable[[], None]] = None,
        addr_cmd: Optional[Callable[[], str]] = None,
    ) -> None:
        self.cfg = cfg
        self.database = database
        self.config_path = Path(config_path) if config_path else None
        self.script_path = Path(script_path) if script_path else None
        self.run_command = run_command or self._default_run_command
        self.spawn_restart = spawn_restart or self._default_spawn_restart
        self.addr_cmd = addr_cmd or self._default_addr_cmd

    # ------------------------------------------------------------------ env

    def _default_run_command(self, cmd: list[str], env: dict[str, str]
                             ) -> tuple[int, str]:
        full_env = dict(os.environ)
        full_env.update(env)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=full_env,
                              timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out

    def _default_spawn_restart(self) -> None:
        # Detached (new session) so the restart survives this process being
        # killed by systemd. The 2 s delay lets the HTTP/WS responses flush
        # before the gateway goes down.
        subprocess.Popen(["sh", "-c", "sleep 2 && systemctl restart quota-gateway"],
                         start_new_session=True)

    def _default_addr_cmd(self) -> str:
        try:
            proc = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                                  capture_output=True, text=True, timeout=10)
            return proc.stdout or ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    # ------------------------------------------------------------- discovery

    def lan_interface(self) -> str:
        """The client-facing NIC: the ``shaping.interface`` the setup script
        wrote, else the NIC carrying the client gateway IP.

        The ``ip -o -4 addr show`` line format is ``2: eth0    inet
        192.168.2.1/24 brd ... scope global eth0`` — the interface NAME is the
        token after ``:``, NOT the index before it (the old ``split(":", 1)[0]``
        returned the index ``2``, which is meaningless to the applier)."""
        iface = getattr(self.cfg.shaping, "interface", "") or ""
        if iface:
            return iface
        text = self.addr_cmd()
        for line in text.splitlines():
            if f"inet {self.cfg.dhcp.gateway_ip}/" in line:
                return line.split(":", 1)[1].strip().split(" ", 1)[0].strip()
        return ""

    def upstream_dns(self) -> str:
        """The public resolver the box forwards to when it terminates the WAN
        itself. The LAN list is [router, public] — the public resolver is the
        LAST entry that is not the router. A one-entry [router] list (or a
        stale list whose last entry IS the router) must never forward to the
        router in WAN mode: the router does not exist on the WAN segment, so
        every upstream DNS query would go nowhere. Fall back to 8.8.8.8."""
        router = (getattr(self.cfg.dhcp, "router_ip", "") or
                  getattr(self.cfg.dhcp, "lan_router_ip", "") or "")
        servers = self.cfg.dhcp.dns_servers or ["8.8.8.8"]
        for srv in reversed(servers):
            if srv and srv != router:
                return srv
        return "8.8.8.8"

    def uplink_ip(self) -> tuple[str, int]:
        """The box's static LAN (uplink) IP + prefix — the value WAN mode erases.

        Resolution order: the ``dhcp.uplink_ip`` config key written by a prior
        apply/setup -> a live NIC address that is NOT on the client subnet ->
        the setup default 192.168.1.110/24.
        """
        if getattr(self.cfg.dhcp, "uplink_ip", ""):
            ip = self.cfg.dhcp.uplink_ip
            return ip, getattr(self.cfg.dhcp, "lan_cidr", None) or DEFAULT_LAN_CIDR
        client_net = self._client_network()
        for ip, prefix in _parse_ip_addr_out(self.addr_cmd()):
            if ip == self.cfg.dhcp.gateway_ip:
                continue  # the client alias, not the uplink
            if client_net is not None and ipaddress.ip_address(ip) in client_net:
                continue
            if not ip.startswith("127.") and ip != "0.0.0.0":
                return ip, prefix
        return DEFAULT_UPLINK_IP, DEFAULT_LAN_CIDR

    def _client_network(self) -> ipaddress.IPv4Network | None:
        raw = getattr(self.cfg.engine, "client_subnet", "") or ""
        if raw:
            try:
                return ipaddress.ip_network(raw, strict=False)
            except ValueError:
                pass
        return None

    def lan_values(self) -> dict[str, object]:
        """The full LAN reality the Revert button restores.

        Source of truth: config keys written by the setup script / a prior
        apply (``dhcp.lan_router_ip`` / ``dhcp.lan_dns_servers`` /
        ``dhcp.uplink_ip`` / ``dhcp.lan_cidr`` / ``engine.lan_gateway_arp_lock``).
        When absent (an old box that was set to WAN directly by the setup
        script) fall back to the setup-script defaults — the LAN it booted with.
        """
        dhcp = self.cfg.dhcp
        engine = self.cfg.engine
        router = getattr(dhcp, "lan_router_ip", "") or DEFAULT_LAN_ROUTER
        dns = getattr(dhcp, "lan_dns_servers", None) or list(DEFAULT_LAN_DNS)
        uplink_ip, cidr = self.uplink_ip()
        arp_lock = getattr(engine, "lan_gateway_arp_lock", True)
        return {
            "router_ip": router,
            "dns_servers": list(dns),
            "uplink_ip": uplink_ip,
            "lan_cidr": cidr,
            "uplink_subnet": f"{_net_of(uplink_ip, cidr)}/{cidr}",
            "gateway_arp_lock": bool(arp_lock),
        }

    # ------------------------------------------------------------ config.yaml

    def render_config(self, topology: str, lan: dict[str, object]) -> str:
        """Render the full config.yaml text for ``topology``.

        The active keys (``router_ip`` / ``dns_servers`` / ``uplink_subnet`` /
        ``topology`` / ``gateway_arp_lock``) flip with the mode; the ``lan_*``
        snapshot keys keep the LAN reality so a revert is always exact. Every
        other value flows through from the loaded :class:`Config` unchanged, so
        a bundle edit or a custom pool survives an apply.
        """
        cfg = self.cfg
        dhcp, engine, shaping = cfg.dhcp, cfg.engine, cfg.shaping
        upstream = self.upstream_dns()
        wan = topology == "wan"
        active_router = "" if wan else lan["router_ip"]
        active_dns = [upstream] if wan else list(lan["dns_servers"])
        active_uplink_subnet = "" if wan else lan["uplink_subnet"]
        active_arp = False if wan else bool(lan["gateway_arp_lock"])
        data: dict[str, object] = {
            "bundle": {"total_gb": cfg.bundle.total_gb,
                       "reset_day": cfg.bundle.reset_day,
                       "period_type": cfg.bundle.period_type},
            "db_path": cfg.db_path,
            "log_file": cfg.log_file,
            "log_level": cfg.log_level,
            "timezone": cfg.timezone,
            "dhcp": {
                "enable": dhcp.enable,
                "interface": dhcp.interface,
                "gateway_ip": dhcp.gateway_ip,
                "router_ip": active_router,
                "dns_servers": active_dns,
                "dns_forward": dhcp.dns_forward,
                "subnet": dhcp.subnet,
                "pool_start": dhcp.pool_start,
                "pool_end": dhcp.pool_end,
                "lease_hours": dhcp.lease_hours,
                "lease_file": dhcp.lease_file,
                # LAN reality — preserved in BOTH topologies so the panel can
                # revert exactly (see module docstring, invariant 2).
                "lan_router_ip": lan["router_ip"],
                "lan_dns_servers": list(lan["dns_servers"]),
                "uplink_ip": lan["uplink_ip"],
                "lan_cidr": lan["lan_cidr"],
            },
            "engine": {
                "enabled": engine.enabled,
                "count_direction": engine.count_direction,
                "backend": engine.backend,
                "table": engine.table,
                "client_subnet": engine.client_subnet,
                "uplink_subnet": active_uplink_subnet,
                "topology": topology,
                "gateway_arp_lock": active_arp,
                # LAN reality for the ARP lock (the setup script enables it in
                # LAN mode; WAN forces it off on the active key only).
                "lan_gateway_arp_lock": bool(lan["gateway_arp_lock"]),
            },
            "shaping": {
                "enabled": shaping.enabled,
                "interface": shaping.interface,
                "client_subnet": shaping.client_subnet,
                "ifb": shaping.ifb,
                "lan_rate_mbps": shaping.lan_rate_mbps,
            },
            "history": {
                "enabled": cfg.history.enabled,
                "dnsmasq_log_file": cfg.history.dnsmasq_log_file,
                "retention_days": cfg.history.retention_days,
            },
            "web": {"host": cfg.web.host, "port": cfg.web.port},
        }
        # default_flow_style=None keeps scalar lists inline (`[a, b]`) — the
        # same shape the setup script's heredoc writes — while nested mappings
        # stay block style.
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=None)

    def _write_config(self, text: str) -> None:
        path = self.config_path
        if path is None:
            raise RuntimeError("config path unknown — cannot persist topology")
        path.write_text(text, encoding="utf-8")
        log.info("config.yaml rewritten for topology")

    # --------------------------------------------------------------- applier

    def applier_env(self, topology: str, lan: dict[str, object],
                    pppoe_user: str = "", pppoe_password: str = "",
                    wan_if: str = "") -> dict[str, str]:
        """Build the environment the runtime applier expects (see topology.sh)."""
        cfg = self.cfg
        dhcp = cfg.dhcp
        cidr = int(lan["lan_cidr"])
        return {
            "TOPO": topology,
            "LAN_IF": self.lan_interface(),
            "LAN_IP": str(lan["uplink_ip"]),
            "LAN_CIDR": str(cidr),
            "SUBNET_MASK": dhcp.subnet,
            "CLIENT_IP": dhcp.gateway_ip,
            "CLIENT_NET": cfg.engine.client_subnet,
            "WAN_GATEWAY": str(lan["router_ip"]),
            "UPSTREAM_DNS": self.upstream_dns(),
            "POOL_START": dhcp.pool_start,
            "POOL_END": dhcp.pool_end,
            "LEASE_HOURS": str(dhcp.lease_hours),
            "WAN_IF": wan_if or "",
            "PPPOE_USER": pppoe_user or "",
            "PPPOE_PASSWORD": pppoe_password or "",
        }

    def _run_applier(self, env: dict[str, str]) -> tuple[int, str]:
        script = self.script_path
        if script is None:
            raise RuntimeError("topology.sh path unknown — cannot apply topology")
        if not script.exists():
            raise RuntimeError(f"topology applier not found: {script}")
        log.info("running %s (TOPO=%s)", script.name, env.get("TOPO"))
        return self.run_command(["bash", str(script)], env)

    # --------------------------------------------------- PPPoE connection test

    def test_script_path(self) -> Path:
        """``scripts/test_pppoe.sh`` — the throwaway-dial helper next to the
        topology applier. A ``None`` ``script_path`` (degraded boot without a
        repo) makes the test unavailable."""
        if self.script_path is None:
            raise RuntimeError("test script path unknown — cannot test PPPoE")
        return self.script_path.parent / "test_pppoe.sh"

    async def test_pppoe(self, pppoe_user: str = "", pppoe_password: str = "",
                         wan_if: str = "") -> dict[str, object]:
        """Dial the PPPoE line with the entered credentials on a THROWAWAY
        interface (``ppp200``) and report whether an internet connection is
        established. Never touches the running topology: no config.yaml write,
        no DB write, no ``ppp0`` (the real dial), no default-route change.

        Returns a result dict consumed by the WAN tab:
        ``status`` = success|auth-failed|concurrent-session|no-pppoe-server|
        link-down|error;
        ``ok`` bool; ``local_ip`` / ``peer_ip`` when a link came up;
        ``internet`` bool (ping to 8.8.8.8/1.1.1.1 over the test link);
        ``detail`` human text; ``script_output`` the raw script log tail.
        """
        script = self.test_script_path()
        if not script.exists():
            raise RuntimeError(f"PPPoE test script not found: {script}")
        env = {
            "PPP_IF": wan_if or self.lan_interface(),
            "PPPOE_USER": pppoe_user or "",
            "PPPOE_PASSWORD": pppoe_password or "",
        }
        rc, out = await asyncio.to_thread(self.run_command, ["bash", str(script)], env)
        return self._parse_pppoe_test(rc, out)

    @staticmethod
    def _parse_pppoe_test(rc: int, out: str) -> dict[str, object]:
        """Parse the test script's RESULT= lines into a result dict."""
        fields: dict[str, str] = {}
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("RESULT", "LOCAL", "PEER", "INTERNET", "DETAIL"):
                fields[key] = value.strip()
        result = fields.get("RESULT", "error")
        if rc != 0 and result == "error":
            result = "error"
        detail = fields.get("DETAIL", "").strip()
        internet = fields.get("INTERNET", "").strip().lower() == "yes"
        return {
            "status": result,
            "ok": result == "success",
            "local_ip": fields.get("LOCAL", "").strip(),
            "peer_ip": fields.get("PEER", "").strip(),
            "internet": internet,
            "detail": detail,
            "script_output": out.strip()[-2000:],
        }

    # ---------------------------------------------------------------- apply

    async def apply(self, topology: str, pppoe_user: str = "",
                    pppoe_password: str = "", wan_if: str = "") -> dict[str, object]:
        """Apply ``topology`` ("lan" | "wan") and schedule a detached restart.

        Returns a summary dict for the API. Raises :class:`RuntimeError` when a
        step fails (validated value, unwritable config, applier failure) — the
        API turns that into an HTTP 500 with the applier's stderr.

        On an applier failure BOTH persisted sources are rolled back to the
        pre-apply state: leaving config.yaml + the DB at the new topology would
        make the next boot apply a topology its NIC never got — the gateway
        would boot WAN onto a LAN NIC and cut everyone's internet. The box
        stays on the working LAN until a successful apply.
        """
        topology = (topology or "").strip().lower()
        if topology not in ("lan", "wan"):
            raise RuntimeError("topology must be 'lan' or 'wan'")
        if topology == "wan" and not pppoe_user:
            log.warning("WAN apply without PPPoE credentials — the applier will "
                        "dial with 'noauth', which most ISP lines reject")

        # Invariant 2: snapshot the LAN reality BEFORE the active keys flip.
        lan = self.lan_values()
        text = self.render_config(topology, lan)
        env = self.applier_env(topology, lan, pppoe_user, pppoe_password, wan_if)

        # Capture the pre-apply persisted state so a failed applier can restore
        # it (the revert bug — booting into a topology the NIC never got).
        old_existed = self.config_path is not None and self.config_path.exists()
        old_config = self._read_config()
        old_source = await self.database.get_setting("topology_source", None)
        old_topology = await self.database.get_setting("topology", None)

        # Invariant 1: config.yaml and the DB are written together, in the same
        # apply, so the next boot cannot pick one and ignore the other.
        self._write_config(text)
        await self.database.set_setting("topology_source", "dashboard")
        await self.database.set_setting("topology", topology)
        # Remember the PPPoE credentials (user/password + the optional second
        # NIC) so the WAN tab can prefill them next time instead of asking the
        # admin to retype them. Plaintext in the root-only DB — the same exposure
        # as /etc/ppp/chap-secrets, which the applier already writes.
        # Only NON-EMPTY values are saved: a panel apply carries no creds when
        # the fields were left empty (a "Revert to LAN" sends just
        # {topology: "lan"}), so unconditionally saving `or ""` erased the saved
        # credentials and broke the prefill while the working creds sat on in
        # /etc/ppp/chap-secrets (a live box report).
        for key, value in (("pppoe_user", pppoe_user),
                           ("pppoe_password", pppoe_password),
                           ("wan_if", wan_if)):
            if value:
                await self.database.set_setting(key, value)
        await self.database.add_event(
            f"WAN topology set to {topology} (panel apply, restarting)",
            "warn")

        rc, out = await asyncio.to_thread(self._run_applier, env)
        if rc != 0:
            # Do NOT restart into a half-applied state. Restore the previous
            # config + DB settings, then surface the applier's output.
            await self._rollback_apply(old_existed, old_config, old_source,
                                       old_topology)
            raise RuntimeError(
                f"topology applier failed (exit {rc}); previous config restored "
                f"so no restart was scheduled:\n{out.strip() or 'no output'}")

        self.spawn_restart()
        return {
            "applied": topology,
            "restart_scheduled": True,
            "script_rc": rc,
            "script_output": out.strip()[-2000:],
        }

    def _read_config(self) -> str | None:
        path = self.config_path
        if path is None or not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    async def _rollback_apply(self, old_existed: bool, old_config: str | None,
                              old_source: str | None,
                              old_topology: str | None) -> None:
        """Restore config.yaml + DB topology settings to their pre-apply state."""
        if old_existed:
            if old_config is not None and self.config_path is not None:
                try:
                    self.config_path.write_text(old_config, encoding="utf-8")
                except OSError as exc:
                    log.error("rollback: could not restore config.yaml: %s", exc)
            else:
                log.error("rollback: config.yaml existed but its text was "
                          "unreadable — left as-is")
        elif self.config_path is not None:
            # The file did not exist before the apply (a box with no config?):
            # remove what the apply wrote so nothing is left behind.
            try:
                self.config_path.unlink()
            except OSError as exc:
                log.error("rollback: could not remove created config.yaml: %s", exc)
        if old_source is None:
            await self.database.delete_setting("topology_source")
        else:
            await self.database.set_setting("topology_source", old_source)
        if old_topology is None:
            await self.database.delete_setting("topology")
        else:
            await self.database.set_setting("topology", old_topology)
        await self.database.add_event(
            "WAN topology apply FAILED — config.yaml and DB settings restored "
            "to the previous (LAN) state, no restart scheduled", "error")
        log.error("topology apply failed; restored config + DB settings")
