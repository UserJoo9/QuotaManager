"""Unit tests for the packet engine's resilience + filter scoping (no pydivert).

Covers the WinError 1232 regression: a WinDivertSend failure (e.g. re-injecting
a packet the OS deems unroutable) must NEVER kill the engine thread. We also
verify the WinDivert filter is scoped to the DHCP pool so unroutable broadcasts
and the PC's own traffic are never diverted in the first place.
"""

from __future__ import annotations

from core import config as cfg_mod
from quota.engine import PacketEngine, SnapshotHolder


def _engine(pool_start: str = "192.168.1.100", pool_end: str = "192.168.1.200"):
    cfg = cfg_mod.Config()
    cfg.dhcp.pool_start = pool_start
    cfg.dhcp.pool_end = pool_end
    return PacketEngine(cfg, SnapshotHolder())


class _Packet:
    """Minimal stand-in for a pydivert packet (network-layer fields only)."""

    def __init__(self, src: str, dst: str, raw: bytes = b"\x45" + b"\x00" * 59):
        self.ipv4 = _Ipv4(src, dst)
        self.raw = raw


class _Ipv4:
    def __init__(self, src: str, dst: str):
        self.src_addr = src
        self.dst_addr = dst


class _Win:
    """Fake WinDivert handle whose ``send`` always fails with WinError 1232."""

    def __init__(self, err: OSError | None = None):
        self.sent = 0
        self._err = err or OSError(1232, "The network location cannot be reached")

    def send(self, packet) -> None:
        self.sent += 1
        raise self._err


def test_build_filter_scopes_to_pool():
    eng = _engine("192.168.1.111", "192.168.1.200")
    f = eng._build_filter()
    assert f.startswith("ip and (")
    assert "ip.SrcAddr >= 192.168.1.111" in f
    assert "ip.SrcAddr <= 192.168.1.200" in f
    assert "ip.DstAddr >= 192.168.1.111" in f
    assert f != "ip"


def test_build_filter_falls_back_to_all_ipv4_without_pool():
    cfg = cfg_mod.Config()
    cfg.dhcp.pool_start = ""
    cfg.dhcp.pool_end = ""
    eng = PacketEngine(cfg, SnapshotHolder())
    assert eng._build_filter() == "ip"


def test_send_failure_never_raises_and_counts_direction():
    """WinError 1232 on re-injection must not crash _handle_packet."""
    eng = _engine()
    eng.update_state({"192.168.1.150": "aa:bb:cc:dd:ee:ff"}, {})

    w = _Win()
    # A client -> internet packet whose re-injection fails (WinError 1232).
    eng._handle_packet(_Packet("192.168.1.150", "8.8.8.8"), w)
    assert w.sent == 1, "send attempted once, then swallowed the OSError"

    snap = eng.flush()
    assert snap.by_ip["192.168.1.150"].up >= len(
        b"\x45" + b"\x00" * 59), "packet was still counted before the failed send"


def test_pass_through_send_failure_survives():
    """A non-client packet (e.g. a DHCP broadcast) whose re-inject fails must
    also be swallowed — this is the exact WinError 1232 crash we fixed."""
    eng = _engine()
    w = _Win()
    # src 0.0.0.0 / dst 255.255.255.255 (broadcast) — not in the pool map.
    eng._handle_packet(_Packet("0.0.0.0", "255.255.255.255"), w)
    assert w.sent == 1
    # no exception -> thread would keep running


def test_recv_failure_does_not_raise_loop():
    """_handle_packet with an ipv4-less packet passes through safely."""
    eng = _engine()
    w = _Win()

    class NoIp:
        raw = b"\x00" * 20

    eng._handle_packet(NoIp(), w)
    assert w.sent == 1
