"""Tests for the nftables engine (Linux accounting + block), using a fake
``nft`` binary so no root or kernel features are required.

The fake records every invocation and simulates kernel counters + a blocked
set, so we can assert both the ruleset that gets programmed and the byte
deltas that ``flush()`` returns.
"""

from __future__ import annotations

import json

from core.config import Config
from quota.engine import SnapshotHolder
from quota.nftables import NftablesEngine, _counter_name


class FakeNft:
    """In-memory stand-in for ``nft``.

    ``__call__`` takes the full argv (``["nft", ...]``) like a subprocess would.
    Two tables are tracked independently — the inet ``quota_gateway`` table and
    the arp ``quota_arp_lock`` table the gateway-lock uses — so a ``flush table``
    of one never wipes the other. Sets are keyed by name, so the ``blocked`` and
    ``known_ips`` sets stay separate.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.counters: dict[str, int] = {}
        #: (table, rule-expr) in insertion order; ``rules`` exposes the exprs.
        self._rules: list[tuple[str, str]] = []
        #: table -> setname -> member IPs
        self._sets: dict[str, dict[str, set[str]]] = {
            "inet quota_gateway": {"blocked": set(), "known_ips": set()},
            "arp quota_arp_lock": {},
        }
        self.tables: set[str] = set()

    @property
    def rules(self) -> list[str]:
        return [expr for _, expr in self._rules]

    @property
    def blocked(self) -> set[str]:
        return self._sets["inet quota_gateway"]["blocked"]

    @property
    def known_ips(self) -> set[str]:
        return self._sets["inet quota_gateway"]["known_ips"]

    @staticmethod
    def _table(args: list[str]) -> str:
        # args[2] == "inet quota_gateway forward" / "arp quota_arp_lock input"
        return " ".join(args[2].split()[:2])

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[0] != "nft":
            return 1, f"unknown binary {argv[0]}"
        args = argv[1:]
        cmd = args[0]

        if cmd == "-j":  # nft -j list counters
            return self._list_counters()

        if cmd == "add" and args[1] == "table":
            if args[2] in self.tables:
                return 1, "File exists"
            self.tables.add(args[2])
            return 0, ""
        if cmd == "flush" and args[1] == "table":
            # Real nft: a table flush removes rules/sets/chains but NOT named
            # counter objects, which keep their byte totals. That is exactly the
            # restart-resurrection bug the engine guards against, so the fake
            # must preserve counters here too. Only the named table is flushed.
            table = args[2]
            self._rules = [(t, e) for t, e in self._rules if t != table]
            for s in self._sets.setdefault(table, {}):
                self._sets[table][s].clear()
            return 0, ""
        if cmd == "add" and args[1] in ("chain", "set", "rule"):
            self._rules.append((self._table(args), args[-1]))
            return 0, ""
        if cmd == "add" and args[1] == "counter":
            name = args[-1]
            if name in self.counters:
                return 1, "File exists"  # carried over from a previous process
            self.counters[name] = 0
            return 0, ""
        if cmd == "add" and args[1] == "element":
            # args[2] == "inet quota_gateway blocked", args[-1] == "{ ip }"
            name = args[2].split()[-1]
            target = self._sets[self._table(args)].setdefault(name, set())
            for ip in args[-1].strip("{}").split(","):
                target.add(ip.strip())
            return 0, ""
        if cmd == "flush" and args[1] == "set":
            name = args[2].split()[-1]
            self._sets[self._table(args)].setdefault(name, set()).clear()
            return 0, ""
        return 0, ""

    def _list_counters(self) -> tuple[int, str]:
        entries = [{"metainfo": {"version": "1.0.6"}}]
        for name, bytes_ in self.counters.items():
            entries.append({
                "counter": {"family": "inet", "table": "quota_gateway",
                            "name": name, "handle": 1,
                            "packets": 0, "bytes": bytes_},
            })
        return 0, json.dumps({"nftables": entries})


def _engine(fake: FakeNft) -> NftablesEngine:
    # Realistic Linux gateway: clients on 192.168.2.0/24, uplink LAN
    # 192.168.1.0/24 (the router's subnet). The two local subnets are what the
    # engine excludes from accounting so LAN traffic never consumes the bundle.
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    return NftablesEngine(cfg, SnapshotHolder(), run_command=fake)


def test_start_programs_base_ruleset():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    assert eng.available
    joined = " | ".join(fake.rules)
    assert "type filter hook forward priority 0; policy accept;" in joined
    # Block drops are LAN-aware: a quota-blocked device keeps local traffic.
    # (Local subnets are sorted, so uplink 192.168.1.0/24 comes first.)
    assert ("ip saddr @blocked ip daddr != 192.168.1.0/24 "
            "ip daddr != 192.168.2.0/24 drop") in joined
    assert ("ip daddr @blocked ip saddr != 192.168.1.0/24 "
            "ip saddr != 192.168.2.0/24 drop") in joined
    # table flushed + rebuilt from scratch
    assert [c for c in fake.calls if c[1] == "flush" and c[2] == "table"]


def test_update_state_installs_per_device_counters():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01",
                      "192.168.2.112": "aa:bb:cc:dd:ee:02"}, {})
    assert "q_up_192_168_2_111" in fake.counters
    assert "q_down_192_168_2_111" in fake.counters
    assert "q_up_192_168_2_112" in fake.counters
    # Counting rules carry the LAN exclusions so local traffic never consumes
    # the bundle: uploads exclude local daddrs, downloads exclude local saddr.
    assert ("ip saddr 192.168.2.111 ip daddr != 192.168.1.0/24 "
            "ip daddr != 192.168.2.0/24 counter name q_up_192_168_2_111") \
        in fake.rules
    assert ("ip daddr 192.168.2.112 ip saddr != 192.168.1.0/24 "
            "ip saddr != 192.168.2.0/24 counter name q_down_192_168_2_112") \
        in fake.rules


def test_update_state_is_add_only():
    """A departed IP's rules stay in place; no deletes are issued."""
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    n_calls = len(fake.calls)
    eng.update_state({}, {})  # device gone
    assert len(fake.calls) == n_calls + 1  # only the blocked-set flush
    # counters still exist but are not surfaced (flush filters by ip_to_mac)
    assert "q_up_192_168_2_111" in fake.counters


