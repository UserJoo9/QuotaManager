"""Self-applying IPv6 host configuration (no manual setup-script re-run).

The setup script used to be the only thing that turned IPv6 on (sysctl, the
dnsmasq Router-Advertisement config). That made every update that touches
IPv6 a manual step. This module makes the app itself bring the host to the
required state at startup — idempotently, and only writing/restarting when
something actually differs — so pushing an update (git pull, ``.deb``,
copied folder) is enough:

* ``/etc/dnsmasq.d/quota-ipv6.conf``   — RA + SLAAC/DHCPv6 on the ULA prefix
* ``/etc/sysctl.d/99-zz-quota-ipv6.conf`` — IPv6 on, forwarding, ``accept_ra=2``
  on the LAN NIC. Named ``99-zz-…`` so it sorts AFTER an old
  ``99-quota-gateway.conf`` that still says ``disable_ipv6 = 1``.

The setup script writes the very same fragment content, so a fresh install and
an updated one converge on identical files.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger("quota.ipv6_setup")

RunCommand = Callable[[list[str]], tuple[int, str]]

DNSMASQ_FRAGMENT = Path("/etc/dnsmasq.d/quota-ipv6.conf")
SYSCTL_FRAGMENT = Path("/etc/sysctl.d/99-zz-quota-ipv6.conf")
_PROC_IPV6 = Path("/proc/sys/net/ipv6/conf")


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def dnsmasq_fragment(prefix: str, gateway: str) -> str:
    """RA + SLAAC/DHCPv6 config for the ULA ``prefix`` (e.g. ``fd00:da7a:2::/64``)."""
    net = ipaddress.ip_network(prefix, strict=False)
    base = str(net.network_address)               # "fd00:da7a:2::"
    return (
        "# Quota Manager — IPv6 dual-stack (generated, do not edit by hand).\n"
        "enable-ra\n"
        f"dhcp-range={base}100,{base}ffff,slaac,ra-names,12h\n"
        f"dhcp-option=option6:dns-server,[{gateway}]\n"
    )


def sysctl_fragment(iface: str) -> str:
    lines = [
        "# Quota Manager — IPv6 dual-stack (generated, do not edit by hand).",
        "net.ipv6.conf.all.disable_ipv6 = 0",
        "net.ipv6.conf.default.disable_ipv6 = 0",
        "net.ipv6.conf.all.forwarding = 1",
        "net.ipv6.conf.default.forwarding = 1",
    ]
    if iface:
        lines += [f"net.ipv6.conf.{iface}.disable_ipv6 = 0",
                  # accept_ra=2: keep taking the ISP router's RA (for the box's
                  # own IPv6) even though forwarding is on.
                  f"net.ipv6.conf.{iface}.accept_ra = 2"]
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except OSError as exc:
        log.warning("ipv6 setup: cannot write %s: %s", path, exc)
        return False


def _live_ipv6_off(proc_root: Path, iface: str) -> bool:
    """True when the kernel still has IPv6 disabled somewhere we need it."""
    for name in ("all", iface):
        if not name:
            continue
        f = proc_root / name / "disable_ipv6"
        try:
            if f.read_text(encoding="utf-8").strip() == "1":
                return True
        except OSError:
            continue
    return False


def ensure_ipv6_host_config(
    *,
    prefix: str,
    gateway: str,
    iface: str = "",
    run: RunCommand | None = None,
    dnsmasq_path: Path | None = None,
    sysctl_path: Path | None = None,
    proc_root: Path | None = None,
) -> dict[str, bool]:
    """Bring dnsmasq + sysctl to the dual-stack state. Best-effort, never raises.

    Returns ``{"dnsmasq_changed": bool, "sysctl_applied": bool}`` — the caller
    restarts dnsmasq when its fragment changed.
    """
    run = run or _default_run
    out = {"dnsmasq_changed": False, "sysctl_applied": False}
    try:
        dns_text = dnsmasq_fragment(prefix, gateway)
    except ValueError:
        log.warning("ipv6 setup: invalid dhcp.ipv6_prefix %r — skipping", prefix)
        return out

    out["dnsmasq_changed"] = _write_if_changed(dnsmasq_path or DNSMASQ_FRAGMENT, dns_text)
    if out["dnsmasq_changed"]:
        log.info("ipv6 setup: wrote the dnsmasq RA fragment (restart needed)")

    sysctl_file = sysctl_path or SYSCTL_FRAGMENT
    file_changed = _write_if_changed(sysctl_file, sysctl_fragment(iface))
    if file_changed or _live_ipv6_off(proc_root or _PROC_IPV6, iface):
        code, msg = run(["sysctl", "-p", str(sysctl_file)])
        if code == 0:
            out["sysctl_applied"] = True
            log.info("ipv6 setup: IPv6 sysctl applied (%s)", sysctl_file)
        else:
            log.warning("ipv6 setup: sysctl -p failed: %s", (msg or "").strip())
    return out


def disable_ipv6_host_config(
    *,
    run: RunCommand | None = None,
    dnsmasq_path: Path | None = None,
    sysctl_path: Path | None = None,
) -> dict[str, bool]:
    """Kill-switch for ``dhcp.ipv6_enable: false``: undo what we installed.

    Acts ONLY when our own fragments exist (a box that never had IPv6 enabled by
    this app is left alone). The sysctl fragment is rewritten to
    ``disable_ipv6 = 1`` (it keeps sorting after the legacy file) and applied,
    and the dnsmasq RA fragment is removed. Never raises.
    """
    run = run or _default_run
    out = {"dnsmasq_changed": False, "sysctl_applied": False}
    dns_file = dnsmasq_path or DNSMASQ_FRAGMENT
    sysctl_file = sysctl_path or SYSCTL_FRAGMENT
    try:
        if dns_file.exists():
            dns_file.unlink()
            out["dnsmasq_changed"] = True
        if sysctl_file.exists():
            off = ("# Quota Manager — IPv6 disabled (dhcp.ipv6_enable: false).\n"
                   "net.ipv6.conf.all.disable_ipv6 = 1\n"
                   "net.ipv6.conf.default.disable_ipv6 = 1\n")
            if _write_if_changed(sysctl_file, off):
                code, msg = run(["sysctl", "-p", str(sysctl_file)])
                out["sysctl_applied"] = code == 0
                if code != 0:
                    log.warning("ipv6 setup: sysctl -p failed: %s", (msg or "").strip())
    except OSError as exc:
        log.warning("ipv6 setup: could not disable IPv6 config: %s", exc)
    return out


_RESOLV_CONF = Path("/etc/resolv.conf")
_V6_TOKEN = re.compile(r"[0-9a-fA-F:]{3,}")


def _addresses_in(text: str, net: "ipaddress.IPv6Network") -> bool:
    for tok in _V6_TOKEN.findall(text):
        try:
            if ipaddress.ip_address(tok) in net:
                return True
        except ValueError:
            continue
    return False


def box_uses_ula_dns(prefix: str, *, run: RunCommand | None = None,
                     resolv_path: Path | None = None, iface: str = "") -> bool:
    """True when the BOX's own resolver points at an address of our ULA prefix.

    dnsmasq's Router Advertisement carries the gateway as RDNSS; on a box whose
    NetworkManager accepts RAs on the LAN NIC it can take that as ITS OWN DNS.
    Programs on the box (the VPN client resolving its server's hostname) then
    query ``[ula]:53`` and time out — the whole VPN share goes down.
    """
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return False
    try:
        for line in (resolv_path or _RESOLV_CONF).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                try:
                    if ipaddress.ip_address(parts[1].split("%")[0]) in net:
                        return True
                except ValueError:
                    continue
    except OSError:
        pass
    run = run or _default_run
    if iface:
        code, out = run(["resolvectl", "status", iface])
        if code == 0 and _addresses_in(out or "", net):
            return True
    return False


def box_has_usable_dns(prefix: str, *, resolv_path: Path | None = None,
                       run: RunCommand | None = None, iface: str = "") -> bool:
    """True when the box's resolver lists at least one real server: not our ULA
    and not a bare ``::1`` / ``127.0.0.1`` (nothing listens there on the box —
    that is the "connection refused" the VPN client hit)."""
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return True  # cannot judge -> do not touch anything
    try:
        for line in (resolv_path or _RESOLV_CONF).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != "nameserver":
                continue
            try:
                addr = ipaddress.ip_address(parts[1].split("%")[0])
            except ValueError:
                continue
            if addr in net or addr in (ipaddress.ip_address("::1"),
                                       ipaddress.ip_address("127.0.0.1")):
                continue
            return True
    except OSError:
        return True
    return False


def ensure_box_ignores_ula_dns(*, prefix: str, iface: str, run: RunCommand | None = None,
                               resolv_path: Path | None = None,
                               fallback_dns: list[str] | None = None) -> bool:
    """Make the BOX's own resolver sane. Returns True if it changed something.

    1. It must not use the gateway's own ULA (advertised as RDNSS) as its DNS.
    2. After that it must still have a REAL resolver: when none is left, the
       configured upstream servers (``fallback_dns``, IPv4) are set on the NIC's
       NetworkManager profile. Without one, programs on the box (the VPN client
       looking up its server) query ``::1``/``127.0.0.1`` and get refused.
    Idempotent (does nothing once the resolver is fine); never raises."""
    run = run or _default_run
    try:
        if not iface:
            return False
        uses_ula = box_uses_ula_dns(prefix, run=run, resolv_path=resolv_path, iface=iface)
        no_dns = not box_has_usable_dns(prefix, resolv_path=resolv_path, run=run,
                                        iface=iface)
        if not uses_ula and not no_dns:
            return False
        code, out = run(["nmcli", "-t", "-f", "NAME,DEVICE", "con", "show", "--active"])
        if code != 0:
            log.warning("ipv6 setup: the box's own DNS points at the ULA but nmcli is "
                        "unavailable — remove the fd00: nameserver manually")
            return False
        profile = ""
        for line in (out or "").splitlines():
            name, _, dev = line.rpartition(":")
            if dev == iface and name:
                profile = name.replace("\\:", ":")
                break
        if not profile:
            return False
        c1, _ = run(["nmcli", "con", "mod", profile, "ipv6.ignore-auto-dns", "yes"])
        servers = []
        for d in fallback_dns or []:
            try:
                if ipaddress.ip_address(str(d)).version == 4:
                    servers.append(str(d))
            except ValueError:
                continue
        if no_dns and servers:
            run(["nmcli", "con", "mod", profile, "ipv4.dns", " ".join(servers)])
            log.info("ipv6 setup: the box had no usable DNS — set %s on %s", servers, profile)
        c2, _ = run(["nmcli", "device", "reapply", iface])
        if c2 != 0:
            c2, _ = run(["nmcli", "con", "up", profile])
        log.info("ipv6 setup: the box no longer uses the ULA as its own DNS (%s)", profile)
        return c1 == 0 and c2 == 0
    except Exception as exc:  # noqa: BLE001 — never block startup
        log.warning("ipv6 setup: could not fix the box DNS: %s", exc)
        return False


def rollback_if_dnsmasq_down(*, run: RunCommand | None = None,
                             dnsmasq_path: Path | None = None) -> bool:
    """Safety net: if dnsmasq is not running while OUR IPv6 fragment exists, drop
    the fragment and start dnsmasq again. DHCP + DNS for the household must never
    stay down because of an optional IPv6 add-on. Returns True if it rolled back."""
    run = run or _default_run
    frag = dnsmasq_path or DNSMASQ_FRAGMENT
    try:
        if not frag.exists():
            return False
        code, _ = run(["systemctl", "is-active", "--quiet", "dnsmasq"])
        if code == 0:
            return False
        frag.unlink()
        run(["systemctl", "restart", "dnsmasq"])
        log.error("ipv6 setup: dnsmasq was down with the IPv6 fragment present — "
                  "removed %s and restarted dnsmasq (IPv6 router advertisements off)", frag)
        return True
    except OSError as exc:
        log.warning("ipv6 setup: rollback failed: %s", exc)
        return False
