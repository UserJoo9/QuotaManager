"""Tests for the VPN-share policy routing (quota/vpnshare.py), using a fake
``ip`` runner + a fake /sys/class/net so no root or kernel features are
required.

The fake records every argv and scripts the few reads (``ip rule show``,
``ip -o -4 addr show``) — we assert the command sequence that programs the
kernel, not kernel behavior (mirrors FakeTc in test_shaping.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.config import Config
from quota.vpnshare import (STATE_ERROR, STATE_NO_INTERFACE, STATE_OFF,
                            STATE_ON, VpnShareManager)


def make_cfg(client_subnet: str = "192.168.2.0/24",
             uplink_subnet: str = "192.168.1.0/24",
             tunnel_iface: str = "") -> Config:
    """A Config whose vpn_share block is fully specified (no auto-detect)."""
    cfg = Config()
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.subnet = "255.255.255.0"
    cfg.dhcp.router_ip = "192.168.1.1"
    cfg.engine.client_subnet = client_subnet
    cfg.engine.uplink_subnet = uplink_subnet
    cfg.vpn_share.interface = tunnel_iface
    return cfg


class FakeIp:
    """In-memory stand-in for ``ip``.

    Default: everything succeeds. Scriptable responses key the FULL argv
    (tuple) -> (returncode, output); a bare unknown argv returns ``(1, "")``.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.script: dict[tuple[str, ...], tuple[int, str]] = {}

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return self.script.get(tuple(argv), (0, ""))

    def has(self, *argv: str) -> bool:
        return any(a[:len(argv)] == list(argv) for a in self.calls)

    def count(self, prefix: str, *tail: str) -> int:
        n = 0
        for argv in self.calls:
            if len(argv) >= len(tail) + 1 and argv[0] == prefix:
                if argv[1:1 + len(tail)] == list(tail):
                    n += 1
        return n

    # scripted helpers ------------------------------------------------------

    def rule_show(self, present: bool) -> None:
        out = ("local:\n"
               "1000:\tfrom 192.168.2.0/24 lookup 200\n" if present else
               "local:\n")
        self.script[("ip", "rule", "show")] = (0, out)

    def addr_shows(self, lines: str) -> None:
        self.script[("ip", "-o", "-4", "addr", "show")] = (0, lines)


def make_sysfs(*type_pairs: tuple[str, str]) -> Path:
    """A fake sysfs root: a ``net`` dir whose entries carry ``type`` files.
    Returns the ``net`` dir — the path the manager scans for interfaces."""
    dummy = Path(tempfile.mkdtemp(prefix="qmsysfs-"))
    net = dummy / "net"
    net.mkdir(parents=True, exist_ok=True)
    for name, link_type in type_pairs:
        d = net / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "type").write_text(link_type, encoding="utf-8")
    return net


def mgr(cfg: Config | None = None, fake: FakeIp | None = None,
        sysfs_root: Path | None = None) -> tuple[VpnShareManager, FakeIp]:
    fake = fake or FakeIp()
    m = VpnShareManager(cfg or make_cfg(), run_command=fake,
                        sysfs_root=sysfs_root or make_sysfs())
    return m, fake


def tunnel_mgr(*tunnels: str) -> tuple[VpnShareManager, FakeIp]:
    """A manager whose sysfs carries the given TUN devices (ready for
    direct ``apply`` calls, which refuse to route into a missing device).
    Each tunnel carries an IPv4 address for convenience, though a live
    addressless UP tunnel would also be accepted."""
    m, fake = mgr(sysfs_root=make_sysfs(*((t, "65534") for t in tunnels)))
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    for i, t in enumerate(tunnels):
        fake.script[("ip", "-o", "-4", "addr", "show", "dev", t)] = \
            (0, f"{i + 3}: {t}    inet 10.8.0.{i + 2}/24 scope global {t}\n")
    return m, fake


