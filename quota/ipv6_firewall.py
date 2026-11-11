"""Pure IPv6 nftables policy rendering for ``inet quota_firewall``.

The legacy renderer remains the owner of IPv4 rules. This module supplies the
family-specific IPv6 sets and policy expressions that are composed into the
same table by :mod:`quota.firewall`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quota.ip_addresses import address_family, split_ip_networks

SET_BANS6 = "fw_bans6"
SET_SCAN6 = "fw_scan_watch6"
SET_ALLOW6 = "fw_allow6"
SET_DENY6 = "fw_deny6"


@dataclass(frozen=True)
class RenderContext:
    """Inputs shared by the IPv6 policy renderer."""

    cfg: dict[str, Any]
    table: str
    chain_input: str
    chain_forward: str
    local_nets: list[str]
    box_ips: list[str]
    wan: bool
    lan_guard: bool
    web_port: int
    tailscale_port: int
    counter_ban_drop: str
    counter_deny_drop: str
    counter_wan_in_drop: str
    counter_wan_fwd_drop: str
    counter_syn_pass: str
    counter_syn_drop: str


def split_cidrs(values: list[Any] | None) -> tuple[list[str], list[str]]:
    """Return normalized, deduplicated IPv4 and IPv6 networks separately."""
    return split_ip_networks(values)


def _address_family(value: str) -> int | None:
    return address_family(value, network=True)


def _neg(networks: list[str], key: str) -> str:
    return " ".join(f"{key} != {network}" for network in sorted(set(networks)))


def _custom_rule(rule: dict[str, Any], index: int) -> tuple[str, str] | None:
    """Render one IPv6 custom rule, including unscoped dual-stack rules."""
    src = str(rule.get("src", "") or "")
    dst = str(rule.get("dst", "") or "")
    src_family, dst_family = _address_family(src), _address_family(dst)
    if (src_family not in (None, 6) or dst_family not in (None, 6)
            or (src_family and dst_family and src_family != dst_family)):
        return None

    chain = "fw_input" if str(rule.get("chain", "forward")).lower() == "input" \
        else "fw_forward"
    action = str(rule.get("action", "deny")).lower()
    proto = str(rule.get("protocol", "") or "").lower()
    parts = ["meta nfproto ipv6"]
    if src:
        parts.append(f"ip6 saddr {src}")
    if dst:
        parts.append(f"ip6 daddr {dst}")
    if proto in ("tcp", "udp"):
        parts.append(f"meta l4proto {proto}")
    elif proto == "icmp":
        # nftables calls the IPv6 transport protocol ``ipv6-icmp``.
        parts.append("meta l4proto ipv6-icmp")
    for field, direction in (("src_port", "sport"), ("dst_port", "dport")):
        try:
            port = int(rule.get(field, 0) or 0)
        except (TypeError, ValueError):
            port = 0
        if port and proto in ("tcp", "udp"):
            parts.append(f"{proto} {direction} {port}")
    if bool(rule.get("log", True)):
        parts.append(f"counter name fw_custom_{index}")
    parts.append("accept" if action == "allow" else "drop")
    return chain, " ".join(parts)


def render_ipv6_commands(context: RenderContext) -> list[list[str]]:
    """Render IPv6 set declarations, family-split elements, and filter rules."""
    cfg = context.cfg
    table = context.table
    chain_input = context.chain_input
    chain_forward = context.chain_forward
    local_nets = context.local_nets
    box_ips = context.box_ips
    wan = context.wan
    lan_guard = context.lan_guard
    web_port = context.web_port
    tailscale_port = context.tailscale_port
    counter_ban_drop = context.counter_ban_drop
    counter_deny_drop = context.counter_deny_drop
    counter_wan_in_drop = context.counter_wan_in_drop
    counter_wan_fwd_drop = context.counter_wan_fwd_drop
    counter_syn_pass = context.counter_syn_pass
    counter_syn_drop = context.counter_syn_drop
    commands: list[list[str]] = [
        ["add", "set", table, SET_BANS6,
         "{ type ipv6_addr; flags interval, timeout; }"],
        ["add", "set", table, SET_ALLOW6,
         "{ type ipv6_addr; flags interval; }"],
        ["add", "set", table, SET_DENY6,
         "{ type ipv6_addr; flags interval; }"],
        ["add", "set", table, SET_SCAN6,
         "{ type ipv6_addr; flags dynamic, timeout; timeout 60s; counter; }"],
    ]

    _allow4, allow6 = split_cidrs(cfg.get("allow_cidrs", []))
    _deny4, deny6 = split_cidrs(cfg.get("deny_cidrs", []))
    if allow6:
        commands.append(["add", "element", table, SET_ALLOW6,
                         "{ " + ", ".join(allow6) + " }"])
    if deny6:
        commands.append(["add", "element", table, SET_DENY6,
                         "{ " + ", ".join(deny6) + " }"])

    def add_rule(chain: str, expression: str) -> None:
        commands.append(["add", "rule", table, chain, expression])

    _geo4, geo6 = split_cidrs(cfg.get("_geo_cidrs", []))
    rules = [rule for rule in cfg.get("rules", []) if isinstance(rule, dict)]
    scan = cfg.get("scan_detect", {}) or {}
    flood = cfg.get("syn_flood", {}) or {}
    scan_enabled = bool(scan.get("enabled", True))
    flood_rate = int(flood.get("rate", 10) or 10)
    flood_burst = int(flood.get("burst", 20) or 20)

    for network in sorted(set(local_nets)):
        add_rule(chain_input, f"ip6 saddr {network} accept")
    for address in sorted(set(box_ips)):
        add_rule(chain_input, f"ip6 saddr {address} accept")
    if geo6:
        add_rule(chain_input, f"{_neg(geo6, 'ip6 saddr')} "
                 f"counter name {counter_deny_drop} drop")
    add_rule(chain_input, f"ip6 saddr @{SET_DENY6} "
             f"counter name {counter_deny_drop} drop")
    add_rule(chain_input, f"ip6 saddr @{SET_ALLOW6} accept")
    for index, rule in enumerate(rules):
        if str(rule.get("chain", "forward")).lower() != "input":
            continue
        rendered = _custom_rule(rule, index)
        if rendered:
            add_rule(*rendered)
    add_rule(chain_input, f"ip6 saddr @{SET_BANS6} "
             f"counter name {counter_ban_drop} drop")
    add_rule(chain_input, f"meta nfproto ipv6 udp dport {tailscale_port} accept")
    if wan:
        add_rule(chain_input,
                 f'iifname "ppp0" meta nfproto ipv6 ct state new '
                 f"counter name {counter_wan_in_drop} drop")
    elif lan_guard:
        local = _neg(sorted(set(local_nets)), "ip6 saddr")
        add_rule(chain_input,
                 f"meta nfproto ipv6 tcp dport {web_port} ct state new "
                 f"{local} counter name {counter_wan_in_drop} drop".strip())
    if scan_enabled:
        add_rule(chain_input,
                 f"meta nfproto ipv6 tcp flags syn ct state new "
                 f"update @{SET_SCAN6} {{ ip6 saddr timeout 60s }}")
    add_rule(chain_input,
             f"meta nfproto ipv6 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new limit rate {flood_rate}/second burst "
             f"{flood_burst} packets counter name {counter_syn_pass} accept")
    add_rule(chain_input,
             f"meta nfproto ipv6 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new counter name {counter_syn_drop} drop")

    # Forward policy mirrors the family-specific address policy, without
    # introducing IPv6 DNAT, port-forward, DMZ, or public service publishing.
    for network in sorted(set(local_nets)):
        add_rule(chain_forward, f"ip6 saddr {network} accept")
    if geo6:
        add_rule(chain_forward, f"{_neg(geo6, 'ip6 saddr')} "
                 f"counter name {counter_deny_drop} drop")
    add_rule(chain_forward, f"ip6 saddr @{SET_DENY6} "
             f"counter name {counter_deny_drop} drop")
    add_rule(chain_forward, f"ip6 saddr @{SET_ALLOW6} accept")
    for index, rule in enumerate(rules):
        if str(rule.get("chain", "forward")).lower() != "forward":
            continue
        rendered = _custom_rule(rule, index)
        if rendered:
            add_rule(*rendered)
    add_rule(chain_forward, f"ip6 saddr @{SET_BANS6} "
             f"counter name {counter_ban_drop} drop")
    if wan:
        add_rule(chain_forward,
                 f'iifname "ppp0" meta nfproto ipv6 ct state new '
                 f"counter name {counter_wan_fwd_drop} drop")
    if scan_enabled:
        add_rule(chain_forward,
                 f"meta nfproto ipv6 tcp flags syn ct state new "
                 f"update @{SET_SCAN6} {{ ip6 saddr timeout 60s }}")
    add_rule(chain_forward,
             f"meta nfproto ipv6 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new limit rate {flood_rate}/second burst "
             f"{flood_burst} packets counter name {counter_syn_pass} accept")
    add_rule(chain_forward,
             f"meta nfproto ipv6 tcp flags & (fin|syn|rst|ack) == syn "
             f"ct state new counter name {counter_syn_drop} drop")
    return commands