def test_blocked_mac_adds_its_ip_to_set():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": True})
    assert "192.168.2.111" in fake.blocked


def test_blocked_mac_without_ip_is_not_dropped():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"},
                     {"zz:zz:zz:zz:zz:zz": True})  # blocked mac, no lease
    assert fake.blocked == set()


def test_unblock_removes_ip_from_set():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": True})
    assert "192.168.2.111" in fake.blocked
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": False})
    assert "192.168.2.111" not in fake.blocked


def test_flush_returns_delta_since_last_flush():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    fake.counters["q_up_192_168_2_111"] = 1000
    fake.counters["q_down_192_168_2_111"] = 500

    snap = eng.flush()
    assert snap.by_ip["192.168.2.111"].up == 1000
    assert snap.by_ip["192.168.2.111"].down == 500

    # no new traffic -> empty delta
    assert eng.flush().by_ip == {}

    # counter keeps accumulating in the kernel; flush reports only the growth
    fake.counters["q_up_192_168_2_111"] = 1500
    fake.counters["q_down_192_168_2_111"] = 800
    snap2 = eng.flush()
    assert snap2.by_ip["192.168.2.111"].up == 500
    assert snap2.by_ip["192.168.2.111"].down == 300


def test_flush_only_reports_known_ips():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    # stale counter from an IP no longer in ip_to_mac
    fake.counters["q_up_192_168_9_9"] = 99999
    snap = eng.flush()
    assert "192.168.9.9" not in snap.by_ip
    assert "q_up_192_168_9_9" not in snap.by_ip


def test_snapshot_carries_maps_and_blocked():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": True})
    fake.counters["q_up_192_168_2_111"] = 42
    snap = eng.flush()
    assert snap.ip_to_mac == {"192.168.2.111": "aa:bb:cc:dd:ee:01"}
    assert snap.blocked == {"aa:bb:cc:dd:ee:01": True}
    # counters_for aggregates live bytes by MAC
    live = snap.counters_for("aa:bb:cc:dd:ee:01")
    assert live.up == 42


