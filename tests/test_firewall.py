"""Firewall module tests (``quota.firewall``).

Covers the 9 acceptance sections with a fake ``nft`` binary (same pattern as
test_nftables.py — no root / no kernel on the dev box):

1. mode-aware posture (LAN permissive / WAN default-deny, topology-derived)
2. never breaks quota/accounting (own table, priority -100, LAN pass-through)
3. safe-apply snapshot + watchdog auto-revert + last-good restore
4. bans / SYN-flood guard / brute-force hook / port-scan detection
5. allow/deny CIDR lists (deny > allow) + ordered custom rules
6. port forwards + DMZ (WAN-only; API 409 in LAN)
7. logging: every drop/ban/apply surfaces a level+timestamped event
8. reconcile convergence (no rebuild when unchanged; converges on change)
9. API surface: CRUD, revert, ban/unban, geo, 409/404, auth, brute-force ban

Every test shuts the manager down (the safe-apply watchdog schedules an
``asyncio.sleep``; a leaked task on an unclosed loop hangs the pytest process
at exit) — the ``env`` fixture guarantees it via ``yield``/finally.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Config
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.firewall import (
    CHAIN_DNAT,
    CHAIN_FORWARD,
    CHAIN_INPUT,
    COUNTER_BAN_DROP,
    COUNTER_SYN_DROP,
    COUNTER_SYN_PASS,
    COUNTER_WAN_FWD_DROP,
    COUNTER_WAN_IN_DROP,
    SET_ALLOW,
    SET_BANS,
    SET_DENY,
    FirewallManager,
    render_commands,
)
from quota.service import QuotaService

TZ = "Africa/Cairo"

LOCAL_NETS = ["192.168.1.0/24", "192.168.2.0/24"]
BOX_IPS = ["192.168.2.1", "192.168.1.1"]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeNft:
    """In-memory stand-in for ``nft`` scoped to the ``quota_firewall`` table.

    Records every invocation; simulates the sets, named counters, the
    ``-j list set`` / ``-j list counters`` JSON the manager parses, and the
    ``list chain`` text the watchdog self-audit reads.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[str] = []
        self.elements: dict[str, set[str]] = {SET_ALLOW: set(), SET_DENY: set()}
        self.bans: dict[str, int] = {}   # ip -> timeout seconds
        self.counters: dict[str, int] = {}  # name -> packets
        self.scan_set: dict[str, int] = {}  # ip -> syn packets
        self.chain_rules: dict[str, list[str]] = {}  # hook -> [expr]
        #: persistent write failure (add/flush/delete/reset all return 1).
        self.reject_writes = False

    def _counters_json(self) -> tuple[int, str]:
        entries = [{"name": name, "value": {"packets": packets, "bytes": packets * 96}}
                   for name, packets in self.counters.items()]
        return 0, json.dumps({"counters": entries})

    def _scan_json(self) -> tuple[int, str]:
        return 0, json.dumps({
            "set": {"elem": {ip: {"counter": {"packets": n}}
                             for ip, n in self.scan_set.items()}}})

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[0] != "nft":
            return 1, f"unknown binary {argv[0]}"
        args = argv[1:]
        if self.reject_writes and args[0] in ("add", "flush", "delete", "reset"):
            return 1, "fake: write rejected"
        if args[0] == "-j":
            if args[1] == "list" and args[2] == "counters":
                return self._counters_json()
            if args[1] == "list" and args[2] == "set":
                return self._scan_json()
            return 0, ""
        if args[0] == "list":
            if args[1] == "chain":
                parts = args[2].split()
                hook = parts[-1]
                lines = ""
                for i, expr in enumerate(self.chain_rules.get(hook, [])):
                    lines += f"\t{expr} # handle {i + 1}\n"
                return 0, lines
            if args[1] == "ruleset":
                return 0, "table inet quota_firewall { }"
            return 0, ""
        if args[0] == "add":
            if args[1] == "rule":
                expr = args[-1]
                self.rules.append(expr)
                hook = args[3]
                self.chain_rules.setdefault(hook, []).append(expr)
                return 0, ""
            if args[1] in ("table", "chain", "set"):
                return 0, ""
            if args[1] == "counter":
                self.counters.setdefault(args[-1], 0)
                return 0, ""
            if args[1] == "element":
                # ban_ip/scan/seed use "inet quota_firewall fw_bans" (one arg);
                # render uses ["inet quota_firewall", "fw_allow", "{...}"] (split).
                if len(args) >= 4 and args[3] in (SET_ALLOW, SET_DENY, SET_BANS):
                    name = args[3]
                else:
                    name = args[2].split()[-1]
                members = {m.strip()
                           for m in args[-1].strip("{}").split(",") if m.strip()}
                if name == SET_BANS:
                    for m in members:
                        ip, _, timeout = m.partition("timeout")
                        self.bans[ip.strip()] = int(timeout.strip().rstrip("s"))
                else:
                    self.elements.setdefault(name, set()).update(members)
                return 0, ""
            return 0, ""
        if args[0] == "flush" and args[1] == "table":
            self.rules = []
            self.chain_rules = {}
            self.bans = {}
            return 0, ""
        if args[0] == "delete" and args[1] == "element":
            name = args[2].split()[-1]
            members = {m.strip() for m in args[-1].strip("{}").split(",") if m.strip()}
            if name == SET_BANS:
                for m in members:
                    self.bans.pop(m, None)
            return 0, ""
        if args[0] == "reset":
            return 0, ""
        return 0, ""