# ---------------------------------------------------------------------------
# interface detection (sysfs ARPHRD_NONE) + peer/lan resolution
# ---------------------------------------------------------------------------

def test_detect_only_arphrd_none():
    root = make_sysfs(("eth0", "1"), ("lo", "772"), ("ppp0", "512"),
                      ("utun4", "65534"), ("wg0", "65534"))
    m, fake = mgr(sysfs_root=root)
    # ppp0 (512) and ethernet (1) are NOT tunnels; tun/wg are
    assert set(m.detect_interfaces()) == {"utun4", "wg0"}


def test_detect_missing_sysfs_is_empty():
    m, fake = mgr(sysfs_root=Path(tempfile.mkdtemp()))
    assert m.detect_interfaces() == []


def test_detect_prefers_ipv4_carrying_tunnel():
    root = make_sysfs(("utun4", "65534"), ("wg0", "65534"))
    fake = FakeIp()
    # wg0 carries an IPv4 address, utun4 is bare -> wg0 wins despite the
    # tun-prefix ordering
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "utun4")] = (0, "")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "wg0")] = \
        (0, "3: wg0    inet 10.8.0.2/24 scope global wg0\n")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == ["wg0", "utun4"]


def test_detect_ranks_xray_tun_like_classic_tunnels():
    # xray's kernel tun is named "xray_tun" (not tun0) — it must rank as a
    # first-class tunnel, not the generic tail group, so a real xray TUN wins
    # over a leftover junk ARPHRD_NONE device.
    root = make_sysfs(("xray_tun", "65534"), ("evice", "65534"))
    fake = FakeIp()
    for dev in ("xray_tun", "evice"):
        fake.script[("ip", "-o", "-4", "addr", "show", "dev", dev)] = (0, "")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == ["xray_tun", "evice"]


# ---------------------------------------------------------------------------
# ip-link fallback (sysfs incomplete / missing)
# ---------------------------------------------------------------------------

_IP_LINK_OUTPUT_MULTI = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
    "    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast\n"
    "    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff\n"
    "3: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500\n"
    "    link/none\n"
    "4: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420\n"
    "    link/none\n"
)


def test_ip_link_fallback_finds_tun0():
    """When sysfs yields zero candidates, _detect_interfaces_ip parses the
    ip -o -d link show output and returns interfaces with link/none."""
    root = make_sysfs()                       # empty sysfs → no candidates
    fake = FakeIp()
    fake.script[("ip", "-o", "-d", "link", "show")] = (0, _IP_LINK_OUTPUT_MULTI)
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    found = m.detect_interfaces()
    assert "tun0" in found and "wg0" in found
    assert "lo" not in found and "eth0" not in found


def test_ip_link_fallback_finds_wg0():
    """The fallback returns wg0 even when only WireGuard is present."""
    root = make_sysfs()
    fake = FakeIp()
    fake.script[("ip", "-o", "-d", "link", "show")] = (0,
        "1: lo: <LOOPBACK,UP> mtu 65536\n"
        "    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
        "10: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420\n"
        "    link/none\n"
    )
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == ["wg0"]


def test_ip_link_skipped_when_sysfs_works():
    """When sysfs DOES expose ARPHRD_NONE candidates, ip -o -d is never
    called (sysfs is faster and chroot-safe)."""
    root = make_sysfs(("tun0", "65534"))
    fake = FakeIp()
    fake.script[("ip", "-o", "-d", "link", "show")] = (
        "3: tun0: <POINTOPOINT,UP> mtu 1500\n"
        "    link/none\n"
    )
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == ["tun0"]
    assert not fake.has("ip", "-o", "-d", "link", "show")


def test_ip_link_fallback_empty_returns_empty():
    """When ip -o -d link show returns no link/none, detect returns []."""
    root = make_sysfs()
    fake = FakeIp()
    fake.script[("ip", "-o", "-d", "link", "show")] = (0,
        "1: lo: <LOOPBACK,UP> mtu 65536\n"
        "    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
    )
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == []


def test_ip_link_fallback_ip_unavailable():
    """When ip -o -d link show fails (code != 0), fallback yields []."""
    root = make_sysfs()
    fake = FakeIp()
    fake.script[("ip", "-o", "-d", "link", "show")] = (1, "")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == []


def test_ip_link_fallback_excludes_loopback():
    """Loopback and ethernet never carry link/none."""
    root = make_sysfs()
    fake = FakeIp()
    fake.script[("ip", "-o", "-d", "link", "show")] = (0,
        "1: lo: <LOOPBACK,UP> mtu 65536\n"
        "    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
        "2: eth0: <BROADCAST,MULTICAST,UP> mtu 1500\n"
        "    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff\n"
    )
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m.detect_interfaces() == []


# ---------------------------------------------------------------------------
# _iface_exists: sysfs primary + ip fallback
# ---------------------------------------------------------------------------

def test_iface_exists_sysfs_primary():
    """Sysfs directory is the fast path — no subprocess."""
    root = make_sysfs(("tun0", "65534"))
    fake = FakeIp()
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m._iface_exists("tun0")
    assert not fake.has("ip", "link", "show", "dev", "tun0")


def test_iface_exists_ip_fallback():
    """When sysfs is incomplete, _iface_exists falls back to ip link show dev
    so a pinned tunnel discovered via the ip-link path is not treated as gone."""
    root = make_sysfs()                       # no tun0 in sysfs
    fake = FakeIp()
    fake.script[("ip", "link", "show", "dev", "tun0")] = (0,
        "3: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500 "
        "qdisc pfifo_fast state UP\n"
    )
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert m._iface_exists("tun0")
    assert fake.has("ip", "link", "show", "dev", "tun0")


def test_iface_exists_ip_fallback_not_found():
    """When both sysfs and ip link show dev fail, _iface_exists returns False."""
    root = make_sysfs()
    fake = FakeIp()
    fake.script[("ip", "link", "show", "dev", "tun0")] = (1, "")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    assert not m._iface_exists("tun0")


def test_peer_ip_only_from_peer_field():
    m, fake = mgr()
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "tun0")] = \
        (0, "7: tun0    inet 10.8.0.2 peer 10.8.0.1/32 scope global tun0\n")
    assert m.peer_ip("tun0") == "10.8.0.1"
    # cached: a second call must not re-run ip
    before = len(fake.calls)
    assert m.peer_ip("tun0") == "10.8.0.1"
    assert len(fake.calls) == before


def test_peer_ip_empty_without_peer():
    m, fake = mgr()
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "utun4")] = \
        (0, "8: utun4    inet 10.9.0.2/32 scope global utun4\n")
    assert m.peer_ip("utun4") == ""


def test_lan_interface_found_by_client_subnet():
    m, fake = mgr()
    fake.addr_shows("2: eth0    inet 192.168.1.110/24 brd 192.168.1.255 scope global eth0\n"
                    "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 scope global eth0\n")
    assert m.lan_interface() == "eth0"


def test_lan_interface_falls_back_to_shaping_iface():
    cfg = make_cfg()
    cfg.shaping.interface = "enp0s3"
    m, fake = mgr(cfg)
    assert m.lan_interface() == "enp0s3"


def test_ifindexes_maps_names_to_indices():
    m, fake = mgr()
    fake.script[("ip", "-o", "link", "show")] = (0,
        "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
        "11: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 state UNKNOWN\n"
        "19: xray_tun@if2: <POINTOPOINT,UP,LOWER_UP> mtu 9000 state UNKNOWN\n")
    assert m.ifindexes() == {"lo": 1, "eth0": 2, "wg0": 11, "xray_tun": 19}


def test_ifindexes_empty_on_failure():
    m, fake = mgr()
    fake.script[("ip", "-o", "link", "show")] = (127, "ip: not found")
    assert m.ifindexes() == {}


