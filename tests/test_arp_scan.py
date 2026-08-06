"""Rogue-device scanner tests: ARP frame build/parse round-trips, LAN NIC
resolution from ``ip`` output, and scan() exclusions — all with injected fakes,
so no root, sockets, or ping are needed.

The default probe is never exercised here (it needs AF_PACKET + root); the
``_neigh_probe`` ping-sweep fallback is tested against a fake ``ip``/``ping``.
"""

from __future__ import annotations

import ipaddress

from core.config import Config
from quota.arp_scan import (
    ArpScanner,
    _box_ip_in,
    _normalize_mac,
    arp_request_frame,
    arp_reply_frame,
    parse_arp_reply,
    parse_arp_request,
    resolve_nic,
)

# `ip -o -4 addr show` on the single-NIC gateway: one NIC owns both the uplink
# IP and the client-subnet alias.
ADDR_OUT = (
    "2: eth0    inet 192.168.1.110/24 brd 192.168.1.255 scope global eth0\\\n"
    "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 scope global eth0\\\n"
)
LINK_OUT = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
    "state UP mode DEFAULT group default qlen 1000\n"
    "    link/ether aa:bb:cc:dd:ee:01 brd ff:ff:ff:ff:ff:ff\n"
)


def _cfg() -> Config:
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = "192.168.1.1"
    return cfg


def _run(argv):
    if argv == ["ip", "-o", "-4", "addr", "show"]:
        return 0, ADDR_OUT
    if argv == ["ip", "-o", "link", "show", "eth0"]:
        return 0, LINK_OUT
    return 1, ""


# --------------------------------------------------------------------------- #
# MAC + ARP frame helpers
# --------------------------------------------------------------------------- #

def test_normalize_mac():
    assert _normalize_mac("AA-BB-CC-DD-EE-01") == "aa:bb:cc:dd:ee:01"
    assert _normalize_mac("  AA:BB:CC:DD:EE:01  ") == "aa:bb:cc:dd:ee:01"


def test_arp_frame_roundtrip():
    req = arp_request_frame("aa:bb:cc:dd:ee:01", "192.168.2.1", "192.168.2.50")
    assert req[:6] == b"\xff" * 6                 # broadcast dst
    assert req[12:14] == b"\x08\x06"              # ARP ethertype
    spa, sha, tpa = parse_arp_request(req)
    assert (spa, sha, tpa) == ("192.168.2.1", "aa:bb:cc:dd:ee:01",
                               "192.168.2.50")

    rep = arp_reply_frame("aa:bb:cc:dd:ee:01", "192.168.1.1",
                          "11:22:33:44:55:66", "192.168.2.50")
    assert rep[:6] == bytes.fromhex("112233445566")  # unicast to the requester
    ip, mac = parse_arp_reply(rep)
    assert (ip, mac) == ("192.168.1.1", "aa:bb:cc:dd:ee:01")


def test_parse_rejections():
    assert parse_arp_request(b"") is None
    assert parse_arp_reply(b"") is None
    # an IP frame (ethertype 0x0800) is not ARP
    ip_frame = b"\x00" * 12 + b"\x08\x00" + b"\x00" * 30
    assert parse_arp_request(ip_frame) is None
    assert parse_arp_reply(ip_frame) is None
    # a request is not a reply, and vice versa
    req = arp_request_frame("aa:bb:cc:dd:ee:01", "192.168.2.1", "192.168.2.50")
    assert parse_arp_reply(req) is None


# --------------------------------------------------------------------------- #
# NIC resolution
# --------------------------------------------------------------------------- #

def test_resolve_nic_finds_owning_nic():
    iface, mac, ips = resolve_nic("192.168.2.1", _run)
    assert iface == "eth0"
    assert mac == "aa:bb:cc:dd:ee:01"
    assert ips == {"192.168.1.110", "192.168.2.1"}


def test_resolve_nic_returns_none_when_no_nic_owns_gateway():
    def run(argv):
        if argv == ["ip", "-o", "-4", "addr", "show"]:
            return 0, ("2: eth0    inet 192.168.9.9/24 brd 192.168.9.255 "
                       "scope global eth0\\\n")
        return 1, ""
    assert resolve_nic("192.168.2.1", run) is None


def test_resolve_nic_degrades_when_ip_missing():
    def run(argv):
        return 127, "ip: not found"
    assert resolve_nic("192.168.2.1", run) is None


def test_box_ip_in_picks_host_in_net():
    net = ipaddress.ip_network("192.168.2.0/24")
    assert _box_ip_in(net, {"192.168.1.110", "192.168.2.1"}, "0.0.0.0") \
        == "192.168.2.1"
    # no box IP in the net -> the caller's fallback is returned
    assert _box_ip_in(net, {"192.168.1.110"}, "192.168.2.1") == "192.168.2.1"


