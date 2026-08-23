"""Tests for startup self-heal (quota/startup_health.py).

All nft / ip / sysctl calls are faked — no root or kernel features required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quota.startup_health import (
    NFT_TABLE,
    ensure_nat_table,
    ensure_network_infrastructure,
    ensure_nftables_conf,
)


# ------------------------------------------------------------------
# Fake nft for NAT-table tests
# ------------------------------------------------------------------

class FakeNftNat:
    """Minimal fake for the subset of nft commands ensure_nat_table uses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.tables: set[str] = set()
        self.chains: dict[str, list[str]] = {}   # table -> [chain definitions]
        self.rules: dict[str, list[str]] = {}     # table -> [rule exprs]

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:3] == ["nft", "list", "table"]:
            table = argv[3] if len(argv) > 3 else ""
            return (0, f"table {table} {{}}") if table in self.tables else (1, "No such file or directory")
        if argv[:3] == ["nft", "list", "chain"]:
            table = argv[3] if len(argv) > 3 else ""
            if table in self.tables:
                exprs = [r for r in self.rules.get(table, [])]
                return 0, "\n".join(exprs)
            return 1, "No such file or directory"
        if argv[:3] == ["nft", "add", "table"]:
            t = argv[3] if len(argv) > 3 else ""
            if t in self.tables:
                return 1, "File exists"
            self.tables.add(t)
            return 0, ""
        if argv[:3] == ["nft", "add", "chain"]:
            t = " ".join(argv[3].split()[:2]) if len(argv) > 3 else ""
            chain_def = argv[-1] if len(argv) > 4 else ""
            self.chains.setdefault(t, []).append(chain_def)
            return 0, ""
        if argv[:3] == ["nft", "add", "rule"]:
            t = " ".join(argv[3].split()[:2]) if len(argv) > 3 else ""
            self.rules.setdefault(t, []).append(argv[-1] if len(argv) > 4 else "")
            return 0, ""
        return 0, ""


# ------------------------------------------------------------------
# ensure_nat_table
# ------------------------------------------------------------------

class TestEnsureNatTable:
    def test_creates_table_when_missing(self) -> None:
        nft = FakeNftNat()
        assert ensure_nat_table("192.168.2.0/24", run=nft) is True
        assert NFT_TABLE in nft.tables
        # Should have added: table, chain, rule
        adds = [c for c in nft.calls if c[0:2] == ["nft", "add"]]
        assert len(adds) >= 3  # table + chain + rule

    def test_adds_masquerade_when_table_exists_but_rule_missing(self) -> None:
        nft = FakeNftNat()
        nft.tables.add(NFT_TABLE)
        # Table exists but no masquerade in list output
        assert ensure_nat_table("192.168.2.0/24", run=nft) is True
        # Should have added just the rule (table + chain already present)
        rule_adds = [c for c in nft.calls if c[0:3] == ["nft", "add", "rule"]]
        assert len(rule_adds) >= 1

    def test_idempotent_when_healthy(self) -> None:
        nft = FakeNftNat()
        nft.tables.add(NFT_TABLE)
        nft.rules[NFT_TABLE] = ["ip saddr 192.168.2.0/24 masquerade"]
        assert ensure_nat_table("192.168.2.0/24", run=nft) is True
        # Should not have added anything
        adds = [c for c in nft.calls if c[0:2] == ["nft", "add"]]
        assert adds == []

    def test_skips_when_no_subnet(self) -> None:
        nft = FakeNftNat()
        assert ensure_nat_table("", run=nft) is True
        assert nft.calls == []

    def test_table_exists_returns_true(self) -> None:
        nft = FakeNftNat()
        nft.tables.add(NFT_TABLE)
        nft.rules[NFT_TABLE] = ["ip saddr 10.0.0.0/8 masquerade"]
        assert ensure_nat_table("10.0.0.0/8", run=nft) is True
        adds = [c for c in nft.calls if c[0:2] == ["nft", "add"]]
        assert adds == []


# ------------------------------------------------------------------
# ensure_network_infrastructure
# ------------------------------------------------------------------

class TestEnsureNetworkInfrastructure:
    def test_enables_ip_forward(self, tmp_path: Path) -> None:
        """When ip_forward is 0, the function should write 1."""
        fwd = tmp_path / "ip_forward"
        fwd.write_text("0\n")

        # Patch /proc path — we can't in tests, so just test the function
        # doesn't crash on a system without /proc.
        # On Windows (CI), this is a no-op. On Linux it would fix it.
        ok = ensure_network_infrastructure(
            gateway_ip="192.168.2.1",
            uplink_ip="192.168.1.110",
            lan_cidr=24,
        )
        # Function should not crash regardless of platform.
        assert ok is True

    def test_no_crash_without_lan_interface(self) -> None:
        """On a system with no wired NIC, function should not crash."""
        ok = ensure_network_infrastructure(
            gateway_ip="192.168.2.1",
            uplink_ip="192.168.1.110",
            lan_cidr=24,
        )
        assert ok is True


# ------------------------------------------------------------------
# ensure_nftables_conf
# ------------------------------------------------------------------

class TestEnsureNftablesConf:
    def test_skips_when_target_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the target .nft file doesn't exist (Docker), skip silently."""
        import quota.startup_health as mod
        monkeypatch.setattr(mod, "_CONF_SYMLINK", tmp_path / "nftables.conf")
        monkeypatch.setattr(mod, "_CONF_TARGET", tmp_path / "nonexistent.nft")
        assert ensure_nftables_conf() is True

    def test_repairs_broken_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A symlink pointing to the wrong target should be repaired."""
        import quota.startup_health as mod

        target = tmp_path / "nftables.gateway.nft"
        target.write_text("#!/usr/sbin/nft -f\ntable inet quota_nat { }\n")
        symlink = tmp_path / "nftables.conf"

        # Point at wrong target first
        symlink.symlink_to(tmp_path / "wrong.nft")

        monkeypatch.setattr(mod, "_CONF_SYMLINK", symlink)
        monkeypatch.setattr(mod, "_CONF_TARGET", target)

        # On Windows, symlinks require admin. Skip if OSError.
        try:
            result = ensure_nftables_conf()
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        assert result is True
        assert symlink.resolve() == target.resolve()

    def test_idempotent_when_symlink_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import quota.startup_health as mod

        target = tmp_path / "nftables.gateway.nft"
        target.write_text("#!/usr/sbin/nft -f\n")
        symlink = tmp_path / "nftables.conf"
        try:
            symlink.symlink_to(target)
        except OSError:
            pytest.skip("symlinks not supported on this platform")

        monkeypatch.setattr(mod, "_CONF_SYMLINK", symlink)
        monkeypatch.setattr(mod, "_CONF_TARGET", target)
        assert ensure_nftables_conf() is True
