"""API integration tests (FastAPI TestClient + real temp SQLite DB)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.service import GB, QuotaService

TZ = ZoneInfo("Africa/Cairo")


def _login(c: TestClient) -> None:
    """Every admin route now requires a valid session cookie (auth change)."""
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


@pytest.fixture
def client(tmp_path):
    """A TestClient wired to a temp database and quota service."""
    database = _db.Database(tmp_path / "api.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()

    async def _init():
        await database.connect()
        return database, service

    import asyncio
    asyncio.get_event_loop().run_until_complete(_init())

    app = create_app(database, service, holder)
    with TestClient(app) as c:
        yield c, database, service
    asyncio.get_event_loop().run_until_complete(database.close())


def test_login_and_me(client):
    c, _, _ = client
    r = c.get("/api/me")
    assert r.json() == {"authenticated": False}
    r = c.post("/api/login", json={"password": "admin"})
    assert r.status_code == 200, r.text
    r = c.get("/api/me")
    assert r.json() == {"authenticated": True}


def test_wrong_password(client):
    c, _, _ = client
    r = c.post("/api/login", json={"password": "nope"})
    assert r.status_code == 401


def test_device_crud_and_dashboard(client):
    c, _, _ = client
    _login(c)
    # add a fixed device
    r = c.post("/api/devices", json={
        "mac": "aa:bb:cc:dd:ee:ff", "name": "Phone",
        "quota_mode": "fixed", "fixed_gb": 20.0})
    assert r.status_code == 201, r.text
    dev_id = r.json()["id"]

    # dashboard should list it
    r = c.get("/api/dashboard")
    data = r.json()
    assert data["total_devices"] == 1
    dev = data["devices"][0]
    assert dev["name"] == "Phone"
    assert dev["allowance_gb"] == 20.0
    assert dev["blocked"] is False

    # block it via PATCH
    r = c.patch(f"/api/devices/{dev_id}", json={"block": True})
    assert r.status_code == 200
    r = c.get("/api/dashboard")
    assert r.json()["blocked_count"] == 1

    # unblock
    c.patch(f"/api/devices/{dev_id}", json={"block": False})
    assert c.get("/api/dashboard").json()["blocked_count"] == 0

    # delete
    r = c.delete(f"/api/devices/{dev_id}")
    assert r.status_code == 200
    assert c.get("/api/dashboard").json()["total_devices"] == 0


def test_bundle_and_reset(client):
    c, db, _ = client
    _login(c)
    r = c.get("/api/bundle")
    assert r.json()["total_gb"] == 140.0

    r = c.post("/api/bundle", json={"total_gb": 50.0, "reset_day": 15})
    assert r.status_code == 200
    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 50.0 and b["reset_day"] == 15
    # dashboard owns the bundle now -> config.yaml won't override on restart
    import asyncio
    src = asyncio.get_event_loop().run_until_complete(
        db.get_setting("bundle_source", "config"))
    assert src == "dashboard"


def test_topup_clears_block(client):
    c, db, service = client
    _login(c)
    r = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:01",
                                     "quota_mode": "auto"})
    dev_id = r.json()["id"]
    # use more than allowance (bundle default 140, one auto device -> 140)
    # simulate usage directly in DB
    import asyncio
    async def _add():
        await db.add_usage(dev_id, "2026-08-01", int(150 * GB), 0)
        await service.evaluate_blocks()
    asyncio.get_event_loop().run_until_complete(_add())

    r = c.get("/api/dashboard")
    assert r.json()["blocked_count"] == 1

    r = c.post(f"/api/devices/{dev_id}/topup", json={"extra_gb": 20})
    assert r.status_code == 200
    assert r.json()["allowance_gb"] >= 160

    r = c.get("/api/dashboard")
    assert r.json()["blocked_count"] == 0


def test_usage_series(client):
    c, db, _ = client
    _login(c)
    r = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:02",
                                     "name": "TV", "quota_mode": "fixed",
                                     "fixed_gb": 5})
    dev_id = r.json()["id"]
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        db.add_usage(dev_id, "2026-08-01", 100, 200))
    asyncio.get_event_loop().run_until_complete(
        db.add_usage(dev_id, "2026-08-02", 300, 0))

    r = c.get(f"/api/usage/{dev_id}")
    series = r.json()
    assert len(series) == 2
    assert series[0]["up"] == 100 and series[0]["down"] == 200


def test_bundle_recharge_grows_total(client):
    c, db, _ = client
    _login(c)
    # one auto device: share = 140 at first
    r = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:03",
                                     "quota_mode": "auto"})
    assert r.status_code == 201
    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 140.0

    r = c.post("/api/bundle", json={"add_gb": 50})
    assert r.status_code == 200, r.text
    assert r.json()["total_gb"] == 190.0
    assert r.json()["added_gb"] == 50.0
    # a recharge is a dashboard action: it takes bundle ownership
    import asyncio
    src = asyncio.get_event_loop().run_until_complete(
        db.get_setting("bundle_source", "config"))
    assert src == "dashboard"

    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 190.0
    assert b["allowances"]["aa:bb:cc:dd:ee:03"] == 190.0  # auto share grew

    dash = c.get("/api/dashboard").json()
    assert dash["bundle"]["remaining_gb"] == pytest.approx(190.0)


def test_password_change_requires_session(client):
    """Not logged in -> 401 (client shows the login screen), not a wrong-password 400."""
    c, _, _ = client
    r = c.post("/api/password", json={"current": "admin", "new": "secret"})
    assert r.status_code == 401, r.text


def test_password_change_wrong_current_is_400(client):
    """Wrong current password -> 400 (bad request), so the client can show
    'Current password is wrong.' instead of logging the user out."""
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/password", json={"current": "wrong", "new": "secret"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "current password incorrect"
    # old password still works
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_password_change_success(client):
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/password", json={"current": "admin", "new": "secret"})
    assert r.status_code == 200, r.text
    # new password logs in, old one is rejected
    assert c.post("/api/login", json={"password": "secret"}).status_code == 200
    assert c.post("/api/login", json={"password": "admin"}).status_code == 401


def test_bundle_reset_day_0_disables_auto_reset(client):
    c, _, _ = client
    _login(c)
    r = c.post("/api/bundle", json={"reset_day": 0})
    assert r.status_code == 200
    assert c.get("/api/bundle").json()["reset_day"] == 0
    dash = c.get("/api/dashboard").json()
    assert dash["bundle"]["days_left"] == -1
    assert dash["bundle"]["period_end"] == ""
