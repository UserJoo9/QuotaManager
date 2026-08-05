"""Tests for the DNS forwarder (quota/dns.py).

The forwarder normally binds udp/53 (needs Administrator), so these tests bind
it on an ephemeral localhost port and point it at a stub upstream server on a
high port — no privileges required. They exercise the real relay path: a client
query datagram in, an upstream-answered reply datagram out.
"""

from __future__ import annotations

import asyncio
import socket
import threading

from quota import dns as dns_mod


def _make_query(name: str = "example.com") -> bytes:
    """Minimal valid DNS query: id=0x1234, RD=1, one question, no answers."""
    header = b"\x12\x34" + b"\x01\x00" + b"\x00\x01\x00\x00\x00\x00\x00\x00"
    qname = b"".join(bytes([len(part)]) + part.encode()
                     for part in name.split(".")) + b"\x00"
    return header + qname + b"\x00\x01\x00\x01"  # A, IN


class StubUpstream:
    """UDP server that answers any query by echoing it with the QR bit set."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.received: list[bytes] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self.received.append(data)
            reply = data[:2] + b"\x81\x80" + data[4:]  # set QR, echo rest
            try:
                self.sock.sendto(reply, addr)
            except OSError:
                break

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def test_forwarder_relays_query_and_reply():
    """A client query is forwarded to the upstream and its reply comes back."""
    upstream = StubUpstream()
    fwd = dns_mod.DnsForwarder(
        upstreams=["127.0.0.1"], upstream_port=upstream.port,
        bind_host="127.0.0.1", bind_port=0, timeout_sec=1.0)
    loop = asyncio.new_event_loop()
    task = loop.create_task(fwd.start())
    try:
        loop.run_until_complete(asyncio.sleep(0.05))  # let it bind
        assert fwd.port, "forwarder must be bound"

        query = _make_query()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.setblocking(False)

        async def _exchange() -> bytes:
            # The client's send+recv must run inside the loop: while the client
            # awaits a reply, the loop is free to run the forwarder's relay task.
            await loop.sock_sendto(client, query, ("127.0.0.1", fwd.port))
            reply, _ = await asyncio.wait_for(
                loop.sock_recvfrom(client, 4096), timeout=3.0)
            return reply

        try:
            reply = loop.run_until_complete(_exchange())
        finally:
            client.close()

        # The client's transaction id must be preserved end to end...
        assert reply[0:2] == query[0:2], "transaction id must be preserved"
        # ...and the QR bit must be set (the upstream answered).
        assert reply[2] & 0x80, "reply must be a response (QR=1)"
        # The upstream really did receive our (forwarded) query.
        assert any(q[0:2] == query[0:2] for q in upstream.received)
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()
        upstream.close()


def test_forwarder_ignores_incoming_responses():
    """A datagram with the QR bit set is not treated as a client query."""
    upstream = StubUpstream()
    fwd = dns_mod.DnsForwarder(
        upstreams=["127.0.0.1"], upstream_port=upstream.port,
        bind_host="127.0.0.1", bind_port=0, timeout_sec=0.3)
    loop = asyncio.new_event_loop()
    task = loop.create_task(fwd.start())
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
        assert fwd.port
        # A response-shaped datagram (QR=1) must not be relayed upstream.
        resp = b"\x12\x34\x81\x80" + b"\x00" * 8
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.5)
        try:
            client.sendto(resp, ("127.0.0.1", fwd.port))
        finally:
            client.close()
        loop.run_until_complete(asyncio.sleep(0.1))
        assert not upstream.received, "QR=1 datagrams must not be forwarded"
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()
        upstream.close()


def test_forwarder_skips_dead_upstream():
    """An unreachable upstream (closed port) must not crash the forwarder."""
    fwd = dns_mod.DnsForwarder(
        upstreams=["127.0.0.1"], upstream_port=9,  # discard port: nothing there
        bind_host="127.0.0.1", bind_port=0, timeout_sec=0.3)
    loop = asyncio.new_event_loop()
    task = loop.create_task(fwd.start())
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
        assert fwd.port
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.5)
        try:
            client.sendto(_make_query(), ("127.0.0.1", fwd.port))
        finally:
            client.close()
        loop.run_until_complete(asyncio.sleep(0.4))
        # No exception, server still accepting queries.
        assert fwd._sock is not None
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()
