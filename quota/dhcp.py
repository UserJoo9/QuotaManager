"""Minimal DHCP server (IPv4) for the one-armed gateway.

The router's DHCP is disabled and *this* server hands out addresses inside the
router's LAN subnet, with the default gateway set to the PC. Because we are the
authoritative DHCP server, we learn the MAC <-> IP binding of every device for
free — which is exactly what the quota engine needs.

Implementation notes
--------------------
* Raw UDP on port 67 (needs Administrator on Windows). Bound to 0.0.0.0 so
  DHCPDISCOVER broadcasts are received.
* RFC 2131 state machine: DISCOVER/OFFER/REQUEST/ACK/NAK/RELEASE (+ INFORM).
* Replies go to 255.255.255.255:68 or the unicast ciaddr per RFC 2131.
* Options served: subnet mask, router (= gateway = PC), DNS, lease time,
  server identifier.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from ipaddress import ip_network
from typing import Any, Callable, Optional

from core.config import detect_local_interface_ips

log = logging.getLogger("quota.dhcp")

OP_OFFER = 2
OP_ACK = 5
OP_NAK = 6

MSG_DISCOVER = 1
MSG_OFFER = 2
MSG_REQUEST = 3
MSG_DECLINE = 4
MSG_ACK = 5
MSG_NAK = 6
MSG_RELEASE = 7
MSG_INFORM = 8

OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_PARAMETER_REQUEST = 55
OPT_END = 255

BOOTP_MAGIC = b"\x63\x82\x53\x63"
REPLY_PORT = 68
MAX_DGRAM = 4096


def broadcast_address(gateway: str, mask: str) -> str:
    """Directed broadcast of the served subnet (e.g. 192.168.1.255).

    Replies to a client with no IP yet (src 0.0.0.0) are sent to this address
    rather than the limited broadcast 255.255.255.255: Windows routes a
    directed broadcast deterministically out the interface owning the subnet,
    which is the one the DHCP clients are on.
    """
    return str(ip_network(f"{gateway}/{mask}", strict=False).broadcast_address)


def make_dhcp_socket() -> socket.socket:
    """Create the UDP datagram socket used by the DHCP server.

    ``SO_BROADCAST`` is required on Windows: replies to a client's DISCOVER
    are broadcast to 255.255.255.255:68, and sending to a broadcast address
    without this option raises ``PermissionError`` (WinError 10013). Without
    it the server dies on its very first broadcast reply.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