class Env(NamedTuple):
    fw: FirewallManager
    db: Any
    fake: FakeNft
    cfg: Config


def _make_env(tmp_path, *, fake=None, probe=None, topology="lan") -> Env:
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    cfg.engine.topology = topology
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = "192.168.1.1"
    db = _db.Database(tmp_path / "fw.db")
    _run(db.connect())
    fake = fake or FakeNft()
    fw = FirewallManager(cfg, db, run_command=fake, probe=probe,
                         snapshot_dir=tmp_path / "snaps", web_port=8080)
    return Env(fw, db, fake, cfg)


def _close(env: Env) -> None:
    """Cancel watchdogs + close the DB. A leaked watchdog task (asyncio.sleep)
    on an unclosed loop hangs the pytest process at exit, so this MUST always
    run even when a test's assertions fail."""
    _run(env.fw.shutdown())
    _run(env.db.close())


@pytest.fixture
def env(tmp_path):
    e = _make_env(tmp_path)
    yield e
    _close(e)


def _base_cfg(**overrides) -> dict:
    data = {
        "enabled": True,
        "watchdog_seconds": 45,
        "services": [], "port_forwards": [], "rules": [], "dmz": "",
        "allow_cidrs": [], "deny_cidrs": [],
        "syn_flood": {"rate": 10, "burst": 20},
        "brute_force": {"threshold": 10, "ban_seconds": 1800},
        "scan_detect": {"enabled": True, "syn_threshold": 200, "ban_seconds": 3600},
        "geo_block": False, "wan_confirmed": False,
    }
    data.update(overrides)
    return data


def _rules(cfg_cmds):
    return [c[-1] for c in cfg_cmds if c[0] == "add" and c[1] == "rule"]


# ---------------------------------------------------------------------------
# 1. mode-aware posture (derived from topology, LAN permissive / WAN default-deny)
# ---------------------------------------------------------------------------