def test_missing_binary_degrades_gracefully():
    def missing(argv):  # noqa: ARG001 - the real nft binary is absent
        return 127, "nft: not found"
    eng = _engine(missing)
    eng.start()
    assert not eng.available
    # flush() returns an empty-but-valid snapshot, never raises
    snap = eng.flush()
    assert snap.by_ip == {}


def test_counter_name():
    assert _counter_name("192.168.2.111", "up") == "q_up_192_168_2_111"
    assert _counter_name("10.0.0.7", "down") == "q_down_10_0_0_7"


def test_start_resets_surviving_counters():
    """start() zeroes named counters left behind by a previous process."""
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    reset = [c for c in fake.calls if c[1] == "reset" and c[2] == "counters"]
    assert reset, "start() should issue `nft reset counters`"
    # table-scoped: never touches counters in other tables
    assert reset[0][3] == "inet quota_gateway"


def test_restart_does_not_resurrect_carried_over_counters():
    """A counter that survives a table flush must not be drained as new usage.

    Regression: on restart the kernel keeps the named counters (with their
    cumulative totals) but the engine's in-memory delta baseline is lost, so
    the first flush after boot used to re-add the whole pre-restart total to
    usage_daily — a consumed-and-reset quota came back after every restart.
    """
    fake = FakeNft()
    eng = _engine(fake)
    # A previous process left its counters in the kernel; `flush table` does
    # not delete them, only the in-memory baseline is gone. (FakeNft ignores
    # `reset counters`, so this exercises the reseed fallback as well.)
    fake.counters = {"q_up_192_168_2_111": 6_000_000_000,
                     "q_down_192_168_2_111": 3_000_000_000}
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})

    # First flush after restart: the carried-over total is the baseline, so
    # nothing is reported as new usage.
    assert eng.flush().by_ip == {}

    # Traffic that actually flows after the restart is reported normally.
    fake.counters["q_up_192_168_2_111"] += 1024
    fake.counters["q_down_192_168_2_111"] += 2048
    snap = eng.flush()
    assert snap.by_ip["192.168.2.111"].up == 1024
    assert snap.by_ip["192.168.2.111"].down == 2048


def test_fresh_counter_has_no_carried_over_baseline():
    """A brand-new engine counts a device's traffic from its first byte."""
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    assert eng.flush().by_ip == {}  # counter created at zero -> no delta yet
    fake.counters["q_up_192_168_2_111"] = 777
    assert eng.flush().by_ip["192.168.2.111"].up == 777


# --------------------------------------------------------------------------- #
# LOCAL traffic is never counted against the metered bundle
# --------------------------------------------------------------------------- #

def test_local_networks_are_derived_from_dhcp_when_unset():
    """Empty engine subnets derive from the dhcp block (client + uplink)."""
    fake = FakeNft()
    cfg = Config()
    # Config() dhcp defaults: gateway 192.168.1.2, router 192.168.1.1, /24.
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    # Both hosts are on the same /24, so the two subnets dedupe to one.
    assert eng._local_networks == ["192.168.1.0/24"]


def test_local_networks_use_explicit_config_over_derivation():
    """Explicit engine subnets win and are applied to the rules."""
    fake = FakeNft()
    eng = _engine(fake)  # client 192.168.2.0/24, uplink 192.168.1.0/24
    assert eng._local_networks == ["192.168.1.0/24", "192.168.2.0/24"]
    eng.start()
    joined = " | ".join(fake.rules)
    assert "ip daddr != 192.168.1.0/24" in joined
    assert "ip daddr != 192.168.2.0/24" in joined


def test_invalid_explicit_subnet_falls_back_to_derivation():
    """A typo'd CIDR must not brick accounting — derive instead."""
    fake = FakeNft()
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/33"     # invalid prefix
    cfg.engine.uplink_subnet = "not-a-subnet"
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = "192.168.1.1"
    cfg.dhcp.subnet = "255.255.255.0"
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    assert eng._local_networks == ["192.168.1.0/24", "192.168.2.0/24"]
    eng.start()
    assert eng.available  # rules still program fine


