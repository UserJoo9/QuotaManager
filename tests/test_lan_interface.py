import json

import pytest

from quota.lan_interface import (
    LanRequest,
    LanResolutionError,
    LanSelection,
    resolve_lan,
    validate_lan,
)


class FakeNetwork:
    def __init__(self, root, links, addresses=None, nm=(), default_route=""):
        self.root = root
        self.links = links
        self.addresses = addresses or {}
        self.nm = nm
        self.default_route = default_route
        for name, (ifindex, carrier, operstate, physical) in links.items():
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "ifindex").write_text(f"{ifindex}\n")
            (directory / "type").write_text("1\n")
            (directory / "carrier").write_text(f"{carrier}\n")
            (directory / "operstate").write_text(f"{operstate}\n")
            if physical:
                (directory / "device").mkdir()

    def run(self, argv):
        if argv[:4] == ["ip", "-j", "-4", "addr"]:
            rows = []
            for name, ips in self.addresses.items():
                rows.append({"ifname": name, "addr_info": [
                    {"family": "inet", "local": ip} for ip in ips
                ]})
            return 0, json.dumps(rows)
        if argv[:5] == ["ip", "-j", "route", "show", "default"]:
            return 0, json.dumps([{"dev": self.default_route}]
                                 if self.default_route else [])
        if argv[:4] == ["nmcli", "-t", "--escape", "no"]:
            return 0, "\n".join(":".join(row) for row in self.nm)
        return 1, "unsupported command"


def _link(ifindex, carrier=1, operstate="up", physical=True):
    return ifindex, carrier, operstate, physical


def _request(**kwargs):
    return LanRequest(
        gateway_ip=kwargs.pop("gateway_ip", "192.168.2.1"),
        uplink_ip=kwargs.pop("uplink_ip", "192.168.1.110"),
        last_applied=kwargs.pop("last_applied", ""),
        requested=kwargs.pop("requested", ""),
        wan_interface=kwargs.pop("wan_interface", ""),
    )


def test_stale_down_gateway_loses(tmp_path):
    network = FakeNetwork(tmp_path, {
        "eno1": _link(2, carrier=0, operstate="down"),
        "enp10s0f1": _link(3),
    }, {"eno1": ["192.168.2.1"], "enp10s0f1": ["192.168.2.1"]})
    assert resolve_lan(_request(), run=network.run, sysfs_net=tmp_path).name == "enp10s0f1"


def test_no_carrier_loses(tmp_path):
    network = FakeNetwork(tmp_path, {
        "eno1": _link(2), "enp10s0f1": _link(3, carrier=0, operstate="down"),
    }, {"enp10s0f1": ["192.168.2.1"]})
    assert resolve_lan(_request(), run=network.run, sysfs_net=tmp_path).name == "eno1"


def test_unique_usb_nic_without_ipv4(tmp_path):
    network = FakeNetwork(tmp_path, {"enx001122334455": _link(5)})
    assert resolve_lan(_request(), run=network.run, sysfs_net=tmp_path).name == "enx001122334455"


@pytest.mark.parametrize(("links", "addresses", "nm", "default", "expected"), [
    ({"eno1": _link(2), "enp2s0": _link(3)},
     {"eno1": ["192.168.2.1"]}, (), "", "eno1"),
    ({"eno1": _link(2), "enp2s0": _link(3)}, {},
     (("enp2s0", "ethernet", "connected"),), "", "enp2s0"),
    ({"eno1": _link(2), "enp2s0": _link(3)},
     {"enp2s0": ["192.168.1.110"]}, (), "", "enp2s0"),
    ({"eno1": _link(2), "enp2s0": _link(3)}, {}, (), "enp2s0", "enp2s0"),
    ({"eno1": _link(2), "enp2s0": _link(3)},
     {"enp2s0": ["192.168.1.110"]},
     (("eno1", "ethernet", "connected"),
      ("enp2s0", "ethernet", "connected")), "", "enp2s0"),
    ({"eno1": _link(2), "enp2s0": _link(3)}, {},
     (("eno1", "ethernet", "connected"),
      ("enp2s0", "ethernet", "connected")), "enp2s0", "enp2s0"),
])
def test_gateway_then_nm_then_uplink(tmp_path, links, addresses, nm, default, expected):
    network = FakeNetwork(tmp_path, links, addresses, nm, default)
    assert resolve_lan(_request(), run=network.run, sysfs_net=tmp_path).name == expected


def test_tie_is_actionable(tmp_path):
    network = FakeNetwork(tmp_path, {"eno1": _link(2), "enp2s0": _link(3)})
    with pytest.raises(LanResolutionError, match="multiple"):
        resolve_lan(_request(), run=network.run, sysfs_net=tmp_path)


def test_no_candidate(tmp_path):
    network = FakeNetwork(tmp_path, {"eno1": _link(2, carrier=0)})
    with pytest.raises(LanResolutionError, match="no active"):
        resolve_lan(_request(), run=network.run, sysfs_net=tmp_path)


def test_explicit_bridge_and_dedicated_wan(tmp_path):
    network = FakeNetwork(tmp_path, {
        "br-lan": _link(2, physical=False), "enp2s0": _link(3),
        "enp3s0": _link(4),
    })
    (tmp_path / "br-lan" / "bridge").mkdir()
    request = _request(requested="br-lan", wan_interface="enp3s0")
    assert resolve_lan(request, run=network.run, sysfs_net=tmp_path).name == "br-lan"

    automatic = _request(wan_interface="enp3s0")
    with pytest.raises(LanResolutionError, match="multiple"):
        resolve_lan(automatic, run=network.run, sysfs_net=tmp_path)


def test_validate_rejects_same_name_new_ifindex(tmp_path):
    network = FakeNetwork(tmp_path, {"enp2s0": _link(9)})
    with pytest.raises(LanResolutionError, match="identity changed"):
        validate_lan(LanSelection("enp2s0", 8, "gateway"), gateway_cidr=None,
                     run=network.run, sysfs_net=tmp_path)
