"""API integration tests (FastAPI TestClient + real temp SQLite DB)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import EngineSnapshot, RogueHost, SnapshotHolder
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
        "quota_mode": "fixed", "fixed_gb": 20.0,
        "limit_down_mbps": 10, "limit_up_mbps": 5})
    assert r.status_code == 201, r.text
    dev_id = r.json()["id"]
    assert r.json()["user_id"] is not None  # auto-created a user for the device

    # dashboard should list it (owned by an auto-created user)
    r = c.get("/api/dashboard")
    data = r.json()
    assert data["total_devices"] == 1
    assert data["total_users"] == 1
    dev = data["devices"][0]
    assert dev["name"] == "Phone"
    assert dev["allowance_gb"] == 20.0
    assert dev["blocked"] is False
    # per-device speed caps surfaced on the dashboard device view
    assert dev["limit_down_mbps"] == 10.0
    assert dev["limit_up_mbps"] == 5.0
    # vendor field present (empty here — test MAC isn't a registered OUI)
    assert "vendor" in dev
    # per-device consumption monitor fields present (no usage yet)
    assert dev["device_used_gb"] == 0.0
    assert "device_percent" in dev
    assert "device_up_gb" in dev and "device_down_gb" in dev

    # block it via PATCH
    r = c.patch(f"/api/devices/{dev_id}", json={"block": True})
    assert r.status_code == 200
    r = c.get("/api/dashboard")
    assert r.json()["blocked_count"] == 1

    # unblock
    c.patch(f"/api/devices/{dev_id}", json={"block": False})
    assert c.get("/api/dashboard").json()["blocked_count"] == 0

    # update the device's speed caps via PATCH
    r = c.patch(f"/api/devices/{dev_id}",
                json={"limit_down_mbps": 25, "limit_up_mbps": 0})
    assert r.status_code == 200
    dev = next(d for d in c.get("/api/dashboard").json()["devices"]
               if d["id"] == dev_id)
    assert dev["limit_down_mbps"] == 25.0
    assert dev["limit_up_mbps"] == 0.0   # up reset to unlimited

    # delete
    r = c.delete(f"/api/devices/{dev_id}")
    assert r.status_code == 200
    assert c.get("/api/dashboard").json()["total_devices"] == 0


def test_network_and_user_speed_caps(client):
    c, _, _ = client
    _login(c)
    # defaults: shaping off, no totals, AQM on
    n = c.get("/api/network").json()
    assert n == {"enabled": False, "total_down_mbps": 0.0,
                 "total_up_mbps": 0.0, "aqm": True}

    # partial POST — only the given fields change
    r = c.post("/api/network", json={"enabled": True, "total_down_mbps": 100})
    assert r.status_code == 200
    n = r.json()
    assert n["enabled"] is True
    assert n["total_down_mbps"] == 100.0
    assert n["total_up_mbps"] == 0.0
    assert n["aqm"] is True

    r = c.post("/api/network", json={"total_up_mbps": 20, "aqm": False})
    assert r.json()["total_up_mbps"] == 20.0
    assert r.json()["aqm"] is False
    assert r.json()["enabled"] is True   # untouched by the partial update

    # per-user aggregate caps
    r = c.post("/api/users", json={"name": "Mom", "quota_mode": "auto",
                                   "limit_down_mbps": 50, "limit_up_mbps": 10})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    u = next(x for x in c.get("/api/dashboard").json()["users"]
             if x["id"] == uid)
    assert u["limit_down_mbps"] == 50.0
    assert u["limit_up_mbps"] == 10.0

    # PATCH updates the caps without touching quota
    r = c.patch(f"/api/users/{uid}", json={"limit_up_mbps": 0})
    assert r.status_code == 200
    u = next(x for x in c.get("/api/dashboard").json()["users"]
             if x["id"] == uid)
    assert u["limit_down_mbps"] == 50.0
    assert u["limit_up_mbps"] == 0.0


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
    user_id = r.json()["user_id"]  # allowance is keyed by the device's user
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
    assert b["allowances"][str(user_id)] == 190.0  # auto share grew

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


# ---------------------------------------------------------------------------
# first-run welcome panel (/api/setup)
# ---------------------------------------------------------------------------

def test_setup_fresh_db_not_complete(client):
    """A brand-new DB has no users yet -> welcome panel shows."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/setup")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["setup_complete"] is False
    assert data["total_gb"] == 140.0   # config.yaml default on a fresh DB
    assert data["reset_day"] == 1


