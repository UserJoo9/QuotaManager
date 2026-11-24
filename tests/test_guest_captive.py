"""Tests for Guest Captive Portal & Search Redirection."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Config, ReportConfig
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
        assert "Netrex" in r.text


def test_guest_portal_hidden_states_and_reduced_motion(captive_env):
    _, _, _, app = captive_env
    with TestClient(app) as client:
        html = client.get("/guest/pending").text

    assert "[hidden]" in html and "display: none !important" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert "voucherForm.contains(document.activeElement)" in html
    assert "document.getElementById('browse-btn').focus()" in html


def test_guest_voucher_redemption_sends_csrf_header(captive_env):
    _, _, _, app = captive_env
    with TestClient(app) as client:
        html = client.get("/guest/pending").text

    assert "'X-QM-CSRF': '1'" in html


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

        r_asset = guest_client.get("/assets/pub.css")
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


def test_guest_voucher_admin_lifecycle_and_redemption(captive_env):
    database, service, holder, app = captive_env
    _get_loop().run_until_complete(service.set_guest_mode(True))

    async def _setup_guest():
        dev = await database.upsert_device(
            "aa:bb:cc:dd:ee:89", name="Voucher guest",
            quota_mode=_db.QUOTA_FIXED, fixed_gb=0.0, guest=True)
        await database.set_device_state(dev.id, _db.BLOCK_GUEST_PENDING)
        await database.update_user(dev.user_id,
                                   block_state=_db.BLOCK_GUEST_PENDING)
        await database.set_lease(dev.mac, "192.168.2.89")
        return dev

    dev = _get_loop().run_until_complete(_setup_guest())
    holder.swap(EngineSnapshot(
        pending_guests={"192.168.2.89"},
        ip_to_mac={"192.168.2.89": dev.mac},
        blocked={dev.mac: True},
    ))

    with TestClient(app, client=("192.168.2.10", 45000)) as admin:
        assert admin.post("/api/guest/vouchers", json={"quota_gb": 2.5}).status_code == 401
        assert admin.post("/api/login", json={"password": "admin"}).status_code == 200
        created = admin.post("/api/guest/vouchers", json={
            "quota_gb": 2.5, "comment": "Weekend visitor"})
        assert created.status_code == 201, created.text
        voucher = created.json()
        assert voucher["status"] == "unused"
        assert voucher["quota_gb"] == 2.5
        code = voucher["code"]

        guest = TestClient(app, client=("192.168.2.89", 45000))
        redeemed = guest.post("/api/guest/voucher/redeem", json={"code": code.lower()})
        assert redeemed.status_code == 200, redeemed.text
        assert redeemed.json() == {
            "ok": True, "status": "approved", "quota_gb": 2.5}
        assert code not in redeemed.text
        guest.close()

        listed = admin.get("/api/guest/vouchers")
        assert listed.status_code == 200
        assert listed.json()["vouchers"][0]["status"] == "used"
        assert admin.post(f"/api/guest/vouchers/{code}/revoke").status_code == 409

    user = _get_loop().run_until_complete(database.get_user(dev.user_id))
    saved = _get_loop().run_until_complete(database.get_device(device_id=dev.id))
    assert user.fixed_gb == saved.fixed_gb == 2.5
    assert user.block_state == saved.block_state == _db.BLOCK_OK


def test_pending_guest_is_reported_as_blocked_in_dashboard_and_report(captive_env):
    database, service, holder, app = captive_env
    _get_loop().run_until_complete(service.set_guest_mode(True))

    async def _setup_guest():
        dev = await database.upsert_device(
            "aa:bb:cc:dd:ee:91", name="Pending guest",
            quota_mode=_db.QUOTA_FIXED, fixed_gb=0.0, guest=True)
        await database.set_device_state(dev.id, _db.BLOCK_GUEST_PENDING)
        await database.update_user(dev.user_id,
                                   block_state=_db.BLOCK_GUEST_PENDING)
        await database.set_lease(dev.mac, "192.168.2.91")
        return dev

    dev = _get_loop().run_until_complete(_setup_guest())
    holder.swap(EngineSnapshot(
        pending_guests={"192.168.2.91"},
        ip_to_mac={"192.168.2.91": dev.mac},
        blocked={dev.mac: True},
    ))

    with TestClient(app, client=("192.168.2.10", 45000)) as admin:
        assert admin.post("/api/login", json={"password": "admin"}).status_code == 200
        dashboard = admin.get("/api/dashboard").json()
        user = next(u for u in dashboard["users"] if u["id"] == dev.user_id)
        device = next(d for d in dashboard["devices"] if d["id"] == dev.id)
        assert (user["blocked"], user["block_state"]) == (
            True, _db.BLOCK_GUEST_PENDING)
        assert (device["blocked"], device["block_state"]) == (
            True, _db.BLOCK_GUEST_PENDING)

    report_app = create_app(
        database, service, holder,
        report_config=ReportConfig(
            enabled=True, allowed_ips=["192.168.2.0/24"]))
    with TestClient(report_app, client=("192.168.2.10", 45000)) as report:
        response = report.get("/api/report")
        assert response.status_code == 200, response.text
        user = next(u for u in response.json()["users"]
                    if u["id"] == dev.user_id)
        assert (user["blocked"], user["block_state"]) == (
            True, _db.BLOCK_GUEST_PENDING)


def test_guest_voucher_requires_live_identity_and_redacts_invalid_input(captive_env):
    database, service, holder, app = captive_env
    _get_loop().run_until_complete(service.set_guest_mode(True))

    async def _setup_guest():
        dev = await database.upsert_device(
            "aa:bb:cc:dd:ee:90", name="Pending guest",
            quota_mode=_db.QUOTA_FIXED, fixed_gb=0.0, guest=True)
        await database.set_device_state(dev.id, _db.BLOCK_GUEST_PENDING)
        await database.update_user(dev.user_id,
                                   block_state=_db.BLOCK_GUEST_PENDING)
        await database.set_lease(dev.mac, "192.168.2.90")
        return dev

    dev = _get_loop().run_until_complete(_setup_guest())
    with TestClient(app, client=("192.168.2.90", 45000)) as guest:
        unknown = guest.post("/api/guest/voucher/redeem", json={"code": "UNUSED"})
        assert unknown.status_code == 403
        assert unknown.json()["detail"] == {"code": "device_unrecognized"}
        malformed = guest.post("/api/guest/voucher/redeem", json={"code": ["secret"]})
        assert malformed.status_code == 422
        assert "secret" not in malformed.text
    assert _get_loop().run_until_complete(database.get_user(dev.user_id)).guest


def test_guest_voucher_memory_database_returns_clear_error():
    database = _db.Database(":memory:")
    service = QuotaService(
        database, timezone="Africa/Cairo",
        clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()
    _get_loop().run_until_complete(database.connect())

    async def _setup_guest():
        await service.ensure_period()
        await service.set_guest_mode(True)
        dev = await database.upsert_device(
            "aa:bb:cc:dd:ee:30", name="Pending guest",
            quota_mode=_db.QUOTA_FIXED, fixed_gb=0.0, guest=True)
        await database.set_device_state(dev.id, _db.BLOCK_GUEST_PENDING)
        await database.update_user(dev.user_id,
                                   block_state=_db.BLOCK_GUEST_PENDING)
        return dev, await database.create_guest_voucher("memory-1234", 1.0)

    try:
        dev, voucher = _get_loop().run_until_complete(_setup_guest())
        holder.swap(EngineSnapshot(
            pending_guests={"192.168.2.30"},
            ip_to_mac={"192.168.2.30": dev.mac},
            blocked={dev.mac: True},
        ))
        app = create_app(database, service, holder)

        with TestClient(app, client=("192.168.2.30", 45000)) as guest:
            response = guest.post("/api/guest/voucher/redeem",
                                  json={"code": voucher.code})

        assert response.status_code == 503
        assert response.json() == {
            "detail": {"code": "voucher_storage_unsupported"}}
        saved = _get_loop().run_until_complete(
            database.get_guest_voucher(voucher.code))
        assert saved is not None and saved.used_at is None
    finally:
        _get_loop().run_until_complete(database.close())


def test_guest_voucher_invalid_attempts_are_limited_per_device(captive_env):
    database, service, holder, app = captive_env
    _get_loop().run_until_complete(service.set_guest_mode(True))

    async def _setup_guests():
        devices = []
        for suffix in (91, 92):
            mac = f"aa:bb:cc:dd:ee:{suffix}"
            ip = f"192.168.2.{suffix}"
            dev = await database.upsert_device(
                mac, name="Pending guest", quota_mode=_db.QUOTA_FIXED,
                fixed_gb=0.0, guest=True)
            await database.set_device_state(dev.id, _db.BLOCK_GUEST_PENDING)
            await database.update_user(dev.user_id,
                                       block_state=_db.BLOCK_GUEST_PENDING)
            await database.set_lease(mac, ip)
            devices.append((dev, ip))
        return devices

    devices = _get_loop().run_until_complete(_setup_guests())
    holder.swap(EngineSnapshot(
        ip_to_mac={ip: dev.mac for dev, ip in devices},
        pending_guests={ip for _, ip in devices},
        blocked={dev.mac: True for dev, _ in devices},
    ))

    with TestClient(app, client=("192.168.2.10", 45000)) as admin:
        assert admin.post("/api/login", json={"password": "admin"}).status_code == 200
        code = admin.post("/api/guest/vouchers", json={"quota_gb": 1.5}).json()["code"]

        first_ip = devices[0][1]
        with TestClient(app, client=(first_ip, 45000)) as first_guest:
            for _ in range(5):
                failed = first_guest.post(
                    "/api/guest/voucher/redeem", json={"code": "wrong-code"})
                assert failed.status_code == 400
                assert "wrong-code" not in failed.text
            throttled = first_guest.post(
                "/api/guest/voucher/redeem", json={"code": code})
            assert throttled.status_code == 429
            assert int(throttled.headers["Retry-After"]) > 0

        second_ip = devices[1][1]
        with TestClient(app, client=(second_ip, 45000)) as second_guest:
            redeemed = second_guest.post(
                "/api/guest/voucher/redeem", json={"code": code})
            assert redeemed.status_code == 200, redeemed.text


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


def test_public_pages_allow_their_inline_scripts(captive_env):
    """The guest portal and the other public pages ship inline scripts; a strict
    ``script-src 'self'`` CSP would silently disable them (status polling, the
    "Check now" button)."""
    _, _, _, app = captive_env
    with TestClient(app) as client:
        for path in ("/guest/pending", "/milestone", "/spoof.html"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "script-src 'self' 'unsafe-inline'" in r.headers["content-security-policy"], path
        assert "no-store" in client.get("/guest/pending").headers["cache-control"]


def test_portal_redirect_keeps_the_scheme_of_the_request(captive_env):
    """With HTTPS enforced uvicorn speaks TLS on the web port: redirecting a
    pending guest to ``http://`` there makes the browser talk plain HTTP to a TLS
    socket ("server unexpectedly dropped the connection")."""
    database, service, holder, app = captive_env
    holder.swap(EngineSnapshot(
        pending_guests={"192.168.2.55"},
        ip_to_mac={"192.168.2.55": "11:22:33:44:55:66"},
        blocked={"11:22:33:44:55:66": True},
    ))
    for scheme in ("http", "https"):
        with TestClient(app, base_url=f"{scheme}://192.168.2.1:8080",
                        client=("192.168.2.55", 45000)) as c:
            for path in ("/", "/some/page", "/generate_204", "/hotspot-detect.html",
                         "/connecttest.txt", "/canonical.html"):
                r = c.get(path, follow_redirects=False)
                assert r.status_code == 302, (scheme, path)
                loc = r.headers["location"]
                assert loc.startswith(f"{scheme}://192.168.2.1:8080/guest/pending"), (scheme, path, loc)
