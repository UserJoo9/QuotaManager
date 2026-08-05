"""Unit tests for the DHCP server's IP-allocation logic (no sockets needed).

The DHCP server hands clients addresses from its pool but must never hand out
addresses in the router's electric-cut fallback range (``reserved_ips``).
"""

from __future__ import annotations

import socket

import pytest

from core import config as cfg_mod
from quota import dhcp as dhcp_mod


def _server(pool: list[str], reserved: list[str] | None = None,
            cfg: cfg_mod.DhcpConfig | None = None) -> dhcp_mod.DhcpServer:
    return dhcp_mod.DhcpServer(
        cfg=cfg or cfg_mod.DhcpConfig(),
        pool=pool,
        gateway="192.168.1.2",
        subnet_mask="255.255.255.0",
        reserved_ips=set(reserved or ()),
    )


def test_candidate_skips_reserved_ips():
    srv = _server(["192.168.1.100", "192.168.1.101", "192.168.1.102",
                   "192.168.1.103"],
                  reserved=["192.168.1.103"])
    seen = {srv._candidate() for _ in range(6)}
    assert "192.168.1.103" not in seen
    # every other pool member is still reachable
    assert seen >= {"192.168.1.100", "192.168.1.101", "192.168.1.102"}


def test_candidate_when_all_reserved_raises():
    srv = _server(["192.168.1.100", "192.168.1.101"],
                  reserved=["192.168.1.100", "192.168.1.101"])
    with pytest.raises(RuntimeError):
        srv._candidate()


def test_candidate_refuses_requested_reserved_ip():
    srv = _server(["192.168.1.100", "192.168.1.101"],
                  reserved=["192.168.1.101"])
    # a client asking for a reserved IP must not receive it
    granted = {srv._candidate("192.168.1.101") for _ in range(3)}
    assert granted == {"192.168.1.100"}


def test_request_ack_refuses_reserved_ip():
    """A REQUEST for a reserved IP gets NAK'd (never granted)."""
    srv = _server(["192.168.1.100", "192.168.1.101"],
                  reserved=["192.168.1.101"])
    mac = "aa:bb:cc:dd:ee:ff"
    # DISCOVER: offer must not be the reserved IP
    offer = srv._candidate()
    assert offer != "192.168.1.101"
    srv._offers[mac] = offer
    resp = srv.handle(_request(mac, requested="192.168.1.101"),
                      ("0.0.0.0", 68))
    assert resp is not None
    opts = dhcp_mod.DhcpServer._parse_options(resp, 240)
    assert opts[dhcp_mod.OPT_MSG_TYPE] == bytes([dhcp_mod.MSG_NAK]), (
        "reserved IP must be NAK'd (message type lives in option 53, not htype)")


def _request(mac: str, requested: str = "", msg_type: int = 3) -> bytes:
    """Build a minimal BOOTREQUEST datagram (REQUEST=3 by default)."""
    mac_b = dhcp_mod.DhcpServer.str_to_mac(mac)
    hdr = bytes([1, 1, 6, 0])                       # bootrequest, eth, hlen 6
    hdr += bytes(4)                                  # xid
    hdr += bytes([0x80]) + bytes(3)                  # broadcast flag
    hdr += bytes(4)                                  # ciaddr
    hdr += bytes(4)                                  # yiaddr
    hdr += bytes(4)                                  # siaddr
    hdr += bytes(4)                                  # giaddr
    hdr += mac_b + bytes(10)                         # chaddr (16 bytes)
    hdr += bytes(64) + bytes(128)                    # sname + file
    opts = bytes([dhcp_mod.OPT_MSG_TYPE, 1, msg_type])
    if requested:
        import socket
        opts += bytes([dhcp_mod.OPT_REQUESTED_IP, 4]) + socket.inet_aton(requested)
    opts += bytes([dhcp_mod.OPT_END])
    return hdr + dhcp_mod.BOOTP_MAGIC + opts


def test_expand_ip_range():
    assert cfg_mod.expand_ip_range("192.168.1.100", "192.168.1.102") == [
        "192.168.1.100", "192.168.1.101", "192.168.1.102"]
    with pytest.raises(ValueError):
        cfg_mod.expand_ip_range("192.168.1.105", "192.168.1.100")


def test_broadcast_address():
    assert dhcp_mod.broadcast_address("192.168.1.110", "255.255.255.0") == "192.168.1.255"
    assert dhcp_mod.broadcast_address("10.0.0.2", "255.255.0.0") == "10.0.255.255"


def test_offers_are_sticky_same_mac():
    """A MAC that keeps retrying DISCOVER must get the SAME offered IP, not a
    fresh one each time (which was burning pool addresses on every retry)."""
    srv = _server(["192.168.1.100", "192.168.1.101", "192.168.1.102"])
    mac = "aa:bb:cc:dd:ee:30"
    first = srv.handle(_request(mac, msg_type=1), ("0.0.0.0", 68))
    second = srv.handle(_request(mac, msg_type=1), ("0.0.0.0", 68))
    assert first is not None and second is not None
    assert socket.inet_ntoa(first[16:20]) == socket.inet_ntoa(second[16:20])


def test_socket_sets_broadcast_option():
    """The DHCP socket must be able to send broadcast replies (Windows needs
    SO_BROADCAST or the first reply to a DISCOVER dies with WinError 10013)."""
    s = dhcp_mod.make_dhcp_socket()
    try:
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST) == 1
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    finally:
        s.close()


