"""Continuous ARP gateway-lock responder (Linux, opt-in).

When a device sets its gateway to the ROUTER instead of this box (a static-IP
bypass), its traffic goes straight to the router at Layer 2 and the box never
sees a byte — no accounting, no blocking, invisible. The gateway-lock closes
that: a small background thread claims the ROUTER's IP on the CLIENT subnet.
It answers ARP requests for ``router_ip`` from client-subnet hosts with the
box's own MAC, so the bypasser's frames arrive at the box — where the engine's
forward deny rule (``quota/nftables.py``, ``engine.gateway_arp_lock``) drops
them. The cheat stops working until the device uses the box as its gateway.

Why the deny is self-sustaining: the box drops the bypasser's traffic, so its
ARP cache entry for the router goes stale, so it re-asks "who has <router>?"
before its next packet, and the responder answers again. No periodic
gratuitous ARP is sent, so legitimate uplink-subnet hosts (NAS, router) keep
resolving the router normally.

Only requesters in the client subnet are answered — an uplink-subnet static-IP
device is NOT captured (surfaced by the scanner instead; the README pushes the
router-side MAC allowlist for those). The lock is opt-in via
``engine.gateway_arp_lock`` and degrades to a no-op without raw sockets.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from typing import Any, Callable

from quota.arp_scan import (
    ETH_P_ARP,
    RunCommand,
    _default_run_command,
    arp_reply_frame,
    parse_arp_request,
    resolve_nic,
)
from quota.nftables import resolve_local_networks

log = logging.getLogger("quota.arp_lock")

#: socket() callable; injectable for tests (returns a fake bound socket).
SocketFactory = Callable[[], Any]


def _open_raw_socket() -> socket.socket:
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                         socket.htons(ETH_P_ARP))
    return sock


class ArpLock:
    """Background thread that claims ``dhcp.router_ip`` on the client subnet.

    Call :meth:`start` once at gateway startup and :meth:`stop` on shutdown.
    Construction is cheap; nothing touches the network until :meth:`start`.
    """

    #: how long the loop waits for an ARP frame before re-checking stop
    RECV_TIMEOUT = 0.5

    def __init__(self, cfg, run_command: RunCommand | None = None,
                 socket_factory: SocketFactory | None = None) -> None:
        self._run = run_command or _default_run_command
        self._socket_factory = socket_factory or _open_raw_socket
        dhcp = getattr(cfg, "dhcp", None)
        self._client_ip = getattr(dhcp, "gateway_ip", "") if dhcp else ""
        self._router_ip = getattr(dhcp, "router_ip", "") if dhcp else ""
        self._networks = resolve_local_networks(getattr(cfg, "engine", None), dhcp)
        self._iface = ""
        self._box_mac = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _client_network(self) -> ipaddress.IPv4Network | None:
        """The local network that contains the box's client gateway address.

        Only hosts on THIS network are answered — an uplink-subnet host (NAS,
        the router itself) must keep resolving the real router.
        """
        for net_str in self._networks:
            try:
                net = ipaddress.ip_network(net_str, strict=False)
            except ValueError:
                continue
            try:
                if ipaddress.ip_address(self._client_ip) in net:
                    return net
            except ValueError:
                continue
        return None

    def _is_client(self, ip: str) -> bool:
        net = self._client_network()
        if net is None:
            return False
        try:
            return ipaddress.ip_address(ip) in net
        except ValueError:
            return False

    def start(self) -> None:
        if self._thread is not None or not self._router_ip:
            return
        nic = resolve_nic(self._client_ip, self._run)
        if nic is None:
            log.warning("arp-lock disabled: cannot resolve the LAN NIC")
            return
        self._iface, self._box_mac, _ = nic
        self._thread = threading.Thread(target=self._loop, name="arp-lock",
                                        daemon=True)
        self._thread.start()
        log.info("arp-lock active: claiming %s on %s (client subnet %s)",
                 self._router_ip, self._iface,
                 self._networks or self._client_ip)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        try:
            sock = self._socket_factory()
        except OSError as exc:
            log.warning("arp-lock disabled: cannot open a raw ARP socket: %s", exc)
            return
        try:
            sock.settimeout(self.RECV_TIMEOUT)
            while not self._stop.is_set():
                try:
                    frame = sock.recv(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                req = parse_arp_request(frame)
                if req is None:
                    continue
                spa, sha, tpa = req
                if tpa != self._router_ip:
                    continue          # not asking about the router
                if not self._is_client(spa):
                    continue          # uplink hosts keep the real router
                if sha == self._box_mac:
                    continue          # our own announce
                try:
                    sock.send(arp_reply_frame(self._box_mac, self._router_ip,
                                              sha, spa))
                except OSError:
                    continue
                log.debug("arp-lock: answered %s for %s", spa, self._router_ip)
        finally:
            sock.close()