class DhcpServer:
    """Async DHCP server; ``on_lease(mac, ip)`` fires on every granted lease."""

    def __init__(
        self,
        cfg: Any,
        pool: list[str],
        gateway: str,
        subnet_mask: str,
        on_lease: Callable[[str, str], Any] | None = None,
        reserved_ips: set[str] | None = None,
        advertise_self_dns: bool = False,
    ) -> None:
        self.cfg = cfg
        self.pool = list(pool)
        self._pool_index = 0
        self.gateway = gateway
        self.subnet_mask = subnet_mask
        self.broadcast = broadcast_address(gateway, subnet_mask)
        self.lease_hours = float(getattr(cfg, "lease_hours", 24))
        self.on_lease_cb = on_lease
        #: When the PC runs the DNS forwarder, advertise the PC (the gateway)
        #: as the DNS server so every device's DNS deterministically crosses the
        #: PC. Otherwise hand out the configured upstream servers directly.
        self.advertise_self_dns = advertise_self_dns
        #: IPs this server must never hand out (e.g. the router's electric-cut
        #: fallback pool, which is validated non-overlapping with ``pool``).
        self.reserved = set(reserved_ips or ())

        # mac -> ip (authoritative leases); ip -> mac reverse lookup.
        self.leases: dict[str, str] = {}
        self._ip_to_mac: dict[str, str] = {}
        self._offers: dict[str, str] = {}  # mac -> offered ip
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def mac_to_str(raw: bytes) -> str:
        return ":".join(f"{b:02x}" for b in raw[:6])

    @staticmethod
    def str_to_mac(mac: str) -> bytes:
        return bytes(int(x, 16) for x in mac.replace("-", ":").split(":")[:6])

    def _candidate(self, requested: str = "") -> str:
        """Next free IP, honouring a requested IP if it is in the pool and free.

        Reserved IPs (the router's fallback range) are never handed out, and a
        requested IP that falls in the reserved set is refused just like one
        outside the pool.
        """
        if (requested in self.pool and requested not in self._ip_to_mac
                and requested not in self.reserved):
            return requested
        for _ in range(len(self.pool)):
            ip = self.pool[self._pool_index % len(self.pool)]
            self._pool_index += 1
            if ip in self.reserved:
                continue
            if ip not in self._ip_to_mac:
                return ip
        raise RuntimeError("DHCP pool exhausted")

    def _release(self, mac: str) -> None:
        ip = self.leases.pop(mac, None)
        if ip:
            self._ip_to_mac.pop(ip, None)
        self._offers.pop(mac, None)

    def _grant(self, mac: str, ip: str) -> None:
        self.leases[mac] = ip
        self._ip_to_mac[ip] = mac
        self._offers.pop(mac, None)
        if self.on_lease_cb:
            try:
                self.on_lease_cb(mac, ip)
            except Exception:  # noqa: BLE001
                log.exception("on_lease callback failed")

    # ------------------------------------------------------------------ builder

    def _build(self, xid: bytes, yiaddr: str, mac: str,
               msg_type: int, secs: bytes = b"\x00\x00",
               flags: bytes = b"\x00\x00") -> bytes:
        """Build a DHCP reply datagram.

        Header is ``op=BOOTREPLY(2), htype=1 (Ethernet), hlen=6, hops=0``.
        ``htype``/``hlen`` must be exact: phone DHCP clients (iOS/Android)
        validate them and silently discard replies with a wrong hardware type
        or address length — the symptom is a client stuck at "Obtaining IP
        address…". ``secs``/``flags`` are echoed from the request (RFC 2131).
        """
        mac_b = self.str_to_mac(mac)
        hdr = struct.pack("!BBBB", 2, 1, 6, 0)           # op, htype=1(eth), hlen=6
        hdr += struct.pack(">I", int.from_bytes(xid, "big"))
        hdr += secs + flags                              # secs, flags (echoed)
        hdr += bytes(4)                                  # ciaddr
        hdr += socket.inet_aton(yiaddr or "0.0.0.0")     # yiaddr
        hdr += socket.inet_aton("0.0.0.0")               # siaddr
        hdr += bytes(4)                                  # giaddr
        hdr += mac_b + bytes(16 - len(mac_b))            # chaddr (16 bytes)
        hdr += bytes(64) + bytes(128)                    # sname + file

        opts = [(OPT_MSG_TYPE, bytes([msg_type]))]
        opts.append((OPT_SERVER_ID, socket.inet_aton(self.gateway)))
        if yiaddr:
            opts.append((OPT_LEASE_TIME,
                         struct.pack(">I", int(self.lease_hours * 3600))))
            opts.append((OPT_SUBNET_MASK, socket.inet_aton(self.subnet_mask)))
            opts.append((OPT_ROUTER, socket.inet_aton(self.gateway)))
            if self.advertise_self_dns:
                # Point clients at the PC so every DNS query crosses it (the
                # DNS forwarder on udp/53 answers and relays upstream).
                dns = [self.gateway]
            else:
                dns = getattr(self.cfg, "dns_servers", []) or []
            if dns:
                opts.append((OPT_DNS, b"".join(socket.inet_aton(x) for x in dns)))

        body = b"".join(bytes([code, len(val)]) + val for code, val in opts)
        body += b"\xff"  # END option (single byte)
        return hdr + BOOTP_MAGIC + body

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def _parse_options(data: bytes, offset: int) -> dict[int, bytes]:
        opts: dict[int, bytes] = {}
        while offset < len(data):
            code = data[offset]
            offset += 1
            if code == OPT_END:
                break
            if code == 0:  # padding
                continue
            if offset >= len(data):
                break
            length = data[offset]
            offset += 1
            opts[code] = data[offset:offset + length]
            offset += length
        return opts

    # ------------------------------------------------------------------ handle

    def handle(self, data: bytes, addr: tuple[str, int]) -> Optional[bytes]:
        """Parse one DHCP request; return the response datagram or None."""
        if len(data) < 240 or data[0] != 1:  # BOOTREQUEST only
            return None
        xid = data[4:8]
        secs = data[8:10]
        flags = data[10:12]
        ciaddr = socket.inet_ntoa(data[12:16])
        mac = self.mac_to_str(data[28:34])
        if data[236:240] != BOOTP_MAGIC:
            return None
        opts = self._parse_options(data, 240)
        msg_type = opts.get(OPT_MSG_TYPE, b"\x01")[0]

        if msg_type == MSG_DISCOVER:
            requested = opts.get(OPT_REQUESTED_IP, b"")
            req_ip = socket.inet_ntoa(requested) if len(requested) == 4 else ""
            # Sticky offers: re-offer the same IP to a MAC that keeps retrying
            # (a client that never got the previous offer would otherwise burn a
            # fresh pool address on every DISCOVER).
            try:
                offered = self._candidate(req_ip or self._offers.get(mac, ""))
            except RuntimeError:
                log.warning("DHCP pool exhausted for %s", mac)
                return None
            self._offers[mac] = offered
            log.info("DHCP DISCOVER %s -> offer %s (server %s, reply via %s)",
                     mac, offered, self.gateway, self.broadcast)
            return self._build(xid, offered, mac, MSG_OFFER, secs, flags)

        if msg_type == MSG_REQUEST:
            requested = opts.get(OPT_REQUESTED_IP, b"")
            req_ip = socket.inet_ntoa(requested) if len(requested) == 4 else ""
            # Prefer the requested IP, then our offer, then ciaddr.
            target = req_ip or self._offers.get(mac, "") or ciaddr
            if (target in self.pool and target not in self.reserved
                    and (target == req_ip or target == self._offers.get(mac, ""))):
                self._grant(mac, target)
                log.info("DHCP REQUEST %s -> ACK %s", mac, target)
                return self._build(xid, target, mac, MSG_ACK, secs, flags)
            # NAK: requested address is not ours.
            log.warning("DHCP REQUEST %s -> NAK %s", mac, target or "?")
            return self._build(xid, "", mac, MSG_NAK, secs, flags)

        if msg_type == MSG_RELEASE:
            log.info("DHCP RELEASE %s (was %s)", mac, self.leases.get(mac, "?"))
            self._release(mac)
            return None

        if msg_type == MSG_INFORM:
            # Client already has an address; ACK with no address options.
            return self._build(xid, "", mac, MSG_ACK, secs, flags)

        return None

    # ------------------------------------------------------------------ loop

    async def start(self) -> None:
        """Bind UDP 67 and serve until cancelled (needs Administrator).

        Raises ``PermissionError`` (not elevated) or ``OSError`` (bind failed)
        with a message specific to the failure, so callers can distinguish a
        privilege problem from a runtime failure.
        """
        loop = asyncio.get_running_loop()
        self._sock = make_dhcp_socket()
        try:
            self._sock.bind(("0.0.0.0", 67))
        except PermissionError:
            self._sock.close()
            self._sock = None
            raise PermissionError(
                "binding udp/67 requires Administrator privileges") from None
        except OSError as exc:
            self._sock.close()
            self._sock = None
            raise OSError(f"cannot bind udp/67: {exc}") from None
        self._sock.setblocking(False)
        log.info("DHCP server listening on udp/67 (pool %s..%s)",
                 self.pool[0] if self.pool else "?", self.pool[-1] if self.pool else "?")
        # Sanity check: the server-id / default gateway we advertise must be a
        # real address on THIS PC, or clients would end up pointing at a dead
        # gateway even after a successful handshake.
        local = detect_local_interface_ips()
        if self.gateway not in local:
            log.warning(
                "DHCP server-id %s is not an IP on this PC (%s) — clients would "
                "receive a default gateway that does not exist. Fix "
                "config.yaml dhcp.gateway_ip.", self.gateway, ", ".join(local))
        try:
            while True:
                data, addr = await loop.sock_recvfrom(self._sock, MAX_DGRAM)
                resp = self.handle(data, addr)
                if resp is not None:
                    # RFC 2131: unicast to the client's address if it has one,
                    # else broadcast on the served subnet (directed broadcast —
                    # reliably egresses the right NIC on Windows).
                    target = addr[0] if addr[0] != "0.0.0.0" else self.broadcast
                    await loop.sock_sendto(self._sock, resp, (target, REPLY_PORT))
        except asyncio.CancelledError:
            raise
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None
