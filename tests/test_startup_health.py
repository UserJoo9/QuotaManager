"""Tests for startup self-heal (quota/startup_health.py).

All nft / ip / sysctl calls are faked — no root or kernel features required.
"""

from __future__ import annotations

import tempfile
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

class FakeIp:
    """In-memory ``ip`` stand-in: records argv, scripts select responses.

    Script keys match by LONGEST PREFIX (an exact key wins) — so
    ``("ip", "addr", "add")`` scripts every address-add regardless of its
    cidr/dev tail, while ``("ip", "-o", "-4", "addr", "show", "dev",
    "eth0")`` still beats the shorter bare-show key.
    """

    def __init__(self, script: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.script = {tuple(k): v for k, v in (script or {}).items()}

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        key = tuple(argv)
        if key in self.script:
            return self.script[key]
        best: tuple[str, ...] | None = None
        for k in self.script:
            if len(k) <= len(key) and key[:len(k)] == k:
                if best is None or len(k) > len(best):
                    best = k
        return self.script[best] if best else (0, "")

    def adds(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["ip", "addr", "add"]]


def make_net(*ifaces: tuple[str, str, str]) -> Path:
    """Fake /sys/class/net: entries of (name, link-type, carrier)."""
    root = Path(tempfile.mkdtemp(prefix="qm-sysnet-"))
    for name, link_type, carrier in ifaces:
        d = root / name
        d.mkdir()
        (d / "type").write_text(link_type, encoding="utf-8")
        if carrier:
            (d / "carrier").write_text(carrier, encoding="utf-8")
    return root


ADDR_SHOW = ("ip", "-o", "-4", "addr", "show")
ADDR_SHOW_DEV = ("ip", "-o", "-4", "addr", "show", "dev", "eth0")


def _fwd_on(tmp_path: Path) -> Path:
    """A healthy ip_forward file (already 1) — never touches the real /proc."""
    fwd = tmp_path / "ip_forward"
    fwd.write_text("1\n", encoding="utf-8")
    return fwd


class TestEnsureNetworkInfrastructure:
    def test_enables_ip_forward_when_disabled(self, tmp_path: Path) -> None:
        """A 0 in the ip_forward file is rewritten to 1."""
        fwd = tmp_path / "ip_forward"
        fwd.write_text("0\n", encoding="utf-8")
        ok = ensure_network_infrastructure(
            run=FakeIp(), proc_ipforward=fwd, sysfs_net=make_net())
        assert ok is True
        assert fwd.read_text(encoding="utf-8").strip() == "1"

    def test_ip_forward_already_on_is_untouched(self, tmp_path: Path) -> None:
        fwd = tmp_path / "ip_forward"
        fwd.write_text("1\n", encoding="utf-8")
        assert ensure_network_infrastructure(
            run=FakeIp(), proc_ipforward=fwd, sysfs_net=make_net()) is True
        assert fwd.read_text(encoding="utf-8") == "1\n"

    def test_missing_proc_file_skips_silently(self, tmp_path: Path) -> None:
        """No ip_forward file (non-Linux / container) is not a failure."""
        ok = ensure_network_infrastructure(
            run=FakeIp(),
            proc_ipforward=tmp_path / "nonexistent",
            sysfs_net=make_net(),
        )
        assert ok is True

    def test_readds_missing_gateway_and_uplink(self, tmp_path: Path) -> None:
        """The wired NIC lost its static addresses — both are re-added."""
        other = "2: eth0    inet 10.9.9.5/24 brd 10.9.9.255 scope global eth0\n"
        fake = FakeIp({
            ADDR_SHOW: (0, other),
            ADDR_SHOW_DEV: (0, other),
        })
        ok = ensure_network_infrastructure(
            gateway_ip="192.168.2.1",
            uplink_ip="192.168.1.110",
            lan_cidr=24,
            run=fake,
            proc_ipforward=_fwd_on(tmp_path),
            sysfs_net=make_net(("eth0", "1", "1")),
        )
        assert ok is True
        added = [c[3] for c in fake.adds()]
        assert added == ["192.168.2.1/24", "192.168.1.110/24"]

    def test_healthy_system_makes_no_changes(self, tmp_path: Path) -> None:
        """Both addresses present — nothing is re-added (idempotent)."""
        out = ("2: eth0    inet 192.168.1.110/24 brd 192.168.1.255 scope global eth0\n"
               "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 scope global eth0\n")
        fake = FakeIp({ADDR_SHOW: (0, out), ADDR_SHOW_DEV: (0, out)})
        ok = ensure_network_infrastructure(
            gateway_ip="192.168.2.1",
            uplink_ip="192.168.1.110",
            lan_cidr=24,
            run=fake,
            proc_ipforward=_fwd_on(tmp_path),
            sysfs_net=make_net(("eth0", "1", "1")),
        )
        assert ok is True
        assert fake.adds() == []

    def test_addr_add_failure_returns_false(self, tmp_path: Path) -> None:
        """A failed address add (no root) is reported via False, never raised."""
        other = "2: eth0    inet 10.9.9.5/24 brd 10.9.9.255 scope global eth0\n"
        fake = FakeIp({
            ADDR_SHOW: (0, other),
            ADDR_SHOW_DEV: (0, other),
            ("ip", "addr", "add"): (1, "RTNETLINK answers: Operation not permitted"),
        })
        ok = ensure_network_infrastructure(
            gateway_ip="192.168.2.1",
            uplink_ip="192.168.1.110",
            lan_cidr=24,
            run=fake,
            proc_ipforward=_fwd_on(tmp_path),
            sysfs_net=make_net(("eth0", "1", "1")),
        )
        assert ok is False
        assert len(fake.adds()) == 2

    def test_no_wired_interface_makes_no_changes(self, tmp_path: Path) -> None:
        """WiFi-only / no-carrier boxes skip the address check gracefully."""
        fake = FakeIp()
        ok = ensure_network_infrastructure(
            run=fake,
            proc_ipforward=_fwd_on(tmp_path),
            sysfs_net=make_net(("wlan0", "1", ""),   # no carrier file value
                               ("lo", "772", "1")),
        )
        assert ok is True
        assert fake.adds() == []


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
