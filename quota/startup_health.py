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

log = logging.getLogger("quota.startup_health")

#: argv → (returncode, output).  Mirrors ``quota.nftables.RunCommand``.
RunCommand = Callable[[list[str]], tuple[int, str]]

NFT_TABLE = "inet quota_nat"

_CONF_SYMLINK = Path("/etc/nftables.conf")
_CONF_TARGET = Path("/etc/quota-gateway/nftables.gateway.nft")


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


def ensure_nat_table(
    client_subnet: str,
    *,
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

    # Fast path: table + masquerade already present.
    if _table_exists(run) and _has_masquerade(run):
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

    code, out = run(["nft", "add", "rule", NFT_TABLE, "postrouting",
                     f"ip saddr {client_subnet} masquerade"])
    if code != 0 and "File exists" not in out and "already exists" not in out:
        log.error("ensure_nat_table: add masquerade rule failed: %s", out.strip())
        return False

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
                  "(Docker / non-setup deployment)", _CONF_TARGET)
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

def _find_lan_interface() -> str:
    """Detect the wired Ethernet NIC carrying the gateway IP.

    Mirrors ``quota.netmgr.TopologyManager.lan_interface()`` but without
    needing the full config object.
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    # Prefer the kernel's default-route interface if it's an Ethernet NIC.
    for line in out.splitlines():
        # line format: "2: eth0    inet 192.168.2.1/24 ..."
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        iface = parts[1].strip().split(" ", 1)[0].strip()
        if not iface or iface == "lo":
            continue
        # Check it's wired (type 1 = Ethernet).
        type_path = f"/sys/class/net/{iface}/type"
        try:
            with open(type_path) as f:
                if f.read().strip() != "1":
                    continue
        except OSError:
            continue
        # Check it has a live link.
        carrier_path = f"/sys/class/net/{iface}/carrier"
        try:
            with open(carrier_path) as f:
                if f.read().strip() != "1":
                    continue
        except OSError:
            continue
        return iface
    return ""


def _current_addrs(iface: str) -> dict[str, str]:
    """Map of CIDR → ip-addr output line for all IPv4 addrs on *iface*."""
    addrs: dict[str, str] = {}
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "dev", iface],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return addrs
    for line in out.splitlines():
        # "2: eth0    inet 192.168.2.1/24 brd ..."
        for token in line.split():
            if "/" in token and not token.startswith("brd"):
                addrs[token] = line
                break
    return addrs


def ensure_network_infrastructure(
    *,
    gateway_ip: str = "192.168.2.1",
    uplink_ip: str = "",
    lan_cidr: int = 24,
    run: RunCommand | None = None,
) -> bool:
    """Verify critical network settings survive reboots / NM reconnections.

    Checks:
    1. ``net.ipv4.ip_forward = 1``
    2. The LAN interface carries both the client gateway IP and the uplink IP.
    3. ``/etc/nftables.conf`` symlink integrity.

    Best-effort: failures are logged, never raised.  Returns True when
    everything was (already) healthy or was repaired.
    """
    run = run or _default_run
    ok = True

    # -- ip_forward -------------------------------------------------
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            val = f.read().strip()
        if val != "1":
            log.warning("net.ipv4.ip_forward is %s — enabling", val)
            try:
                with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                    f.write("1\n")
            except OSError as exc:
                log.error("could not enable ip_forward: %s", exc)
                ok = False
    except OSError:
        pass  # non-Linux or container — skip silently

    # -- NIC addresses ----------------------------------------------
    iface = _find_lan_interface()
    if not iface:
        log.debug("ensure_network_infrastructure: no wired LAN interface found — "
                  "skipping address check")
        return ok and ensure_nftables_conf()

    addrs = _current_addrs(iface)
    needed: dict[str, str] = {}

    # Client gateway IP (always required).
    if gateway_ip:
        gw_cidr = f"{gateway_ip}/24"  # subnet from the CIDR, not from config
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

    # -- nftables.conf symlink --------------------------------------
    if not ensure_nftables_conf():
        ok = False

    return ok
