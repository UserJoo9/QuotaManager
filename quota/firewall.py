"""Firewall module for the Linux gateway (nftables ``inet quota_firewall``).

A separate, self-contained kernel firewall layered NEXT TO the existing quota
engine — it never touches the ``quota_gateway`` (accounting + hard block),
``quota_nat`` (setup-owned masquerade) or ``quota_arp_lock`` tables. The two
policies compose by hook priority:

* :data:`fw_input` — ``type filter hook input priority -100`` — protects the
  box itself (the dashboard, exposed services). Runs before the engine's
  priority-0 input hooks, so firewall-denied box traffic is never counted.
* :data:`fw_forward` — ``type filter hook forward priority -100`` — guards the
  forwarded path. Runs before the quota engine's forward (priority 0): denied
  traffic never reaches the quota counters, while a quota-blocked device is
  still cut by the engine afterward. LAN pass-through (client<->uplink) is
  accepted here just like the engine excludes it from counting.
* :data:`fw_dnat` — ``type nat hook prerouting priority -100`` — port
  forwarding + DMZ (WAN mode only). dnat runs before routing; the existing
  masquerade is postrouting priority 100, so they compose cleanly.

IPv4 and IPv6 use separate typed sets for bans, scan watches, allowlists, and
blocklists. Bans auto-expire in the kernel; scan-watch entries count new SYNs
for the port-scan detector; deny wins over allow within each address family.

Posture is **derived from the deployment topology at render time** — never
stored. Under ``lan`` the firewall is permissive-out with explicit denies
(blocklist, bans, custom rules, box-service SYN flood guard). Under ``wan`` it
adds a **default-deny for NEW inbound on ppp0** (input + forward); the
dashboard port is never exposed on ppp0 unless ``wan_confirmed`` is set.
Port-forwards and DMZ are WAN-only.

Safe apply
----------
``apply`` first runs :meth:`FirewallManager.sanitize`, which refuses configs
that could lock the admin out (a deny rule matching the client subnet / the
box's own IPs), snapshots the current ruleset + config (``data/firewall_snapshots``
+ the ``firewall_last_good`` DB setting), programs the new rules, then starts a
watchdog that re-verifies the ruleset invariant after ``watchdog_seconds`` and
**auto-reverts** to the last-good config if it failed. Every ban (brute-force,
port-scan, manual) lands in the matching address-family set with a kernel
timeout and a DB event.

Like the packet engine, this module degrades gracefully: no ``nft``/no root =>
``available`` becomes False and every kernel op is a no-op. The command runner
is injected so tests drive a fake ``nft`` binary and assert the exact ruleset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from quota.ip_addresses import (
    address_family,
    parse_ip_address,
    parse_ip_network,
    split_ip_networks,
)
from quota.ipv6_firewall import (
    RenderContext,
    SET_BANS6,
    SET_SCAN6,
    render_ipv6_commands,
)

log = logging.getLogger("quota.firewall")

#: argv -> (returncode, output). Tests inject a fake; the default shells out
#: to the real ``nft`` binary (mirrors quota.nftables).
RunCommand = Callable[[list[str]], tuple[int, str]]

FAMILY = "inet"
TABLE = "quota_firewall"
CHAIN_INPUT = "fw_input"
#: Interface the Tailscale client creates (quota/tailscale.py INTERFACE).
TAILSCALE_IF = "tailscale0"
TAILSCALE_PORT = 41641
CHAIN_FORWARD = "fw_forward"
CHAIN_DNAT = "fw_dnat"
SET_BANS = "fw_bans"
SET_SCAN = "fw_scan_watch"
SET_ALLOW = "fw_allow"
SET_DENY = "fw_deny"

#: Counter names used for the Firewall log view (deltas read by the tick).
COUNTER_WAN_IN_DROP = "fw_in_drop"
COUNTER_WAN_FWD_DROP = "fw_fwd_drop"
COUNTER_BAN_DROP = "fw_ban_drop"
COUNTER_SYN_DROP = "fw_syn_drop"
COUNTER_SYN_PASS = "fw_syn_pass"
COUNTER_DENY_DROP = "fw_deny_drop"

DEFAULT_WATCHDOG_SEC = 45


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    import subprocess

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, "nft: not found"
    except subprocess.TimeoutExpired:
        return 124, "nft: timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def _cidr(value: str) -> str | None:
    """Normalize an IPv4 or IPv6 network string; None when invalid."""
    network = parse_ip_network(value)
    return str(network) if network is not None else None


def _as_ip(value: str) -> str | None:
    """Normalize a bare IPv4 or IPv6 address; None when invalid."""
    address = parse_ip_address(value)
    return address.compressed.lower() if address is not None else None


def _is_ipv6(value: str) -> bool:
    return address_family(value, network=True) == 6


def _net_containing(networks: list[str], host: str) -> str | None:
    """The network in ``networks`` that contains ``host`` (CIDR string)."""
    address = parse_ip_address(host)
    if address is None:
        return None
    for net_str in networks:
        network = parse_ip_network(net_str)
        if network is not None and address in network:
            return net_str
    return None


def _resolve_local_nets(engine_cfg: Any, dhcp_cfg: Any) -> list[str]:
    """Reuse the quota engine's LAN-subnet resolution (same derivation rules)."""
    from quota.nftables import resolve_local_networks

    return resolve_local_networks(engine_cfg, dhcp_cfg)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


@dataclass
class SynFloodConfig:
    """SYN-flood guard for the box + forwarded services (non-local NEW SYNs)."""

    rate: int = 10
    burst: int = 20


@dataclass
class BruteForceConfig:
    """Login brute-force -> kernel ban (hooked into the API login route)."""

    threshold: int = 10  # failures within the existing 300 s window
    ban_seconds: int = 1800


@dataclass
class ScanDetectConfig:
    """Port-scan detection via the ``fw_scan_watch`` dynamic set."""

    enabled: bool = True
    syn_threshold: int = 200  # new SYNs per 60 s watch window before a ban
    ban_seconds: int = 3600


@dataclass
class PortForward:
    """WAN-mode inbound port forward (dnat to an internal host)."""

    name: str = ""
    protocol: str = "tcp"
    source_port: int = 0
    target_ip: str = ""
    target_port: int = 0


@dataclass
class ServiceRule:
    """A box service exposed on the internet (WAN mode, ``fw_input``)."""

    name: str = ""
    protocol: str = "tcp"
    port: int = 0
    source: str = "0.0.0.0/0"


@dataclass
class CustomRule:
    """An ordered user rule on ``fw_input`` or ``fw_forward``.

    ``action`` is ``allow`` (accept + optional log) or ``deny`` (drop + log).
    Empty fields mean "any". ``log`` (default True) counts the rule in the
    Firewall log view; nft's kernel ``log`` statement is NOT used (it would
    flood dmesg) — drops surface through named counters + DB events instead.
    """

    name: str = ""
    chain: str = "forward"  # "input" | "forward"
    action: str = "deny"  # "allow" | "deny"
    src: str = "0.0.0.0/0"
    dst: str = "0.0.0.0/0"
    protocol: str = ""  # "" | "tcp" | "udp" | "icmp"
    src_port: int = 0
    dst_port: int = 0
    log: bool = True


@dataclass
class FirewallConfig:
    """Firewall settings. ``firewall:`` in config.yaml seeds the DB once; the
    DB setting ``firewall_config`` (JSON) is the runtime master after that
    (the bundle/shaping pattern). All fields optional with safe defaults."""

    enabled: bool = True
    #: Seconds the safe-apply watchdog waits before re-verifying the ruleset
    #: and auto-reverting to the last-good config.
    watchdog_seconds: int = DEFAULT_WATCHDOG_SEC
    #: IP the watchdog treats as "the admin" for lockout detection. Empty =>
    #: derived from the client subnet (the box's gateway address). This IP can
    #: never appear in a deny/ban rule (the sanitizer refuses it).
    probe_ip: str = ""
    #: Box services exposed on the internet under WAN mode (fw_input accepts).
    services: list[dict[str, Any]] = field(default_factory=list)
    #: WAN-mode inbound port forwards (dnat + forward accept).
    port_forwards: list[dict[str, Any]] = field(default_factory=list)
    #: WAN-mode DMZ target (catch-all dnat); empty = off.
    dmz: str = ""
    #: Ordered custom rules (input/forward, allow/deny).
    rules: list[dict[str, Any]] = field(default_factory=list)
    #: CIDR allowlist (bypasses the WAN default-deny).
    allow_cidrs: list[str] = field(default_factory=list)
    #: CIDR blocklist (deny > allow).
    deny_cidrs: list[str] = field(default_factory=list)
    syn_flood: SynFloodConfig = field(default_factory=SynFloodConfig)
    brute_force: BruteForceConfig = field(default_factory=BruteForceConfig)
    scan_detect: ScanDetectConfig = field(default_factory=ScanDetectConfig)
    #: Country-blocking consumes the ``firewall_geo`` DB setting (a JSON
    #: country-code -> CIDR map, e.g. {"CN": ["1.0.1.0/24", ...]}); inert when
    #: the map is absent or empty. Off by default — the map must be maintained
    #: externally (the module does not bundle/refresh geo databases).
    geo_block: bool = False
    #: Explicit opt-in to expose the dashboard web port on ppp0 under WAN
    #: mode. NEVER enabled implicitly — the dashboard is LAN-only by default
    #: (a WAN-facing admin UI with a default password is the box's #1 risk).
    wan_confirmed: bool = False


