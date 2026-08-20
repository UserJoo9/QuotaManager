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

Sets: ``fw_bans`` (``interval, timeout`` — CIDR bans auto-expire at the kernel),
``fw_scan_watch`` (``dynamic, timeout 60s, counter`` — per-source SYN counting
that drives the port-scan detector), ``fw_allow`` / ``fw_deny`` (``interval`` —
allowlist/blocklist, deny wins).

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
port-scan, manual) lands in ``@fw_bans`` with a kernel timeout and a DB event.

Like the packet engine, this module degrades gracefully: no ``nft``/no root =>
``available`` becomes False and every kernel op is a no-op. The command runner
is injected so tests drive a fake ``nft`` binary and assert the exact ruleset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("quota.firewall")

#: argv -> (returncode, output). Tests inject a fake; the default shells out
#: to the real ``nft`` binary (mirrors quota.nftables).
RunCommand = Callable[[list[str]], tuple[int, str]]

FAMILY = "inet"
TABLE = "quota_firewall"
CHAIN_INPUT = "fw_input"
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
    """Normalize an IPv4 CIDR string (``192.168.2.0/24``); None when invalid."""
    try:
        return str(ip_network(value, strict=False))
    except ValueError:
        return None


def _as_ip(value: str) -> str | None:
    """Normalize a bare IPv4 address; None when invalid."""
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def _net_containing(networks: list[str], host: str) -> str | None:
    """The network in ``networks`` that contains ``host`` (CIDR string)."""
    for net_str in networks:
        try:
            if ip_address(host) in ip_network(net_str, strict=False):
                return net_str
        except ValueError:
            continue
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


