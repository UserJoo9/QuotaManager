"""Regression tests for the client-NIC (LAN_IF) selection and dnsmasq binding.

Background (v0.5.6 upgrade on a single-NIC WAN box, Debian 12 arm64, NIC lan0):

1. In WAN mode the default route is ppp0 (type 512), so setup_gateway_kali.sh
   fell back to scanning /sys/class/net alphabetically. The shaping device ifb0
   reports type=1 and carrier=1 like a real NIC and sorts before lan0, so it
   was picked as LAN_IF: dnsmasq, the firewall and shaping were bound to ifb0
   and no client got a DHCP lease.
2. post_install.sh meant to hand the existing NIC to the setup script, but its
   `^interface:` match hit `dhcp.interface: ''` first and never passed it on.
3. dnsmasq used `bind-interfaces`, which only binds the addresses present at
   start. When the client gateway IP is added a moment later (startup_health
   re-adds it), DNS on the gateway IP stays dead until dnsmasq is restarted.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "scripts" / "setup_gateway_kali.sh"
POST = REPO / "scripts" / "post_install.sh"
TOPO = REPO / "scripts" / "topology.sh"


def _detect_block() -> str:
    """The `if [ -z "$LAN_IF" ]; then ... fi` auto-detect block of the setup script."""
    text = SETUP.read_text()
    m = re.search(r'^if \[ -z "\$LAN_IF" \]; then\n.*?^fi\n', text, re.S | re.M)
    assert m, "LAN_IF auto-detect block not found in setup_gateway_kali.sh"
    return m.group(0)


def _post_install_awk() -> str:
    """The awk program post_install.sh uses to read shaping.interface."""
    text = POST.read_text()
    m = re.search(r"_lan=\"\$\(awk '(.*?)' \"\$CONF_DIR/config\.yaml\"", text, re.S)
    assert m, "shaping.interface awk reader not found in post_install.sh"
    return m.group(1)


def _fake_sysfs(root: Path, nics: dict[str, dict]) -> None:
    """Build class/net + devices/virtual/net trees under `root`."""
    for name, spec in nics.items():
        d = root / "class" / "net" / name
        d.mkdir(parents=True)
        (d / "type").write_text(f"{spec.get('type', 1)}\n")
        (d / "carrier").write_text(f"{spec.get('carrier', 1)}\n")
        if spec.get("wireless"):
            (d / "wireless").mkdir()
        if spec.get("virtual"):
            (root / "devices" / "virtual" / "net" / name).mkdir(parents=True)


def _run_detect(tmp_path: Path, nics: dict[str, dict], default_if: str) -> str:
    sysfs = tmp_path / "sys"
    _fake_sysfs(sysfs, nics)
    block = (_detect_block()
             .replace("/sys/", f"{sysfs}/")
             .replace("$(ip route | awk '/default/ {print $5}')", default_if)
             .replace("$(ls /sys/class/net", f"$(ls {sysfs}/class/net"))
    script = f'LAN_IF=""\n{block}echo "$LAN_IF"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
needs_awk = pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")

WAN_BOX = {
    "ifb0": {"type": 1, "carrier": 1, "virtual": True},   # shaping device
    "lan0": {"type": 1, "carrier": 1},                    # the real NIC
    "lo": {"type": 772, "carrier": 1, "virtual": True},
    "ppp0": {"type": 512, "carrier": 1, "virtual": True},
}


@needs_bash
def test_wan_mode_does_not_pick_ifb0(tmp_path):
    """Default route on ppp0 -> sysfs fallback must skip ifb0 and pick lan0."""
    assert _run_detect(tmp_path, WAN_BOX, "ppp0") == "lan0"


@needs_bash
def test_virtual_ethernet_like_devices_are_skipped(tmp_path):
    nics = {
        "br-lan": {"type": 1, "carrier": 1, "virtual": True},
        "docker0": {"type": 1, "carrier": 1, "virtual": True},
        "eth0": {"type": 1, "carrier": 1},
        "veth1a2b": {"type": 1, "carrier": 1, "virtual": True},
    }
    assert _run_detect(tmp_path, nics, "") == "eth0"


@needs_bash
def test_lan_mode_default_route_nic_still_wins(tmp_path):
    nics = {"eth0": {"type": 1, "carrier": 1}, "eth1": {"type": 1, "carrier": 1}}
    assert _run_detect(tmp_path, nics, "eth1") == "eth1"


@needs_bash
def test_wifi_and_uncabled_nics_still_skipped(tmp_path):
    nics = {
        "eth0": {"type": 1, "carrier": 0},
        "eth1": {"type": 1, "carrier": 1},
        "wlan0": {"type": 1, "carrier": 1, "wireless": True},
    }
    assert _run_detect(tmp_path, nics, "wlan0") == "eth1"


CONFIGS = {
    "flow": "dhcp:\n  interface: ''\nshaping: {enabled: true, interface: lan0, "
            "client_subnet: 192.168.2.0/24,\n  ifb: ifb0}\n",
    "block": "dhcp:\n  interface: ''\nshaping:\n  enabled: true\n  interface: eth1\n"
             "  ifb: ifb0\nweb: {port: 8080}\n",
    "wrapped-flow": "dhcp:\n  interface: ''\nshaping: {enabled: true,\n"
                    "  interface: enp3s0, ifb: ifb0}\n",
    "no-shaping": "dhcp:\n  interface: ''\nweb:\n  interface: nope\n",
}
EXPECTED = {"flow": "lan0", "block": "eth1", "wrapped-flow": "enp3s0", "no-shaping": ""}


@needs_awk
@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_post_install_reads_shaping_interface(tmp_path, name):
    """Never `dhcp.interface: ''` — always shaping.interface, in any YAML style."""
    conf = tmp_path / "config.yaml"
    conf.write_text(CONFIGS[name])
    out = subprocess.run(["awk", _post_install_awk(), str(conf)],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == EXPECTED[name]


def test_post_install_never_hands_back_a_shaping_or_tunnel_device():
    text = POST.read_text()
    assert 'export LAN_IF="$_lan"' in text
    guard = text.index('case "$_lan" in ifb*|ppp*')
    assert guard < text.index('export LAN_IF="$_lan"'), \
        "a stale ifb0 in config.yaml must not be handed back as LAN_IF"


@needs_bash
def test_default_route_on_a_bridge_bond_or_vlan_is_trusted(tmp_path):
    """A box that lives on br0/bond0/eth0.10 keeps it: skipping every virtual
    NIC picked the enslaved port (no address on it) or nothing at all."""
    nics = {"br0": {"type": 1, "carrier": 1, "virtual": True},
            "enp3s0": {"type": 1, "carrier": 1},
            "ifb0": {"type": 1, "carrier": 1, "virtual": True}}
    assert _run_detect(tmp_path / "a", nics, "br0") == "br0"
    nics = {"bond0": {"type": 1, "carrier": 1, "virtual": True},
            "eth0.10": {"type": 1, "carrier": 1, "virtual": True}}
    assert _run_detect(tmp_path / "b", nics, "bond0") == "bond0"


@needs_bash
def test_default_route_on_a_shaping_or_container_device_is_never_trusted(tmp_path):
    nics = {"docker0": {"type": 1, "carrier": 1, "virtual": True},
            "eth0": {"type": 1, "carrier": 1},
            "ifb0": {"type": 1, "carrier": 1, "virtual": True}}
    for route in ("ifb0", "docker0"):
        assert _run_detect(tmp_path / route, nics, route) == "eth0"


@pytest.mark.parametrize("script", [SETUP, TOPO])
def test_dnsmasq_main_conf_keeps_bind_interfaces(script):
    """bind-dynamic cannot coexist with a bind-interfaces line in another
    /etc/dnsmasq.d file (dnsmasq refuses to start), so the main conf keeps
    bind-interfaces; the late-address case is handled by startup_health
    restarting dnsmasq after it re-adds the gateway address."""
    text = script.read_text()
    assert not re.search(r"^bind-dynamic$", text, re.M), script.name
    assert len(re.findall(r"^bind-interfaces$", text, re.M)) == 2, script.name
