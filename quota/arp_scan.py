"""Rogue static-IP detection for the Linux gateway.

The box sits on the same Layer-2 segment as the clients and the router, so it
can actively probe every host on the LAN subnets (client subnet + uplink
subnet). Any host that answers but is NOT a known DHCP device (not in the
dnsmasq lease file) is a rogue — typically a static-IP device pointing its
gateway at the router, which routes around the box entirely: never counted,
never blocked, invisible.

Two probe backends:
  * raw AF_PACKET ARP requests (default; fast, precise, needs root)
  * ``ip neigh`` after a broadcast ping sweep (no raw sockets; slower)

Both degrade gracefully: if probing is impossible the scanner returns an empty
list and the rest of the app keeps working (mirrors the engine's degradation).

The frame-build/parse helpers here are shared with the ARP gateway-lock
responder (quota/arp_lock.py), which claims the router's IP on the client
subnet so a bypasser's frames arrive at the box where the engine drops them.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import struct
from typing import Callable

from quota.engine import RogueHost
from quota.nftables import resolve_local_networks
from quota.vendor import vendor_for

log = logging.getLogger("quota.arp_scan")

RunCommand = Callable[[list[str]], tuple[int, str]]
#: probe(networks, iface, box_mac, box_ips) -> {ip: mac}
Probe = Callable[[list[str], str, str, set[str]], dict[str, str]]

ETH_P_ARP = 0x0806


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def _normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def _mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(_normalize_mac(mac).replace(":", ""))


def _mac_to_str(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


def arp_request_frame(src_mac: str, src_ip: str, dst_ip: str) -> bytes:
    """Broadcast Ethernet frame carrying an ARP *request* for ``dst_ip``."""
    smac = _mac_to_bytes(src_mac)
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
    arp += smac + socket.inet_aton(src_ip)
    arp += b"\x00" * 6 + socket.inet_aton(dst_ip)
    return b"\xff" * 6 + smac + struct.pack("!H", ETH_P_ARP) + arp


def arp_reply_frame(src_mac: str, src_ip: str, dst_mac: str, dst_ip: str) -> bytes:
    """Unicast Ethernet frame carrying an ARP *reply* claiming ``src_ip``."""
    smac = _mac_to_bytes(src_mac)
    dmac = _mac_to_bytes(dst_mac)
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2)
    arp += smac + socket.inet_aton(src_ip)
    arp += dmac + socket.inet_aton(dst_ip)
    return dmac + smac + struct.pack("!H", ETH_P_ARP) + arp


def parse_arp_reply(frame: bytes) -> tuple[str, str] | None:
    """Return ``(ip, mac)`` when ``frame`` is an ARP reply, else None."""
    if len(frame) < 42 or frame[12:14] != struct.pack("!H", ETH_P_ARP):
        return None
    arp = frame[14:]
    op = struct.unpack("!H", arp[6:8])[0]
    if op != 2:
        return None
    return socket.inet_ntoa(arp[14:18]), _mac_to_str(arp[8:14])


def parse_arp_request(frame: bytes) -> tuple[str, str, str] | None:
    """Return ``(spa, sha, tpa)`` when ``frame`` is an ARP request, else None."""
    if len(frame) < 42 or frame[12:14] != struct.pack("!H", ETH_P_ARP):
        return None
    arp = frame[14:]
    op = struct.unpack("!H", arp[6:8])[0]
    if op != 1:
        return None
    spa = socket.inet_ntoa(arp[14:18])
    sha = _mac_to_str(arp[8:14])
    tpa = socket.inet_ntoa(arp[24:28])
    return spa, sha, tpa


def _box_ip_in(net: ipaddress.IPv4Network, box_ips: set[str],
               fallback: str) -> str:
    """A box IP that lives in ``net`` (for the ARP sender field), else fallback."""
    for ip in box_ips:
        try:
            if ipaddress.ip_address(ip) in net:
                return ip
        except ValueError:
            continue
    return fallback


def resolve_nic(client_ip: str, run_command: RunCommand) -> tuple[str, str, set[str]] | None:
    """Find the NIC owning the client-subnet address and return ``(iface, mac, ips)``.

    Uses ``ip`` (iproute2) so the box never has to be told which NIC is the LAN
    one — it is the NIC that owns ``dhcp.gateway_ip``. Returns None when the NIC
    cannot be resolved (scan/responder then degrade to a no-op).
    """
    code, out = run_command(["ip", "-o", "-4", "addr", "show"])
    if code != 0:
        log.warning("cannot resolve LAN NIC (`ip -o -4 addr show` failed) — "
                    "rogue scan disabled")
        return None
    iface = ""
    box_ips: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        # 2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 scope global eth0\ ...
        if len(parts) < 4 or parts[2] != "inet":
            continue
        ip = parts[3].split("/")[0]
        box_ips.add(ip)
        if ip == client_ip:
            iface = parts[1].rstrip(":")
    if not iface:
        log.warning("no NIC owns the client gateway %s — rogue scan disabled",
                    client_ip)
        return None
    mac = ""
    code, out = run_command(["ip", "-o", "link", "show", iface])
    if code == 0:
        m = re.search(r"link/ether ([0-9a-f:]+)", out)
        if m:
            mac = _normalize_mac(m.group(1))
    if not mac:
        log.warning("cannot read the MAC of %s — rogue scan disabled", iface)
        return None
    return iface, mac, box_ips


class ArpScanner:
    """Probe both LAN subnets and report active hosts that are NOT DHCP devices.

    Constructed once at startup; :meth:`scan` is called off the event loop
    (``asyncio.to_thread``) on a slow cadence (60 s — not the 15 s tick).
    """

    def __init__(self, cfg, run_command: RunCommand | None = None,
                 probe: Probe | None = None) -> None:
        self._run = run_command or _default_run_command
        self._probe = probe or self._default_probe
        dhcp = getattr(cfg, "dhcp", None)
        self._client_ip = getattr(dhcp, "gateway_ip", "") if dhcp else ""
        self._router_ip = getattr(dhcp, "router_ip", "") if dhcp else ""
        # wan_client_only: in WAN mode the router is bridged and only the client
        # subnet is masqueraded, so the uplink subnet (the box's admin alias +
        # the router's own IP) can't hold a quota-bypass host — probing it would
        # just flag the bridged router as a false rogue.
        self._networks = resolve_local_networks(getattr(cfg, "engine", None), dhcp,
                                                wan_client_only=True)

    def _default_probe(self, networks: list[str], iface: str, box_mac: str,
                       box_ips: set[str]) -> dict[str, str]:
        found = self._raw_probe(networks, iface, box_mac, box_ips)
        if found is not None:
            return found
        # No raw sockets (no root / non-Linux) — slower ping-sweep fallback.
        return self._neigh_probe(networks, iface, box_mac, box_ips)

    def _raw_probe(self, networks: list[str], iface: str, box_mac: str,
                   box_ips: set[str]) -> dict[str, str] | None:
        """Send an ARP request to every host; collect replies.

        Returns None when raw sockets are unavailable (caller falls back), else
        the ``{ip: mac}`` map (possibly empty when the segment is quiet).
        """
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                 socket.htons(ETH_P_ARP))
        except (AttributeError, OSError):
            log.warning("cannot open a raw ARP socket (root?) — rogue scan "
                        "falls back to a ping sweep")
            return None
        found: dict[str, str] = {}
        try:
            sock.bind((iface, 0))
            targets: list[tuple[str, str]] = []
            for net_str in networks:
                net = ipaddress.ip_network(net_str, strict=False)
                src = _box_ip_in(net, box_ips, self._client_ip)
                for ip in net.hosts():
                    s = str(ip)
                    if s in box_ips or s == str(net.broadcast_address):
                        continue
                    targets.append((s, src))
            for ip, src in targets:
                try:
                    sock.send(arp_request_frame(box_mac, src, ip))
                except OSError:
                    break
            # Read replies until the wire goes quiet for ~0.8 s total.
            sock.settimeout(0.4)
            quiet = 0
            while quiet < 2:
                try:
                    frame = sock.recv(2048)
                except socket.timeout:
                    quiet += 1
                    continue
                except OSError:
                    break
                quiet = 0
                parsed = parse_arp_reply(frame)
                if parsed:
                    found[parsed[0]] = parsed[1]
        finally:
            sock.close()
        return found

    def _neigh_probe(self, networks: list[str], iface: str, box_mac: str,
                     box_ips: set[str]) -> dict[str, str]:
        """Concurrent ping sweep to populate the kernel neighbor table, then
        read it back with ``ip -j neigh show``."""
        import concurrent.futures
        import json

        def ping(ip: str) -> None:
            self._run(["ping", "-c", "1", "-W", "1", "-n", "-q", ip])

        hosts: list[str] = []
        for net_str in networks:
            net = ipaddress.ip_network(net_str, strict=False)
            for ip in net.hosts():
                s = str(ip)
                if s in box_ips or s == str(net.broadcast_address):
                    continue
                hosts.append(s)
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(ping, hosts))
        code, out = self._run(["ip", "-j", "neigh", "show", "dev", iface])
        found: dict[str, str] = {}
        if code != 0:
            return found
        try:
            data = json.loads(out)
        except ValueError:
            return found
        for entry in data:
            mac = entry.get("lladdr")
            ip = entry.get("dst")
            if mac and ip:
                found[ip] = _normalize_mac(mac)
        return found

    def scan(self, known_macs: set[str]) -> list[RogueHost]:
        """Return active hosts on the LAN that are NOT known DHCP devices.

        ``known_macs`` is the set of MACs with a dnsmasq lease. The box's own
        addresses and the router's IP are never reported.
        """
        if not self._networks:
            return []
        nic = resolve_nic(self._client_ip, self._run)
        if nic is None:
            return []
        iface, box_mac, box_ips = nic
        found = self._probe(self._networks, iface, box_mac, box_ips)
        excluded_ips = set(box_ips)
        if self._router_ip:
            excluded_ips.add(self._router_ip)
        rogues: list[RogueHost] = []
        for ip, mac in sorted(found.items()):
            mac = _normalize_mac(mac)
            if mac in known_macs or mac == box_mac:
                continue
            if ip in excluded_ips:
                continue
            rogues.append(RogueHost(ip=ip, mac=mac, vendor=vendor_for(mac),
                                    online=True))
        return rogues