# ---------------------------------------------------------------------------
# apply: the policy-routing program
# ---------------------------------------------------------------------------

def test_apply_full_sequence_with_peer_and_lan_routes():
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "utun4")] = \
        (0, "8: utun4    inet 10.9.0.2 peer 10.9.0.1/32 scope global utun4\n")
    st = m.apply("utun4")
    assert st.state == STATE_ON and st.interface == "utun4" and st.peer == "10.9.0.1"
    # the client subnet is diverted BEFORE the main table
    assert fake.has("ip", "rule", "add", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")
    # LAN direct routes never enter the tunnel
    assert fake.has("ip", "route", "replace", "table", "200",
                    "192.168.2.0/24", "dev", "eth0")
    assert fake.has("ip", "route", "replace", "table", "200",
                    "192.168.1.0/24", "dev", "eth0")
    # the tunnel default route rides the peer
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "via", "10.9.0.1", "dev", "utun4")
    assert m._applied and m._iface == "utun4"


def test_apply_no_peer_uses_dev_only():
    m, fake = tunnel_mgr("utun4")
    st = m.apply("utun4")
    assert st.state == STATE_ON
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "dev", "utun4")
    assert not fake.has("ip", "route", "replace", "table", "200",
                        "default", "via")


def test_apply_dev_only_scope_link_fallback():
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "route", "replace", "table", "200", "default",
                 "dev", "utun4")] = (1, "Nexthop has invalid gateway")
    st = m.apply("utun4")
    assert st.state == STATE_ON
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "dev", "utun4", "scope", "link")


def test_apply_brings_link_up_before_default_route():
    """The kernel refuses a route through a DOWN/missing tunnel ("Device for
    nexthop is not up"). The manager must ensure the link is UP first, and
    that bring-up must happen BEFORE the default route lands."""
    m, fake = tunnel_mgr("utun4")
    st = m.apply("utun4")
    assert st.state == STATE_ON
    up_i = next(i for i, c in enumerate(fake.calls)
                if c == ["ip", "link", "set", "dev", "utun4", "up"])
    route_i = next(i for i, c in enumerate(fake.calls)
                   if c[:2] == ["ip", "route"] and "default" in c)
    assert up_i < route_i


def test_apply_link_up_failure_does_not_abort():
    """A failed `ip link set ... up` (no such device / not root) must be
    non-fatal — the route add is still attempted."""
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "link", "set", "dev", "utun4", "up")] = (2, "cannot")
    st = m.apply("utun4")
    assert st.state == STATE_ON


def test_apply_route_retried_after_link_up_then_succeeds():
    """A route that fails once (fresh tunnel still settling) must succeed on
    the next attempt after a short pause — not give up after one try."""
    m, fake = tunnel_mgr("utun4")
    fail = ("ip", "route", "replace", "table", "200", "default",
            "dev", "utun4")
    fail_fallback = ("ip", "route", "replace", "table", "200", "default",
                     "dev", "utun4", "scope", "link")
    n = 0

    def flaky(argv):
        nonlocal n
        if tuple(argv) == fail or tuple(argv) == fail_fallback:
            n += 1
            if n == 1:
                return 1, "Device for nexthop is not up"
        return fake.script.get(tuple(argv), (0, ""))

    m._run_command = flaky
    st = m.apply("utun4")
    assert st.state == STATE_ON
    assert n == 2  # failed once (route + fallback), retried, succeeded


def test_apply_default_route_failure_reports_link_state():
    """When every attempt + the scope-link fallback fail, the message must
    carry the interface's REAL state (not the kernel's bare "not up"), so
    the dashboard shows whether the device is missing, down, or dead."""
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "route", "replace", "table", "200", "default",
                 "dev", "utun4")] = (2, "Cannot find device utun4")
    fake.script[("ip", "route", "replace", "table", "200", "default",
                 "dev", "utun4", "scope", "link")] = (2, "Cannot find device utun4")
    fake.script[("ip", "-o", "link", "show", "dev", "utun4")] = \
        (0, "8: utun4: <POINTOPOINT,DOWN> mtu 1500 qdisc noqueue state DOWN\n")
    st = m.apply("utun4")
    assert st.state == STATE_ERROR
    assert m._applied is False  # retry next tick
    assert "DOWN" in st.message