def config_to_dict(cfg: FirewallConfig) -> dict[str, Any]:
    """Serialize a :class:`FirewallConfig` to the JSON shape stored in the DB."""
    return asdict(cfg)


def dict_to_config(data: dict[str, Any]) -> FirewallConfig:
    """Deserialize the stored JSON into a :class:`FirewallConfig`, tolerating
    unknown/absent keys (forward-compatible, mirrors core/config.py)."""
    data = dict(data or {})
    if isinstance(data.get("syn_flood"), dict):
        data["syn_flood"] = SynFloodConfig(**{
            k: v for k, v in data["syn_flood"].items()
            if k in SynFloodConfig.__dataclass_fields__})
    if isinstance(data.get("brute_force"), dict):
        data["brute_force"] = BruteForceConfig(**{
            k: v for k, v in data["brute_force"].items()
            if k in BruteForceConfig.__dataclass_fields__})
    if isinstance(data.get("scan_detect"), dict):
        data["scan_detect"] = ScanDetectConfig(**{
            k: v for k, v in data["scan_detect"].items()
            if k in ScanDetectConfig.__dataclass_fields__})
    known = set(FirewallConfig.__dataclass_fields__)
    return FirewallConfig(**{k: v for k, v in data.items() if k in known})


def _merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing keys from the schema defaults (new keys arrive safely)."""
    return config_to_dict(dict_to_config(data))


# ---------------------------------------------------------------------------
# Rendering (pure) — the ruleset the manager programs
# ---------------------------------------------------------------------------


def _proto(protocol: str) -> str:
    """nft protocol guard string for a rule (empty = any)."""
    p = (protocol or "").strip().lower()
    if p in ("tcp", "udp"):
        return f"meta l4proto {p}"
    return ""


def _dport(port: int, protocol: str) -> str:
    """nft dport match (only when a concrete port was given)."""
    p = (protocol or "").strip().lower()
    if not port:
        return ""
    if p in ("tcp", "udp"):
        return f"{p} dport {int(port)}"
    return ""


def _sport(port: int, protocol: str) -> str:
    p = (protocol or "").strip().lower()
    if not port:
        return ""
    if p in ("tcp", "udp"):
        return f"{p} sport {int(port)}"
    return ""


def _neg(networks: list[str], key: str) -> str:
    """Negated CIDR matches for LOCAL networks (never firewall LAN traffic)."""
    return " ".join(f"{key} != {n}" for n in sorted(set(networks)))


def _render_command_plan(
    cfg: dict[str, Any],
    topology: str,
    local_nets: list[str],
    box_ips: list[str],
    *,
    local_nets6: list[str] | None = None,
    box_ips6: list[str] | None = None,
) -> tuple[list[list[str]], int | None, int | None]:
    """Render the full ``nft`` command list for the given config + topology.

    Pure and deterministic — the primary unit-test surface. ``cfg`` is the
    sanitized config dict, ``topology`` ``"lan"``/``"wan"``, ``local_nets`` the
    IPv4 LAN subnets (client + uplink) that are always accepted, and ``box_ips``
    the IPv4 box addresses. The optional IPv6 arguments provide the LAN prefix
    and gateway. Returns argv suffix lists each prefixed with ``nft`` by caller.
    """
    internal_cfg = {key: value for key, value in cfg.items()
                    if key.startswith("_")}
    cfg = {**_merge_defaults(cfg), **internal_cfg}
    cmds: list[list[str]] = []
    table = f"{FAMILY} {TABLE}"
    cmds.append(["add", "table", table])
    cmds.append(["flush", "table", table])
    if not cfg["enabled"]:
        return cmds, None, None

    wan = topology == "wan"
    client_nets = [n for n in sorted(set(local_nets)) if n and
                   not _is_ipv6(n)]

    # -- chains ------------------------------------------------------------
    cmds.append(["add", "chain", table, CHAIN_INPUT,
                 "{ type filter hook input priority -100; policy accept; }"])
    cmds.append(["add", "chain", table, CHAIN_FORWARD,
                 "{ type filter hook forward priority -100; policy accept; }"])
    if wan:
        cmds.append(["add", "chain", table, CHAIN_DNAT,
                     "{ type nat hook prerouting priority -100; policy accept; }"])

    # These family-neutral accepts belong to the common setup, outside the
    # fallible IPv6 segment. A rejected IPv6 set must not weaken IPv4 stateful,
    # loopback, or Tailscale access.
    cmds.extend([
        ["add", "rule", table, CHAIN_INPUT,
         "ct state established,related accept"],
        ["add", "rule", table, CHAIN_INPUT, "iif lo accept"],
        ["add", "rule", table, CHAIN_INPUT,
         'iifname "tailscale0" accept'],
        ["add", "rule", table, CHAIN_FORWARD,
         "ct state established,related accept"],
    ])

    # -- sets --------------------------------------------------------------
    cmds.append(["add", "set", table, SET_BANS,
                 "{ type ipv4_addr; flags interval, timeout; }"])
    cmds.append(["add", "set", table, SET_ALLOW,
                 "{ type ipv4_addr; flags interval; }"])
    cmds.append(["add", "set", table, SET_DENY,
                 "{ type ipv4_addr; flags interval; }"])
    cmds.append(["add", "set", table, SET_SCAN,
                 "{ type ipv4_addr; flags dynamic, timeout; timeout 60s; counter; }"])

    def add_rule(chain: str, expr: str) -> None:
        cmds.append(["add", "rule", table, chain, expr])

    # -- shared building blocks --------------------------------------------
    allow_elems, _allow6 = split_ip_networks(cfg["allow_cidrs"])
    deny_elems, _deny6 = split_ip_networks(cfg["deny_cidrs"])
    if allow_elems:
        cmds.append(["add", "element", table, SET_ALLOW,
                     "{ " + ", ".join(allow_elems) + " }"])
    if deny_elems:
        cmds.append(["add", "element", table, SET_DENY,
                     "{ " + ", ".join(deny_elems) + " }"])

    scan = cfg.get("scan_detect", {}) or {}
    scan_enabled = bool(scan.get("enabled", True))
    flood = cfg.get("syn_flood", {}) or {}
    flood_rate = int(flood.get("rate", 10) or 10)
    flood_burst = int(flood.get("burst", 20) or 20)

    geo, _geo6 = split_ip_networks(cfg.get("_geo_cidrs", []))
    services = [s for s in cfg.get("services", []) if isinstance(s, dict)]
    rules = [r for r in cfg.get("rules", []) if isinstance(r, dict)]
    forwards = [f for f in cfg.get("port_forwards", []) if isinstance(f, dict)]
    dmz = _as_ip(str(cfg.get("dmz", "") or "")) or ""

    wan_expose = bool(cfg.get("wan_confirmed"))
    web_port = int(cfg.get("_web_port", 8080) or 8080)

    # -- named counters -----------------------------------------------------
    # Every ``counter name X`` a rule references must be DECLARED first —
    # ``nft add rule ... counter name X`` fails with "No such file or
    # directory" when the counter object does not exist yet (the engine does
    # the same: nftables.py ``_add_counter`` before ``_add_device``).
    counter_names = {COUNTER_DENY_DROP, COUNTER_BAN_DROP,
                     COUNTER_SYN_DROP, COUNTER_SYN_PASS}
    # LAN topology (box behind the ISP router): the dashboard is closed to
    # everything outside the home networks unless the admin allows it.
    lan_guard = (not wan) and (not wan_expose) and bool(client_nets)
    if lan_guard:
        counter_names.add(COUNTER_WAN_IN_DROP)
    if wan:
        counter_names.add(COUNTER_WAN_IN_DROP)
        counter_names.add(COUNTER_WAN_FWD_DROP)
        for i, fw in enumerate(forwards):
            tip = _as_ip(str(fw.get("target_ip", "") or ""))
            tport = int(fw.get("target_port", 0) or 0)
            if tip and not _is_ipv6(tip) and tport:
                counter_names.add(f"fw_fwd_{i}")
        if dmz and not _is_ipv6(dmz):
            counter_names.add("fw_dmz")
    for i, r in enumerate(rules):
        if bool(r.get("log", True)):
            counter_names.add(f"fw_custom_{i}")
    for name in sorted(counter_names):
        cmds.append(["add", "counter", table, name])

    # IPv6 renders the family-neutral accepts shared by both address families.
    # Keep this anchor explicit so family health can be reported independently.
    ipv6_start = len(cmds)

    # -- fw_input ----------------------------------------------------------
    add_rule(CHAIN_INPUT, "ip saddr 127.0.0.0/8 accept")
    # Tailscale (built-in remote access): the private, authenticated tailnet
    # reaches the box here in BOTH topologies, never through ppp0 / the router.
    for net in client_nets:
        add_rule(CHAIN_INPUT, f"ip saddr {net} accept")
    for ip in box_ips:
        if _as_ip(ip):
            add_rule(CHAIN_INPUT, f"ip saddr {_as_ip(ip)} accept")
    if geo:
        add_rule(CHAIN_INPUT, f"{_neg(geo, 'ip saddr')} "
                              f"counter name {COUNTER_DENY_DROP} drop")
    add_rule(CHAIN_INPUT, f"ip saddr @{SET_DENY} "
                          f"counter name {COUNTER_DENY_DROP} drop")
    add_rule(CHAIN_INPUT, f"ip saddr @{SET_ALLOW} accept")
    # Box services exposed on the internet (WAN mode). In LAN mode these are
    # harmless accepts (LAN-only anyway).
    for s in services:
        proto = _proto(s.get("protocol", "tcp"))
        port = int(s.get("port", 0) or 0)
        src = _cidr(str(s.get("source", "0.0.0.0/0"))) or "0.0.0.0/0"
        if _is_ipv6(src):
            continue
        expr = " ".join(x for x in
                        (proto, f"ip saddr {src}" if src else "", _dport(port, s.get("protocol", "")))
                        if x)
        add_rule(CHAIN_INPUT, f"{expr} accept".strip())
    # Custom input rules (ordered).
    for i, r in enumerate(rules):
        if str(r.get("chain", "forward")).lower() != "input":
            continue
        _render_custom(add_rule, r, i)
    add_rule(CHAIN_INPUT, f"ip saddr @{SET_BANS} "
                          f"counter name {COUNTER_BAN_DROP} drop")
    # Tailscale's WireGuard port: lets the phone reach the box DIRECTLY instead
    # of through a slow relay (authenticated + encrypted, nothing else listens).
    # After the deny/ban drops, so a banned address still cannot reach it.
    add_rule(CHAIN_INPUT,
             f"meta nfproto ipv4 udp dport {TAILSCALE_PORT} accept")
    # WAN-mode ppp0 drop MUST come before the SYN flood guard below —
    # otherwise the flood-accept matches new SYNs from any interface and
    # external connections slip through before the ppp0 drop is reached.
    if wan:
        if wan_expose:
            add_rule(CHAIN_INPUT,
                     f'meta nfproto ipv4 iifname "ppp0" '
                     f'tcp dport {web_port} accept')
        add_rule(CHAIN_INPUT,
                 f'meta nfproto ipv4 iifname "ppp0" ct state new '
                 f'counter name {COUNTER_WAN_IN_DROP} drop')
    elif lan_guard:
        # Behind a router the "WAN" is whatever the router forwards in (a port
        # forward, DMZ or UPnP mapping): the box sees the public client's own
        # source address. With the remote-access switch OFF, refuse NEW
        # connections to the dashboard port from any source outside the home
        # networks (local nets / box IPs were accepted above).
        add_rule(CHAIN_INPUT,
                 f"meta nfproto ipv4 tcp dport {web_port} ct state new "
                 f"{_neg(client_nets, 'ip saddr')} "
                 f"counter name {COUNTER_WAN_IN_DROP} drop")
    if scan_enabled:
        add_rule(CHAIN_INPUT,
                 f"meta nfproto ipv4 tcp flags syn ct state new "
                 f"update @{SET_SCAN} {{ ip saddr timeout 60s }}")
    add_rule(CHAIN_INPUT,
             f"meta nfproto ipv4 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new "
             f"limit rate {flood_rate}/second burst {flood_burst} packets "
             f"counter name {COUNTER_SYN_PASS} accept")
    add_rule(CHAIN_INPUT,
             f"meta nfproto ipv4 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new "
             f"counter name {COUNTER_SYN_DROP} drop")

    # -- fw_forward --------------------------------------------------------
    for net in client_nets:
        add_rule(CHAIN_FORWARD, f"ip saddr {net} accept")
    if geo:
        add_rule(CHAIN_FORWARD, f"{_neg(geo, 'ip saddr')} "
                                f"counter name {COUNTER_DENY_DROP} drop")
    add_rule(CHAIN_FORWARD, f"ip saddr @{SET_DENY} "
                            f"counter name {COUNTER_DENY_DROP} drop")
    add_rule(CHAIN_FORWARD, f"ip saddr @{SET_ALLOW} accept")
    # Custom forward rules (ordered).
    for i, r in enumerate(rules):
        if str(r.get("chain", "forward")).lower() != "forward":
            continue
        _render_custom(add_rule, r, i)
    # WAN-mode forwarded services: post-dnat packets already carry the internal
    # port + target, so these accept the internal tuple on ppp0.
    if wan:
        for i, fw in enumerate(forwards):
            proto = _proto(fw.get("protocol", "tcp"))
            tport = int(fw.get("target_port", 0) or 0)
            tip = _as_ip(str(fw.get("target_ip", "") or ""))
            if not tip or _is_ipv6(tip) or not tport:
                continue
            expr = " ".join(x for x in
                            ('iifname "ppp0"', proto,
                             f"ip daddr {tip}" if tip else "",
                             _dport(tport, fw.get("protocol", "tcp")),
                             "ct state new",
                             f"counter name fw_fwd_{i}", "accept")
                            if x)
            add_rule(CHAIN_FORWARD, expr)
        if dmz and not _is_ipv6(dmz):
            add_rule(CHAIN_FORWARD,
                     f'iifname "ppp0" ip daddr {dmz} ct state new '
                     f"counter name fw_dmz accept")
    add_rule(CHAIN_FORWARD, f"ip saddr @{SET_BANS} "
                            f"counter name {COUNTER_BAN_DROP} drop")
    # WAN-mode ppp0 drop MUST come before the SYN flood guard below —
    # same ordering fix as fw_input (see comment there).
    if wan:
        add_rule(CHAIN_FORWARD,
                 f'meta nfproto ipv4 iifname "ppp0" ct state new '
                 f'counter name {COUNTER_WAN_FWD_DROP} drop')
    if scan_enabled:
        add_rule(CHAIN_FORWARD,
                 f"meta nfproto ipv4 tcp flags syn ct state new "
                 f"update @{SET_SCAN} {{ ip saddr timeout 60s }}")
    add_rule(CHAIN_FORWARD,
             f"meta nfproto ipv4 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new "
             f"limit rate {flood_rate}/second burst {flood_burst} packets "
             f"counter name {COUNTER_SYN_PASS} accept")
    add_rule(CHAIN_FORWARD,
             f"meta nfproto ipv4 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new "
             f"counter name {COUNTER_SYN_DROP} drop")

    # -- fw_dnat (WAN mode only) -------------------------------------------
    if wan:
        for i, fw in enumerate(forwards):
            proto = _proto(fw.get("protocol", "tcp"))
            sport = int(fw.get("source_port", 0) or 0)
            tport = int(fw.get("target_port", 0) or 0)
            tip = _as_ip(str(fw.get("target_ip", "") or ""))
            if not tip or _is_ipv6(tip) or not sport or not tport:
                continue
            expr = " ".join(x for x in
                            ('iifname "ppp0"', proto,
                             _dport(sport, fw.get("protocol", "tcp")),
                             f"counter name fw_fwd_{i}",
                             f"dnat to {tip}:{tport}")
                            if x)
            add_rule(CHAIN_DNAT, expr)
        if dmz and not _is_ipv6(dmz):
            excludes = " ".join(f"ip daddr != {n}" for n in client_nets)
            add_rule(CHAIN_DNAT,
                     f'iifname "ppp0" {excludes} counter name fw_dmz '
                     f"dnat to {dmz}".strip())
    ipv6_commands = render_ipv6_commands(RenderContext(
        cfg=cfg,
        table=table,
        chain_input=CHAIN_INPUT,
        chain_forward=CHAIN_FORWARD,
        local_nets=local_nets6 or [],
        box_ips=box_ips6 or [],
        wan=wan,
        lan_guard=lan_guard,
        web_port=web_port,
        tailscale_port=TAILSCALE_PORT,
        counter_ban_drop=COUNTER_BAN_DROP,
        counter_deny_drop=COUNTER_DENY_DROP,
        counter_wan_in_drop=COUNTER_WAN_IN_DROP,
        counter_wan_fwd_drop=COUNTER_WAN_FWD_DROP,
        counter_syn_pass=COUNTER_SYN_PASS,
        counter_syn_drop=COUNTER_SYN_DROP,
    ))
    ipv6_end = ipv6_start + len(ipv6_commands)
    cmds[ipv6_start:ipv6_start] = ipv6_commands
    return cmds, ipv6_start, ipv6_end


def render_commands(
    cfg: dict[str, Any],
    topology: str,
    local_nets: list[str],
    box_ips: list[str],
    *,
    local_nets6: list[str] | None = None,
    box_ips6: list[str] | None = None,
) -> list[list[str]]:
    """Render the complete nft command list with the stable legacy signature."""
    return _render_command_plan(
        cfg, topology, local_nets, box_ips,
        local_nets6=local_nets6, box_ips6=box_ips6)[0]


def _render_custom(add_rule: Callable[[str, str], None], r: dict[str, Any], i: int) -> None:
    """Render one ordered custom rule (input or forward)."""
    chain = CHAIN_INPUT if str(r.get("chain", "forward")).lower() == "input" \
        else CHAIN_FORWARD
    action = str(r.get("action", "deny")).lower()
    src = _cidr(str(r.get("src", "") or ""))
    dst = _cidr(str(r.get("dst", "") or ""))
    if (src and _is_ipv6(src)) or (dst and _is_ipv6(dst)):
        return
    proto = _proto(r.get("protocol", ""))
    sport = _sport(int(r.get("src_port", 0) or 0), r.get("protocol", ""))
    dport = _dport(int(r.get("dst_port", 0) or 0), r.get("protocol", ""))
    parts = ["meta nfproto ipv4",
             f"ip saddr {src}" if src else "",
             f"ip daddr {dst}" if dst else "", proto, sport, dport]
    expr = " ".join(part for part in parts if part)
    counter = f"fw_custom_{i}" if bool(r.get("log", True)) else None
    tail = f"counter name {counter}" if counter else ""
    if action == "allow":
        add_rule(chain, f"{expr} {tail} accept".strip())
    else:
        add_rule(chain, f"{expr} {tail} drop".strip())


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class FirewallManager:
    """Reconciles the ``quota_firewall`` table with the stored config.

    Async surface for DB/IO, sync kernel core (mirrors NftablesEngine's
    sync/graceful-degrade design). ``run_command`` and ``probe`` are injected
    for tests; ``probe`` answers "is management still reachable?" for the
    safe-apply watchdog (defaults to the ruleset-invariant self-audit).
    """

    def __init__(
        self,
        cfg: Any,
        database: Any = None,
        *,
        run_command: RunCommand | None = None,
        probe: Callable[[], bool] | None = None,
        snapshot_dir: str | Path | None = None,
        web_port: int = 8080,
    ) -> None:
        self._cfg = cfg
        self._db = database
        self._run_command = run_command or _default_run_command
        self._probe = probe
        self._web_port = web_port
        db_path = getattr(cfg, "db_path", "")
        #: Directory that SURVIVES an app-folder replacement (next to the DB).
        self._state_dir: Path | None = (
            Path(db_path).parent if isinstance(db_path, str) and db_path else None)
        self._snapshot_dir = Path(
            snapshot_dir or ((self._state_dir / "firewall_snapshots")
                             if self._state_dir else "data/firewall_snapshots"))
        self._fail_mono = 0.0

        engine_cfg = getattr(cfg, "engine", None)
        self._local_nets = _resolve_local_nets(
            engine_cfg, getattr(cfg, "dhcp", None))
        dhcp_cfg = getattr(cfg, "dhcp", None)
        self._client_net = _net_containing(
            self._local_nets, getattr(dhcp_cfg, "gateway_ip", "")
            if dhcp_cfg is not None else "")
        self._client_gw = getattr(dhcp_cfg, "gateway_ip", "") if dhcp_cfg else ""
        self._router_ip = getattr(dhcp_cfg, "router_ip", "") if dhcp_cfg else ""
        self._box_ips = [self._client_gw, self._router_ip]
        ipv6_prefix = _cidr(str(getattr(dhcp_cfg, "ipv6_prefix", "") or "")) \
            if dhcp_cfg is not None else None
        self._local_nets6 = ([ipv6_prefix] if ipv6_prefix and
                             _is_ipv6(ipv6_prefix) else [])
        self._client_net6 = self._local_nets6[0] if self._local_nets6 else None
        ipv6_gateway = _as_ip(str(getattr(dhcp_cfg, "ipv6_gateway", "") or "")) \
            if dhcp_cfg is not None else None
        self._ipv6_gateway = ipv6_gateway if ipv6_gateway and \
            _is_ipv6(ipv6_gateway) else ""
        self._box_ips6 = [self._ipv6_gateway] if self._ipv6_gateway else []

        self.available = True
        self._warned = False
        self._config: dict[str, Any] = config_to_dict(
            getattr(cfg, "firewall", None) or FirewallConfig())
        self._last_good: dict[str, Any] | None = None
        self._last_sig: str | None = None
        self._applied_sig: str | None = None
        self._last_apply_ok = False
        self._last_error = ""
        self._ipv4_apply_ok = False
        self._ipv4_last_error = ""
        self._ipv6_available = False
        self._ipv6_apply_ok = False
        self._ipv6_last_error = ""
        self._bans: dict[str, dict[str, Any]] = {}
        self._scan_last: dict[str, int] = {}
        self._counters_last: dict[str, tuple[int, int]] = {}
        self._geo_cached: list[str] = []
        #: Temporary posture override for the WAN-transition pre-apply (render
        #: the TARGET topology before netmgr.apply runs — no exposure window).
        #: Cleared by run.py right after; None = derive from cfg.engine.topology.
        self._topology_override: str | None = None
        self._counter_stats: list[dict[str, Any]] = []
        self._log_ring: list[dict[str, Any]] = []
        self._watchdog_tasks: set[asyncio.Task[Any]] = set()
        # All ruleset rebuilds and live set edits share this lock: nft flush +
        # add sequences must never interleave. Async entry points also hold a
        # loop lock across config persistence and the corresponding apply.
        self._apply_thread_lock = threading.RLock()
        self._async_apply_lock = asyncio.Lock()

    # -- topology / helpers --------------------------------------------------

    @property
    def topology(self) -> str:
        if self._topology_override:
            return self._topology_override
        return (getattr(getattr(self._cfg, "engine", None), "topology", "")
                or "lan").strip().lower()

    def set_topology_override(self, topology: str | None) -> None:
        """Temporarily render in ``topology`` (``"lan"``/``"wan"``/None).

        Used by the WAN-transition pre-apply so the firewall programs the
        TARGET posture before ``netmgr.apply`` brings ppp0 up — the in-memory
        cfg stays on the current topology until the scheduled restart.
        """
        self._topology_override = (topology or "").strip().lower() or None

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", True)) and self.available

    def probe_ip(self) -> str:
        """The IP the watchdog protects (never denied by any rule)."""
        cfg_ip = (self._config.get("probe_ip", "") or "").strip()
        if _as_ip(cfg_ip):
            return cfg_ip
        return self._client_gw or self._router_ip

    def _fail(self, message: str, *, preserve_ipv6: bool = False) -> None:
        self.available = False
        self._fail_mono = time.monotonic()
        self._last_error = message
        self._last_apply_ok = False
        self._applied_sig = None
        self._ipv4_apply_ok = False
        if not preserve_ipv6:
            self._ipv6_apply_ok = False
        if not self._warned:
            self._warned = True
            log.warning("firewall unavailable: %s", message)

    def _run(self, args: list[str], *, degrade: bool = True,
             preserve_ipv6: bool = False,
             exists_ok: bool = True) -> bool:
        """Run ``nft <args>``; True on success or benign "File exists"."""
        if not self.available:
            return False
        code, out = self._run_command(["nft", *args])
        if code == 0:
            return True
        if args[0] == "add" and any(s in out for s in ("File exists",
                                                       "already exists")):
            if exists_ok:
                return True
            self._last_error = f"nft {args[0]} failed: {out.strip()}"
            return False
        message = f"nft {args[0]} failed: {out.strip()}"
        environment_failure = (
            code in (127, 124) or
            any(term in out.lower() for term in (
                "operation not permitted", "permission denied")))
        if degrade or environment_failure:
            self._fail(message, preserve_ipv6=preserve_ipv6)
        else:
            self._last_error = message
        return False

    # -- config persistence --------------------------------------------------

    async def load_config(self) -> dict[str, Any]:
        """Runtime master = the ``firewall_config`` DB setting; fall back to the
        seeded in-memory config (YAML defaults) when unset/unparseable."""
        if self._db is None:
            return self._config
        raw = await self._db.get_setting("firewall_config", "")
        if not raw:
            raw = self._read_mirror()
            if raw:   # DB was replaced/reset: restore the admin's rules from the mirror
                log.warning("firewall: firewall_config missing in DB - restored from %s",
                            self._mirror_path())
                await self._db.set_setting("firewall_config", raw)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    self._config = _merge_defaults(data)
                    return self._config
            except ValueError:
                log.warning("firewall: ignoring unparseable firewall_config")
        self._config = _merge_defaults(self._config)
        return self._config

    def _mirror_path(self) -> Path | None:
        return (self._state_dir / "firewall_config.json") if self._state_dir else None

    def _read_mirror(self) -> str:
        path = self._mirror_path()
        try:
            return path.read_text(encoding="utf-8") if path and path.exists() else ""
        except OSError:
            return ""

    def _write_mirror(self, text: str) -> None:
        path = self._mirror_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("firewall: config mirror write failed: %s", exc)

    def _table_present(self) -> bool:
        code, _ = self._run_command(["nft", "list", "table", FAMILY, TABLE])
        return code == 0

    async def persist_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Sanitize + store a config and make it the runtime master."""
        clean = self.sanitize(data)
        stored = {k: v for k, v in clean.items() if not k.startswith("_")}
        if self._db is not None:
            await self._db.set_setting("firewall_config",
                                       json.dumps(stored, sort_keys=True))
            self._write_mirror(json.dumps(stored, sort_keys=True))
        self._config = clean
        return clean

    def sanitize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Refuse configurations that can lock the admin out.

        * a deny rule whose source/dest covers the client subnet, the probe IP
          or the box's own IPs (input deny = dashboard lockout; forward deny =
          whole-household cut) is dropped with a warning;
        * a deny_cidr covering a protected IP is dropped;
        * invalid CIDRs / ports / protocols are dropped per-field.

        Returns the sanitized config (never raises). Callers SHOULD inspect the
        returned ``_warnings`` list (popped by the API layer into the response).
        """
        warnings: list[str] = []
        protected = [self.probe_ip(), self._client_gw, self._router_ip,
                     self._ipv6_gateway]
        protected_nets = [n for n in (self._client_net, self._client_net6) if n]

        def is_protected(ip_or_cidr: str) -> bool:
            net = _cidr(ip_or_cidr)
            if net is not None:
                candidate_network = parse_ip_network(net)
                if candidate_network is None:
                    return False
                for pn in protected_nets:
                    protected_network = parse_ip_network(pn)
                    if protected_network is None or \
                            candidate_network.version != protected_network.version:
                        continue
                    if (candidate_network.subnet_of(protected_network)
                            or protected_network.subnet_of(candidate_network)):
                        return True
                for p in protected:
                    protected_address = parse_ip_address(p)
                    if (protected_address is not None
                            and protected_address.version == candidate_network.version
                            and protected_address in candidate_network):
                        return True
                return False
            address = parse_ip_address(ip_or_cidr)
            if address is None:
                return False
            return any(
                (protected_address := parse_ip_address(p)) is not None
                and protected_address.version == address.version
                and protected_address == address
                for p in protected)

        data = dict(data or {})
        # allow_cidrs / deny_cidrs — drop protected overlaps from DENY only.
        deny = []
        for c in data.get("deny_cidrs", []) or []:
            if is_protected(str(c)):
                warnings.append(f"deny_cidr {c} covers a protected IP — ignored")
                continue
            deny.append(str(c))
        data["deny_cidrs"] = deny
        data["allow_cidrs"] = [
            str(_cidr(str(c))) for c in data.get("allow_cidrs", []) or []
            if _cidr(str(c))]

        def clean_rule(r: dict[str, Any], kind: str) -> dict[str, Any] | None:
            r = dict(r or {})
            src = str(r.get("src", "") or "")
            dst = str(r.get("dst", "") or "")
            r["name"] = str(r.get("name", "") or "")
            r["chain"] = str(r.get("chain", "forward")).lower()
            r["action"] = str(r.get("action", "deny")).lower()
            r["protocol"] = str(r.get("protocol", "") or "").lower()
            if r["chain"] not in ("input", "forward"):
                warnings.append(f"{kind} {r.get('name', '?')}: bad chain "
                                f"{r['chain']!r} — ignored")
                return None
            if r["action"] not in ("allow", "deny"):
                warnings.append(f"{kind} {r.get('name', '?')}: bad action "
                                f"{r['action']!r} — ignored")
                return None
            if r["protocol"] not in ("", "tcp", "udp", "icmp"):
                warnings.append(f"{kind} {r.get('name', '?')}: bad protocol "
                                f"{r['protocol']!r} — ignored")
                return None
            for field_, val in (("src", src), ("dst", dst)):
                if val and not _cidr(val) and not _as_ip(val):
                    warnings.append(f"{kind} {r.get('name', '?')}: bad {field_} "
                                    f"{val!r} — ignored")
                    return None
            src_network = parse_ip_network(src) if src else None
            dst_network = parse_ip_network(dst) if dst else None
            src_family = src_network.version if src_network else None
            dst_family = dst_network.version if dst_network else None
            if src_family and dst_family and src_family != dst_family:
                warnings.append(f"{kind} {r.get('name', '?')}: src and dst address families "
                                "must match — ignored")
                return None
            if r["action"] == "deny":
                # The one failure mode worth refusing: a deny rule that is
                # UNCONDITIONAL for the admin's own traffic (no dst/port
                # scope) — an input deny of everything from the client subnet
                # locks the dashboard, a forward deny of everything from the
                # client subnet cuts the whole household's internet. Port-
                # scoped denies (torrent blocks, blocking a specific server)
                # are fine and common.
                src_any = not src or _cidr(src) in ("0.0.0.0/0", "::/0")
                src_hits = src_any or is_protected(src)
                if r["chain"] == "input":
                    dst_any = not dst or _cidr(dst) in ("0.0.0.0/0", "::/0")
                    if src_hits and dst_any and not r.get("dst_port"):
                        warnings.append(
                            f"{kind} {r.get('name', '?')}: input deny with no "
                            f"port/dst scope would lock the dashboard out — "
                            f"ignored")
                        return None
                else:  # forward
                    dst_any = not dst or _cidr(dst) in ("0.0.0.0/0", "::/0")
                    if src_hits and dst_any and not r.get("dst_port"):
                        warnings.append(
                            f"{kind} {r.get('name', '?')}: forward deny with "
                            f"no port/dst scope would cut the whole client "
                            f"subnet's internet — ignored")
                        return None
            for f_ in ("src_port", "dst_port"):
                try:
                    v = int(r.get(f_, 0) or 0)
                except (TypeError, ValueError):
                    v = 0
                if v and not 1 <= v <= 65535:
                    warnings.append(f"{kind} {r.get('name', '?')}: bad {f_} "
                                    f"{v} — ignored")
                    return None
                r[f_] = v
            return r

        rules = []
        for i, r in enumerate(data.get("rules", []) or []):
            if isinstance(r, dict):
                clean = clean_rule(r, "rule")
                if clean is not None:
                    rules.append(clean)
        data["rules"] = rules

        # Services + port forwards are WAN-inbound only — safe to sanitize
        # loosely (they can't lock anyone out of the LAN).
        services = []
        for s in data.get("services", []) or []:
            if not isinstance(s, dict):
                continue
            s = dict(s)
            try:
                port = int(s.get("port", 0) or 0)
            except (TypeError, ValueError):
                port = 0
            source = str(s.get("source", "0.0.0.0/0") or "0.0.0.0/0")
            source_net = _cidr(source)
            if not source_net:
                warnings.append(f"service {s.get('name', '?')}: bad source "
                                f"{source!r} — ignored")
                continue
            if _is_ipv6(source_net):
                warnings.append(f"service {s.get('name', '?')}: IPv6 service "
                                "publishing is unsupported — ignored")
                continue
            if port and 1 <= port <= 65535 and \
                    str(s.get("protocol", "tcp")).lower() in ("tcp", "udp"):
                services.append({"name": str(s.get("name", "") or ""),
                                 "protocol": str(s.get("protocol", "tcp")).lower(),
                                 "port": port,
                                 "source": source_net})
        data["services"] = services

        forwards = []
        for f in data.get("port_forwards", []) or []:
            if not isinstance(f, dict):
                continue
            f = dict(f)
            tip = _as_ip(str(f.get("target_ip", "") or ""))
            try:
                sp, tp = int(f.get("source_port", 0) or 0), int(f.get("target_port", 0) or 0)
            except (TypeError, ValueError):
                sp = tp = 0
            if tip and _is_ipv6(tip):
                warnings.append(f"port forward {f.get('name', '?')}: IPv6 "
                                "port forwarding is unsupported — ignored")
                continue
            if tip and 1 <= sp <= 65535 and 1 <= tp <= 65535 and \
                    str(f.get("protocol", "tcp")).lower() in ("tcp", "udp"):
                forwards.append({"name": str(f.get("name", "") or ""),
                                 "protocol": str(f.get("protocol", "tcp")).lower(),
                                 "source_port": sp,
                                 "target_ip": tip,
                                 "target_port": tp})
        data["port_forwards"] = forwards

        dmz = _as_ip(str(data.get("dmz", "") or ""))
        if dmz and _is_ipv6(dmz):
            warnings.append("IPv6 DMZ is unsupported — ignored")
            dmz = None
        data["dmz"] = dmz or ""

        try:
            data["watchdog_seconds"] = max(1, int(data.get("watchdog_seconds",
                                                           DEFAULT_WATCHDOG_SEC) or DEFAULT_WATCHDOG_SEC))
        except (TypeError, ValueError):
            data["watchdog_seconds"] = DEFAULT_WATCHDOG_SEC

        # Geo CIDRs are injected from the firewall_geo DB setting (not part of
        # the user-config JSON).
        geo = data.get("_geo_cidrs") or []
        data["_geo_cidrs"] = [c for c in geo if _cidr(c)]
        data["_web_port"] = int(data.get("_web_port", self._web_port) or self._web_port)
        data["_warnings"] = warnings
        return data

    # -- apply / reconcile ---------------------------------------------------

    def _signature(self, cfg: dict[str, Any]) -> str:
        core = {k: v for k, v in cfg.items() if not k.startswith("_")}
        return json.dumps([self.topology, core], sort_keys=True, default=str)

    async def reconcile(self) -> bool:
        """Re-verify the stored config + topology and re-apply when changed.

        Called every maintenance tick + right after a LAN<->WAN transition.
        Cheap when nothing changed (signature compare) — the heavy nft rebuild
        only runs on a real change. All kernel work runs off the event loop
        (``asyncio.to_thread``); drained events are persisted to the DB.
        """
        async with self._async_apply_lock:
            return await self._reconcile_locked()

    async def _reconcile_locked(self) -> bool:
        cfg = await self.load_config()
        if not self.available:
            # A failed nft call must not switch the firewall off until the next
            # restart: retry once a minute (not when nft itself is missing).
            if (self._fail_mono and time.monotonic() - self._fail_mono >= 60
                    and "not found" not in self._last_error):
                self.available = True
                self._last_sig = self._applied_sig = None
            else:
                return False
        drained: list[tuple[str, str]] = []

        def _work() -> bool:
            sig = self._signature(cfg)
            changed = not (sig == self._last_sig and self._applied_sig == sig)
            if (not changed and cfg.get("enabled", True)
                    and not self._table_present()):
                # `nft flush ruleset` (nftables restart, package upgrade...) wiped us
                log.warning("firewall: table vanished - re-applying the stored rules")
                changed = True
            if changed:
                if not self.apply(cfg):
                    self._last_sig = sig
                    if not (self._ipv4_apply_ok or self._ipv6_apply_ok):
                        self._applied_sig = None
                    return False
                self._applied_sig = sig
                self._last_sig = sig
            drained.extend(self._drain())
            return self._last_apply_ok

        ok = await asyncio.to_thread(_work)
        if drained:
            for level, message in drained:
                await self._event(message, level)
        return ok

    def apply(self, cfg: dict[str, Any] | None = None) -> bool:
        """Serialize a complete nft rebuild against every other writer."""
        with self._apply_thread_lock:
            return self._apply_unlocked(cfg)

    def _apply_unlocked(self, cfg: dict[str, Any] | None = None) -> bool:
        """Program the kernel ruleset for ``cfg`` (or the current config).

        Sync (subprocess — callers wrap in ``asyncio.to_thread``). Reseeds the
        delta baselines so a flush never reports stale bytes as events.
        """
        if not self.available:
            return False
        cfg = _merge_defaults(cfg if cfg is not None else self._config)
        topology = self.topology
        geo = self._geo_cidrs()
        cfg = _merge_defaults(cfg)
        cfg["_geo_cidrs"] = geo
        cfg["_web_port"] = self._web_port
        self._last_apply_ok = False
        self._applied_sig = None
        self._ipv4_apply_ok = False
        self._ipv4_last_error = ""
        self._ipv6_apply_ok = False
        self._ipv6_last_error = ""
        self._ipv6_available = False
        try:
            cmds, ipv6_start, ipv6_end = _render_command_plan(
                cfg, topology, self._local_nets, self._box_ips,
                local_nets6=self._local_nets6,
                box_ips6=self._box_ips6)
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            self._last_error = f"render failed: {exc}"
            self._ipv4_last_error = self._last_error
            self._ipv6_last_error = self._last_error
            log.exception("firewall: render failed")
            return False
        self._last_error = ""
        ipv6_failed = False
        for index, cmd in enumerate(cmds):
            in_ipv6_block = (ipv6_start is not None and ipv6_end is not None
                             and ipv6_start <= index < ipv6_end)
            if in_ipv6_block and ipv6_failed:
                continue
            preserve_ipv6 = (ipv6_end is not None and index >= ipv6_end)
            if not self._run(cmd, degrade=not in_ipv6_block,
                             preserve_ipv6=preserve_ipv6):
                message = self._last_error or (
                    f"nft {cmd[0]} {cmd[1]} failed — firewall unavailable")
                if in_ipv6_block:
                    self._ipv6_available = False
                    self._ipv6_last_error = message
                    ipv6_failed = True
                    # IPv4 rules remain independent. Skip the failed IPv6
                    # block and continue with the IPv4 rules after it.
                    if self.available:
                        continue
                    self._ipv4_last_error = message
                elif ipv6_end is not None and index >= ipv6_end:
                    self._ipv4_last_error = message
                else:
                    self._ipv4_last_error = message
                    self._ipv6_last_error = message
                self._last_apply_ok = False
                if in_ipv6_block and self.available:
                    continue
                return self._finish_apply(
                    cfg, topology=topology, command_count=index + 1)
            if in_ipv6_block and index == ipv6_end - 1 and not ipv6_failed:
                self._ipv6_available = True
                self._ipv6_apply_ok = True
                self._ipv6_last_error = ""
        self._ipv4_apply_ok = self.available
        if not self._ipv4_apply_ok and not self._ipv4_last_error:
            self._ipv4_last_error = self._last_error
        return self._finish_apply(cfg, topology=topology,
                                  command_count=len(cmds))

    def _finish_apply(self, cfg: dict[str, Any], *, topology: str,
                      command_count: int) -> bool:
        """Restore active bans and record full or safely partial family state."""
        if not cfg.get("enabled", True):
            self._config = cfg
            self._last_apply_ok = self.available
            if self._last_apply_ok:
                self._last_error = ""
                self._applied_sig = self._last_sig = self._signature(cfg)
                return True
            self._applied_sig = None
            if not self._last_error:
                self._last_error = "firewall disable failed"
            return False

        for family in (4, 6):
            family_applied = (self._ipv4_apply_ok if family == 4
                              else self._ipv6_apply_ok)
            if not family_applied:
                continue
            ok, error = self._reseed_bans(family)
            if ok:
                continue
            if family == 4:
                self._ipv4_apply_ok = False
                self._ipv4_last_error = error
            else:
                self._ipv6_apply_ok = False
                self._ipv6_available = False
                self._ipv6_last_error = error

        self._config = cfg
        self._last_apply_ok = self._ipv4_apply_ok and self._ipv6_apply_ok
        if self._last_apply_ok:
            self._last_error = ""
            self._counters_last = {}
            self._scan_last = {}
            self._applied_sig = self._last_sig = self._signature(cfg)
            log.info("firewall: applied (%s mode, %d commands)",
                     topology, command_count)
            return True

        errors = [message for message in
                  (self._ipv4_last_error, self._ipv6_last_error) if message]
        self._last_error = errors[0] if errors else "firewall family apply failed"
        signature = self._signature(cfg)
        # Keep a healthy family's installed rules stable across unchanged
        # reconcile ticks. A config change or missing table triggers a retry.
        if self._ipv4_apply_ok or self._ipv6_apply_ok:
            self._applied_sig = self._last_sig = signature
        else:
            self._applied_sig = None
            self._last_sig = signature
        return False

    async def safe_apply(self, data: dict[str, Any], reason: str = "dashboard") -> dict[str, Any]:
        async with self._async_apply_lock:
            return await self._safe_apply_locked(data, reason)

    async def safe_apply_partial(
            self, changes: dict[str, Any], reason: str = "dashboard", *,
            require_wan: bool = False) -> dict[str, Any]:
        """Merge a partial update and apply it under the same config lock."""
        async with self._async_apply_lock:
            current = dict(await self.load_config())
            current.update(changes)
            if (require_wan and self.topology != "wan" and
                    (current.get("port_forwards") or current.get("dmz"))):
                return {
                    "ok": False,
                    "applied": False,
                    "conflict": True,
                    "error": "port forwards and DMZ are WAN-mode only — apply "
                             "them after switching to Strong (WAN) mode",
                }
            return await self._safe_apply_locked(current, reason)

    async def _safe_apply_locked(self, data: dict[str, Any],
                                 reason: str) -> dict[str, Any]:
        """Sanitize -> snapshot -> apply -> watchdog. Returns a status dict.

        The previous good config is preserved as ``firewall_last_good`` (DB)
        and the pre-apply ruleset snapshot lands in ``data/firewall_snapshots/``.
        A watchdog task re-verifies reachability after ``watchdog_seconds`` and
        auto-reverts to the last-good config on failure.
        """
        # The config in force BEFORE this apply is the rollback target. It must
        # be read before persist_config(), which makes the new config the
        # runtime master (reading it afterwards made the very first apply since
        # boot "revert" to itself, and later applies keep reverting to the
        # first one instead of the previous one).
        previous_good = json.loads(json.dumps(self._config)) if self._config else None
        clean = await self.persist_config(data)
        warnings = clean.pop("_warnings", [])
        if not self.available:
            return {"ok": False, "applied": False, "error": "firewall unavailable",
                    "warnings": warnings}
        if not clean.get("enabled"):
            ok = await asyncio.to_thread(self.apply, clean)
            if not ok:
                await self._event(f"FW: disable failed ({reason}) — {self._last_error}",
                                  "error")
                return {"ok": False, "applied": False,
                        "error": self._last_error, "warnings": warnings}
            await self._event(f"FW: firewall disabled ({reason})", "warn")
            return {"ok": True, "applied": True, "warnings": warnings}

        self._snapshot(clean, reason)
        # Rollback target = the config that was GOOD BEFORE this apply. A
        # failed watchdog must restore the previous working rules — NOT the
        # just-applied ones that locked the admin out (the naive "last good =
        # what I just applied" would revert to the same broken ruleset).
        if previous_good is None:
            previous_good = self._last_good or clean
        ok = await asyncio.to_thread(self.apply, clean)
        if not ok:
            await self._event(f"FW: apply failed ({reason}) — {self._last_error}", "error")
            return {"ok": False, "applied": False,
                    "error": self._last_error, "warnings": warnings}
        self._last_good = previous_good
        if self._db is not None:
            await self._db.set_setting("firewall_last_good",
                                       json.dumps(previous_good, sort_keys=True))
        await self._event(f"FW: rules applied ({reason}, {self.topology} mode)", "info")

        watchdog = max(1, int(clean.get("watchdog_seconds", DEFAULT_WATCHDOG_SEC)))
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._watchdog(clean, watchdog, reason))
        self._watchdog_tasks.add(task)
        task.add_done_callback(self._watchdog_tasks.discard)
        return {"ok": True, "applied": True, "watchdog_seconds": watchdog,
                "warnings": warnings}

    async def revert_last_good(self) -> dict[str, Any]:
        async with self._async_apply_lock:
            return await self._revert_last_good_locked()

    async def _revert_last_good_locked(self) -> dict[str, Any]:
        """Re-apply the stored last-good config (manual / watchdog rollback)."""
        good: dict[str, Any] | None = None
        if self._db is not None:
            raw = await self._db.get_setting("firewall_last_good", "")
            if raw:
                try:
                    good = json.loads(raw)
                except ValueError:
                    good = None
        if good is None:
            good = self._last_good or self._config
        if not good.get("enabled", True):
            good = {**good, "enabled": True}
        applied = await asyncio.to_thread(self.apply, good)
        if not applied:
            await self._event(
                f"FW: revert failed — {self._last_error}", "error")
            return {"ok": False, "applied": False, "error": self._last_error}
        # The reverted rules become the stored config too, otherwise the next
        # restart (reconcile) would bring the reverted-away rules back.
        try:
            await self.persist_config(good)
        except Exception:  # noqa: BLE001 — the kernel is already reverted
            log.exception("firewall: could not persist the reverted config")
        await self._event("FW: reverted to last-good firewall config", "warn")
        return {"ok": True, "applied": True}

    async def apply_topology(self, topology: str) -> bool:
        """Apply a temporary topology while excluding concurrent reconciles."""
        async with self._async_apply_lock:
            previous = self._topology_override
            self._topology_override = (topology or "").strip().lower() or None
            try:
                return await asyncio.to_thread(self.apply)
            finally:
                self._topology_override = previous

    # -- watchdog ------------------------------------------------------------

    async def _watchdog(self, cfg: dict[str, Any], seconds: int,
                        reason: str) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if self._management_reachable():
            return
        await self._event(
            f"FW: watchdog — management unreachable after apply ({reason}); "
            f"auto-reverting to last-good config", "error")
        await self.revert_last_good()

    def _management_reachable(self) -> bool:
        """Verify the admin is NOT locked out by the applied rules.

        The deterministic check is a ruleset self-audit (the box cannot truly
        simulate a client-subnet source — loopback bypasses the input chain):
        the client subnet must be accepted in fw_input/fw_forward before any
        drop. Tests may inject a custom ``probe`` to simulate an external
        reachability verdict.
        """
        if self._probe is not None:
            return bool(self._probe())
        if not self.available:
            return True
        code, out = self._run_command(["nft", "list", "chain",
                                       f"{FAMILY} {TABLE} {CHAIN_INPUT}"])
        code2, out2 = self._run_command(["nft", "list", "chain",
                                         f"{FAMILY} {TABLE} {CHAIN_FORWARD}"])
        if code != 0 or code2 != 0:
            return True  # nft broken is not a firewall lockout
        text = out + "\n" + out2
        protected = [self.probe_ip(), self._client_gw, self._router_ip,
                     self._ipv6_gateway]
        for ip in protected:
            if _as_ip(ip):
                family_key = "ip6 saddr" if _is_ipv6(ip) else "ip saddr"
                ban_set = SET_BANS6 if _is_ipv6(ip) else SET_BANS
                if re.search(rf"{family_key} {re.escape(_as_ip(ip))}.*"
                             rf"(drop|@{ban_set})", text, re.I):
                    return False
        client_ok = any(n in text for n in
                        [*self._local_nets, *self._local_nets6])
        return client_ok

    # -- bans ----------------------------------------------------------------

    def _reseed_bans(self, family: int) -> tuple[bool, str]:
        """Restore active bans for one successfully rebuilt address family."""
        now = time.time()
        active = [ip for ip, b in self._bans.items()
                  if b.get("until", 0) > now and address_family(ip) == family]
        for ip in active:
            set_name = SET_BANS6 if _is_ipv6(ip) else SET_BANS
            if not self._run(
                    ["add", "element", f"{FAMILY} {TABLE} {set_name}",
                     f"{{ {ip} timeout {max(60, int(self._bans[ip]['until'] - now))}s }}"],
                    degrade=False, preserve_ipv6=(family == 4)):
                return False, self._last_error or f"IPv{family} ban restore failed"
        return True, ""

    def validate_ban_target(self, ip: str) -> str:
        """Return a client-facing reason when an IP cannot be banned."""
        ip = _as_ip(str(ip))
        if not ip:
            return "invalid IP address"
        for protected in [self.probe_ip(), self._client_gw, self._router_ip,
                          self._ipv6_gateway]:
            if _as_ip(protected) == ip:
                return "refusing to ban the box itself"
        address = parse_ip_address(ip)
        for network_text in (self._client_net, self._client_net6):
            network = parse_ip_network(network_text) if network_text else None
            if network is not None and address is not None and address in network:
                return "refusing to ban the client subnet"
        return ""

    async def ban_ip(self, ip: str, seconds: int, reason: str) -> bool:
        """Kernel-ban an IP in @fw_bans (auto-expiring) + persist + event.

        Refuses to ban the box's own IPs, the probe IP, or the client subnet —
        that would be a self-DoS / lockout.
        """
        ip = _as_ip(str(ip))
        rejection = self.validate_ban_target(ip)
        if rejection:
            if rejection.startswith("refusing"):
                log.warning("firewall: %s (%s)", rejection, ip)
            return False
        seconds = max(60, int(seconds))
        async with self._async_apply_lock:
            applied, changed = await asyncio.to_thread(
                self._change_ban, ip, seconds=seconds,
                reason=reason, remove=False)
        if not applied:
            return False
        if changed:
            await self._event(
                f"FW: banned {ip} for {seconds}s ({reason})", "warn")
        return True

    async def unban_ip(self, ip: str) -> bool:
        ip = _as_ip(str(ip))
        if not ip:
            return False
        async with self._async_apply_lock:
            applied, _ = await asyncio.to_thread(
                self._change_ban, ip, seconds=0, reason="", remove=True)
        if not applied:
            return False
        await self._event(f"FW: unbanned {ip}", "info")
        return True

    def _change_ban(self, ip: str, *, seconds: int, reason: str,
                    remove: bool) -> tuple[bool, bool]:
        """Apply one family-scoped ban edit without degrading the other family."""
        family = address_family(ip)
        if family not in (4, 6):
            return False, False
        set_name = SET_BANS6 if family == 6 else SET_BANS
        # Exclusive creation makes a repeated ban observable as EEXIST. nft's
        # non-exclusive `add element` can return success without refreshing an
        # existing timeout, which would make the application overstate expiry.
        verb = "delete" if remove else "create"
        with self._apply_thread_lock:
            existing = self._bans.get(ip)
            existing_active = bool(
                existing and existing.get("until", 0) > time.time())
            if not self.available:
                return False, False
            previous_error = self._last_error
            if not self._run(
                    [verb, "element", f"{FAMILY} {TABLE} {set_name}",
                     f"{{ {ip} }}" if remove else
                     f"{{ {ip} timeout {seconds}s }}"], degrade=False,
                    exists_ok=remove):
                if (not remove and existing_active and
                        any(term in self._last_error.lower() for term in
                            ("file exists", "already exists"))):
                    # The cached ban is still present in the kernel. nft does
                    # not refresh an element timeout on EEXIST, so keep its
                    # current deadline and do not claim a new duration.
                    self._last_error = previous_error
                    return True, False
                message = self._last_error or "firewall unavailable"
                if family == 4:
                    self._ipv4_apply_ok = False
                    self._ipv4_last_error = message
                else:
                    self._ipv6_available = False
                    self._ipv6_apply_ok = False
                    self._ipv6_last_error = message
                self._last_apply_ok = False
                self._last_error = message
                self._applied_sig = None
                return False, False

            if remove:
                self._bans.pop(ip, None)
            else:
                self._bans[ip] = {
                    "until": time.time() + seconds,
                    "reason": reason,
                    "seconds": seconds,
                }
            self._last_apply_ok = self._ipv4_apply_ok and self._ipv6_apply_ok
            return True, True

    def list_bans(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"ip": ip, "reason": b.get("reason", ""),
                 "until": b["until"], "remaining": max(0, b["until"] - now)}
                for ip, b in sorted(self._bans.items())
                if b.get("until", 0) > now]

    # -- scan detection + counters -------------------------------------------

    def _parse_scan_set(self, out: str) -> dict[str, int]:
        """Parse ``nft -j list set`` into {ip: new_syn_packets}."""
        try:
            data = json.loads(out)
            elements = data.get("set", {}).get("elem", {}) or {}
        except (ValueError, AttributeError):
            return {}
        result: dict[str, int] = {}
        for key, meta in elements.items():
            key = str(key).strip().strip('"')
            counter = (meta or {}).get("counter", {}) or {}
            result[key] = int(counter.get("packets", 0) or 0)
        return result

    def scan_detect_tick(self) -> list[str]:
        """Poll fw_scan_watch; ban sources exceeding the SYN threshold.

        Returns human-readable events for the log view. Element counters
        accumulate until the element expires (60 s), so the delta since the
        last read is the source's new-SYN rate over the window.
        """
        if not self.available:
            return []
        scan = self._config.get("scan_detect", {}) or {}
        if not scan.get("enabled", True):
            return []
        threshold = int(scan.get("syn_threshold", 200) or 200)
        ban_seconds = int(scan.get("ban_seconds", 3600) or 3600)
        events: list[str] = []
        current: dict[str, int] = {}
        previous_scan = dict(self._scan_last)
        failed_bans: set[str] = set()
        successful_families: set[int] = set()
        for set_name, family in ((SET_SCAN, 4), (SET_SCAN6, 6)):
            code, out = self._run_command(
                ["nft", "-j", "list", "set", f"{FAMILY} {TABLE} {set_name}"])
            if code != 0:
                continue
            successful_families.add(family)
            current.update(self._parse_scan_set(out))
        for ip, count in current.items():
            prev = self._scan_last.get(ip, 0)
            delta = max(0, count - prev)
            if delta >= threshold:
                address = parse_ip_address(ip)
                if address is None:
                    continue
                reason = f"port-scan ({delta} SYNs)"
                applied, changed = self._change_ban(
                    ip, seconds=ban_seconds, reason=reason, remove=False)
                if not applied:
                    failed_bans.add(ip)
                    continue
                if not changed:
                    continue
                msg = f"FW: banned {ip} for {ban_seconds}s (port-scan, {delta} SYNs)"
                events.append(msg)
                log.warning("firewall: %s", msg)
        if len(successful_families) == 2:
            self._scan_last = current
        else:
            self._scan_last = {
                ip: count for ip, count in self._scan_last.items()
                if (address := parse_ip_address(ip)) is not None
                and address.version not in successful_families}
            self._scan_last.update(current)
        for ip in failed_bans:
            if ip in previous_scan:
                self._scan_last[ip] = previous_scan[ip]
            else:
                self._scan_last.pop(ip, None)
        return events

    def _parse_counters(self, out: str) -> dict[str, tuple[int, int]]:
        """Parse ``nft -j list counters`` into {name: (packets, bytes)}."""
        try:
            data = json.loads(out)
            counters = data.get("counters", []) or []
        except (ValueError, AttributeError):
            return {}
        result: dict[str, tuple[int, int]] = {}
        for entry in counters:
            name = entry.get("name", "")
            if not name or not str(name).startswith("fw_"):
                continue
            value = entry.get("value", {}) or {}
            result[str(name)] = (int(value.get("packets", 0) or 0),
                                 int(value.get("bytes", 0) or 0))
        return result

    def drain_counters(self) -> list[str]:
        """Read fw_* counters and return per-rule delta events for the log."""
        if not self.available:
            return []
        code, out = self._run_command(["nft", "-j", "list", "counters"])
        if code != 0:
            return []
        current = self._parse_counters(out)
        events: list[str] = []
        for name, (packets, _bytes) in sorted(current.items()):
            prev = self._counters_last.get(name, (0, 0))[0]
            delta = max(0, packets - prev)
            if delta > 0:
                label = _RULE_LABELS.get(name, name)
                events.append(f"FW: {label} dropped {delta} packets")
            self._counters_last[name] = (packets, _bytes)
        self._counter_stats = [{"rule": name, "packets": packets, "bytes": b}
                               for name, (packets, b) in sorted(current.items())]
        return events

    def _drain(self) -> list[tuple[str, str]]:
        """Tick-level counter + scan drain (sync, off-loop caller). Returns
        ``(level, message)`` events for the caller to persist."""
        events: list[tuple[str, str]] = []
        try:
            for ev in self.drain_counters():
                self._push_log("info", ev)
                events.append(("info", ev))
            for ev in self.scan_detect_tick():
                self._push_log("warn", ev)
                events.append(("warn", ev))
        except Exception:  # noqa: BLE001
            log.exception("firewall: drain failed")
        return events

    # -- log / events --------------------------------------------------------

    def _push_log(self, level: str, message: str) -> None:
        self._log_ring.append({"ts": time.time(), "level": level,
                               "message": message})
        if len(self._log_ring) > 200:
            del self._log_ring[:len(self._log_ring) - 200]

    def record_event(self, level: str, message: str) -> None:
        """Public, synchronous entry for OUT-OF-BAND events (e.g. the WAF
        middleware blocking a request) that must land in the Firewall log tab.
        Sync-only by design: the ring is thread-safe enough for this and the
        caller (WAF middleware) persists to the events table itself."""
        self._push_log(level, message)

    async def _event(self, message: str, level: str = "info") -> None:
        self._push_log(level, message)
        if self._db is not None:
            try:
                await self._db.add_event(message, level)
            except Exception:  # noqa: BLE001
                log.exception("firewall: add_event failed")

    def recent_log(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(reversed(self._log_ring[-limit:]))

    def status(self) -> dict[str, Any]:
        firewall_enabled = bool(self._config.get("enabled", True))
        ipv6_applied = bool(firewall_enabled and self._ipv6_apply_ok)
        ipv4_applied = bool(firewall_enabled and self._ipv4_apply_ok)
        return {
            "enabled": firewall_enabled,
            "available": self.available,
            "topology": self.topology,
            "mode": "wan" if self.topology == "wan" else "lan",
            "applied": self._last_apply_ok,
            "apply_ok": self._last_apply_ok,
            "last_error": self._last_error,
            "ipv4_available": self.available,
            "ipv4_applied": ipv4_applied,
            "ipv4_healthy": ipv4_applied,
            "ipv4_last_error": "" if ipv4_applied else self._ipv4_last_error,
            "ipv6_available": self._ipv6_available,
            "ipv6_enabled": firewall_enabled,
            "ipv6_applied": ipv6_applied,
            "ipv6_healthy": ipv6_applied,
            "ipv6_last_error": "" if ipv6_applied else self._ipv6_last_error,
            "bans": self.list_bans(),
            "counters": self._counter_stats,
            "probe_ip": self.probe_ip(),
            "watchdog_seconds": int(self._config.get("watchdog_seconds",
                                                     DEFAULT_WATCHDOG_SEC) or
                                    DEFAULT_WATCHDOG_SEC),
        }

    # -- snapshot / geo ------------------------------------------------------

    def _geo_cidrs(self) -> list[str]:
        """Geo-block CIDRs from the ``firewall_geo`` DB setting (inert when
        geo_block is off or the map is empty). Sync-safe: the DB read happens
        in :meth:`load_geo` (async); this only returns the cached list."""
        return self._geo_cached if self._geo_cached is not None else []

    async def load_geo(self) -> list[str]:
        """Load + cache the country->CIDR map; called on config load."""
        self._geo_cached = []
        if not self._config.get("geo_block", False):
            return self._geo_cached
        if self._db is None:
            return self._geo_cached
        raw = await self._db.get_setting("firewall_geo", "")
        if not raw:
            return self._geo_cached
        try:
            mapping = json.loads(raw)
        except ValueError:
            log.warning("firewall: unparseable firewall_geo setting")
            return self._geo_cached
        cidrs: list[str] = []
        if isinstance(mapping, dict):
            for country, nets in mapping.items():
                if not isinstance(nets, list):
                    continue
                for net in nets:
                    norm = _cidr(str(net))
                    if norm:
                        cidrs.append(norm)
        elif isinstance(mapping, list):
            for net in mapping:
                norm = _cidr(str(net))
                if norm:
                    cidrs.append(norm)
        self._geo_cached = sorted(set(cidrs))
        return self._geo_cached

    async def save_geo(self, mapping: dict[str, Any]) -> None:
        """Persist the country->CIDR map (external maintenance)."""
        if self._db is None:
            return
        await self._db.set_setting("firewall_geo", json.dumps(mapping))
        await self.load_geo()
        await self.reconcile()

    def _snapshot(self, cfg: dict[str, Any], reason: str) -> None:
        """Timestamped snapshot: ruleset text + config JSON for rollback."""
        try:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            code, out = self._run_command(["nft", "list", "ruleset"])
            ruleset = out if code == 0 else ""
            payload = {"ts": time.time(), "reason": reason,
                       "topology": self.topology, "config": cfg,
                       "ruleset": ruleset}
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            path = self._snapshot_dir / f"fw-snapshot-{ts}.json"
            path.write_text(json.dumps(payload, indent=1, sort_keys=True,
                                       default=str),
                            encoding="utf-8")
        except OSError as exc:
            log.warning("firewall: snapshot write failed: %s", exc)

    async def shutdown(self) -> None:
        """Cancel watchdogs. Kernel rules stay in place (conservative)."""
        for task in list(self._watchdog_tasks):
            task.cancel()
        for task in list(self._watchdog_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._watchdog_tasks.clear()


#: Human labels for the named counters surfaced in the Firewall log view.
_RULE_LABELS = {
    COUNTER_WAN_IN_DROP: "WAN default-deny (box input)",
    COUNTER_WAN_FWD_DROP: "WAN default-deny (forwarded inbound)",
    COUNTER_BAN_DROP: "kernel ban",
    COUNTER_SYN_DROP: "SYN-flood guard",
    COUNTER_SYN_PASS: "SYN-flood guard (allowed within rate)",
    COUNTER_DENY_DROP: "blocklist / geo-block",
}
