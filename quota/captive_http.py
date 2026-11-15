"""Plain-HTTP side door for the guest portal while the dashboard runs HTTPS.

With HTTPS enforced, uvicorn speaks TLS on the dashboard port. A pending
guest's port-80 traffic is redirected there by nftables, but a browser (or the
phone's "sign in to network" probe) talking plain HTTP to a TLS socket just sees
a dropped connection, so the "waiting for approval" page never shows.

This module runs a SECOND, plain-HTTP listener that exposes ONLY the portal:

* the portal page, its status API, its static assets and the OS captive probes
  are passed to the real app;
* a pending guest's other requests are passed too — the app's own middleware
  answers them with a redirect to the portal and never reaches a handler;
* everything else (the dashboard, the login, the API) is redirected to HTTPS,
  so nothing sensitive is ever served in clear text;
* callers from outside private networks are refused.
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

PORTAL_PREFIXES = ("/guest/pending", "/api/guest/status", "/assets/")
PORTAL_PATHS = frozenset({
    "/favicon.ico", "/generate_204", "/gen_204", "/hotspot-detect.html",
    "/connecttest.txt", "/ncsi.txt", "/canonical.html",
})


def is_portal_path(path: str) -> bool:
    return path in PORTAL_PATHS or path.startswith(PORTAL_PREFIXES)


def _is_local(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


def _hostname(host_header: str, fallback: str) -> str:
    """Host header without its port ("box:8081" -> "box", "[::1]:80" -> "[::1]")."""
    host = (host_header or "").strip()
    if not host:
        return fallback
    if host.startswith("["):
        return host[:host.index("]") + 1] if "]" in host else fallback
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    # Only a plain name/IPv4 goes into a Location header.
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in name):
        return fallback
    return name


class CaptiveHttpApp:
    """ASGI wrapper: portal only over plain HTTP, the rest goes to HTTPS."""

    def __init__(self, app: Any, is_pending: Callable[[str], bool], https_port: int) -> None:
        self._app = app
        self._is_pending = is_pending
        self._https_port = int(https_port)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        kind = scope.get("type")
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            # lifespan: the side server runs with lifespan off; hand anything
            # else (a test harness) to the app unchanged.
            await self._app(scope, receive, send)
            return
        client = scope.get("client") or ("", 0)
        ip = str(client[0] or "")
        if not _is_local(ip):
            await self._plain(send, 403, b"forbidden")
            return
        path = scope.get("path") or "/"
        pending = False
        try:
            pending = bool(self._is_pending(ip))
        except Exception:  # noqa: BLE001 — never break the portal on a lookup error
            log.debug("pending-guest lookup failed", exc_info=True)
        if pending or is_portal_path(path):
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        server = scope.get("server") or ("", 0)
        name = _hostname(headers.get("host", ""), str(server[0] or "localhost"))
        raw = scope.get("raw_path") or path.encode("utf-8")
        target = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        query = scope.get("query_string") or b""
        if query:
            target += "?" + query.decode("latin-1")
        location = f"https://{name}:{self._https_port}{target}"
        await send({"type": "http.response.start", "status": 307,
                    "headers": [(b"location", location.encode("latin-1", "replace")),
                                (b"content-length", b"0"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _plain(send: Callable, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def bind_socket(host: str, port: int) -> Optional[socket.socket]:
    """Bind the side-door port up front. Returns None when it cannot be bound
    (port taken, no permission) — the gateway then simply runs without it."""
    family = socket.AF_INET6 if ":" in (host or "") else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host or "0.0.0.0", int(port)))
        sock.setblocking(False)
        return sock
    except OSError as e:
        log.warning("guest portal HTTP port %s unavailable (%s) — portal stays HTTPS-only", port, e)
        with contextlib.suppress(OSError):
            sock.close()
        return None


def make_server(app: Any, is_pending: Callable[[str], bool], https_port: int) -> Any:
    """A uvicorn server for the side door. It must not touch process signals or
    run the app's startup/shutdown a second time — the main server owns both."""
    import uvicorn

    class _SideServer(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # older uvicorn
            return None

        @contextlib.contextmanager
        def capture_signals(self):  # newer uvicorn
            yield

    config = uvicorn.Config(CaptiveHttpApp(app, is_pending, https_port),
                            log_level="warning", lifespan="off",
                            timeout_graceful_shutdown=2)
    return _SideServer(config)
