"""Plain-HTTP guest-portal side door used while the dashboard runs HTTPS."""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Config
from quota import captive_http
from quota import db as _db
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.nftables import NftablesEngine
from quota.service import QuotaService
from tests.test_nftables import FakeNft

TZ = ZoneInfo("Africa/Cairo")
GUEST = "192.168.2.55"
ADMIN = "192.168.2.10"


_cached_loop = None


def _get_loop():
    """One event loop for the whole module, never closed - the same pattern as
    test_guest_captive. aiosqlite binds its worker thread to the loop that
    opened the connection; closing that loop (or awaiting database.close() on
    it after TestClient ran the app on its own loop) can hang the teardown."""
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
    return _cached_loop


@pytest.fixture
def side(tmp_path):
    loop = _get_loop()
    database = _db.Database(tmp_path / "side.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()
    loop.run_until_complete(database.connect())
    loop.run_until_complete(service.ensure_period())
    holder.swap(EngineSnapshot(pending_guests={GUEST},
                               ip_to_mac={GUEST: "11:22:33:44:55:66"},
                               blocked={"11:22:33:44:55:66": True}))
    app = create_app(database=database, service=service, holder=holder)
    wrapped = captive_http.CaptiveHttpApp(
        app, lambda ip: ip in holder.get().pending_guests, 8080)
    return wrapped, holder


def _client(wrapped, ip, host="192.168.2.1:8081"):
    return TestClient(wrapped, base_url=f"http://{host}", client=(ip, 45000))


def test_pending_guest_gets_the_portal_over_plain_http(side):
    wrapped, _ = side
    with _client(wrapped, GUEST) as c:
        r = c.get("/guest/pending")
        assert r.status_code == 200 and "Awaiting Administrator Approval" in r.text
        assert c.get("/api/guest/status").json()["blocked"] is True
        # any site the guest opens (port 80 -> side door) lands on the portal, in http
        r = c.get("/search?q=x", headers={"host": "example.com"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("http://example.com/guest/pending?continue=")
        # OS "sign in to network" probes
        for probe in ("/generate_204", "/hotspot-detect.html", "/connecttest.txt", "/canonical.html"):
            r = c.get(probe, follow_redirects=False)
            assert r.status_code == 302 and r.headers["location"].startswith("http://"), probe


def test_pending_guest_cannot_reach_the_dashboard_or_login_in_clear(side):
    wrapped, _ = side
    with _client(wrapped, GUEST) as c:
        for path in ("/", "/api/dashboard", "/js/main.js"):
            assert c.get(path, follow_redirects=False).status_code == 302, path
        r = c.post("/api/login", json={"password": "x"}, follow_redirects=False)
        assert r.status_code == 302 and "/guest/pending" in r.headers["location"]


def test_everyone_else_is_sent_to_https(side):
    wrapped, _ = side
    with _client(wrapped, ADMIN) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "https://192.168.2.1:8080/"
        r = c.get("/api/dashboard?x=1", follow_redirects=False)
        assert r.headers["location"] == "https://192.168.2.1:8080/api/dashboard?x=1"
        r = c.post("/api/login", json={"password": "x"}, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].startswith("https://")
        # the portal page itself stays readable (an approved guest finishing up)
        assert c.get("/guest/pending").status_code == 200


def test_redirect_never_follows_a_hostile_host_or_path(side):
    wrapped, _ = side
    with _client(wrapped, ADMIN) as c:
        r = c.get("/x", headers={"host": "evil.com/@a b"}, follow_redirects=False)
        assert r.status_code == 307
        assert "evil.com/@" not in r.headers["location"]
    assert captive_http._hostname("box:8081", "f") == "box"
    assert captive_http._hostname("192.168.2.1", "f") == "192.168.2.1"
    assert captive_http._hostname("", "f") == "f"


def test_callers_from_outside_private_networks_are_refused(side):
    wrapped, _ = side
    with _client(wrapped, "8.8.8.8") as c:
        assert c.get("/guest/pending").status_code == 403
        assert c.get("/").status_code == 403


def test_bind_socket_reports_a_taken_port_instead_of_raising():
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        assert captive_http.bind_socket("127.0.0.1", busy.getsockname()[1]) is None
    finally:
        busy.close()
    free = captive_http.bind_socket("127.0.0.1", 0)
    assert free is not None
    free.close()


def test_side_server_serves_real_http_and_leaves_signals_alone(side):
    """The real uvicorn side server on a real socket: portal page for the guest
    path, HTTPS redirect for the dashboard, and no signal handler changes."""
    import signal
    wrapped, _ = side
    before = signal.getsignal(signal.SIGINT)

    async def run() -> tuple[bytes, bytes]:
        sock = captive_http.bind_socket("127.0.0.1", 0)
        assert sock is not None
        port = sock.getsockname()[1]
        server = captive_http.make_server(wrapped._app, lambda ip: False, 8080)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)

            async def get(path: str) -> bytes:
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
                await w.drain()
                data = await asyncio.wait_for(r.read(), 10)
                w.close()
                return data
            return await get("/guest/pending"), await get("/")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)

    loop = asyncio.new_event_loop()
    try:
        page, root = loop.run_until_complete(run())
    finally:
        loop.close()
    assert page.startswith(b"HTTP/1.1 200") and b"Awaiting Administrator Approval" in page
    assert root.startswith(b"HTTP/1.1 307") and b"location: https://127.0.0.1:8080/" in root.lower()
    assert signal.getsignal(signal.SIGINT) is before


def test_nftables_redirects_pending_guests_to_the_side_door():
    for portal, expected in ((None, ":8080"), (8081, ":8081")):
        fake = FakeNft()
        cfg = Config()
        cfg.engine.client_subnet = "192.168.2.0/24"
        cfg.engine.uplink_subnet = "192.168.1.0/24"
        eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
        eng.portal_port = portal
        eng.start()
        assert f"ip saddr @pending_guests tcp dport 80 redirect to {expected}" in " | ".join(fake.rules)
