"""Tests for Guest Captive Portal & Search Redirection."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Config
from quota import db as _db
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.nftables import NftablesEngine
from quota.service import QuotaService
from tests.test_nftables import FakeNft

TZ = ZoneInfo("Africa/Cairo")


_cached_loop = None
def _get_loop():
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop


@pytest.fixture
def captive_env(tmp_path):
    """Set up database, service, holder, and app for captive tests."""
    database = _db.Database(tmp_path / "captive.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()

    _get_loop().run_until_complete(database.connect())
    _get_loop().run_until_complete(service.ensure_period())

    app = create_app(
        database=database,
        service=service,
        holder=holder,
    )
    return database, service, holder, app


def test_guest_pending_page_serves_html(captive_env):
    database, service, holder, app = captive_env
    with TestClient(app) as client:
        r = client.get("/guest/pending")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "بانتظار موافقة المسؤول" in r.text
        assert "Awaiting Administrator Approval" in r.text
        assert "Quota Manager" in r.text


def test_captive_probes_redirect_pending_guests(captive_env):
    database, service, holder, app = captive_env

    # 1. Simulate pending guest with IP 192.168.2.55
    holder.swap(EngineSnapshot(
        pending_guests={"192.168.2.55"},
        ip_to_mac={"192.168.2.55": "11:22:33:44:55:66"},
        blocked={"11:22:33:44:55:66": True},
    ))

    # Client connecting from the pending guest IP
    with TestClient(app, client=("192.168.2.55", 45000)) as guest_client:
        # Android probe
        r = guest_client.get("/generate_204", follow_redirects=False)
        assert r.status_code == 302
        assert "/guest/pending" in r.headers["location"]

        r = guest_client.get("/gen_204", follow_redirects=False)
        assert r.status_code == 302

        # Apple probe
        r = guest_client.get("/hotspot-detect.html", follow_redirects=False)
        assert r.status_code == 302
        assert "/guest/pending" in r.headers["location"]

        # Windows probe
        r = guest_client.get("/connecttest.txt", follow_redirects=False)
        assert r.status_code == 302

        # Firefox probe
        r = guest_client.get("/canonical.html", follow_redirects=False)
        assert r.status_code == 302

    # 2. Approved/regular client connecting from 192.168.2.99
    with TestClient(app, client=("192.168.2.99", 45000)) as approved_client:
        r = approved_client.get("/generate_204")
        assert r.status_code == 204

        r = approved_client.get("/hotspot-detect.html")
        assert r.status_code == 200
        assert "<TITLE>Success</TITLE>" in r.text

        r = approved_client.get("/connecttest.txt")
        assert r.status_code == 200
        assert "Microsoft Connect Test" in r.text


def test_pending_guest_search_interception_middleware(captive_env):
    database, service, holder, app = captive_env

    holder.swap(EngineSnapshot(
        pending_guests={"192.168.2.77"},
        ip_to_mac={"192.168.2.77": "aa:bb:cc:dd:ee:77"},
        blocked={"aa:bb:cc:dd:ee:77": True},
    ))

    with TestClient(app, client=("192.168.2.77", 45000)) as guest_client:
        # When a guest attempts any web search or external URL
        r = guest_client.get("/search?q=football", follow_redirects=False)
        assert r.status_code == 302
        assert "/guest/pending" in r.headers["location"]
        assert "continue=" in r.headers["location"]

        # Directly visiting root
        r = guest_client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/guest/pending" in r.headers["location"]

        # Whitelisted endpoints must pass freely
        r_page = guest_client.get("/guest/pending")
        assert r_page.status_code == 200

        r_status = guest_client.get("/api/guest/status")
        assert r_status.status_code == 200

        r_asset = guest_client.get("/assets/styles.css")
        assert r_asset.status_code == 200


def test_guest_status_api_and_admin_approval_lifecycle(captive_env):
    database, service, holder, app = captive_env

    async def _setup_guest():
        # Register a new guest device awaiting admin approval
        dev = await database.upsert_device(
            "aa:bb:cc:dd:ee:88", name="GuestPhone", quota_mode=_db.QUOTA_FIXED,
            fixed_gb=0.0, guest=True
        )
        await database.set_device_state(dev.id, _db.BLOCK_ADMIN)
        if dev.user_id:
            await database.update_user(dev.user_id, block_state=_db.BLOCK_ADMIN)
        await database.set_lease("aa:bb:cc:dd:ee:88", "192.168.2.88")
        return dev

    dev = _get_loop().run_until_complete(_setup_guest())

    # Set pending guest in snapshot
    holder.swap(EngineSnapshot(
        pending_guests={"192.168.2.88"},
        ip_to_mac={"192.168.2.88": dev.mac},
        blocked={dev.mac: True},
    ))

    with TestClient(app, client=("192.168.2.88", 45000)) as guest_client:
        # Check initial status
        r = guest_client.get("/api/guest/status")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "pending"
        assert data["is_guest"] is True
        assert data["blocked"] is True
        assert data["approved"] is False
        assert data["ip"] == "192.168.2.88"
        assert data["mac"] == "aa:bb:cc:dd:ee:88"

    # Now admin approves the guest
    with TestClient(app, client=("192.168.2.10", 45000)) as admin_client:
        # Admin login
        assert admin_client.post("/api/login", json={"password": "admin"}).status_code == 200

        # Approve guest device
        r_appr = admin_client.post(f"/api/guest/{dev.id}/approve")
        assert r_appr.status_code == 200
        assert r_appr.json()["ok"] is True
        assert r_appr.json()["block_state"] == _db.BLOCK_OK

    # Update snapshot simulating the subsequent maintenance tick
    holder.swap(EngineSnapshot(
        pending_guests=set(),  # no longer pending!
        ip_to_mac={"192.168.2.88": dev.mac},
        blocked={dev.mac: False},
    ))

    with TestClient(app, client=("192.168.2.88", 45000)) as guest_client:
        # Guest polls status again
        r = guest_client.get("/api/guest/status")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "approved"
        assert data["approved"] is True
        assert data["blocked"] is False
        assert data["quota_gb"] > 0

        # Guest browsing is now unblocked
        r_browse = guest_client.get("/search?q=hello", follow_redirects=False)
        # Not redirected to /guest/pending!
        assert r_browse.status_code != 302 or "/guest/pending" not in r_browse.headers.get("location", "")


def test_nftables_pending_guests_kernel_sync():
    fake = FakeNft()
    cfg = Config()
    cfg.engine.client_subnet = "192.168.2.0/24"
    cfg.engine.uplink_subnet = "192.168.1.0/24"
    eng = NftablesEngine(cfg, SnapshotHolder(), run_command=fake)
    eng.start()

    joined = " | ".join(fake.rules)
    assert "ip saddr @pending_guests tcp dport 80 redirect to :8080" in joined

    # Program pending guests
    eng.update_state(
        {"192.168.2.55": "aa:bb:cc:dd:ee:55"},
        {"aa:bb:cc:dd:ee:55": True},
        pending_guests=["192.168.2.55"],
    )
    assert "192.168.2.55" in fake._sets["inet quota_gateway"]["pending_guests"]

    # When admin approves, pending_guests becomes empty
    eng.update_state(
        {"192.168.2.55": "aa:bb:cc:dd:ee:55"},
        {"aa:bb:cc:dd:ee:55": False},
        pending_guests=[],
    )
    assert len(fake._sets["inet quota_gateway"]["pending_guests"]) == 0