def test_no_resolvable_subnets_keeps_plain_rules():
    """No local subnet information => count every forwarded packet (old form)."""
    fake = FakeNft()
    cfg = Config()
    cfg.engine.client_subnet = ""
    cfg.engine.uplink_subnet = ""
    cfg.dhcp.gateway_ip = ""       # nothing to derive from
    cfg.dhcp.router_ip = ""
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    assert eng._local_networks == []
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    joined = " | ".join(fake.rules)
    assert "ip saddr @blocked drop" in joined
    assert "ip daddr @blocked drop" in joined
    assert "ip saddr 192.168.2.111 counter name q_up_192_168_2_111" in joined
    assert "ip daddr 192.168.2.111 counter name q_down_192_168_2_111" in joined


# --------------------------------------------------------------------------- #
# WAN mode (engine.topology = "wan"): the box terminates the PPPoE line itself
# --------------------------------------------------------------------------- #

def _wan_engine(fake: FakeNft) -> NftablesEngine:
    """A WAN-mode gateway: the client subnet + the router-admin alias the box
    keeps are local; no router on the segment to lock against."""
    cfg = Config()
    cfg.engine.topology = "wan"
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"   # the router-admin subnet — LOCAL
    cfg.engine.gateway_arp_lock = True            # must be forced OFF in WAN mode
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = ""                       # no router in WAN mode
    cfg.dhcp.uplink_ip = "192.168.1.110"          # LAN snapshot: the kept alias
    cfg.dhcp.lan_cidr = 24
    return NftablesEngine(cfg, SnapshotHolder(), run_command=fake)


def test_wan_mode_keeps_router_admin_subnet_local():
    """In WAN mode the box keeps the uplink IP as a router-admin alias, so the
    uplink subnet IS local: router-admin traffic never consumes quota and a
    blocked device keeps local (router admin) access — matching LAN mode."""
    fake = FakeNft()
    eng = _wan_engine(fake)
    assert eng._local_networks == ["192.168.1.0/24", "192.168.2.0/24"]
    eng.start()
    joined = " | ".join(fake.rules)
    assert "ip daddr != 192.168.1.0/24" in joined
    assert "ip daddr != 192.168.2.0/24" in joined


def test_wan_mode_derives_client_from_gateway_when_unset():
    """No explicit client_subnet in WAN mode => derive it from dhcp.gateway_ip.
    With no uplink snapshot either, only the client subnet is local."""
    fake = FakeNft()
    cfg = Config()
    cfg.engine.topology = "wan"
    cfg.engine.client_subnet = ""
    cfg.engine.uplink_subnet = ""
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = ""
    cfg.dhcp.uplink_ip = ""
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    assert eng._local_networks == ["192.168.2.0/24"]


def test_wan_mode_derives_router_admin_subnet_from_lan_snapshot():
    """No explicit uplink_subnet, but the LAN snapshot (uplink_ip + lan_cidr)
    survives in WAN mode => the router-admin subnet derives from it."""
    fake = FakeNft()
    cfg = Config()
    cfg.engine.topology = "wan"
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = ""
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = ""               # the ACTIVE router key is erased in WAN
    cfg.dhcp.uplink_ip = "192.168.1.110"  # ...but the LAN snapshot keeps it
    cfg.dhcp.lan_cidr = 24
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    assert eng._local_networks == ["192.168.1.0/24", "192.168.2.0/24"]


def test_wan_mode_forces_arp_lock_off():
    """WAN mode has no router on the client segment to lock against — the ARP
    gateway-lock is forced off even when gateway_arp_lock is set. Only the
    lock's deny rules are gone: quota enforcement (@blocked drops) stays."""
    fake = FakeNft()
    eng = _wan_engine(fake)
    assert eng._arp_lock is False
    eng.start()
    assert "quota_arp_lock" not in fake.tables
    joined = " | ".join(fake.rules)
    assert "known_ips" not in joined       # no known_ips set or deny rule
    assert "@known_ips" not in joined      # no `ip saddr != @known_ips drop`
    assert "@blocked" in joined            # quota block drops remain active