def test_apply_rule_file_exists_is_idempotent():
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "rule", "add", "from", "192.168.2.0/24", "lookup",
                 "200", "pref", "1000")] = (2, "RTNETLINK answers: File exists")
    st = m.apply("utun4")
    assert st.state == STATE_ON


def test_apply_default_route_failure_is_error():
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "route", "replace", "table", "200", "default",
                 "dev", "utun4")] = (2, "Cannot find device utun4")
    fake.script[("ip", "route", "replace", "table", "200", "default",
                 "dev", "utun4", "scope", "link")] = (2, "Cannot find device utun4")
    st = m.apply("utun4")
    assert st.state == STATE_ERROR
    assert m._applied is False  # retry next tick


def test_apply_without_interface_is_no_interface():
    m, fake = mgr()
    fake.addr_shows("")
    st = m.apply("")
    assert st.state == STATE_NO_INTERFACE
    assert fake.calls == []  # nothing attempted


def test_lan_route_failure_does_not_break_apply():
    """The LAN direct routes are best-effort (the main table still answers
    them) — a failure must not flip the state to error."""
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "route", "replace", "table", "200", "192.168.2.0/24",
                 "dev", "eth0")] = (1, "simulated")
    st = m.apply("utun4")
    assert st.state == STATE_ON


# ---------------------------------------------------------------------------
# remove: idempotent teardown
# ---------------------------------------------------------------------------

def test_remove_rule_and_flush_table():
    m, fake = tunnel_mgr("utun4")
    m.apply("utun4")
    before = len(fake.calls)
    m.remove()
    assert fake.has("ip", "rule", "del", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")
    assert fake.has("ip", "route", "flush", "table", "200")
    assert len(fake.calls) > before
    assert m._applied is False and m._iface is None


def test_remove_tolerates_missing_rule():
    m, fake = mgr()
    fake.script[("ip", "rule", "del", "from", "192.168.2.0/24",
                 "lookup", "200", "pref", "1000")] = \
        (2, "RTNETLINK answers: No such file or directory")
    m.remove()  # must not raise


# ---------------------------------------------------------------------------
# reconcile: the desired-state driver
# ---------------------------------------------------------------------------

def test_reconcile_off_no_rules_no_commands():
    m, fake = mgr()
    fake.rule_show(present=False)
    st = m.reconcile(False)
    assert st.state == STATE_OFF
    # exactly the one leftover probe ran; no rule/route mutators
    assert fake.count("ip", "rule", "show") == 1
    assert fake.count("ip", "rule", "add") == 0
    assert fake.count("ip", "rule", "del") == 0
    assert fake.count("ip", "route") == 0


def test_reconcile_off_cleans_leftovers_from_crashed_run():
    m, fake = mgr()
    fake.rule_show(present=True)
    st = m.reconcile(False)
    assert st.state == STATE_OFF
    assert fake.has("ip", "rule", "del", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")
    assert fake.has("ip", "route", "flush", "table", "200")


def test_reconcile_on_auto_detects_tunnel():
    root = make_sysfs(("utun4", "65534"))
    cfg = make_cfg()
    fake = FakeIp()
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "utun4")] = \
        (0, "8: utun4    inet 10.9.0.2 peer 10.9.0.1/32 scope global utun4\n")
    m = VpnShareManager(cfg, run_command=fake, sysfs_root=root)
    st = m.reconcile(True)
    assert st.state == STATE_ON and st.interface == "utun4"
    assert fake.has("ip", "rule", "add", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")


def test_reconcile_on_respects_pin_over_detection():
    root = make_sysfs(("utun4", "65534"), ("wg0", "65534"))
    fake = FakeIp()
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    for dev in ("utun4", "wg0"):
        fake.script[("ip", "-o", "-4", "addr", "show", "dev", dev)] = \
            (0, f"5: {dev}    inet 10.8.0.2/24 scope global {dev}\n")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    st = m.reconcile(True, interface_pin="wg0")
    assert st.state == STATE_ON and st.interface == "wg0"
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "dev", "wg0")


