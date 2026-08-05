"""Minimal DNS forwarder (UDP/53) for the one-armed gateway.

Android and iOS treat the default gateway as a DNS resolver when they cannot
validate a network (and captive-portal probing always queries the gateway on
port 53 / 80 / 853). For this gateway the default gateway IS the PC, so a
device's very first step — resolving ``connectivitycheck.gstatic.com`` — goes
to the PC's UDP/53. With no service there, every query is silently dropped, no
hostname ever resolves, and every device reports "connected, no internet".

This forwarder makes the PC answer DNS for clients by relaying each query to
the upstream resolvers from config (default 8.8.8.8) and returning the reply
unchanged. The raw datagram is relayed untouched — the client's transaction ID
lives inside the packet, so the upstream answers it and we relay that answer
back verbatim. No DNS parsing, no ID rewriting.

Needs Administrator to bind UDP/53, same as the DHCP server.
"""

from __future__ import annotations

import asyncio
import logging
import socket

log = logging.getLogger("quota.dns")

MAX_DGRAM = 4096
REPLY_TIMEOUT = 3.0


class DnsForwarder:
    """Async UDP/53 server that relays client DNS queries to upstream servers.

    One listen socket on 53; each query gets a short-lived ephemeral socket to
    the upstream so replies can never be misrouted to another client (transaction
    IDs are not unique across clients). Tries upstreams in order until one
    answers, then relays the reply back to the client from the listen socket.
    """

    def __init__(
        self,
        upstreams: list[str],
        bind_host: str = "0.0.0.0",
        bind_port: int = 53,
        upstream_port: int = 53,
        timeout_sec: float = REPLY_TIMEOUT,
    ) -> None:
        self.upstreams = [u for u in upstreams if u]
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.upstream_port = upstream_port
        self.timeout_sec = timeout_sec
        self._sock: socket.socket | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def port(self) -> int:
        """The port actually bound (useful when bind_port=0 in tests)."""
        return self._sock.getsockname()[1] if self._sock else 0

    async def start(self) -> None:
        """Bind UDP/53 and serve until cancelled (needs Administrator).

        Raises ``PermissionError`` (not elevated) or ``OSError`` (bind failed)
        so callers can degrade gracefully, mirroring the DHCP server.
        """
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)
        try:
            sock.bind((self.bind_host, self.bind_port))
        except PermissionError:
            sock.close()
            raise PermissionError(
                "binding udp/53 requires Administrator privileges") from None
        except OSError as exc:
            sock.close()
            raise OSError(f"cannot bind udp/53: {exc}") from None
        self._sock = sock
        log.info("DNS forwarder listening on udp/%s (upstream: %s)",
                 self.port, ", ".join(self.upstreams) or "NONE")
        try:
            while True:
                data, addr = await loop.sock_recvfrom(self._sock, MAX_DGRAM)
                if len(data) < 12:
                    continue  # not a DNS datagram
                if data[2] & 0x80:
                    continue  # QR=1: it's a response, not a query — ignore
                task = asyncio.create_task(self._forward(data, addr))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except asyncio.CancelledError:
            raise
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    async def _forward(self, query: bytes, client_addr: tuple[str, int]) -> None:
        """Relay one query to the first upstream that answers, then reply."""
        loop = asyncio.get_running_loop()
        for upstream in self.upstreams:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(False)
            try:
                await loop.sock_sendto(sock, query, (upstream, self.upstream_port))
                reply, _ = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, MAX_DGRAM), timeout=self.timeout_sec)
                if self._sock is not None:
                    await loop.sock_sendto(self._sock, reply, client_addr)
                return
            except (asyncio.TimeoutError, OSError):
                continue  # try the next upstream
            finally:
                sock.close()
        log.warning("all DNS upstreams unreachable for %s", client_addr[0])

    async def stop(self) -> None:
        """Cancel in-flight relays and close the listen socket."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._sock is not None:
            self._sock.close()
            self._sock = None
