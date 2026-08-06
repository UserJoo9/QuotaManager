"""ARP gateway-lock responder tests.

``ArpLock`` runs a background thread over a raw AF_PACKET socket. Here a fake
socket queues inbound ARP requests and records the replies ``send()`` receives,
so the "who has the router?" decision logic is exercised without root. The
gateway-lock must answer CLIENT-subnet requesters for the router's IP (with the
box's MAC), and ignore everything else.
"""

from __future__ import annotations

import ipaddress
import time

from core.config import Config
from quota.arp_lock import ArpLock
from quota.arp_scan import arp_request_frame, parse_arp_reply

ADDR_OUT = (
    "2: eth0    inet 192.168.1.110/24 brd 192.168.1.255 scope global eth0\\\n"
    "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 scope global eth0\\\n"
)
LINK_OUT = (
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
    "state UP mode DEFAULT group default qlen 1000\n"
    "    link/ether aa:bb:cc:dd:ee:01 brd ff:ff:ff:ff:ff:ff\n"
)


class FakeSocket:
    """Queues inbound ARP frames; records what the responder sent back."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[bytes] = []
        self._timeout: float | None = None

    def settimeout(self, t: float) -> None:
        self._timeout = t

    def recv(self, _n: int) -> bytes:
        if self._frames:
            return self._frames.pop(0)
        raise TimeoutError("simulated receive timeout")

    def send(self, frame: bytes) -> int:
        self.sent.append(frame)
        return len(frame)

    def close(self) -> None:
        pass


def _cfg() -> Config:
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    cfg.dhcp.gateway_ip = "192.168.2.1"
    cfg.dhcp.router_ip = "192.168.1.1"
    return cfg


def _run_cmd(argv):
    if argv == ["ip", "-o", "-4", "addr", "show"]:
        return 0, ADDR_OUT
    if argv == ["ip", "-o", "link", "show", "eth0"]:
        return 0, LINK_OUT
    return 1, ""


def _wait(pred, timeout: float = 2.0) -> bool:
    """Spin until ``pred`` is true (the responder thread consumed its input)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _consume(sock: FakeSocket) -> None:
    assert _wait(lambda: not sock._frames), "responder never read its input"


def test_answers_client_requester_for_router_ip():
    """A static-IP bypasser asks 'who has 192.168.1.1?' from the client subnet
    -> the box answers with ITS OWN MAC, claiming the router."""
    sock = FakeSocket([arp_request_frame("11:22:33:44:55:66", "192.168.2.50",
                                         "192.168.1.1")])
    lock = ArpLock(_cfg(), run_command=_run_cmd, socket_factory=lambda: sock)
    lock.start()
    _consume(sock)
    assert len(sock.sent) == 1
    lock.stop()
    ip, mac = parse_arp_reply(sock.sent[0])
    assert (ip, mac) == ("192.168.1.1", "aa:bb:cc:dd:ee:01")


def test_ignores_uplink_subnet_requester():
    """A NAS / static device on the UPLINK subnet must keep the real router —
    answering it would break the box's own uplink ARP."""
    sock = FakeSocket([arp_request_frame("11:22:33:44:55:66", "192.168.1.20",
                                         "192.168.1.1")])
    lock = ArpLock(_cfg(), run_command=_run_cmd, socket_factory=lambda: sock)
    lock.start()
    _consume(sock)
    lock.stop()
    assert sock.sent == []


def test_ignores_non_router_target():
    """A request about some other host is none of the box's business."""
    sock = FakeSocket([arp_request_frame("11:22:33:44:55:66", "192.168.2.50",
                                         "192.168.2.99")])
    lock = ArpLock(_cfg(), run_command=_run_cmd, socket_factory=lambda: sock)
    lock.start()
    _consume(sock)
    lock.stop()
    assert sock.sent == []


def test_ignores_own_announce():
    """A frame whose source MAC is the box itself must not be answered."""
    sock = FakeSocket([arp_request_frame("aa:bb:cc:dd:ee:01", "192.168.2.1",
                                         "192.168.1.1")])
    lock = ArpLock(_cfg(), run_command=_run_cmd, socket_factory=lambda: sock)
    lock.start()
    _consume(sock)
    lock.stop()
    assert sock.sent == []


def test_start_noop_without_router_ip():
    """No router to claim -> the responder never starts (no thread, no socket)."""
    cfg = _cfg()
    cfg.dhcp.router_ip = ""
    lock = ArpLock(cfg, run_command=_run_cmd, socket_factory=lambda: FakeSocket([]))
    lock.start()
    assert lock._thread is None
    lock.stop()  # safe even though nothing was started


def test_wan_mode_answers_client_requesters_only():
    """v18/19.5: under WAN topology the box keeps the uplink subnet as a
    router-admin alias, so ``resolve_local_networks`` reports BOTH subnets as
    local — but the lock's ANSWER scope is still the client subnet only (the
    network containing the box's client gateway). An uplink-subnet host must
    keep resolving the REAL router. The engine + run.py force the lock off
    under wan anyway; this pins the decision logic if it were ever started."""
    cfg = _cfg()
    cfg.engine.topology = "wan"
    lock = ArpLock(cfg, run_command=_run_cmd, socket_factory=lambda: FakeSocket([]))
    assert lock._client_network() == ipaddress.ip_network("192.168.2.0/24")
    assert lock._is_client("192.168.2.5") is True    # client subnet -> answered
    assert lock._is_client("192.168.1.5") is False   # uplink host keeps the router


def test_start_is_idempotent():
    sock = FakeSocket([])
    lock = ArpLock(_cfg(), run_command=_run_cmd, socket_factory=lambda: sock)
    lock.start()
    lock.start()  # a second start must be a no-op
    assert lock._thread is not None
    lock.stop()


def test_stop_without_start_is_safe():
    lock = ArpLock(_cfg(), run_command=_run_cmd, socket_factory=lambda: FakeSocket([]))
    lock.stop()  # must not raise