def test_setup_requires_session(client):
    c, _, _ = client
    r = c.get("/api/setup")
    assert r.status_code == 401
    r = c.post("/api/setup/complete", json={"total_gb": 60})
    assert r.status_code == 401


def test_setup_complete_writes_bundle_and_password(client):
    """Submitting the welcome panel sets the bundle (takes ownership from
    config.yaml), changes the password, and marks setup complete."""
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={
        "total_gb": 60, "reset_day": 15,
        "current_password": "admin", "new_password": "secret"})
    assert r.status_code == 200, r.text
    # bundle updated + dashboard owns it now (config.yaml stops overriding)
    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 60.0
    assert b["reset_day"] == 15
    assert c.get("/api/setup").json()["setup_complete"] is True
    # new password logs in, old one is rejected
    assert c.post("/api/login", json={"password": "secret"}).status_code == 200
    assert c.post("/api/login", json={"password": "admin"}).status_code == 401


def test_setup_password_only_keeps_bundle_source(client):
    """A password-only save must NOT take bundle ownership from config.yaml."""
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={
        "current_password": "admin", "new_password": "secret"})
    assert r.status_code == 200, r.text
    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 140.0   # untouched
    assert b["reset_day"] == 1
    assert c.get("/api/setup").json()["setup_complete"] is True


def test_setup_wrong_current_password_is_400(client):
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={
        "current_password": "wrong", "new_password": "secret"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "current password incorrect"
    # still not marked complete, old password still works
    assert c.get("/api/setup").json()["setup_complete"] is False
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_setup_blank_submit_just_dismisses(client):
    """An all-empty submission marks the panel done without changing anything."""
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={})
    assert r.status_code == 200, r.text
    assert c.get("/api/setup").json()["setup_complete"] is True
    assert c.get("/api/bundle").json()["total_gb"] == 140.0
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_setup_complete_implied_by_existing_users(client):
    """A DB that already has users never shows the welcome panel — the
    heuristic treats 'any users' as setup already done."""
    c, _, service = client
    _login(c)
    r = c.post("/api/users", json={"name": "Dad", "quota_mode": "fixed",
                                   "fixed_gb": 20})
    assert r.status_code == 201, r.text
    assert c.get("/api/setup").json()["setup_complete"] is True


def test_setup_new_password_min_length(client):
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={
        "current_password": "admin", "new_password": "ab"})
    assert r.status_code == 422, r.text  # pydantic min_length=4


def test_setup_reset_day_0(client):
    """The welcome panel can set reset_day=0 (never auto-reset)."""
    c, _, _ = client
    _login(c)
    r = c.post("/api/setup/complete", json={"reset_day": 0})
    assert r.status_code == 200
    assert c.get("/api/bundle").json()["reset_day"] == 0


# ---------------------------------------------------------------------------
# per-user model: people own devices, the quota lives on the user
# ---------------------------------------------------------------------------

def test_user_crud_and_block(client):
    c, _, _ = client
    _login(c)
    r = c.post("/api/users", json={"name": "Dad", "quota_mode": "fixed",
                                   "fixed_gb": 20})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:41", "name": "Phone",
                                 "user_id": uid})
    c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:42", "name": "Laptop",
                                 "user_id": uid})

    dash = c.get("/api/dashboard").json()
    assert dash["total_users"] == 1 and dash["total_devices"] == 2
    u = dash["users"][0]
    assert u["name"] == "Dad"
    assert u["allowance_gb"] == 20.0
    assert len(u["devices"]) == 2

    # user-level block cuts both devices at once
    r = c.patch(f"/api/users/{uid}", json={"block": True})
    assert r.status_code == 200
    assert c.get("/api/dashboard").json()["blocked_count"] == 2
    # resolved, not persisted: the device row reports the user cut
    assert c.get("/api/devices").json()[0]["block_state"] == "admin_off"

    c.patch(f"/api/users/{uid}", json={"block": False})
    assert c.get("/api/dashboard").json()["blocked_count"] == 0

    # rename
    c.patch(f"/api/users/{uid}", json={"name": "Dad ✱"})
    assert c.get("/api/dashboard").json()["users"][0]["name"] == "Dad ✱"

    # delete cascades: user + both devices
    r = c.delete(f"/api/users/{uid}")
    assert r.status_code == 200
    assert r.json()["devices_removed"] == 2
    dash = c.get("/api/dashboard").json()
    assert dash["total_devices"] == 0
    assert dash["total_users"] == 0


