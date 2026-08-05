"""Tests for the electric-cut fallback wiring in run.py (pure config logic)."""

from __future__ import annotations

import pytest

from core import config as cfg_mod
from run import _build_pool, _fallback_reserved, _validate_fallback


def _dhcp_cfg(**kw) -> cfg_mod.DhcpConfig:
    base = dict(pool_start="192.168.1.100", pool_end="192.168.1.200")
    base.update(kw)
    return cfg_mod.DhcpConfig(**base)


def test_fallback_disabled_no_reserved():
    cfg = _dhcp_cfg(fallback_enabled=False)
    assert _fallback_reserved(cfg) == set()
    _validate_fallback(cfg)  # no error


def test_fallback_enabled_builds_reserved_set():
    cfg = _dhcp_cfg(fallback_enabled=True,
                    fallback_pool_start="192.168.1.201",
                    fallback_pool_end="192.168.1.205")
    assert _fallback_reserved(cfg) == {
        "192.168.1.201", "192.168.1.202", "192.168.1.203",
        "192.168.1.204", "192.168.1.205"}


def test_fallback_enabled_without_range_is_inactive():
    cfg = _dhcp_cfg(fallback_enabled=True)  # no start/end
    assert _fallback_reserved(cfg) == set()
    _validate_fallback(cfg)  # warns, does not raise


def test_fallback_overlapping_pool_is_fatal():
    cfg = _dhcp_cfg(fallback_enabled=True,
                    fallback_pool_start="192.168.1.180",  # inside our pool
                    fallback_pool_end="192.168.1.210")
    with pytest.raises(ValueError, match="overlaps"):
        _validate_fallback(cfg)


def test_pool_builder_uses_shared_helper():
    cfg = _dhcp_cfg(pool_start="192.168.1.100", pool_end="192.168.1.102")
    assert _build_pool(cfg) == ["192.168.1.100", "192.168.1.101", "192.168.1.102"]
    with pytest.raises(ValueError):
        _build_pool(_dhcp_cfg(pool_start="192.168.1.105", pool_end="192.168.1.100"))