def test_reconcile_on_no_tunnel_reports_and_never_blackholes():
    """The tunnel vanished while enabled: the rule must come DOWN so clients
    ride the direct uplink instead of a dead-VPN blackhole."""
    m, fake = tunnel_mgr("utun4")
    assert m.reconcile(True, interface_pin="utun4").state == STATE_ON
    # sysfs is now empty — the same tunnel name is gone
    m.sysfs_root = make_sysfs()
    # ip link show dev must fail for the vanished interface (real kernel
    # returns non-zero for a device that does not exist)
    fake.script[("ip", "link", "show", "dev", "utun4")] = (1, "")
    st = m.reconcile(True, interface_pin="utun4")
    assert st.state == STATE_NO_INTERFACE
    assert m._applied is False
    assert fake.has("ip", "rule", "del", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")


def test_reconcile_pin_gone_falls_back_to_redetected_tunnel():
    """The pinned utun4 vanished but a NEW tunnel appeared (the VPN client
    restarted as wg0): route into the new one, not the dead pin."""
    m, fake = tunnel_mgr("utun4")
    assert m.reconcile(True, interface_pin="utun4").state == STATE_ON
    m.sysfs_root = make_sysfs(("wg0", "65534"))
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "wg0")] = \
        (0, "3: wg0    inet 10.8.0.2/24 scope global wg0\n")
    # The old pin (utun4) is gone — ip link show dev must fail for it
    fake.script[("ip", "link", "show", "dev", "utun4")] = (1, "")
    st = m.reconcile(True, interface_pin="utun4")
    assert st.state == STATE_ON and st.interface == "wg0"
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "dev", "wg0")


def test_reconcile_disabled_after_enabled_removes():
    m, fake = tunnel_mgr("utun4")
    assert m.reconcile(True, interface_pin="utun4").state == STATE_ON
    st = m.reconcile(False)
    assert st.state == STATE_OFF
    assert fake.has("ip", "rule", "del", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")


def test_reconcile_cfg_iface_wins_over_pin():
    cfg = make_cfg(tunnel_iface="wg0")
    fake = FakeIp()
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "wg0")] = \
        (0, "4: wg0    inet 10.8.0.2/24 scope global wg0\n")
    m = VpnShareManager(cfg, run_command=fake,
                        sysfs_root=make_sysfs(("utun4", "65534"),
                                              ("wg0", "65534")))
    st = m.reconcile(True, interface_pin="utun4")
    assert st.interface == "wg0"


def test_reconcile_refuses_stale_pin_without_ipv4():
    """A stale pin to a junk device that still EXISTS in sysfs but carries no
    IPv4 and has its link DOWN (the live-box "evice" — ARPHRD_NONE yet routes
    nothing) must NOT be routed into: that would blackhole the whole subnet.
    apply() rejects the dead device; reconcile falls back to the real tunnel."""
    root = make_sysfs(("evice", "65534"), ("utun4", "65534"))
    fake = FakeIp()
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "evice")] = (0, "")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "utun4")] = \
        (0, "8: utun4    inet 10.9.0.2 peer 10.9.0.1/32 scope global utun4\n")
    # evice is a dead device — link DOWN
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "utun4")] = \
        (0, "8: utun4    inet 10.9.0.2 peer 10.9.0.1/32 scope global utun4\n")
    fake.script[("ip", "-o", "link", "show", "dev", "evice")] = \
        (0, "1: evice: <NOARP,UP,LOWER_UP> mtu 1500 qdisc noop state DOWN\n")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    st = m.reconcile(True, interface_pin="evice")
    assert st.state == STATE_ON and st.interface == "utun4"
    # the junk device was never routed into
    assert not fake.has("ip", "route", "replace", "table", "200",
                        "default", "dev", "evice")
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "via", "10.9.0.1", "dev", "utun4")


