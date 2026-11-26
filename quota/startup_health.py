"""Startup self-heal for network infrastructure.

The setup script (``scripts/setup_gateway_kali.sh``) creates two pieces of
infrastructure that the Python app never touches:

* ``inet quota_nat`` — the masquerade NAT table (clients → internet).
* The static IP addresses on the LAN NIC (uplink + client-subnet alias).

Either can be silently lost: ``nft flush ruleset`` (e.g. an ``apt upgrade``
of nftables) wipes every table including ``quota_nat``; NetworkManager
reconnection can drop secondary addresses; a broken symlink on
``/etc/nftables.conf`` causes the table to vanish on service restart.

This module runs once at startup and recreates anything missing.  All
operations are **best-effort** — a failure is logged and never prevents the
app from starting (the rest of the stack degrades gracefully).

``nft`` commands are injected via ``run_command`` for testability.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable

from quota.dnsmasq_binding import DnsmasqPaths, FragmentUpdate, reconcile_dnsmasq
from quota.lan_interface import LanRequest, LanResolutionError, LanSelection, resolve_lan

log = logging.getLogger("quota.startup_health")

#: argv → (returncode, output).  Mirrors ``quota.nftables.RunCommand``.
RunCommand = Callable[[list[str]], tuple[int, str]]

NFT_TABLE = "inet quota_nat"

_CONF_SYMLINK = Path("/etc/nftables.conf")
_CONF_TARGET = Path("/etc/quota-gateway/nftables.gateway.nft")

_PROC_IPFORWARD = Path("/proc/sys/net/ipv4/ip_forward")
_SYS_NET = Path("/sys/class/net")


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


# ------------------------------------------------------------------
# NAT table
# ------------------------------------------------------------------

def _table_exists(run: RunCommand) -> bool:
    """Return True when ``inet quota_nat`` exists (even if empty)."""
    code, _ = run(["nft", "list", "table", NFT_TABLE])
    return code == 0


def _has_masquerade(run: RunCommand) -> bool:
    """Return True when the postrouting chain carries a masquerade rule."""
    code, out = run(["nft", "list", "chain", NFT_TABLE, "postrouting"])
    if code != 0:
        return False
    return "masquerade" in out


def _has_masquerade6(run: RunCommand) -> bool:
    """Return True when the postrouting chain carries an IPv6 (NAT66) masquerade."""
    code, out = run(["nft", "list", "chain", NFT_TABLE, "postrouting"])
    return code == 0 and any(
        "ip6 saddr" in line and "masquerade" in line for line in out.splitlines())


def _ensure_dns_redirect6(run: RunCommand, ipv6_prefix: str) -> None:
    """Re-add the IPv6 DNS redirect (client :53 -> the box) when the setup
    script's ``prerouting`` chain exists but lacks it (older installs). Without
    it a client could bypass domain filtering by using IPv6 DNS directly."""
    code, out = run(["nft", "list", "chain", NFT_TABLE, "prerouting"])
    if code != 0:
        return  # no prerouting chain on this install — nothing to extend
    for proto in ("udp", "tcp"):
        has = any("ip6 saddr" in l and f"{proto} dport 53" in l and "redirect" in l
                  for l in out.splitlines())
        if not has:
            run(["nft", "add", "rule", NFT_TABLE, "prerouting",
                 f"ip6 saddr {ipv6_prefix} {proto} dport 53 redirect to :53"])


def ensure_nat_table(
    client_subnet: str,
    *,
    ipv6_prefix: str = "",
    run: RunCommand | None = None,
) -> bool:
    """Verify ``inet quota_nat`` exists with a masquerade rule; recreate if not.

    Returns True when the table was (already) healthy; False when
    creation failed (logged, never raised).  ``client_subnet`` is the
    CIDR of the client network (e.g. ``192.168.2.0/24``).
    """
    if not client_subnet:
        log.warning("ensure_nat_table: no client_subnet — skipping")
        return True  # nothing we can do

    run = run or _default_run

    # Fast path: table + masquerade (+ NAT66 when dual-stack) already present.
    if (_table_exists(run) and _has_masquerade(run)
            and (not ipv6_prefix or _has_masquerade6(run))):
        if ipv6_prefix:
            _ensure_dns_redirect6(run, ipv6_prefix)
        return True

    if not _table_exists(run):
        log.warning("quota_nat table missing — recreating (client_subnet=%s)",
                    client_subnet)
        for args in (
            ["nft", "add", "table", NFT_TABLE],
            ["nft", "add", "chain", NFT_TABLE, "postrouting",
             "{ type nat hook postrouting priority 100; policy accept; }"],
        ):
            code, out = run(args)
            if code != 0 and "File exists" not in out and "already exists" not in out:
                log.error("ensure_nat_table: %s failed: %s", " ".join(args[2:]), out.strip())
                return False
    elif not _has_masquerade(run):
        log.warning("quota_nat table exists but masquerade rule missing — adding")

    if not _has_masquerade(run):
        code, out = run(["nft", "add", "rule", NFT_TABLE, "postrouting",
                         f"ip saddr {client_subnet} masquerade"])
        if code != 0 and "File exists" not in out and "already exists" not in out:
            log.error("ensure_nat_table: add masquerade rule failed: %s", out.strip())
            return False
    if ipv6_prefix and not _has_masquerade6(run):
        # NAT66 for the dual-stack clients (the engine's v6gate decides whether
        # their IPv6 may leave at all; this rule only rewrites the source).
        code, out = run(["nft", "add", "rule", NFT_TABLE, "postrouting",
                         f"ip6 saddr {ipv6_prefix} masquerade"])
        if code != 0 and "File exists" not in out and "already exists" not in out:
            log.error("ensure_nat_table: add NAT66 rule failed: %s", out.strip())
            return False

    if ipv6_prefix:
        _ensure_dns_redirect6(run, ipv6_prefix)
    log.info("ensure_nat_table: quota_nat table ready (masquerade %s)", client_subnet)
    return True


# ------------------------------------------------------------------
# nftables.conf symlink
# ------------------------------------------------------------------

def ensure_nftables_conf() -> bool:
    """Ensure ``/etc/nftables.conf`` is a symlink to the gateway's nft file.

    An ``apt upgrade nftables`` can overwrite the conf, breaking the link.
    Returns True when the symlink was (already) intact or was repaired.
    """
    if not _CONF_TARGET.exists():
        log.debug("ensure_nftables_conf: target %s not found — skipping "
                  "(non-setup deployment)", _CONF_TARGET)
        return True

    try:
        current = _CONF_SYMLINK.resolve()
    except OSError:
        current = None

    if current == _CONF_TARGET.resolve():
        return True

    log.warning("nftables.conf symlink broken (points to %s) — repairing",
                current)
    try:
        _CONF_SYMLINK.unlink(missing_ok=True)
        _CONF_SYMLINK.symlink_to(_CONF_TARGET)
    except OSError as exc:
        log.error("ensure_nftables_conf: could not repair symlink: %s", exc)
        return False

    # Reload the repaired config (best-effort — nft may be absent in
    # containers or test environments; the symlink itself is the fix).
    run = _default_run
    code, out = run(["nft", "-f", str(_CONF_SYMLINK)])
    if code != 0:
        log.warning("ensure_nftables_conf: nft -f after repair returned %d: "
                    "%s (symlink repaired, reload deferred to nftables.service)",
                    code, out.strip())
    else:
        log.info("ensure_nftables_conf: symlink repaired and config reloaded")
    return True


# ------------------------------------------------------------------
# NIC addresses + sysctl
# ------------------------------------------------------------------

def _find_lan_interface(run: RunCommand,
                        sysfs_net: Path = _SYS_NET,
                        gateway_ip: str = "192.168.2.1",
                        uplink_ip: str = "",
                        wan_interface: str = "") -> str:
    """Use the shared carrier-aware LAN resolver for legacy call sites."""
    try:
        return resolve_lan(LanRequest(gateway_ip, uplink_ip,
                                      wan_interface=wan_interface),
                           run=run, sysfs_net=sysfs_net).name
    except LanResolutionError as exc:
        log.warning("LAN interface discovery failed: %s", exc)
        return ""


def _current_addrs(run: RunCommand, iface: str) -> dict[str, str]:
    """Map of CIDR → ip-addr output line for all IPv4 addrs on *iface*."""
    addrs: dict[str, str] = {}
    code, out = run(["ip", "-o", "-4", "addr", "show", "dev", iface])
    if code != 0:
        return addrs
    for line in (out or "").splitlines():
        # "2: eth0    inet 192.168.2.1/24 brd ..."
        for token in line.split():
            if "/" in token and not token.startswith("brd"):
                addrs[token] = line
                break
    return addrs


def _has_ipv6_addr(run: RunCommand, iface: str, addr: str) -> bool:
    code, out = run(["ip", "-o", "-6", "addr", "show", "dev", iface])
    return code == 0 and f" {addr}/" in (out or "")


def _nm_list(value: str) -> list[str]:
    """Parse NetworkManager's comma-separated address property output."""
    return [item.strip() for item in value.replace("\n", ",").split(",")
            if item.strip() and item.strip() != "--"]