def test_user_topup_via_api(client):
    c, db, service = client
    _login(c)
    uid = c.post("/api/users", json={"name": "Kid", "quota_mode": "auto"}).json()["id"]
    dev_id = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:43",
                                          "user_id": uid}).json()["id"]
    # use more than the user's allowance (bundle 140, one auto user -> 140)
    import asyncio
    async def _add():
        await db.add_usage(dev_id, "2026-08-01", int(150 * GB), 0)
        await service.evaluate_blocks()
    asyncio.get_event_loop().run_until_complete(_add())
    assert c.get("/api/dashboard").json()["blocked_count"] == 1

    r = c.post(f"/api/users/{uid}/topup", json={"extra_gb": 20})
    assert r.status_code == 200
    assert r.json()["allowance_gb"] >= 160
    assert c.get("/api/dashboard").json()["blocked_count"] == 0


def test_device_reassign_user(client):
    c, _, _ = client
    _login(c)
    u1 = c.post("/api/users", json={"name": "A"}).json()["id"]
    u2 = c.post("/api/users", json={"name": "B"}).json()["id"]
    dev_id = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:44",
                                          "user_id": u1}).json()["id"]
    r = c.patch(f"/api/devices/{dev_id}", json={"user_id": u2})
    assert r.status_code == 200
    dash = c.get("/api/dashboard").json()
    by_user = {u["id"]: len(u["devices"]) for u in dash["users"]}
    assert by_user[u1] == 0 and by_user[u2] == 1


def test_device_bypass_and_quota_edit_via_api(client):
    c, db, service = client
    _login(c)
    uid = c.post("/api/users", json={"name": "A", "quota_mode": "auto"}).json()["id"]
    d1 = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:45",
                                      "user_id": uid}).json()["id"]
    c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:46", "user_id": uid})
    import asyncio
    async def _add():
        await db.add_usage(d1, "2026-08-01", int(150 * GB), 0)
        await service.evaluate_blocks()
    asyncio.get_event_loop().run_until_complete(_add())
    assert c.get("/api/dashboard").json()["blocked_count"] == 2

    # exempt ONE device from its user's quota block
    c.patch(f"/api/devices/{d1}", json={"bypass": True})
    dash = c.get("/api/dashboard").json()
    assert dash["blocked_count"] == 1
    by_mac = {dv["mac"]: dv["blocked"] for dv in dash["devices"]}
    assert by_mac["aa:bb:cc:dd:ee:45"] is False
    assert by_mac["aa:bb:cc:dd:ee:46"] is True

    # a device-card quota edit forwards to the owning USER
    c.patch(f"/api/devices/{d1}", json={"fixed_gb": 200, "quota_mode": "fixed"})
    dash = c.get("/api/dashboard").json()
    for dv in dash["devices"]:
        assert dv["allowance_gb"] == 200.0
    assert dash["blocked_count"] == 0