def render_commands(
    cfg: dict[str, Any],
    topology: str,
    local_nets: list[str],
    box_ips: list[str],
) -> list[list[str]]:
    """Render the full ``nft`` command list for the given config + topology.

    Pure and deterministic — the primary unit-test surface. ``cfg`` is the
    sanitized config dict, ``topology`` ``"lan"``/``"wan"``, ``local_nets`` the
    LAN subnets (client + uplink) that are always accepted, ``box_ips`` the
    box's own addresses (never denied, dnat-excluded). Returns argv suffix
    lists each prefixed with ``nft`` by the caller.
    """
    cfg = _merge_defaults(cfg)
    cmds: list[list[str]] = []
    table = f"{FAMILY} {TABLE}"
    cmds.append(["add", "table", table])
    cmds.append(["flush", "table", table])
    if not cfg["enabled"]:
        return cmds

    wan = topology == "wan"
    client_nets = [n for n in sorted(set(local_nets)) if n]

    # -- chains ------------------------------------------------------------
    cmds.append(["add", "chain", table, CHAIN_INPUT,
                 "{ type filter hook input priority -100; policy accept; }"])
    cmds.append(["add", "chain", table, CHAIN_FORWARD,
                 "{ type filter hook forward priority -100; policy accept; }"])
    if wan:
        cmds.append(["add", "chain", table, CHAIN_DNAT,
                     "{ type nat hook prerouting priority -100; policy accept; }"])

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
    allow_elems = [c for c in cfg["allow_cidrs"] if _cidr(c)]
    deny_elems = [c for c in cfg["deny_cidrs"] if _cidr(c)]
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

    geo = [c for c in cfg.get("_geo_cidrs", []) if _cidr(c)]
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
    if wan:
        counter_names.add(COUNTER_WAN_IN_DROP)
        counter_names.add(COUNTER_WAN_FWD_DROP)
        for i, fw in enumerate(forwards):
            tip = _as_ip(str(fw.get("target_ip", "") or ""))
            tport = int(fw.get("target_port", 0) or 0)
            if tip and tport:
                counter_names.add(f"fw_fwd_{i}")
        if dmz:
            counter_names.add("fw_dmz")
    for i, r in enumerate(rules):
        if bool(r.get("log", True)):
            counter_names.add(f"fw_custom_{i}")
    for name in sorted(counter_names):
        cmds.append(["add", "counter", table, name])

    # -- fw_input ----------------------------------------------------------
    add_rule(CHAIN_INPUT, "ct state established,related accept")
    add_rule(CHAIN_INPUT, "iif lo accept")
    add_rule(CHAIN_INPUT, "ip saddr 127.0.0.0/8 accept")
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
    # WAN-mode ppp0 drop MUST come before the SYN flood guard below —
    # otherwise the flood-accept matches new SYNs from any interface and
    # external connections slip through before the ppp0 drop is reached.
    if wan:
        if wan_expose:
            add_rule(CHAIN_INPUT,
                     f'iifname "ppp0" tcp dport {web_port} accept')
        add_rule(CHAIN_INPUT,
                 f'iifname "ppp0" ct state new '
                 f'counter name {COUNTER_WAN_IN_DROP} drop')
    if scan_enabled:
        add_rule(CHAIN_INPUT,
                 f"tcp flags syn ct state new "
                 f"update @{SET_SCAN} {{ ip saddr timeout 60s }}")
    add_rule(CHAIN_INPUT,
             f"tcp flags & (fin|syn|rst|ack) == syn ct state new "
             f"limit rate {flood_rate}/second burst {flood_burst} packets "
             f"counter name {COUNTER_SYN_PASS} accept")
    add_rule(CHAIN_INPUT,
             f"tcp flags & (fin|syn|rst|ack) == syn ct state new "
             f"counter name {COUNTER_SYN_DROP} drop")

    # -- fw_forward --------------------------------------------------------
    add_rule(CHAIN_FORWARD, "ct state established,related accept")
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
            if not tip or not tport:
                continue
            expr = " ".join(x for x in
                            ('iifname "ppp0"', proto,
                             f"ip daddr {tip}" if tip else "",
                             _dport(tport, fw.get("protocol", "tcp")),
                             "ct state new",
                             f"counter name fw_fwd_{i}", "accept")
                            if x)
            add_rule(CHAIN_FORWARD, expr)
        if dmz:
            add_rule(CHAIN_FORWARD,
                     f'iifname "ppp0" ip daddr {dmz} ct state new '
                     f"counter name fw_dmz accept")
    add_rule(CHAIN_FORWARD, f"ip saddr @{SET_BANS} "
                            f"counter name {COUNTER_BAN_DROP} drop")
    # WAN-mode ppp0 drop MUST come before the SYN flood guard below —
    # same ordering fix as fw_input (see comment there).
    if wan:
        add_rule(CHAIN_FORWARD,
                 f'iifname "ppp0" ct state new '
                 f'counter name {COUNTER_WAN_FWD_DROP} drop')
    if scan_enabled:
        add_rule(CHAIN_FORWARD,
                 f"tcp flags syn ct state new "
                 f"update @{SET_SCAN} {{ ip saddr timeout 60s }}")
    add_rule(CHAIN_FORWARD,
             f"tcp flags & (fin|syn|rst|ack) == syn ct state new "
             f"limit rate {flood_rate}/second burst {flood_burst} packets "
             f"counter name {COUNTER_SYN_PASS} accept")
    add_rule(CHAIN_FORWARD,
             f"tcp flags & (fin|syn|rst|ack) == syn ct state new "
             f"counter name {COUNTER_SYN_DROP} drop")

    # -- fw_dnat (WAN mode only) -------------------------------------------
    if wan:
        for i, fw in enumerate(forwards):
            proto = _proto(fw.get("protocol", "tcp"))
            sport = int(fw.get("source_port", 0) or 0)
            tport = int(fw.get("target_port", 0) or 0)
            tip = _as_ip(str(fw.get("target_ip", "") or ""))
            if not tip or not sport or not tport:
                continue
            expr = " ".join(x for x in
                            ('iifname "ppp0"', proto,
                             _dport(sport, fw.get("protocol", "tcp")),
                             f"counter name fw_fwd_{i}",
                             f"dnat to {tip}:{tport}")
                            if x)
            add_rule(CHAIN_DNAT, expr)
        if dmz:
            excludes = " ".join(f"ip daddr != {n}" for n in client_nets)
            add_rule(CHAIN_DNAT,
                     f'iifname "ppp0" {excludes} counter name fw_dmz '
                     f"dnat to {dmz}".strip())
    return cmds