# --------------------------------------------------------------------------- #
# scan(): active-but-unleased hosts are rogues
# --------------------------------------------------------------------------- #

def test_scan_reports_active_non_dhcp_hosts():
    cfg = _cfg()

    def probe(networks, iface, box_mac, box_ips):
        return {
            "192.168.2.50": "11:22:33:44:55:66",   # rogue (static IP)
            "192.168.2.111": "aa:bb:cc:dd:ee:02",   # known DHCP device
            "192.168.1.1": "ff:ff:ff:ff:ff:01",     # the router — never reported
            "192.168.1.110": "aa:bb:cc:dd:ee:01",   # the box's own uplink IP
        }

    scanner = ArpScanner(cfg, run_command=_run, probe=probe)
    rogues = scanner.scan(known_macs={"aa:bb:cc:dd:ee:02"})
    assert [(r.ip, r.mac) for r in rogues] == \
        [("192.168.2.50", "11:22:33:44:55:66")]
    assert rogues[0].online is True
    # unknown OUI -> vendor stays "" (never crashes the lookup)
    assert rogues[0].vendor == ""


def test_scan_matches_known_macs_after_normalization():
    """A probe that returns a dash-separated / uppercase MAC still matches a
    known lease (normalized before comparison)."""
    cfg = _cfg()

    def probe(networks, iface, box_mac, box_ips):
        return {"192.168.2.111": "AA-BB-CC-DD-EE-02"}

    scanner = ArpScanner(cfg, run_command=_run, probe=probe)
    assert scanner.scan(known_macs={"aa:bb:cc:dd:ee:02"}) == []


def test_scan_returns_empty_without_networks():
    cfg = Config()
    cfg.engine.client_subnet = ""
    cfg.engine.uplink_subnet = ""
    cfg.dhcp.gateway_ip = ""
    cfg.dhcp.router_ip = ""
    scanner = ArpScanner(cfg, probe=lambda *a: {})
    assert scanner.scan(set()) == []


def test_scan_probes_only_client_subnet_under_wan():
    """v19.5: in WAN mode the box keeps the uplink IP as a router-admin alias
    (the uplink subnet IS local to the engine), but the ROGUE SCANNER still
    probes only the client subnet — the router is bridged and only $CLIENT_NET
    is masqueraded, so an uplink-subnet static host can never reach the
    internet (probing it would just flag the bridged router's own management IP
    as a false rogue)."""
    cfg = _cfg()
    cfg.engine.topology = "wan"
    cfg.dhcp.router_ip = ""  # no router on the LAN in WAN mode
    scanner = ArpScanner(cfg, run_command=_run, probe=lambda *a: {})
    assert scanner._networks == ["192.168.2.0/24"], \
        "WAN mode must probe only the client subnet"


def test_scan_degrades_when_nic_unresolved():
    def run(argv):
        return 127, "ip: not found"
    scanner = ArpScanner(_cfg(), run_command=run, probe=lambda *a: {
        "192.168.2.50": "11:22:33:44:55:66"})
    assert scanner.scan(set()) == []


# --------------------------------------------------------------------------- #
# ping-sweep fallback (_neigh_probe) parses `ip -j neigh`
# --------------------------------------------------------------------------- #

def test_neigh_probe_parses_ip_neigh_json():
    cfg = _cfg()

    def run(argv):
        if argv[0] == "ping":
            return 0, ""
        if argv == ["ip", "-j", "neigh", "show", "dev", "eth0"]:
            return 0, ('[{"dst":"192.168.2.50","lladdr":"11:22:33:44:55:66",'
                       '"state":"REACHABLE","dev":"eth0"}]')
        return 1, ""

    scanner = ArpScanner(cfg, run_command=run)
    found = scanner._neigh_probe(
        ["192.168.2.0/24", "192.168.1.0/24"], "eth0",
        "aa:bb:cc:dd:ee:01", {"192.168.2.1"})
    assert found == {"192.168.2.50": "11:22:33:44:55:66"}


def test_neigh_probe_handles_bad_json():
    def run(argv):
        if argv[0] == "ping":
            return 0, ""
        if argv[0] == "ip" and argv[1] == "-j":
            return 0, "not-json"
        return 1, ""
    scanner = ArpScanner(_cfg(), run_command=run)
    assert scanner._neigh_probe(["192.168.2.0/24"], "eth0",
                                "aa:bb:cc:dd:ee:01", {"192.168.2.1"}) == {}