def _persist_nm_addresses(run: RunCommand, iface: str,
                          addresses: list[str],
                          ipv6_addresses: list[str] | None = None,
                          uplink_gateway: str = "") -> bool:
    """Persist app-owned addresses in a reusable NetworkManager profile."""
    ipv6_addresses = ipv6_addresses or []
    code, state = run(["nmcli", "-g", "RUNNING", "general"])
    if code != 0 or state.strip().lower() != "running":
        return True
    code, out = run(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", iface])
    connection_uuid = out.strip() if code == 0 else ""
    if connection_uuid == "--":
        connection_uuid = ""
    if not connection_uuid:
        code, out = run(["nmcli", "-t", "--escape", "no", "-f",
                         "UUID,connection.interface-name", "connection", "show"])
        if code == 0:
            connection_uuid = next((row.split(":", 1)[0]
                                    for row in out.splitlines()
                                    if row.count(":") >= 1
                                    and row.split(":", 1)[1] == iface), "")
        if not connection_uuid:
            connection_uuid = f"quota-gateway-{iface}"
            command = ["nmcli", "connection", "add", "type", "ethernet",
                       "con-name", connection_uuid, "ifname", iface,
                       "ipv4.method", "manual", "ipv4.addresses",
                       ",".join(addresses), "ipv6.method", "auto"]
            if uplink_gateway:
                command += ["ipv4.gateway", uplink_gateway]
            if ipv6_addresses:
                command += ["ipv6.addresses", ",".join(ipv6_addresses)]
            code, out = run(command)
            if code != 0:
                log.error("could not create a NetworkManager profile for %s: %s",
                          iface, out.strip())
                return False
            code, out = run(["nmcli", "-g", "connection.uuid", "connection",
                             "show", connection_uuid])
            if code != 0 or not out.strip():
                log.error("could not read the new NetworkManager profile UUID for %s",
                          iface)
                return False
            connection_uuid = out.strip()

    code, current_v4 = run(["nmcli", "-g", "ipv4.addresses", "connection",
                            "show", "uuid", connection_uuid])
    code6, current_v6 = run(["nmcli", "-g", "ipv6.addresses", "connection",
                             "show", "uuid", connection_uuid])
    if code != 0 or code6 != 0:
        log.error("could not read NetworkManager addresses for %s: %s",
                  iface, (current_v4 or current_v6).strip())
        return False
    desired_v4 = list(dict.fromkeys(_nm_list(current_v4) + addresses))
    desired_v6 = list(dict.fromkeys(_nm_list(current_v6) + ipv6_addresses))
    command = ["nmcli", "connection", "modify", "uuid", connection_uuid,
               "ipv4.method", "manual", "ipv4.addresses", ",".join(desired_v4),
               "ipv6.method", "auto"]
    if uplink_gateway:
        command += ["ipv4.gateway", uplink_gateway]
    if desired_v6:
        command += ["ipv6.addresses", ",".join(desired_v6)]
    code, out = run(command)
    if code != 0:
        log.error("could not persist LAN addresses on %s: %s", iface, out.strip())
        return False

    # A device with no active profile needs a connection activation; an active
    # device can reapply the updated profile without tearing down its link.
    code, active_uuid = run(["nmcli", "-g", "GENERAL.CON-UUID", "device",
                             "show", iface])
    active_uuid = active_uuid.strip() if code == 0 else ""
    if active_uuid == "--":
        active_uuid = ""
    if active_uuid == connection_uuid:
        code, out = run(["nmcli", "device", "reapply", iface])
    else:
        code, out = run(["nmcli", "connection", "up", "uuid", connection_uuid,
                         "ifname", iface])
    if code != 0:
        log.warning("NetworkManager could not activate the LAN profile on %s: %s",
                    iface, out.strip())
        return False
    return True


def ensure_network_infrastructure(
    *,
    ipv6_gateway: str = "",
    gateway_ip: str = "192.168.2.1",
    uplink_ip: str = "",
    lan_cidr: int = 24,
    run: RunCommand | None = None,
    proc_ipforward: Path | None = None,
    sysfs_net: Path | None = None,
    lan_selection: LanSelection | None = None,
    client_cidr: int = 24,
    dnsmasq_run: RunCommand | None = None,
    dnsmasq_paths: DnsmasqPaths | None = None,
    ipv6_fragment: FragmentUpdate | None = None,
    uplink_gateway: str = "",
    wan_interface: str = "",
    expect_ipv6: bool = False,
) -> bool:
    """Verify critical network settings survive reboots / NM reconnections.

    Checks:
    1. ``net.ipv4.ip_forward = 1``
    2. The LAN interface carries both the client gateway IP and the uplink IP.
    3. ``/etc/nftables.conf`` symlink integrity.

    Best-effort: failures are logged, never raised.  Returns True when
    everything was (already) healthy or was repaired.  ``run``,
    ``proc_ipforward``, and ``sysfs_net`` are injectable so tests drive a
    fake ``ip`` binary and fake proc/sysfs files — on a real box they
    default to the live kernel interfaces.
    """
    run = run or _default_run
    fwd_path = proc_ipforward or _PROC_IPFORWARD
    net_root = sysfs_net or _SYS_NET
    ok = True

    # -- ip_forward -------------------------------------------------
    try:
        val = fwd_path.read_text(encoding="utf-8").strip()
        if val != "1":
            log.warning("net.ipv4.ip_forward is %s — enabling", val)
            try:
                fwd_path.write_text("1\n", encoding="utf-8")
            except OSError as exc:
                log.error("could not enable ip_forward: %s", exc)
                ok = False
    except OSError:
        pass  # non-Linux or container — skip silently

    # -- NIC addresses ----------------------------------------------
    iface = (lan_selection.name if lan_selection is not None
             else _find_lan_interface(run, net_root, gateway_ip, uplink_ip,
                                      wan_interface))
    if not iface:
        log.debug("ensure_network_infrastructure: no wired LAN interface found — "
                  "skipping address check")
        return ok and ensure_nftables_conf()

    if lan_selection is not None:
        persistent = [f"{gateway_ip}/{client_cidr}"] if gateway_ip else []
        if uplink_ip:
            persistent.append(f"{uplink_ip}/{lan_cidr}")
        persistent_v6 = [f"{ipv6_gateway}/64"] if ipv6_gateway else []
        if not _persist_nm_addresses(run, iface, persistent, persistent_v6,
                                     uplink_gateway):
            ok = False

    addrs = _current_addrs(run, iface)
    needed: dict[str, str] = {}

    # Client gateway IP (always required).
    if gateway_ip:
        gw_cidr = f"{gateway_ip}/{client_cidr}"
        if gw_cidr not in addrs:
            # Also try matching by prefix (the /24 might differ).
            matched = any(a.startswith(gateway_ip + "/") for a in addrs)
            if not matched:
                log.warning("LAN interface %s missing client gateway %s — re-adding",
                            iface, gateway_ip)
                needed[gw_cidr] = gateway_ip

    # Uplink / router-admin alias (required in both LAN and WAN mode).
    if uplink_ip:
        ul_cidr = f"{uplink_ip}/{lan_cidr}"
        if ul_cidr not in addrs:
            matched = any(a.startswith(uplink_ip + "/") for a in addrs)
            if not matched:
                log.warning("LAN interface %s missing uplink alias %s — re-adding",
                            iface, ul_cidr)
                needed[ul_cidr] = uplink_ip

    for cidr, ip in needed.items():
        code, out = run(["ip", "addr", "add", cidr, "dev", iface])
        if code != 0 and "File exists" not in out and "already exists" not in out:
            log.error("ensure_network_infrastructure: ip addr add %s dev %s failed: %s",
                      cidr, iface, out.strip())
            ok = False
        else:
            log.info("ensure_network_infrastructure: added %s to %s", cidr, iface)
            if ip == gateway_ip and lan_selection is None:
                # dnsmasq runs with bind-interfaces: it only listens on addresses
                # that existed when it started. If it started before this address
                # was (re)added, DNS/DHCP on the gateway IP stay dead until a
                # restart (issue #36) - so restart it now. try-restart is a no-op
                # when dnsmasq is not running.
                code, out = run(["systemctl", "try-restart", "dnsmasq"])
                if code != 0:
                    log.warning("could not restart dnsmasq after re-adding %s: %s",
                                gateway_ip, (out or "").strip())
                else:
                    log.info("dnsmasq restarted to listen on %s", gateway_ip)

    # Dual-stack: the ULA gateway address must exist or dnsmasq stops sending
    # Router Advertisements and clients silently lose IPv6.
    if ipv6_gateway and not _has_ipv6_addr(run, iface, ipv6_gateway):
        log.warning("LAN interface %s missing IPv6 gateway %s — re-adding",
                    iface, ipv6_gateway)
        code, out = run(["ip", "-6", "addr", "add", f"{ipv6_gateway}/64", "dev", iface])
        if code != 0 and "File exists" not in out and "already exists" not in out:
            log.error("ensure_network_infrastructure: ip -6 addr add %s dev %s failed: %s",
                      ipv6_gateway, iface, out.strip())
            ok = False

    if lan_selection is not None:
        try:
            reconcile_dnsmasq(
                lan_selection, replacement=None, ipv6_fragment=ipv6_fragment,
                # dnsmasq's RA/DHCPv6 fragment is staged with the interface
                # update so the effective configuration is tested together.
                expect_ipv6=expect_ipv6, force_restart=False,
                paths=dnsmasq_paths or DnsmasqPaths(
                    Path("/etc/dnsmasq.d/quota-gateway.conf"),
                    Path("/etc/dnsmasq.d"), Path("/var/backups/netrex")),
                run=dnsmasq_run or run,
            )
        except Exception as exc:  # noqa: BLE001 — report degraded DHCP, keep app alive
            log.error("dnsmasq LAN binding reconciliation failed: %s", exc)
            ok = False

    # -- nftables.conf symlink --------------------------------------
    if not ensure_nftables_conf():
        ok = False

    return ok