def test_device_consumption_is_per_device(client):
    """Each device row reports ITS OWN period usage (the consumption monitor),
    while the user-aggregate used_gb/percent stay unchanged (the sum)."""
    c, db, _ = client
    _login(c)
    uid = c.post("/api/users", json={"name": "Dad", "quota_mode": "fixed",
                                     "fixed_gb": 20}).json()["id"]
    a = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:61",
                                     "name": "Phone", "user_id": uid}).json()["id"]
    c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:62",
                                 "name": "Laptop", "user_id": uid})

    import asyncio
    asyncio.get_event_loop().run_until_complete(
        db.add_usage(a, "2026-08-05", int(4 * GB), int(2 * GB)))

    dash = c.get("/api/dashboard").json()
    by_mac = {dv["mac"]: dv for dv in dash["devices"]}
    # only the device that used data reports it; the sibling reports zero
    assert by_mac["aa:bb:cc:dd:ee:61"]["device_used_gb"] == pytest.approx(6.0)
    assert by_mac["aa:bb:cc:dd:ee:62"]["device_used_gb"] == 0.0
    # up/down split
    assert by_mac["aa:bb:cc:dd:ee:61"]["device_up_gb"] == pytest.approx(4.0)
    assert by_mac["aa:bb:cc:dd:ee:61"]["device_down_gb"] == pytest.approx(2.0)
    # percent = the device's share of the user's allowance
    assert by_mac["aa:bb:cc:dd:ee:61"]["device_percent"] == pytest.approx(30.0)
    # the user-aggregate fields still show the SUM on both rows
    assert by_mac["aa:bb:cc:dd:ee:61"]["used_gb"] == pytest.approx(6.0)
    assert by_mac["aa:bb:cc:dd:ee:62"]["used_gb"] == pytest.approx(6.0)
    # ...and the per-user bar reports the same aggregate
    assert dash["users"][0]["used_gb"] == pytest.approx(6.0)


def test_guest_defaults(client):
    """Guest mode is off by default with a 1 GB guest allowance."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/guest")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "quota_gb": 1.0}


def test_guest_enable_and_quota(client):
    """POST /api/guest toggles the flag and/or the allowance independently."""
    c, _, _ = client
    _login(c)
    r = c.post("/api/guest", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "quota_gb": 1.0}

    r = c.post("/api/guest", json={"quota_gb": 5})
    assert r.json() == {"enabled": True, "quota_gb": 5.0}

    r = c.post("/api/guest", json={"enabled": False})
    assert r.json() == {"enabled": False, "quota_gb": 5.0}


def test_guest_quota_updates_existing_guest(client):
    """Raising the guest quota applies to guests already registered."""
    import asyncio
    c, db, service = client
    _login(c)

    async def _seed():
        g = await db.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                                 fixed_gb=1.0, guest=True)
        await db.upsert_device("aa:bb:cc:dd:ee:91", name="Phone", user_id=g.id)
        await service.recompute_allowances()
    asyncio.get_event_loop().run_until_complete(_seed())

    c.post("/api/guest", json={"quota_gb": 3})
    dash = c.get("/api/dashboard").json()
    guest = next(u for u in dash["users"] if u["guest"])
    assert guest["name"] == ""            # guest users have no name
    assert guest["allowance_gb"] == 3.0   # existing guest updated immediately
    assert dash["devices"][0]["guest"] is True


def test_guest_and_connected_flags_in_views(client):
    """Device rows report guest + connected; users report the guest flag."""
    import asyncio
    c, db, _ = client
    _login(c)
    async def _seed():
        g = await db.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                                 fixed_gb=1.0, guest=True)
        await db.upsert_device("aa:bb:cc:dd:ee:92", name="Phone", user_id=g.id)
        # a live lease => the guest device is "connected"
        await db.set_lease("aa:bb:cc:dd:ee:92", "192.168.2.50")
        await db.upsert_device("aa:bb:cc:dd:ee:93", name="Old Tablet",
                               user_id=g.id)   # no lease => offline
    asyncio.get_event_loop().run_until_complete(_seed())

    dash = c.get("/api/dashboard").json()
    by_mac = {d["mac"]: d for d in dash["devices"]}
    assert by_mac["aa:bb:cc:dd:ee:92"]["connected"] is True
    assert by_mac["aa:bb:cc:dd:ee:92"]["guest"] is True
    assert by_mac["aa:bb:cc:dd:ee:93"]["connected"] is False
    assert all(u["guest"] for u in dash["users"])


def test_reset_month_deletes_guests(client):
    """A manual reset wipes guest users but keeps normal users."""
    import asyncio
    c, db, service = client
    _login(c)
    async def _seed():
        g = await db.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                                 fixed_gb=1.0, guest=True)
        await db.upsert_device("aa:bb:cc:dd:ee:94", user_id=g.id)
        n = await db.create_user(name="Dad", quota_mode=_db.QUOTA_FIXED,
                                 fixed_gb=20.0)
        await db.upsert_device("aa:bb:cc:dd:ee:95", name="Phone", user_id=n.id)
    asyncio.get_event_loop().run_until_complete(_seed())

    assert c.get("/api/dashboard").json()["total_users"] == 2
    r = c.post("/api/reset-month")
    assert r.status_code == 200, r.text
    dash = c.get("/api/dashboard").json()
    assert dash["total_users"] == 1
    assert dash["users"][0]["name"] == "Dad"
    assert all(not u["guest"] for u in dash["users"])


def test_rogue_endpoint_returns_list(client):
    """With no scan results the rogue endpoints report an empty list (and the
    dashboard payload carries the ``rogue`` key — the WS push shares it)."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/rogue")
    assert r.status_code == 200
    assert r.json() == []
    assert c.get("/api/dashboard").json()["rogue"] == []