def test_lan_render_is_permissive_no_wan_default_deny():
    cmds = render_commands(_base_cfg(), "lan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(_rules(cmds))
    assert "ppp0" not in joined, "LAN mode must not contain WAN rules"
    assert "limit rate 10/second burst 20 packets" in joined  # box SYN-flood guard
    assert "policy accept" in " ".join(c[-1] for c in cmds)
    assert not any(c[1] == "chain" and CHAIN_DNAT in c for c in cmds)


def test_wan_render_adds_default_deny_on_ppp0():
    cmds = render_commands(_base_cfg(), "wan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(_rules(cmds))
    # input + forward both default-deny NEW inbound on ppp0 with named counters
    assert f'iifname "ppp0" ct state new counter name {COUNTER_WAN_IN_DROP} drop' in joined
    assert f'iifname "ppp0" ct state new counter name {COUNTER_WAN_FWD_DROP} drop' in joined
    # fw_dnat chain exists in WAN
    assert any(c[1] == "chain" and CHAIN_DNAT in c for c in cmds)
    # dashboard is NOT exposed without wan_confirmed
    assert "tcp dport 8080 accept" not in joined
    # LAN traffic always accepted even under WAN
    for net in LOCAL_NETS:
        assert f"ip saddr {net} accept" in joined


def test_wan_confirmed_exposes_dashboard_only_when_explicit():
    cmds = render_commands(_base_cfg(wan_confirmed=True), "wan", LOCAL_NETS, BOX_IPS)
    assert 'iifname "ppp0" tcp dport 8080 accept' in " | ".join(_rules(cmds))


def test_all_named_counters_declared_before_use():
    # Real nftables rejects `counter name X` in a rule when the counter object
    # has not been declared first — "Could not process rule: No such file or
    # directory" (the bug that shipped in the first pass; the fake nft in tests
    # tolerated it, the live Kali box did not). Every counter a rule references
    # must have a prior `add counter` command.
    cmds = render_commands(_base_cfg(
        rules=[{"chain": "forward", "src": "0.0.0.0/0", "dst": "",
                "dst_port": 6881, "protocol": "tcp", "action": "deny",
                "log": True},
               {"chain": "input", "src": "", "dst": "", "dst_port": 0,
                "protocol": "", "action": "allow", "log": False}],
        port_forwards=[{"name": "ssh", "source_port": 2222,
                        "target_ip": "192.168.2.10", "target_port": 22,
                        "protocol": "tcp"}],
        dmz="192.168.2.77",
        wan_confirmed=True,
    ), "wan", LOCAL_NETS, BOX_IPS)

    declared = {c[3] for c in cmds
                if c[0] == "add" and c[1] == "counter"}
    used = set()
    for c in cmds:
        if c[0] == "add" and c[1] == "rule":
            toks = c[-1].split()
            for i, t in enumerate(toks[:-1]):
                if t == "name" and toks[i + 1].startswith("fw_"):
                    used.add(toks[i + 1])
    assert used, "test config should reference named counters"
    assert declared >= used, f"undeclared counters: {used - declared}"
    # Declarations are emitted BEFORE the first rule that uses them.
    rule_pos = {i for i, c in enumerate(cmds)
                if c[0] == "add" and c[1] == "rule"}
    counter_pos = {i for i, c in enumerate(cmds)
                   if c[0] == "add" and c[1] == "counter"}
    assert counter_pos and max(counter_pos) < min(rule_pos)


def test_topology_is_derived_and_overridable(env):
    assert env.fw.topology == "lan"
    env.cfg.engine.topology = "wan"
    assert env.fw.topology == "wan"
    env.fw.set_topology_override("lan")
    assert env.fw.topology == "lan"
    env.fw.set_topology_override("wan")
    assert env.fw.topology == "wan"
    env.fw.set_topology_override(None)
    assert env.fw.topology == "wan"  # back to cfg


def test_reconcile_converges_on_topology_change(env):
    _run(env.fw.reconcile())
    before = list(env.fake.rules)
    env.cfg.engine.topology = "wan"
    _run(env.fw.reconcile())
    after = list(env.fake.rules)
    assert after != before
    assert any('iifname "ppp0"' in r for r in after)
    assert not any('iifname "ppp0"' in r for r in before)


# ---------------------------------------------------------------------------
# 2. never breaks quota / accounting
# ---------------------------------------------------------------------------


def test_firewall_uses_its_own_table_only(env):
    _run(env.fw.reconcile())
    joined = " ".join(" ".join(c) for c in env.fake.calls)
    assert "quota_firewall" in joined
    for alien in ("quota_gateway", "quota_nat", "quota_arp_lock"):
        assert alien not in joined


def test_chains_priority_before_quota_engine():
    cmds = render_commands(_base_cfg(), "lan", LOCAL_NETS, BOX_IPS)
    chain_defs = " | ".join(c[-1] for c in cmds if c[0] == "add" and c[1] == "chain")
    # priority -100 => firewall verdicts land before the engine's priority-0
    # hooks, so denied traffic is never counted and quota blocks still apply.
    assert "hook input priority -100" in chain_defs
    assert "hook forward priority -100" in chain_defs


def test_lan_pass_through_always_accepted():
    for topology in ("lan", "wan"):
        joined = " | ".join(_rules(render_commands(
            _base_cfg(), topology, LOCAL_NETS, BOX_IPS)))
        for net in LOCAL_NETS:
            assert f"ip saddr {net} accept" in joined


# ---------------------------------------------------------------------------
# 3. safe-apply snapshot + watchdog auto-revert + last-good
# ---------------------------------------------------------------------------


def test_sanitize_refuses_lockout_deny_rules(env):
    clean = env.fw.sanitize(_base_cfg(rules=[
        {"name": "lock-dashboard", "chain": "input", "action": "deny",
         "src": "0.0.0.0/0", "dst": "0.0.0.0/0", "protocol": "tcp"},
        {"name": "cut-everyone", "chain": "forward", "action": "deny",
         "src": "192.168.2.0/24", "dst": "0.0.0.0/0", "protocol": ""},
        {"name": "torrent", "chain": "forward", "action": "deny",
         "src": "0.0.0.0/0", "dst": "0.0.0.0/0", "protocol": "tcp",
         "dst_port": 6881},
    ]))
    assert [r["name"] for r in clean["rules"]] == ["torrent"]
    warnings = clean.pop("_warnings", [])
    assert any("lock the dashboard" in w for w in warnings)
    assert any("cut the whole client subnet" in w for w in warnings)


def test_sanitize_drops_deny_cidr_over_protected_keeps_allow(env):
    clean = env.fw.sanitize(_base_cfg(
        allow_cidrs=["10.0.0.0/8", "garbage"],
        deny_cidrs=["203.0.113.0/24", "192.168.2.0/24"]))
    assert clean["allow_cidrs"] == ["10.0.0.0/8"]  # invalid dropped
    assert clean["deny_cidrs"] == ["203.0.113.0/24"]  # protected overlap refused
    assert any("protected IP" in w for w in clean.pop("_warnings", []))


def test_safe_apply_snapshots_and_persists(env):
    result = _run(env.fw.safe_apply(_base_cfg(rules=[
        {"name": "torrent", "chain": "forward", "action": "deny",
         "protocol": "tcp", "dst_port": 6881}])))
    assert result["ok"] and result["applied"]
    assert result["watchdog_seconds"] == 45
    assert _run(env.db.get_setting("firewall_config")) != ""
    assert _run(env.db.get_setting("firewall_last_good")) != ""
    snaps = list((env.fw._snapshot_dir).glob("fw-snapshot-*.json"))
    assert snaps
    payload = json.loads(snaps[0].read_text(encoding="utf-8"))
    assert payload["topology"] == "lan"
    assert any("rules applied" in e["message"] for e in env.fw.recent_log(20))


def test_watchdog_reverts_when_probe_fails(tmp_path):
    env = _make_env(tmp_path, probe=lambda: False)
    try:
        _run(env.fw.safe_apply(_base_cfg(watchdog_seconds=1)))
        _run(asyncio.sleep(1.3))  # pump the loop so the watchdog task fires
        messages = [e["message"] for e in env.fw.recent_log(20)]
        assert any("auto-reverting" in m for m in messages)
        assert env.fw._last_good is not None
    finally:
        _close(env)


def test_watchdog_no_revert_when_reachable(tmp_path):
    env = _make_env(tmp_path, probe=lambda: True)
    try:
        _run(env.fw.safe_apply(_base_cfg(watchdog_seconds=1)))
        _run(asyncio.sleep(1.3))
        messages = [e["message"] for e in env.fw.recent_log(20)]
        assert not any("auto-reverting" in m for m in messages)
    finally:
        _close(env)


def test_revert_last_good_restores_previous_config(env):
    _run(env.fw.safe_apply(_base_cfg(rules=[]), reason="first"))
    assert not any("fw_custom" in r for r in env.fake.rules)
    _run(env.fw.safe_apply(_base_cfg(rules=[
        {"name": "block-x", "chain": "forward", "action": "deny",
         "protocol": "tcp", "dst_port": 443}]), reason="second"))
    assert any("fw_custom_0" in r for r in env.fake.rules)
    _run(env.fw.revert_last_good())
    assert not any("fw_custom" in r for r in env.fake.rules), \
        "revert must re-apply the last-good ruleset"
    assert any("reverted to last-good" in e["message"]
               for e in env.fw.recent_log(20))


def test_apply_failure_degrades_gracefully(env):
    env.fake.reject_writes = True
    ok = _run(env.fw.safe_apply(_base_cfg()))
    assert not ok["ok"]
    assert not env.fw.available
    assert any("apply failed" in e["message"] for e in env.fw.recent_log(20))


# ---------------------------------------------------------------------------
# 4. bans / SYN-flood guard / brute-force hook / port-scan detection
# ---------------------------------------------------------------------------


def test_render_syn_flood_guard():
    cmds = render_commands(_base_cfg(syn_flood={"rate": 25, "burst": 50}),
                           "lan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(_rules(cmds))
    assert "limit rate 25/second burst 50 packets" in joined
    assert f"counter name {COUNTER_SYN_PASS} accept" in joined
    assert f"counter name {COUNTER_SYN_DROP} drop" in joined
    # SYN guard is placed AFTER the local accepts so LAN SYN is never limited
    assert joined.index("ip saddr 192.168.2.0/24 accept") < joined.index("== syn")


def test_ban_ip_adds_kernel_element_and_tracks(env):
    _run(env.fw.reconcile())
    ok = _run(env.fw.ban_ip("203.0.113.9", 1800, "manual"))
    assert ok
    assert env.fake.bans["203.0.113.9"] == 1800
    bans = env.fw.list_bans()
    assert bans[0]["ip"] == "203.0.113.9"
    assert bans[0]["reason"] == "manual"
    assert 0 < bans[0]["remaining"] <= 1800


def test_ban_refuses_box_ip_and_client_subnet(env):
    assert not _run(env.fw.ban_ip("192.168.2.1", 1800, "self"))      # box itself
    assert not _run(env.fw.ban_ip("192.168.2.50", 1800, "client"))   # client subnet
    assert not _run(env.fw.ban_ip("not-an-ip", 1800, "junk"))        # invalid
    assert env.fake.bans == {}


def test_unban_removes_kernel_element(env):
    _run(env.fw.reconcile())
    _run(env.fw.ban_ip("203.0.113.9", 1800, "manual"))
    assert _run(env.fw.unban_ip("203.0.113.9"))
    assert env.fake.bans == {}
    assert env.fw.list_bans() == []
    assert any("unbanned" in e["message"] for e in env.fw.recent_log(20))


def test_scan_detect_bans_threshold_source(env):
    _run(env.fw.reconcile())
    env.fake.scan_set = {"203.0.113.7": 250}
    drained = env.fw._drain()  # tick-level drain pushes the event to the log
    assert any("port-scan" in msg for _, msg in drained)
    assert env.fake.bans["203.0.113.7"]
    assert any("port-scan" in e["message"] for e in env.fw.recent_log(20))


def test_scan_detect_counts_deltas_only(env):
    _run(env.fw.reconcile())
    env.fake.scan_set = {"203.0.113.7": 250}
    env.fw.scan_detect_tick()
    assert env.fake.bans["203.0.113.7"]
    env.fake.bans.clear()  # simulate ban expiry in the kernel
    env.fw.scan_detect_tick()  # same absolute count -> delta 0 -> no new ban
    assert env.fake.bans == {}
    env.fake.scan_set = {"203.0.113.7": 500}
    env.fw.scan_detect_tick()  # delta 250 >= threshold -> banned again
    assert env.fake.bans["203.0.113.7"]


def test_counter_drains_emit_drop_events(env):
    _run(env.fw.reconcile())
    env.fake.counters[COUNTER_BAN_DROP] = 7
    events = env.fw.drain_counters()
    assert any("kernel ban dropped 7 packets" in e for e in events)
    assert env.fw.drain_counters() == []  # baseline updated -> nothing new


# ---------------------------------------------------------------------------
# 5. allow/deny CIDR lists (deny > allow) + ordered custom rules
# ---------------------------------------------------------------------------


def test_deny_before_allow_and_before_bans():
    cmds = render_commands(_base_cfg(allow_cidrs=["10.0.0.0/8"],
                                     deny_cidrs=["203.0.113.0/24"]),
                           "wan", LOCAL_NETS, BOX_IPS)
    for chain in (CHAIN_INPUT, CHAIN_FORWARD):
        chain_rules = [c[-1] for c in cmds
                       if c[0] == "add" and c[1] == "rule" and c[3] == chain]
        order = [i for i, r in enumerate(chain_rules)
                 if f"@{SET_DENY}" in r or f"@{SET_ALLOW}" in r or f"@{SET_BANS}" in r]
        deny_i, allow_i, bans_i = order
        assert deny_i < allow_i < bans_i, "deny > allow > bans ordering"
    assert any(c[1] == "element" and c[3] == SET_ALLOW for c in cmds)
    assert any(c[1] == "element" and c[3] == SET_DENY for c in cmds)


def test_allow_bypasses_wan_default_deny():
    cmds = render_commands(_base_cfg(allow_cidrs=["203.0.113.0/24"]),
                           "wan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(_rules(cmds))
    # @fw_allow accept sits before the WAN default-deny, so a whitelisted
    # source reaches the box / is forwarded even though NEW is otherwise denied
    assert joined.index(f"ip saddr @{SET_ALLOW} accept") < \
        joined.index('iifname "ppp0" ct state new')


def test_custom_rules_rendered_ordered_with_counters():
    cmds = render_commands(_base_cfg(rules=[
        {"name": "web-allow", "chain": "input", "action": "allow",
         "protocol": "tcp", "dst_port": 8080},
        {"name": "torrent", "chain": "forward", "action": "deny",
         "protocol": "tcp", "dst_port": 6881},
        {"name": "no-log", "chain": "forward", "action": "deny",
         "protocol": "udp", "dst_port": 9, "log": False},
    ]), "lan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(_rules(cmds))
    assert "tcp dport 8080 counter name fw_custom_0 accept" in joined
    assert "tcp dport 6881 counter name fw_custom_1 drop" in joined
    assert "meta l4proto udp udp dport 9" in joined  # log=False -> no counter
    assert "counter name fw_custom_2" not in joined


# ---------------------------------------------------------------------------
# 6. port forwards + DMZ (WAN-only)
# ---------------------------------------------------------------------------


def test_wan_renders_port_forward_and_dmz():
    cfg = _base_cfg(
        port_forwards=[{"name": "ssh", "protocol": "tcp", "source_port": 2222,
                        "target_ip": "192.168.2.10", "target_port": 22}],
        dmz="192.168.2.99")
    cmds = render_commands(cfg, "wan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(_rules(cmds))
    assert 'iifname "ppp0" meta l4proto tcp tcp dport 2222' in joined
    assert "dnat to 192.168.2.10:22" in joined
    assert 'iifname "ppp0" meta l4proto tcp ip daddr 192.168.2.10' in joined
    # DMZ dnat excludes the client nets (never re-dnat LAN traffic)
    assert "ip daddr != 192.168.1.0/24" in joined
    assert "ip daddr != 192.168.2.0/24" in joined
    assert "dnat to 192.168.2.99" in joined


def test_port_forward_ignored_in_lan_render():
    cfg = _base_cfg(
        port_forwards=[{"name": "ssh", "protocol": "tcp", "source_port": 2222,
                        "target_ip": "192.168.2.10", "target_port": 22}],
        dmz="192.168.2.99")
    cmds = render_commands(cfg, "lan", LOCAL_NETS, BOX_IPS)
    joined = " | ".join(" ".join(c) for c in cmds)
    assert CHAIN_DNAT not in joined
    assert "dnat" not in joined


def test_port_forward_api_409_in_lan_mode(tmp_path):
    env = _make_env(tmp_path, topology="lan")
    try:
        service = QuotaService(env.db, timezone=TZ)
        app = create_app(env.db, service, SnapshotHolder(), firewall=env.fw)
        with TestClient(app) as c:
            assert c.post("/api/login",
                          json={"password": "admin"}).status_code == 200
            body = _base_cfg(port_forwards=[
                {"name": "ssh", "protocol": "tcp", "source_port": 2222,
                 "target_ip": "192.168.2.10", "target_port": 22}])
            r = c.post("/api/firewall", json=body)
            assert r.status_code == 409
            assert "WAN-mode only" in r.json()["detail"]
            # WAN mode accepts the same payload
            env.cfg.engine.topology = "wan"
            r = c.post("/api/firewall", json=body)
            assert r.status_code == 200, r.text
    finally:
        _close(env)


# ---------------------------------------------------------------------------
# 7. logging: every drop/ban/apply surfaces a level + timestamp event
# ---------------------------------------------------------------------------


def test_events_have_level_and_timestamp(env):
    _run(env.fw.reconcile())
    _run(env.fw.ban_ip("203.0.113.9", 1800, "manual"))
    env.fake.counters[COUNTER_BAN_DROP] = 3
    env.fw._drain()  # counter deltas -> info events + DB persistence path
    events = env.fw.recent_log(20)
    assert events
    for e in events:
        assert e["level"] in ("info", "warn", "error")
        assert isinstance(e["ts"], float) and e["ts"] > 0
        assert e["message"]
    assert any(e["level"] == "warn" and "banned" in e["message"] for e in events)
    assert any(e["level"] == "info" and "dropped 3 packets" in e["message"]
               for e in events)
    rows = _run(env.db.list_events(limit=50))
    assert any("FW: banned 203.0.113.9" in r["message"] for r in rows)


# ---------------------------------------------------------------------------
# 8. reconcile convergence + performance (no rebuild when unchanged)
# ---------------------------------------------------------------------------


def test_reconcile_no_rebuild_when_unchanged(env):
    _run(env.fw.reconcile())
    assert env.fw._applied_sig == env.fw._last_sig
    writes = lambda: [c for c in env.fake.calls
                      if c[1] in ("add", "flush", "delete", "reset")]
    n_writes = len(writes())
    _run(env.fw.reconcile())
    assert len(writes()) == n_writes, "unchanged config must not reprogram"
    _run(env.fw.reconcile())  # a second no-op tick for good measure
    assert len(writes()) == n_writes


def test_reconcile_rebuilds_after_config_change(env):
    _run(env.fw.reconcile())
    n_writes = len([c for c in env.fake.calls
                    if c[1] in ("add", "flush", "delete")])
    _run(env.fw.persist_config(_base_cfg(deny_cidrs=["203.0.113.0/24"])))
    _run(env.fw.reconcile())
    assert any(c[1] == "add" and c[2] == "element" and c[4] == SET_DENY
               for c in env.fake.calls)
    assert len([c for c in env.fake.calls
                if c[1] in ("add", "flush", "delete")]) > n_writes


# ---------------------------------------------------------------------------
# 9. API surface: CRUD, revert, ban/unban, geo, auth, brute-force ban
# ---------------------------------------------------------------------------


def test_api_get_firewall_requires_auth(env):
    service = QuotaService(env.db, timezone=TZ)
    app = create_app(env.db, service, SnapshotHolder(), firewall=env.fw)
    with TestClient(app) as c:
        assert c.get("/api/firewall").status_code == 401


def test_api_firewall_crud_and_revert(env):
    service = QuotaService(env.db, timezone=TZ)
    app = create_app(env.db, service, SnapshotHolder(), firewall=env.fw)
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        r = c.get("/api/firewall")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True
        assert data["mode"] == "lan"
        assert "config" in data and "status" in data and "log" in data
        # apply config with a custom rule + a lockout rule (warning surfaced)
        r = c.post("/api/firewall", json=_base_cfg(rules=[
            {"name": "torrent", "chain": "forward", "action": "deny",
             "protocol": "tcp", "dst_port": 6881},
            {"name": "lock", "chain": "input", "action": "deny",
             "src": "0.0.0.0/0"}]))
        assert r.status_code == 200, r.text
        assert r.json()["warnings"], "lockout rule must surface a warning"
        cfg = c.get("/api/firewall").json()["config"]
        assert [x["name"] for x in cfg["rules"]] == ["torrent"]
        # revert restores last-good
        assert c.post("/api/firewall/revert").json()["applied"]
        # ban + unban via API
        r = c.post("/api/firewall/ban", json={"ip": "203.0.113.9",
                                              "seconds": 1800, "reason": "manual"})
        assert r.status_code == 200 and r.json()["ok"]
        assert env.fake.bans["203.0.113.9"] == 1800
        assert c.post("/api/firewall/unban",
                      json={"ip": "203.0.113.9"}).status_code == 200
        assert "203.0.113.9" not in env.fake.bans
        r = c.get("/api/firewall/log")
        assert r.status_code == 200 and isinstance(r.json()["log"], list)


def test_api_firewall_geo(env):
    service = QuotaService(env.db, timezone=TZ)
    app = create_app(env.db, service, SnapshotHolder(), firewall=env.fw)
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        r = c.post("/api/firewall/geo", json={"mapping": {"CN": ["1.0.1.0/24"]}})
        assert r.status_code == 200
        assert c.get("/api/firewall").json()["geo"] == {"CN": ["1.0.1.0/24"]}
        assert c.post("/api/firewall", json=_base_cfg(geo_block=False)).status_code == 200


def test_login_brute_force_triggers_firewall_ban(tmp_path):
    env = _make_env(tmp_path)
    try:
        _run(env.fw.persist_config(_base_cfg(
            brute_force={"threshold": 2, "ban_seconds": 900})))
        calls: list[tuple[str, int, str]] = []

        async def _spy(ip, seconds, reason):
            calls.append((ip, seconds, reason))
            return True

        env.fw.ban_ip = _spy  # type: ignore[method-assign]
        service = QuotaService(env.db, timezone=TZ)
        app = create_app(env.db, service, SnapshotHolder(), firewall=env.fw)
        with TestClient(app) as c:
            assert c.post("/api/login",
                          json={"password": "nope"}).status_code == 401
            assert calls == []
            assert c.post("/api/login",
                          json={"password": "nope"}).status_code == 401
            assert calls, "2nd failure must trip the brute-force kernel ban"
            assert calls[0] == ("testclient", 900, "login brute-force")
    finally:
        _close(env)


def test_api_without_firewall_degrades_gracefully(tmp_path):
    db = _db.Database(tmp_path / "api.db")
    _run(db.connect())
    try:
        service = QuotaService(db, timezone=TZ)
        app = create_app(db, service, SnapshotHolder(), firewall=None)
        with TestClient(app) as c:
            assert c.post("/api/login",
                          json={"password": "admin"}).status_code == 200
            assert c.get("/api/firewall").json()["available"] is False
            assert c.post("/api/firewall", json=_base_cfg()).status_code == 404
            assert c.post("/api/firewall/revert").status_code == 404
            assert c.post("/api/firewall/ban",
                          json={"ip": "1.2.3.4"}).status_code == 404
            assert c.post("/api/firewall/geo",
                          json={"mapping": {}}).status_code == 404
    finally:
        _run(db.close())