# --------------------------------------------------------------------------- #
# ARP gateway-lock (engine.gateway_arp_lock): static-IP bypassers are denied
# --------------------------------------------------------------------------- #

def _locked_engine(fake: FakeNft) -> NftablesEngine:
    """A client-subnet gateway with the ARP lock enabled (router = 192.168.1.1)."""
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    cfg.engine.gateway_arp_lock = True
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = "192.168.1.1"
    return NftablesEngine(cfg, SnapshotHolder(), run_command=fake)


def test_arp_lock_programs_capture_and_deny():
    fake = FakeNft()
    eng = _locked_engine(fake)
    eng.start()
    joined = " | ".join(fake.rules)
    # the deny rule is the FIRST forward-chain rule, before the blocked drops:
    # an intercepted bypasser's packets must be dropped before any counter sees
    # them (and before the blocked-set logic that would let them through).
    deny = "ip saddr 192.168.2.0/24 ip saddr != @known_ips drop"
    assert deny in joined
    assert joined.index(deny) < joined.index("ip saddr @blocked")
    # the arp-family chain drops the router's ARP replies to client-subnet
    # hosts, so no client learns the router's real MAC (the box claims it).
    assert ("arp operation 2 arp saddr ip 192.168.1.1 "
            "arp daddr ip 192.168.2.0/24 drop") in joined
    assert "arp quota_arp_lock" in fake.tables


def test_arp_lock_known_ips_syncs_leased_clients():
    """Every managed (DHCP-leased) client IP lands in the known_ips allowlist."""
    fake = FakeNft()
    eng = _locked_engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    assert fake.known_ips == {"192.168.2.111"}


def test_arp_lock_known_ips_only_rebuilt_on_change():
    """A same-set membership must not re-flush known_ips every tick (a rebuild
    briefly drops every managed device between the flush and the last re-add)."""
    fake = FakeNft()
    eng = _locked_engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    n_flush = sum(1 for c in fake.calls
                  if c[1:3] == ["flush", "set"] and c[3].endswith("known_ips"))
    assert n_flush == 1
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    assert sum(1 for c in fake.calls
               if c[1:3] == ["flush", "set"] and c[3].endswith("known_ips")) \
        == n_flush


def test_arp_lock_known_ips_tracks_new_lease():
    """A new lease is allowed by the deny rule on the very next tick."""
    fake = FakeNft()
    eng = _locked_engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01",
                      "192.168.2.112": "aa:bb:cc:dd:ee:02"}, {})
    assert fake.known_ips == {"192.168.2.111", "192.168.2.112"}


def test_arp_lock_off_programs_nothing():
    """The lock is opt-in: with it off the engine programs only the base ruleset."""
    fake = FakeNft()
    eng = _engine(fake)  # gateway_arp_lock defaults False
    eng.start()
    joined = " | ".join(fake.rules)
    assert "known_ips" not in joined
    assert "arp operation" not in joined
    assert not any("arp" == t.split()[0] for t in fake.tables)


def test_arp_lock_unresolved_degrades_to_noop():
    """Lock requested but the router IP / client subnet don't resolve -> the
    lock never engages (the scanner still reports the rogue)."""
    fake = FakeNft()
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    cfg.engine.gateway_arp_lock = True
    cfg.dhcp.router_ip = ""   # no router -> nothing to claim
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    eng.start()
    joined = " | ".join(fake.rules)
    assert "known_ips" not in joined
    assert "arp operation" not in joined


def test_arp_lock_does_not_break_counters():
    """The arp table is separate from the accounting table: counter reads and
    device rules are unaffected by the lock."""
    fake = FakeNft()
    eng = _locked_engine(fake)
    eng.start()
    eng.update_state({"192.168.2.111": "aa:bb:cc:dd:ee:01"}, {})
    fake.counters["q_up_192_168_2_111"] = 100
    snap = eng.flush()
    assert snap.by_ip["192.168.2.111"].up == 100