def test_reconcile_refuses_apply_into_dead_tunnel():
    """apply() gates on link state: a device that exists but has no IPv4 AND
    its link is DOWN (a dead ARPHRD_NONE junk device) is reported as
    no-interface, never routed into — the subnet cannot be blackholed."""
    root = make_sysfs(("evice", "65534"))
    fake = FakeIp()
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "evice")] = (0, "")
    # dead device — link DOWN
    fake.script[("ip", "-o", "link", "show", "dev", "evice")] = \
        (0, "1: evice: <NOARP,UP,LOWER_UP> mtu 1500 qdisc noop state DOWN\n")
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    st = m.reconcile(True, interface_pin="evice")
    assert st.state == STATE_NO_INTERFACE
    assert not fake.has("ip", "rule", "add", "from", "192.168.2.0/24",
                        "lookup", "200", "pref", "1000")


def test_apply_addressless_but_up_tunnel():
    """A TUN that has no IPv4 but its link is UP (xray-core / sing-box TUN
    mode) IS routed into with a dev-only default route — the policy routing
    works without an address on the device."""
    root = make_sysfs(("xray_tun", "65534"))
    fake = FakeIp()
    fake.addr_shows("2: eth0    inet 192.168.2.1/24 scope global eth0\n")
    # no IPv4 on the TUN
    fake.script[("ip", "-o", "-4", "addr", "show", "dev", "xray_tun")] = (0, "")
    # but the link is UP (TUNs show state UNKNOWN)
    fake.script[("ip", "-o", "link", "show", "dev", "xray_tun")] = \
        (0, ("3: xray_tun: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> "
             "mtu 9000 qdisc fq_codel state UNKNOWN group default qlen 1000\n"))
    m = VpnShareManager(make_cfg(), run_command=fake, sysfs_root=root)
    st = m.reconcile(True)
    assert st.state == STATE_ON and st.interface == "xray_tun"
    # routed with dev-only default route (no via/peer)
    assert fake.has("ip", "route", "replace", "table", "200",
                    "default", "dev", "xray_tun")


# ---------------------------------------------------------------------------
# per-device VPN bypass: exclusion policy rules (pref rule_pref - 1)
# ---------------------------------------------------------------------------

def test_excl_pref_is_one_before_the_subnet_rule():
    m, _fake = mgr()
    assert m.excl_pref == 999


def test_reconcile_on_adds_bypass_rules():
    """Enabled + a bypass IP -> a `from <ip> lookup main` rule at pref 999,
    ONE priority before the subnet divert (999 < 1000), so the device's
    packets consult the main table first and ride the direct uplink."""
    m, fake = tunnel_mgr("utun4")
    st = m.reconcile(True, interface_pin="utun4",
                     exclusions=["192.168.2.50"])
    assert st.state == STATE_ON
    assert fake.has("ip", "rule", "add", "pref", "999",
                    "from", "192.168.2.50", "lookup", "main")


def test_reconcile_on_dedupes_and_ignores_empty_ips():
    m, fake = tunnel_mgr("utun4")
    m.reconcile(True, interface_pin="utun4",
                exclusions=["192.168.2.50", "", "192.168.2.50"])
    adds = [c for c in fake.calls
            if c[:3] == ["ip", "rule", "add"] and "192.168.2.50" in c]
    assert len(adds) == 1