def test_wan_endpoint_defaults(client):
    """Before any maintenance tick the WAN status is ``{}`` — exactly like
    ``rogue``. ``GET /api/wan`` additionally carries the saved PPPoE creds
    (empty here — that is what prefills the panel), while the dashboard payload
    keeps the creds out of the ``wan`` key (the WS push must never carry the
    password). The endpoint never 500s."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/wan")
    assert r.status_code == 200
    assert r.json() == {"pppoe_user": "", "pppoe_password": "", "wan_if": ""}
    assert c.get("/api/dashboard").json()["wan"] == {}


def test_dashboard_surfaces_wan_status(tmp_path):
    """A populated snapshot's wan_status reaches both /api/wan and the dashboard
    payload — the single _dashboard_payload source keeps them in step. The saved
    PPPoE creds ride only on ``GET /api/wan`` (the panel prefill), never in the
    WS-pushed ``wan`` key."""
    import asyncio
    database = _db.Database(tmp_path / "wan.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    holder.swap(EngineSnapshot(wan_status={
        "topology": "lan", "configured": "lan", "source": "config", "pending": None,
        "ppp0": "n/a", "ppp_ip": "", "ppp_peer": "",
    }))
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        expected = {"topology": "lan", "configured": "lan", "source": "config",
                    "pending": None, "ppp0": "n/a", "ppp_ip": "", "ppp_peer": ""}
        assert c.get("/api/wan").json() == {
            **expected,
            "pppoe_user": "", "pppoe_password": "", "wan_if": "",
        }
        assert c.get("/api/dashboard").json()["wan"] == expected
    asyncio.get_event_loop().run_until_complete(database.close())


def test_dashboard_top_level_internet(tmp_path):
    """The dashboard payload carries the internet probe as a TOP-LEVEL key (the
    top-bar pill reads it directly), mirroring wan_status: true / false / None
    (not probed yet = the pre-first-tick 'Checking…' state)."""
    import asyncio
    database = _db.Database(tmp_path / "wan.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    base = {"topology": "lan", "configured": "lan", "source": "config",
            "pending": None, "ppp0": "n/a", "ppp_ip": "", "ppp_peer": ""}
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        holder.swap(EngineSnapshot(wan_status={**base, "internet": True}))
        data = c.get("/api/dashboard").json()
        assert data["internet"] is True
        assert data["wan"]["internet"] is True
        holder.swap(EngineSnapshot(wan_status={**base, "internet": False}))
        data = c.get("/api/dashboard").json()
        assert data["internet"] is False
        assert data["wan"]["internet"] is False
        # no `internet` key yet (pre-first-tick) -> None = "Checking…"
        holder.swap(EngineSnapshot(wan_status=dict(base)))
        data = c.get("/api/dashboard").json()
        assert data["internet"] is None
        assert "internet" not in data["wan"]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_toggle_persists_and_owns_topology(client):
    """POST /api/wan stores the preference (topology_source=dashboard) so it
    wins over config.yaml on the NEXT restart — the bundle_source pattern."""
    c, database, _ = client
    _login(c)
    r = c.post("/api/wan", json={"topology": "wan"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["topology"] == "lan"  # effective value unchanged until restart
    assert data["configured"] == "wan"  # the DESIRED mode — the UI toggle keys off this
    assert data["source"] == "dashboard"
    assert data["pending"] == "wan"
    assert data["applies_on_restart"] is True

    async def _read():
        return (await database.get_setting("topology_source", None),
                await database.get_setting("topology", None))

    import asyncio
    source, topo = asyncio.get_event_loop().run_until_complete(_read())
    assert (source, topo) == ("dashboard", "wan")
    events = asyncio.get_event_loop().run_until_complete(database.list_events())
    assert any("WAN topology set to wan" in e["message"] for e in events)


def test_wan_persist_no_manager_preserves_saved_creds(tmp_path):
    """REGRESSION: in the no-manager path (tests / degraded boot), a
    body with empty creds — a Revert-to-LAN posts only ``{topology: "lan"}`` —
    must not erase the credentials previously saved for the panel prefill."""
    import asyncio
    database = _db.Database(tmp_path / "wan-persist.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, topology_manager=None)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        # save creds first
        r = c.post("/api/wan", json={"topology": "wan", "pppoe_user": "u@isp",
                                     "pppoe_password": "s3cret"})
        assert r.status_code == 200, r.text
        # a LAN revert carries no creds -> they must survive
        r = c.post("/api/wan", json={"topology": "lan"})
        assert r.status_code == 200, r.text
        assert c.get("/api/wan").json()["pppoe_user"] == "u@isp"
        assert c.get("/api/wan").json()["pppoe_password"] == "s3cret"
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_toggle_invalid_is_400(client):
    """Only "lan" / "wan" are valid topology values — anything else is a 400
    and must not touch the persisted preference (checked in the DB directly:
    pre-tick /api/wan is {} so it can't assert the source)."""
    c, database, _ = client
    _login(c)
    r = c.post("/api/wan", json={"topology": "sneaky"})
    assert r.status_code == 400

    async def _source():
        return await database.get_setting("topology_source", "config")

    import asyncio
    assert asyncio.get_event_loop().run_until_complete(_source()) == "config"


def test_wan_toggle_requires_session(client):
    c, _, _ = client
    r = c.post("/api/wan", json={"topology": "wan"})
    assert r.status_code == 401


class _FakeManager:
    """A stand-in for TopologyManager that records the apply call (so the test
    can assert creds + wan_if were forwarded) and can be told to fail."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.tests: list[tuple] = []
        self.fail = False

    async def apply(self, topology, pppoe_user="", pppoe_password="", wan_if=""):
        if self.fail:
            raise RuntimeError("boom: pppd could not dial the line")
        self.calls.append((topology, pppoe_user, pppoe_password, wan_if))
        return {"applied": topology, "restart_scheduled": True,
                "script_rc": 0, "script_output": "configured eth0 + dnsmasq"}

    async def test_pppoe(self, pppoe_user="", pppoe_password="", wan_if=""):
        if self.fail:
            raise RuntimeError("boom: pppd could not dial the line")
        self.tests.append((pppoe_user, pppoe_password, wan_if))
        return {"status": "success", "ok": True, "local_ip": "100.64.0.2",
                "peer_ip": "100.64.0.1", "internet": True,
                "detail": "PPPoE link is up",
                "script_output": "RESULT=success"}


def test_wan_apply_live_with_manager(tmp_path):
    """v19: with a topology manager wired, POST /api/wan APPLIES the topology
    live — PPPoE creds + WAN NIC forwarded, the DB override written in the same
    apply, restart scheduled, and the applier's tail surfaced in the response."""
    import asyncio
    database = _db.Database(tmp_path / "wan-app.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "lan", "source": "config", "pending": None,
        "ppp0": "n/a", "ppp_ip": "", "ppp_peer": "",
    }))
    manager = _FakeManager()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, topology_manager=manager)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan", json={"topology": "wan", "pppoe_user": "u@isp",
                                     "pppoe_password": "s3cret", "wan_if": "eth1"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["restart_scheduled"] is True
        assert data["script_output"] == "configured eth0 + dnsmasq"
        assert data["source"] == "dashboard"
        assert data["configured"] == "wan"
        assert data["pending"] == "wan"
    assert manager.calls == [("wan", "u@isp", "s3cret", "eth1")]
    # The DB override is written INSIDE TopologyManager.apply (invariant 1:
    # config.yaml + DB together) — covered by the netmgr round-trip test. The
    # endpoint only forwards creds and surfaces the manager's result.
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_apply_failure_is_500(tmp_path):
    """v19: an applier failure raises RuntimeError -> HTTP 500 with the cause.
    The preference is still persisted (config + DB agree on wan) — matching
    what a manual setup re-run would have produced — but no restart fires."""
    import asyncio
    database = _db.Database(tmp_path / "wan-fail.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "lan", "source": "config", "pending": None,
        "ppp0": "n/a", "ppp_ip": "", "ppp_peer": "",
    }))
    manager = _FakeManager()
    manager.fail = True
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, topology_manager=manager)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan", json={"topology": "wan"})
        assert r.status_code == 500
        assert "topology apply failed" in r.json()["detail"]
        assert "boom" in r.json()["detail"]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_test_pppoe(tmp_path):
    """v19.1: POST /api/wan/test dials a throwaway link with the entered creds
    and returns the parsed result — nothing is applied (no topology change, no
    restart). The endpoint forwards the NIC too."""
    import asyncio
    database = _db.Database(tmp_path / "wan-test.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "lan", "source": "config", "pending": None,
        "ppp0": "n/a", "ppp_ip": "", "ppp_peer": "",
    }))
    manager = _FakeManager()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, topology_manager=manager)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan/test", json={"pppoe_user": "u@isp",
                                          "pppoe_password": "s3cret",
                                          "wan_if": "eth1"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "success"
        assert data["ok"] is True
        assert data["internet"] is True
    assert manager.tests == [("u@isp", "s3cret", "eth1")]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_test_pppoe_requires_manager(tmp_path):
    """No topology manager wired (degraded boot) -> 503, not a crash."""
    import asyncio
    database = _db.Database(tmp_path / "wan-test-no-mgr.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, topology_manager=None)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan/test", json={"pppoe_user": "u@isp"})
        assert r.status_code == 503
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_test_pppoe_failure_is_500(tmp_path):
    """v19.1: a failing test run (e.g. pppd missing) -> HTTP 500 with the cause."""
    import asyncio
    database = _db.Database(tmp_path / "wan-test-fail.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "lan", "source": "config", "pending": None,
        "ppp0": "n/a", "ppp_ip": "", "ppp_peer": "",
    }))
    manager = _FakeManager()
    manager.fail = True
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, topology_manager=manager)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan/test", json={"pppoe_user": "u@isp"})
        assert r.status_code == 500
        assert "PPPoE test failed" in r.json()["detail"]
        assert "boom" in r.json()["detail"]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_dashboard_surfaces_rogue_snapshot(tmp_path):
    """A populated snapshot's rogues reach both /api/rogue and the dashboard
    payload — the single _dashboard_payload source keeps them in step."""
    import asyncio
    database = _db.Database(tmp_path / "rogue.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    holder.swap(EngineSnapshot(
        rogue=[RogueHost(ip="192.168.2.250", mac="11:22:33:44:55:66",
                         vendor="TestCo", online=True)]))
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        expected = [{"ip": "192.168.2.250", "mac": "11:22:33:44:55:66",
                     "vendor": "TestCo", "online": True}]
        assert c.get("/api/rogue").json() == expected
        assert c.get("/api/dashboard").json()["rogue"] == expected
    asyncio.get_event_loop().run_until_complete(database.close())


def test_logs_endpoint_empty_without_file(client):
    """No log file wired -> empty tail, not an error (the System logs tab)."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/logs")
    assert r.status_code == 200
    data = r.json()
    assert data["lines"] == []
    assert data["total"] == 0 and data["truncated"] is False


def test_logs_endpoint_tails_file(tmp_path):
    """/api/logs reads the gateway log file newest-first, honoring ?limit=."""
    import asyncio
    logf = tmp_path / "quota.log"
    logf.write_text(
        "2026-08-05 10:00:00,000 INFO quota.api: one\n"
        "2026-08-05 10:00:01,000 WARNING quota.engine: two\n"
        "2026-08-05 10:00:02,000 ERROR quota.nftables: three\n",
        encoding="utf-8")
    database = _db.Database(tmp_path / "logapi.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, log_path=logf)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        full = c.get("/api/logs").json()
        assert full["total"] == 3 and full["truncated"] is False
        assert full["lines"][0].startswith("2026-08-05 10:00:02")  # newest first
        assert "ERROR" in full["lines"][0]
        tail = c.get("/api/logs?limit=2").json()
        assert len(tail["lines"]) == 2 and tail["truncated"] is True
    asyncio.get_event_loop().run_until_complete(database.close())