def test_reply_header_ethernet_htype_hlen():
    """Regression: replies must be htype=1 (Ethernet), hlen=6.

    Phone DHCP clients (iOS/Android) validate the hardware type/length and
    silently discard replies that violate them — the client hangs at
    "Obtaining IP address…". A prior bug packed the DHCP message-type into the
    htype slot, so OFFERs went out as htype=2 and ACKs as htype=5.
    """
    srv = _server(["192.168.1.100", "192.168.1.101"])
    mac = "aa:bb:cc:dd:ee:ff"
    resp = srv.handle(_request(mac, msg_type=1), ("0.0.0.0", 68))  # DISCOVER
    assert resp is not None
    # BOOTP fixed header: op=BOOTREPLY(2), htype=1, hlen=6, hops=0
    assert resp[0] == 2, "op must be BOOTREPLY"
    assert resp[1] == 1, "htype must be Ethernet (1)"
    assert resp[2] == 6, "hlen must be 6 (MAC address)"
    assert resp[3] == 0
    assert resp[236:240] == dhcp_mod.BOOTP_MAGIC
    # chaddr (client MAC) echoed at offset 28
    assert resp[28:34] == dhcp_mod.DhcpServer.str_to_mac(mac)
    # message type = OFFER and a yiaddr was offered
    opts = dhcp_mod.DhcpServer._parse_options(resp, 240)
    assert opts[dhcp_mod.OPT_MSG_TYPE] == bytes([dhcp_mod.MSG_OFFER])
    assert resp[16:20] != b"\x00\x00\x00\x00", "offer must carry a yiaddr"
    assert opts[dhcp_mod.OPT_SERVER_ID] == socket.inet_aton("192.168.1.2")
    assert opts[dhcp_mod.OPT_ROUTER] == socket.inet_aton("192.168.1.2")


def test_reply_echoes_secs_and_flags():
    """RFC 2131: the server echoes the request's secs/flags fields."""
    srv = _server(["192.168.1.100"])
    mac = "aa:bb:cc:dd:ee:10"
    req = bytearray(_request(mac, msg_type=1))
    req[8:10] = (2).to_bytes(2, "big")             # secs = 2
    req[10:12] = (0x8000).to_bytes(2, "big")       # broadcast flag set
    resp = srv.handle(bytes(req), ("0.0.0.0", 68))
    assert resp is not None
    assert resp[8:10] == (2).to_bytes(2, "big")
    assert resp[10:12] == (0x8000).to_bytes(2, "big")


def test_discover_request_ack_roundtrip():
    """A real phone does DISCOVER -> (accept offer) -> REQUEST -> ACK."""
    srv = _server(["192.168.1.100", "192.168.1.101"])
    mac = "aa:bb:cc:dd:ee:20"

    offer = srv.handle(_request(mac, msg_type=1), ("0.0.0.0", 68))
    assert offer is not None
    opts = dhcp_mod.DhcpServer._parse_options(offer, 240)
    assert opts[dhcp_mod.OPT_MSG_TYPE] == bytes([dhcp_mod.MSG_OFFER])
    yiaddr = socket.inet_ntoa(offer[16:20])
    assert yiaddr in {"192.168.1.100", "192.168.1.101"}

    ack = srv.handle(_request(mac, requested=yiaddr, msg_type=3), ("0.0.0.0", 68))
    assert ack is not None
    opts2 = dhcp_mod.DhcpServer._parse_options(ack, 240)
    assert opts2[dhcp_mod.OPT_MSG_TYPE] == bytes([dhcp_mod.MSG_ACK])
    assert socket.inet_ntoa(ack[16:20]) == yiaddr, "ACK must confirm the offered IP"
    assert srv.leases[mac] == yiaddr, "lease recorded"
    # options present in the ACK: lease, mask, router, DNS
    assert dhcp_mod.OPT_LEASE_TIME in opts2
    assert dhcp_mod.OPT_SUBNET_MASK in opts2
    assert dhcp_mod.OPT_DNS in opts2


def test_advertise_self_dns_points_clients_at_the_gateway():
    """When the PC runs the DNS forwarder, the DHCP DNS option must be the PC
    itself (the gateway), so every device's DNS deterministically crosses it."""
    cfg = cfg_mod.DhcpConfig(dns_servers=["192.168.1.1", "8.8.8.8"])
    srv = _server(["192.168.1.100"], cfg=cfg)
    srv.advertise_self_dns = True
    mac = "aa:bb:cc:dd:ee:21"
    ack = srv.handle(_request(mac, requested="192.168.1.100", msg_type=3),
                     ("0.0.0.0", 68))
    assert ack is not None
    opts = dhcp_mod.DhcpServer._parse_options(ack, 240)
    assert opts[dhcp_mod.OPT_DNS] == socket.inet_aton("192.168.1.2"), (
        "with advertise_self_dns, DNS option must be the gateway, not upstreams")


def test_default_dns_option_is_config_servers():
    """Without a forwarder, clients get the configured upstream DNS servers."""
    cfg = cfg_mod.DhcpConfig(dns_servers=["192.168.1.1", "8.8.8.8"])
    srv = _server(["192.168.1.100"], cfg=cfg)
    mac = "aa:bb:cc:dd:ee:22"
    ack = srv.handle(_request(mac, requested="192.168.1.100", msg_type=3),
                     ("0.0.0.0", 68))
    assert ack is not None
    opts = dhcp_mod.DhcpServer._parse_options(ack, 240)
    assert opts[dhcp_mod.OPT_DNS] == b"".join(
        socket.inet_aton(x) for x in ["192.168.1.1", "8.8.8.8"])
