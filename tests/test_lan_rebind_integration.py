"""Exercise one LAN move through the production reconciliation coordinator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

import run as gateway_module
from quota.dnsmasq_binding import DnsmasqPaths, reconcile_dnsmasq
from quota.lan_interface import LanSelection, resolve_lan as real_resolve_lan


class FakeHost:
    """Fake live IP output and dnsmasq sockets while using real file updates."""

    def __init__(self, root: Path) -> None:
        self.conf_dir = root / "dnsmasq.d"
        self.conf_dir.mkdir()
        self.target = self.conf_dir / "quota-gateway.conf"
        self.target.write_text("interface=eno1\n", encoding="utf-8")
        self.backups = root / "backups"
        self.socket_iface = "eno1"
        self.calls: list[list[str]] = []

    @property
    def paths(self) -> DnsmasqPaths:
        return DnsmasqPaths(self.target, self.conf_dir, self.backups)

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv == ["ip", "-j", "-4", "addr", "show"]:
            # The old, down NIC still has the stale client gateway address.
            return 0, json.dumps([{
                "ifname": "eno1",
                "addr_info": [{"local": "192.168.2.1", "prefixlen": 24}],
            }])
        if argv == ["systemctl", "is-active", "--quiet", "dnsmasq"]:
            return 0, "active"
        if argv == ["ss", "-H", "-lunp"]:
            return 0, (
                f'UNCONN 0 0 0.0.0.0%{self.socket_iface}:67 0.0.0.0:* '
                'users:(("dnsmasq",pid=12,fd=3))\n'
            )
        if argv == ["dnsmasq", "--test"]:
            return 0, "syntax check OK"
        if argv == ["systemctl", "restart", "dnsmasq"]:
            for line in self.target.read_text(encoding="utf-8").splitlines():
                if line.startswith("interface="):
                    self.socket_iface = line.split("=", 1)[1]
            return 0, ""
        return 0, ""


class FakeShaper:
    def __init__(self) -> None:
        self.iface = "eno1"
        self.rebound: list[LanSelection] = []

    def rebind(self, selection: LanSelection) -> bool:
        self.rebound.append(selection)
        self.iface = selection.name
        return True


def _link(root: Path, name: str, ifindex: int, *, carrier: int,
          state: str) -> None:
    path = root / name
    path.mkdir()
    (path / "type").write_text("1\n", encoding="ascii")
    (path / "ifindex").write_text(f"{ifindex}\n", encoding="ascii")
    (path / "carrier").write_text(f"{carrier}\n", encoding="ascii")
    (path / "operstate").write_text(f"{state}\n", encoding="ascii")


def test_nic_move_reconciles_config_dnsmasq_and_shaper(tmp_path, monkeypatch):
    sysfs = tmp_path / "sys-class-net"
    sysfs.mkdir()
    _link(sysfs, "eno1", 2, carrier=0, state="down")
    _link(sysfs, "enp10s0f1", 7, carrier=1, state="up")

    host = FakeHost(tmp_path)
    selection_calls = []

    def resolve(request, *, run):
        selection = real_resolve_lan(request, run=run, sysfs_net=sysfs)
        selection_calls.append(selection)
        return selection

    monkeypatch.setattr("quota.lan_interface.resolve_lan", resolve)
    monkeypatch.setattr(gateway_module, "_default_run_command", host)

    infra_calls = []

    def ensure_infrastructure(**kwargs):
        infra_calls.append(kwargs["lan_selection"])
        change = reconcile_dnsmasq(
            kwargs["lan_selection"], replacement=None, ipv6_fragment=None,
            expect_ipv6=False, force_restart=False,
            paths=host.paths, run=host,
        )
        return change.binding.healthy

    monkeypatch.setattr(
        "quota.startup_health.ensure_network_infrastructure",
        ensure_infrastructure,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "dhcp": {"gateway_ip": "192.168.2.1", "uplink_ip": "192.168.1.110",
                 "lan_cidr": 24, "subnet": "255.255.255.0",
                 "router_ip": "192.168.1.1", "interface": "eno1",
                 "ipv6_enable": False},
        "shaping": {"interface": "eno1"},
        "engine": {"topology": "lan"},
    }), encoding="utf-8")

    class FakeDatabase:
        async def get_setting(self, key, default=""):
            assert key == "wan_if"
            return default

    gateway = gateway_module.Gateway.__new__(gateway_module.Gateway)
    gateway.database = FakeDatabase()
    gateway.cfg = SimpleNamespace(
        dhcp=SimpleNamespace(
            gateway_ip="192.168.2.1", uplink_ip="192.168.1.110",
            lan_cidr=24, subnet="255.255.255.0", ipv6_enable=False,
            router_ip="192.168.1.1", interface="eno1",
        ),
        shaping=SimpleNamespace(interface="eno1"),
        engine=SimpleNamespace(topology="lan"),
    )
    gateway.config_path = config_path
    gateway._lan_selection = None
    gateway._wan_interface = ""
    gateway._lan_binding_status = {"state": "unknown"}
    gateway._last_lan_reconcile = 0.0
    gateway._lan_lock = asyncio.Lock()
    gateway._shaping_lock = asyncio.Lock()
    gateway.shaper = FakeShaper()

    status = asyncio.run(gateway._reconcile_lan_bindings("manual"))

    expected = "enp10s0f1"
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert selection_calls == [LanSelection(expected, 7, "only-active-wired-interface")]
    assert infra_calls == [selection_calls[0]]
    assert "interface=enp10s0f1" in host.target.read_text(encoding="utf-8")
    assert host.socket_iface == expected
    assert gateway.shaper.iface == expected
    assert gateway.shaper.rebound == [selection_calls[0]]
    assert persisted["shaping"]["interface"] == expected
    assert persisted["dhcp"]["interface"] == expected
    assert status["state"] == "healthy"
    assert status["active"] == status["dhcp_v4"] == status["shaper"] == expected