def test_reconcile_on_deletes_stale_bypass_rules():
    """A bypass rule for an IP no longer marked (or a leftover from a crashed
    run) must be deleted on the next enabled tick — parse-based diff against
    the LIVE kernel state."""
    m, fake = tunnel_mgr("utun4")
    fake.rule_show(present=False)
    fake.script[("ip", "rule", "show")] = (
        0,
        "local:\n"
        "default:\n"
        "999:\tfrom 192.168.2.77 lookup main\n"
        "1000:\tfrom 192.168.2.0/24 lookup 200\n")
    st = m.reconcile(True, interface_pin="utun4",
                     exclusions=["192.168.2.50"])
    assert st.state == STATE_ON
    assert fake.has("ip", "rule", "del", "pref", "999",
                    "from", "192.168.2.77", "lookup", "main")
    assert fake.has("ip", "rule", "add", "pref", "999",
                    "from", "192.168.2.50", "lookup", "main")


def test_reconcile_on_no_change_costs_one_show_only():
    """Steady state (rules already correct): the sync runs exactly one
    `ip rule show` and issues no mutators."""
    m, fake = tunnel_mgr("utun4")
    fake.rule_show(present=False)
    fake.script[("ip", "rule", "show")] = (
        0,
        "local:\n"
        "999:\tfrom 192.168.2.50 lookup main\n"
        "1000:\tfrom 192.168.2.0/24 lookup 200\n")
    before = len(fake.calls)
    assert m.reconcile(True, interface_pin="utun4",
                       exclusions=["192.168.2.50"]).state == STATE_ON
    delta = fake.calls[before:]
    shows = [c for c in delta if c == ["ip", "rule", "show"]]
    mutators = [c for c in delta if c[:2] == ["ip", "rule", "add"]
                or c[:2] == ["ip", "rule", "del"]]
    assert len(shows) == 1 and not mutators


def test_reconcile_disable_flushes_bypass_rules():
    """Turning the share OFF removes the subnet divert AND every bypass
    exclusion (they only matter while the divert exists)."""
    m, fake = tunnel_mgr("utun4")
    assert m.reconcile(True, interface_pin="utun4",
                       exclusions=["192.168.2.50"]).state == STATE_ON
    fake.calls.clear()
    # the kernel still holds the exclusion when the switch flips off
    # (FakeIp is stateless — re-script what a real box would show)
    fake.rule_show(present=True)
    fake.script[("ip", "rule", "show")] = (
        0,
        "local:\n"
        "999:\tfrom 192.168.2.50 lookup main\n"
        "1000:\tfrom 192.168.2.0/24 lookup 200\n")
    st = m.reconcile(False)
    assert st.state == STATE_OFF
    assert fake.has("ip", "rule", "del", "pref", "999",
                    "from", "192.168.2.50", "lookup", "main")
    assert fake.has("ip", "rule", "del", "from", "192.168.2.0/24",
                    "lookup", "200", "pref", "1000")


def test_reconcile_off_boot_probe_cleans_leftover_bypass_rules():
    """A crashed run left ONLY bypass rules behind (subnet divert already
    gone): the one-time boot probe must still detect dirt and clean it."""
    m, fake = mgr()
    fake.script[("ip", "rule", "show")] = (
        0,
        "local:\n"
        "999:\tfrom 192.168.2.77 lookup main\n")
    st = m.reconcile(False)
    assert st.state == STATE_OFF
    assert fake.has("ip", "rule", "del", "pref", "999",
                    "from", "192.168.2.77", "lookup", "main")


def test_bypass_add_failure_is_nonfatal():
    """A failed bypass add (kernels that reject same-pref selectors) logs and
    moves on — the share itself stays ON; the next tick retries."""
    m, fake = tunnel_mgr("utun4")
    fake.script[("ip", "rule", "add", "pref", "999", "from", "192.168.2.50",
                 "lookup", "main")] = (2, "RTNETLINK answers: Invalid argument")
    st = m.reconcile(True, interface_pin="utun4",
                     exclusions=["192.168.2.50"])
    assert st.state == STATE_ON