def _render_custom(add_rule: Callable[[str, str], None], r: dict[str, Any], i: int) -> None:
    """Render one ordered custom rule (input or forward)."""
    chain = CHAIN_INPUT if str(r.get("chain", "forward")).lower() == "input" \
        else CHAIN_FORWARD
    action = str(r.get("action", "deny")).lower()
    src = _cidr(str(r.get("src", "") or ""))
    dst = _cidr(str(r.get("dst", "") or ""))
    proto = _proto(r.get("protocol", ""))
    sport = _sport(int(r.get("src_port", 0) or 0), r.get("protocol", ""))
    dport = _dport(int(r.get("dst_port", 0) or 0), r.get("protocol", ""))
    expr = " ".join(x for x in
                    (f"ip saddr {src}" if src else "",
                     f"ip daddr {dst}" if dst else "",
                     proto, sport, dport)
                    if x)
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
        self._snapshot_dir = Path(snapshot_dir or "data/firewall_snapshots")

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

        self.available = True
        self._warned = False
        self._config: dict[str, Any] = config_to_dict(
            getattr(cfg, "firewall", None) or FirewallConfig())
        self._last_good: dict[str, Any] | None = None
        self._last_sig: str | None = None
        self._applied_sig: str | None = None
        self._last_apply_ok = False
        self._last_error = ""
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

    def _fail(self, message: str) -> None:
        self.available = False
        self._last_error = message
        if not self._warned:
            self._warned = True
            log.warning("firewall unavailable: %s", message)

    def _run(self, args: list[str]) -> bool:
        """Run ``nft <args>``; True on success or benign "File exists"."""
        if not self.available:
            return False
        code, out = self._run_command(["nft", *args])
        if code == 0:
            return True
        if args[0] == "add" and any(s in out for s in ("File exists",
                                                       "already exists")):
            return True
        self._fail(f"nft {args[0]} failed: {out.strip()}")
        return False

    # -- config persistence --------------------------------------------------

    async def load_config(self) -> dict[str, Any]:
        """Runtime master = the ``firewall_config`` DB setting; fall back to the
        seeded in-memory config (YAML defaults) when unset/unparseable."""
        if self._db is None:
            return self._config
        raw = await self._db.get_setting("firewall_config", "")
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

    async def persist_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Sanitize + store a config and make it the runtime master."""
        clean = self.sanitize(data)
        stored = {k: v for k, v in clean.items() if not k.startswith("_")}
        if self._db is not None:
            await self._db.set_setting("firewall_config",
                                       json.dumps(stored, sort_keys=True))
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
        protected = [self.probe_ip(), self._client_gw, self._router_ip]
        protected_nets = [self._client_net] if self._client_net else []

        def is_protected(ip_or_cidr: str) -> bool:
            net = _cidr(ip_or_cidr)
            if net is not None:
                for pn in protected_nets:
                    try:
                        if ip_network(net, strict=False).subnet_of(ip_network(pn, strict=False)) \
                                or ip_network(pn, strict=False).subnet_of(ip_network(net, strict=False)):
                            return True
                    except ValueError:
                        continue
                for p in protected:
                    if _as_ip(p) and ip_address(p) in ip_network(net, strict=False):
                        return True
                return False
            ip = _as_ip(ip_or_cidr)
            return ip is not None and ip in protected

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
            if r["action"] == "deny":
                # The one failure mode worth refusing: a deny rule that is
                # UNCONDITIONAL for the admin's own traffic (no dst/port
                # scope) — an input deny of everything from the client subnet
                # locks the dashboard, a forward deny of everything from the
                # client subnet cuts the whole household's internet. Port-
                # scoped denies (torrent blocks, blocking a specific server)
                # are fine and common.
                src_any = not src or src in ("0.0.0.0/0", "")
                src_hits = src_any or is_protected(src)
                if r["chain"] == "input":
                    dst_any = not dst or dst in ("0.0.0.0/0", "")
                    if src_hits and dst_any and not r.get("dst_port"):
                        warnings.append(
                            f"{kind} {r.get('name', '?')}: input deny with no "
                            f"port/dst scope would lock the dashboard out — "
                            f"ignored")
                        return None
                else:  # forward
                    dst_any = not dst or dst in ("0.0.0.0/0", "")
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
            if port and 1 <= port <= 65535 and \
                    str(s.get("protocol", "tcp")).lower() in ("tcp", "udp"):
                services.append({"name": str(s.get("name", "") or ""),
                                 "protocol": str(s.get("protocol", "tcp")).lower(),
                                 "port": port,
                                 "source": str(s.get("source", "0.0.0.0/0"))})
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
            if tip and 1 <= sp <= 65535 and 1 <= tp <= 65535 and \
                    str(f.get("protocol", "tcp")).lower() in ("tcp", "udp"):
                forwards.append({"name": str(f.get("name", "") or ""),
                                 "protocol": str(f.get("protocol", "tcp")).lower(),
                                 "source_port": sp,
                                 "target_ip": tip,
                                 "target_port": tp})
        data["port_forwards"] = forwards

        dmz = _as_ip(str(data.get("dmz", "") or ""))
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
        cfg = await self.load_config()
        if not self.available:
            return False
        drained: list[tuple[str, str]] = []

        def _work() -> bool:
            sig = self._signature(cfg)
            changed = not (sig == self._last_sig and self._applied_sig == sig)
            if changed:
                if not self.apply(cfg):
                    self._last_sig = sig
                    return False
                self._applied_sig = sig
                self._last_sig = sig
            drained.extend(self._drain())
            return True

        ok = await asyncio.to_thread(_work)
        if ok:
            for level, message in drained:
                await self._event(message, level)
        return ok

    def apply(self, cfg: dict[str, Any] | None = None) -> bool:
        """Program the kernel ruleset for ``cfg`` (or the current config).

        Sync (subprocess — callers wrap in ``asyncio.to_thread``). Reseeds the
        delta baselines so a flush never reports stale bytes as events.
        """
        if not self.available:
            return False
        cfg = _merge_defaults(cfg if cfg is not None else self._config)
        topology = self.topology
        geo = self._geo_cidrs()
        cfg = _merge_defaults({**cfg, "_geo_cidrs": geo,
                               "_web_port": self._web_port})
        try:
            cmds = render_commands(cfg, topology, self._local_nets, self._box_ips)
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            self._last_error = f"render failed: {exc}"
            log.exception("firewall: render failed")
            return False
        for cmd in cmds:
            if not self._run(cmd):
                self._last_apply_ok = False
                if not self._last_error:
                    self._last_error = (f"nft {cmd[0]} {cmd[1]} failed — "
                                        f"firewall unavailable")
                return False
        self._config = cfg
        self._last_apply_ok = True
        self._last_error = ""
        self._counters_last = {}
        self._scan_last = {}
        self._reseed_bans()
        log.info("firewall: applied (%s mode, %d commands)",
                 topology, len(cmds))
        return True

    async def safe_apply(self, data: dict[str, Any], reason: str = "dashboard") -> dict[str, Any]:
        """Sanitize -> snapshot -> apply -> watchdog. Returns a status dict.

        The previous good config is preserved as ``firewall_last_good`` (DB)
        and the pre-apply ruleset snapshot lands in ``data/firewall_snapshots/``.
        A watchdog task re-verifies reachability after ``watchdog_seconds`` and
        auto-reverts to the last-good config on failure.
        """
        clean = await self.persist_config(data)
        warnings = clean.pop("_warnings", [])
        if not self.available:
            return {"ok": False, "applied": False, "error": "firewall unavailable",
                    "warnings": warnings}
        if not clean.get("enabled"):
            self.apply(clean)
            await self._event(f"FW: firewall disabled ({reason})", "warn")
            return {"ok": True, "applied": True, "warnings": warnings}

        self._snapshot(clean, reason)
        # Rollback target = the config that was GOOD BEFORE this apply. A
        # failed watchdog must restore the previous working rules — NOT the
        # just-applied ones that locked the admin out (the naive "last good =
        # what I just applied" would revert to the same broken ruleset).
        previous_good = self._last_good or self._config
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
        await asyncio.to_thread(self.apply, good)
        await self._event("FW: reverted to last-good firewall config", "warn")
        return {"ok": True, "applied": True}

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
        protected = [self.probe_ip(), self._client_gw, self._router_ip]
        for ip in protected:
            if _as_ip(ip):
                if re.search(rf"ip saddr {re.escape(_as_ip(ip))}.*(drop|@fw_bans)",
                             text, re.I):
                    return False
        client_ok = any(n in text for n in self._local_nets)
        return client_ok

    # -- bans ----------------------------------------------------------------

    def _reseed_bans(self) -> None:
        """Re-add still-active bans after a flush (bans outlive a re-apply)."""
        if not self.available:
            return
        now = time.time()
        active = [ip for ip, b in self._bans.items()
                  if b.get("until", 0) > now]
        if not active:
            return
        for ip in active:
            self._run(["add", "element", f"{FAMILY} {TABLE} {SET_BANS}",
                       f"{{ {ip} timeout {max(60, int(self._bans[ip]['until'] - now))}s }}"])

    async def ban_ip(self, ip: str, seconds: int, reason: str) -> bool:
        """Kernel-ban an IP in @fw_bans (auto-expiring) + persist + event.

        Refuses to ban the box's own IPs, the probe IP, or the client subnet —
        that would be a self-DoS / lockout.
        """
        ip = _as_ip(str(ip))
        if not ip:
            return False
        for protected in [self.probe_ip(), self._client_gw, self._router_ip]:
            if _as_ip(protected) == ip:
                log.warning("firewall: refusing to ban the box itself (%s)", ip)
                return False
        if self._client_net:
            try:
                if ip_address(ip) in ip_network(self._client_net, strict=False):
                    log.warning("firewall: refusing to ban the client subnet (%s)", ip)
                    return False
            except ValueError:
                pass
        seconds = max(60, int(seconds))
        self._bans[ip] = {"until": time.time() + seconds, "reason": reason,
                          "seconds": seconds}
        self._run(["add", "element", f"{FAMILY} {TABLE} {SET_BANS}",
                   f"{{ {ip} timeout {seconds}s }}"])
        await self._event(
            f"FW: banned {ip} for {seconds}s ({reason})", "warn")
        return True

    async def unban_ip(self, ip: str) -> bool:
        ip = _as_ip(str(ip))
        if not ip:
            return False
        self._bans.pop(ip, None)
        if self.available:
            self._run(["delete", "element", f"{FAMILY} {TABLE} {SET_BANS}",
                       f"{{ {ip} }}"])
        await self._event(f"FW: unbanned {ip}", "info")
        return True

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
        code, out = self._run_command(
            ["nft", "-j", "list", "set", f"{FAMILY} {TABLE} {SET_SCAN}"])
        if code != 0:
            return []
        current = self._parse_scan_set(out)
        events: list[str] = []
        for ip, count in current.items():
            prev = self._scan_last.get(ip, 0)
            delta = max(0, count - prev)
            if delta >= threshold:
                self._bans[ip] = {"until": time.time() + ban_seconds,
                                  "reason": f"port-scan ({delta} SYNs)",
                                  "seconds": ban_seconds}
                self._run(["add", "element", f"{FAMILY} {TABLE} {SET_BANS}",
                           f"{{ {ip} timeout {ban_seconds}s }}"])
                msg = f"FW: banned {ip} for {ban_seconds}s (port-scan, {delta} SYNs)"
                events.append(msg)
                log.warning("firewall: %s", msg)
        self._scan_last = current
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
        return {
            "enabled": bool(self._config.get("enabled", True)),
            "available": self.available,
            "topology": self.topology,
            "mode": "wan" if self.topology == "wan" else "lan",
            "applied": self._applied_sig is not None,
            "apply_ok": self._last_apply_ok,
            "last_error": self._last_error,
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