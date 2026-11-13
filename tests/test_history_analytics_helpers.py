"""Characterization tests for pure helpers in quota.history_analytics."""
from quota import history_analytics as ha


def test_bps_basic():
    assert ha._bps(1000, 10) == 800
    assert ha._bps(0, 10) == 0
    assert ha._bps(12345, 3.5) == int((12345 * 8) / 3.5)


def test_bps_falsy_byte_count_returns_zero():
    assert ha._bps(False, 5) == 0
    # negative byte counts are truthy and flow through the formula
    assert ha._bps(-8, 1) == -64


def test_bps_window_fractional():
    assert ha._bps(1024, 0.5) == int((1024 * 8) / 0.5)
