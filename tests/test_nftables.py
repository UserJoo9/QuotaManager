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
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.counters: dict[str, int] = {}
        self.blocked: set[str] = set()
        self.rules: list[str] = []
        self.table_exists = False

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[0] != "nft":
            return 1, f"unknown binary {argv[0]}"
        args = argv[1:]
        cmd = args[0]

        if cmd == "-j":  # nft -j list counters
            return self._list_counters()

        if cmd == "add" and args[1] == "table":
            if self.table_exists:
                return 1, "File exists"
            self.table_exists = True
            return 0, ""
        if cmd == "flush" and args[1] == "table":
            self.counters.clear()
            self.blocked.clear()
            self.rules.clear()
            return 0, ""
        if cmd == "add" and args[1] in ("chain", "set", "rule"):
            self.rules.append(args[-1])
            return 0, ""
        if cmd == "add" and args[1] == "counter":
            self.counters.setdefault(args[-1], 0)
            return 0, ""
        if cmd == "add" and args[1] == "element":
            # args[-1] == "{ 192.168.1.111 }"
            for ip in args[-1].strip("{}").split(","):
                self.blocked.add(ip.strip())
            return 0, ""
        if cmd == "flush" and args[1] == "set":
            self.blocked.clear()
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
    cfg = Config()  # engine.backend default "auto", table default quota_gateway
    return NftablesEngine(cfg, SnapshotHolder(), run_command=fake)


def test_start_programs_base_ruleset():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    assert eng.available
    joined = " | ".join(fake.rules)
    assert "type filter hook forward priority 0; policy accept;" in joined
    assert "ip saddr @blocked drop" in joined
    assert "ip daddr @blocked drop" in joined
    # table flushed + rebuilt from scratch
    assert [c for c in fake.calls if c[1] == "flush" and c[2] == "table"]


def test_update_state_installs_per_device_counters():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01",
                      "192.168.1.112": "aa:bb:cc:dd:ee:02"}, {})
    assert "q_up_192_168_1_111" in fake.counters
    assert "q_down_192_168_1_111" in fake.counters
    assert "q_up_192_168_1_112" in fake.counters
    assert "ip saddr 192.168.1.111 counter name q_up_192_168_1_111" in fake.rules
    assert "ip daddr 192.168.1.112 counter name q_down_192_168_1_112" in fake.rules


def test_update_state_is_add_only():
    """A departed IP's rules stay in place; no deletes are issued."""
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"}, {})
    n_calls = len(fake.calls)
    eng.update_state({}, {})  # device gone
    assert len(fake.calls) == n_calls + 1  # only the blocked-set flush
    # counters still exist but are not surfaced (flush filters by ip_to_mac)
    assert "q_up_192_168_1_111" in fake.counters


def test_blocked_mac_adds_its_ip_to_set():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": True})
    assert "192.168.1.111" in fake.blocked


def test_blocked_mac_without_ip_is_not_dropped():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"},
                     {"zz:zz:zz:zz:zz:zz": True})  # blocked mac, no lease
    assert fake.blocked == set()


def test_unblock_removes_ip_from_set():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": True})
    assert "192.168.1.111" in fake.blocked
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": False})
    assert "192.168.1.111" not in fake.blocked


def test_flush_returns_delta_since_last_flush():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"}, {})
    fake.counters["q_up_192_168_1_111"] = 1000
    fake.counters["q_down_192_168_1_111"] = 500

    snap = eng.flush()
    assert snap.by_ip["192.168.1.111"].up == 1000
    assert snap.by_ip["192.168.1.111"].down == 500

    # no new traffic -> empty delta
    assert eng.flush().by_ip == {}

    # counter keeps accumulating in the kernel; flush reports only the growth
    fake.counters["q_up_192_168_1_111"] = 1500
    fake.counters["q_down_192_168_1_111"] = 800
    snap2 = eng.flush()
    assert snap2.by_ip["192.168.1.111"].up == 500
    assert snap2.by_ip["192.168.1.111"].down == 300


def test_flush_only_reports_known_ips():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"}, {})
    # stale counter from an IP no longer in ip_to_mac
    fake.counters["q_up_192_168_9_9"] = 99999
    snap = eng.flush()
    assert "192.168.9.9" not in snap.by_ip
    assert "q_up_192_168_9_9" not in snap.by_ip


def test_snapshot_carries_maps_and_blocked():
    fake = FakeNft()
    eng = _engine(fake)
    eng.start()
    eng.update_state({"192.168.1.111": "aa:bb:cc:dd:ee:01"},
                     {"aa:bb:cc:dd:ee:01": True})
    fake.counters["q_up_192_168_1_111"] = 42
    snap = eng.flush()
    assert snap.ip_to_mac == {"192.168.1.111": "aa:bb:cc:dd:ee:01"}
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
    assert _counter_name("192.168.1.111", "up") == "q_up_192_168_1_111"
    assert _counter_name("10.0.0.7", "down") == "q_down_10_0_0_7"
