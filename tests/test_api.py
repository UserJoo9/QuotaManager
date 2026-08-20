"""API integration tests (FastAPI TestClient + real temp SQLite DB)."""

from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import ReportConfig
from quota import db as _db
from quota.engine import GATEWAY_MAC, EngineSnapshot, RogueHost, SnapshotHolder
from quota.service import GB, QuotaService

TZ = ZoneInfo("Africa/Cairo")


def _login(c: TestClient) -> None:
    """Every admin route now requires a valid session cookie (auth change)."""
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def _login_wan(c: TestClient) -> None:
    """Log in AND move off the factory-default password — the WAN activation
    gate refuses Strong WAN mode while the default password is in use — then
    re-login (a password change rotates the session token)."""
    _login(c)
    r = c.post("/api/password", json={
        "current": "admin", "new": "Str0ng!Passw0rd42"})
    assert r.status_code == 200, r.text
    assert c.post("/api/login",
                  json={"password": "Str0ng!Passw0rd42"}).status_code == 200


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


def test_login_rate_limit(client):
    """Progressive backoff (authentication hardening): the first few failures
    are answered instantly (typo-friendly), then the limiter escalates to 429
    with an increasing Retry-After — and the correct password is throttled
    too (no guessing funnel)."""
    c, _, _ = client
    # attempts 1-4: instant 401s (free tier + the failure that arms backoff)
    for i in range(4):
        r = c.post("/api/login", json={"password": f"wrong-{i}"})
        assert r.status_code == 401
    # 5+: 429 + a Retry-After (the attempt is never processed). The exact
    # ladder (1s, 2s, 4s, ...) is timing-dependent under load and asserted
    # deterministically in the _LoginLimiter unit test (test_security.py);
    # here we pin the contract: throttled, with a sane non-decreasing header.
    retries = []
    for i in range(4, 6):
        r = c.post("/api/login", json={"password": f"wrong-{i}"})
        assert r.status_code == 429
        retries.append(int(r.headers.get("Retry-After", "0")))
    assert retries[0] >= 1 and retries[1] >= retries[0]
    # the correct password is also throttled mid-backoff (no guessing funnel)
    r = c.post("/api/login", json={"password": "admin"})
    assert r.status_code == 429


def test_legacy_password_hash_verified_and_rehashed(client):
    """Pre-v0.2.1 hashes were PBKDF2 at 200k in the 2-part format — they must
    still verify, and a successful login must upgrade the stored hash to the
    600k 3-part format."""
    import hashlib
    import secrets
    from api.app import PBKDF2_ITERATIONS, _hash_password

    c, db, _ = client
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", b"admin", salt, 200_000)
    legacy = f"{salt.hex()}${dk.hex()}"
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        db.set_setting("admin_password", legacy))

    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    stored = asyncio.get_event_loop().run_until_complete(
        db.get_setting("admin_password", ""))
    parts = stored.split("$")
    assert len(parts) == 3, "legacy hash must be upgraded to the 3-part format"
    assert int(parts[1]) == PBKDF2_ITERATIONS
    assert _hash_password("admin", iterations=PBKDF2_ITERATIONS).count("$") == 2


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

    # dashboard should list it (owned by an auto-created user). The gateway
    # box's own protected user + device are always seeded alongside.
    r = c.get("/api/dashboard")
    data = r.json()
    assert data["total_devices"] == 2
    assert data["total_users"] == 2
    dev = next(d for d in data["devices"] if d["id"] == dev_id)
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
    # only the gateway box's own device remains
    assert c.get("/api/dashboard").json()["total_devices"] == 1


def test_network_and_user_speed_caps(client):
    c, _, _ = client
    _login(c)
    # defaults: shaping off, no totals, AQM on, VPN share off, random-MAC gate off
    n = c.get("/api/network").json()
    assert n == {"enabled": False, "total_down_mbps": 0.0,
                 "total_up_mbps": 0.0, "aqm": True, "lan_rate_mbps": 1000.0,
                 "vpn_share": {"enabled": False, "interface": ""},
                 "decline_random_macs": False}

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

    # LAN pass-through rate round-trips independently of the WAN totals
    r = c.post("/api/network", json={"lan_rate_mbps": 250})
    assert r.json()["lan_rate_mbps"] == 250.0
    assert r.json()["total_down_mbps"] == 100.0   # WAN untouched
    r = c.post("/api/network", json={"lan_rate_mbps": 0})
    assert r.json()["lan_rate_mbps"] == 0.0

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
    # use more than allowance (bundle default 140, the protected Gateway user
    # takes 1.0 fixed off the top, so one auto device -> 139)
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
    assert r.json()["allowance_gb"] >= 159

    r = c.get("/api/dashboard")
    assert r.json()["blocked_count"] == 0


def test_orphaned_usage_endpoints_removed(client):
    """GET /api/usage, /api/usage/{id} and /api/events were dead routes (no
    UI/JS consumer) — removed in the v0.2.1 cleanup. Assert they 404 so a
    future re-introduction is a deliberate act."""
    c, _, _ = client
    _login(c)
    assert c.get("/api/usage").status_code == 404
    assert c.get("/api/usage/1").status_code == 404
    assert c.get("/api/events").status_code == 404


def test_dashboard_shaping_and_interface_label(tmp_path):
    """The WS/dashboard payload carries the live shaping state (Network
    preview's "applying…") and per-device WiFi/LAN labels from the neigh
    collector + config interface_tags."""
    database = _db.Database(tmp_path / "api2.db")
    service = QuotaService(database, timezone="Africa/Cairo")

    async def _init():
        await database.connect()
    import asyncio
    asyncio.get_event_loop().run_until_complete(_init())

    app = create_app(
        database, service, SnapshotHolder(),
        interface_tags={"eth0": "LAN", "wlan0": "WiFi"},
        shaping_state_getter=lambda: {"available": True, "applied": False})
    try:
        with TestClient(app) as c:
            _login(c)
            r = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:09",
                                             "quota_mode": "auto"})
            dev_id = r.json()["id"]
            async def _tag():
                await database.update_device(dev_id, source_interface="wlan0")
            asyncio.get_event_loop().run_until_complete(_tag())
            dash = c.get("/api/dashboard").json()
            assert dash["shaping"] == {"available": True, "applied": False}
            dev = next(d for d in dash["devices"] if d["id"] == dev_id)
            assert dev["source_interface"] == "wlan0"
            assert dev["interface_label"] == "WiFi"

            # unmapped NICs get NO box-side label — every client arrives on
            # the same wired NIC, so a guessed "LAN"/"eth0" would lie about
            # WiFi devices; the router-side access probe owns that verdict.
            async def _tag2():
                await database.update_device(dev_id, source_interface="eth1")
            asyncio.get_event_loop().run_until_complete(_tag2())
            dash2 = c.get("/api/dashboard").json()
            dev2 = next(d for d in dash2["devices"] if d["id"] == dev_id)
            assert dev2["interface_label"] == ""
            async def _tag3():
                await database.update_device(dev_id, source_interface="eth0")
            asyncio.get_event_loop().run_until_complete(_tag3())
            dash3 = c.get("/api/dashboard").json()
            dev3 = next(d for d in dash3["devices"] if d["id"] == dev_id)
            assert dev3["interface_label"] == "LAN"  # mapped tag wins
    finally:
        asyncio.get_event_loop().run_until_complete(database.close())


def test_milestone_notify_requires_owner(tmp_path):
    """/api/milestone/notify is session-less but must only let a device
    acknowledge ITS OWN user's milestones (resolved by source IP) — a sibling
    must not clear another user's pills. Unknown sources are denied too."""
    database = _db.Database(tmp_path / "api3.db")
    service = QuotaService(database, timezone="Africa/Cairo")

    async def _init():
        await database.connect()
    import asyncio
    asyncio.get_event_loop().run_until_complete(_init())

    # A dedicated client whose source IP we control (starlette's default test
    # client reports "testclient" — the gate must see a real lease IP).
    app = create_app(database, service, SnapshotHolder())
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as c:
            _login(c)
            r = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:02",
                                             "name": "TV", "quota_mode": "fixed",
                                             "fixed_gb": 5})
            other_user = r.json()["user_id"]
            import asyncio
            gw_dev = asyncio.get_event_loop().run_until_complete(
                database.get_device(mac=GATEWAY_MAC))
            gw_user = asyncio.get_event_loop().run_until_complete(
                database.get_user(gw_dev.user_id))

            def _notify(user_id):
                # 127.0.0.1 has no lease row yet -> 403 for any user, proving the
                # IP-ownership gate runs before the write
                return c.post("/api/milestone/notify",
                              json={"user_id": user_id, "milestone": 50})

            assert _notify(other_user).status_code == 403
            assert _notify(gw_user.id).status_code == 403

            # Happy path: the requesting IP holds a lease owned by the user.
            async def _lease():
                await database.set_lease("aa:bb:cc:dd:ee:02", "127.0.0.1")
                await service.milestone_state()  # computes milestone flags on demand
            asyncio.get_event_loop().run_until_complete(_lease())
            assert _notify(other_user).status_code == 200
            assert _notify(gw_user.id).status_code == 403
    finally:
        # a failed assertion above must still release the aiosqlite worker
        # thread, or the pytest process never exits
        asyncio.get_event_loop().run_until_complete(database.close())


def test_bundle_recharge_grows_total(client):
    c, db, _ = client
    _login(c)
    # one auto device: share = 139 at first (the protected Gateway user takes
    # 1.0 fixed off the top)
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
    assert b["allowances"][str(user_id)] == 189.0  # auto share grew (Gateway keeps 1.0)

    dash = c.get("/api/dashboard").json()
    assert dash["bundle"]["remaining_gb"] == pytest.approx(190.0)


def test_password_change_requires_session(client):
    """Not logged in -> 401 (client shows the login screen), not a wrong-password 400."""
    c, _, _ = client
    r = c.post("/api/password",
               json={"current": "admin", "new": "Str0ng!Passw0rd42"})
    assert r.status_code == 401, r.text


def test_password_change_wrong_current_is_400(client):
    """Wrong current password -> 400 (bad request), so the client can show
    'Current password is wrong.' instead of logging the user out."""
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/password",
               json={"current": "wrong", "new": "Str0ng!Passw0rd42"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "current password incorrect"
    # old password still works
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_password_change_success(client):
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/password",
               json={"current": "admin", "new": "Str0ng!Passw0rd42"})
    assert r.status_code == 200, r.text
    # new password logs in, old one is rejected
    assert c.post("/api/login",
                  json={"password": "Str0ng!Passw0rd42"}).status_code == 200
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


def test_bundle_period_type_round_trip(client):
    """The bundle type ('renew_day' / 'end_of_month') is stored, returned, and
    drives the effective reset day — end_of_month honors the configured day too
    (many ISPs close the month on the 25th/28th), so it's kept, not ignored."""
    c, _, _ = client
    _login(c)
    # default
    b = c.get("/api/bundle").json()
    assert b["period_type"] == "renew_day"
    # switch to an end-of-month bill with the ISP's month-end day
    r = c.post("/api/bundle", json={"period_type": "end_of_month",
                                    "reset_day": 25})
    assert r.status_code == 200, r.text
    b = c.get("/api/bundle").json()
    assert b["period_type"] == "end_of_month"
    assert b["reset_day"] == 25  # the day is kept, not forced to 1
    dash = c.get("/api/dashboard").json()
    assert dash["bundle"]["period_type"] == "end_of_month"
    assert dash["bundle"]["reset_day"] == 25
    assert dash["bundle"]["days_left"] >= 0  # automatic monthly reset, not -1
    # invalid type rejected
    assert c.post("/api/bundle", json={"period_type": "weekly"}).status_code == 400
    # back to renew-day
    r = c.post("/api/bundle", json={"period_type": "renew_day",
                                    "reset_day": 0})
    assert r.status_code == 200, r.text
    b = c.get("/api/bundle").json()
    assert b["period_type"] == "renew_day"
    assert b["reset_day"] == 0


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
        "current_password": "admin", "new_password": "Str0ng!Passw0rd42"})
    assert r.status_code == 200, r.text
    # bundle updated + dashboard owns it now (config.yaml stops overriding)
    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 60.0
    assert b["reset_day"] == 15
    assert c.get("/api/setup").json()["setup_complete"] is True
    # new password logs in, old one is rejected
    assert c.post("/api/login",
                  json={"password": "Str0ng!Passw0rd42"}).status_code == 200
    assert c.post("/api/login", json={"password": "admin"}).status_code == 401


def test_setup_password_only_keeps_bundle_source(client):
    """A password-only save must NOT take bundle ownership from config.yaml."""
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={
        "current_password": "admin", "new_password": "Str0ng!Passw0rd42"})
    assert r.status_code == 200, r.text
    b = c.get("/api/bundle").json()
    assert b["total_gb"] == 140.0   # untouched
    assert b["reset_day"] == 1
    assert c.get("/api/setup").json()["setup_complete"] is True


def test_setup_wrong_current_password_is_400(client):
    c, _, _ = client
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200
    r = c.post("/api/setup/complete", json={
        "current_password": "wrong", "new_password": "Str0ng!Passw0rd42"})
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
    # the protected Gateway user + its device are always seeded alongside
    assert dash["total_users"] == 2 and dash["total_devices"] == 3
    u = next(u for u in dash["users"] if u["id"] == uid)
    assert u["name"] == "Dad"
    assert u["allowance_gb"] == 20.0
    assert len(u["devices"]) == 2

    # user-level block cuts both devices at once
    r = c.patch(f"/api/users/{uid}", json={"block": True})
    assert r.status_code == 200
    assert c.get("/api/dashboard").json()["blocked_count"] == 2
    # resolved, not persisted: the device row reports the user cut
    d = c.get("/api/devices").json()
    assert next(x for x in d if x["user_id"] == uid)["block_state"] == "admin_off"

    c.patch(f"/api/users/{uid}", json={"block": False})
    assert c.get("/api/dashboard").json()["blocked_count"] == 0

    # rename
    c.patch(f"/api/users/{uid}", json={"name": "Dad ✱"})
    assert next(u for u in c.get("/api/dashboard").json()["users"]
                if u["id"] == uid)["name"] == "Dad ✱"

    # delete cascades: user + both devices
    r = c.delete(f"/api/users/{uid}")
    assert r.status_code == 200
    assert r.json()["devices_removed"] == 2
    dash = c.get("/api/dashboard").json()
    # only the protected Gateway user + device remain
    assert dash["total_devices"] == 1
    assert dash["total_users"] == 1


def test_user_topup_via_api(client):
    c, db, service = client
    _login(c)
    uid = c.post("/api/users", json={"name": "Kid", "quota_mode": "auto"}).json()["id"]
    dev_id = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:43",
                                          "user_id": uid}).json()["id"]
    # use more than the user's allowance (bundle 140, one auto user -> 139;
    # the protected Gateway user takes 1.0 fixed off the top)
    import asyncio
    async def _add():
        await db.add_usage(dev_id, "2026-08-01", int(150 * GB), 0)
        await service.evaluate_blocks()
    asyncio.get_event_loop().run_until_complete(_add())
    assert c.get("/api/dashboard").json()["blocked_count"] == 1

    r = c.post(f"/api/users/{uid}/topup", json={"extra_gb": 20})
    assert r.status_code == 200
    assert r.json()["allowance_gb"] >= 159
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
    # (the protected Gateway device belongs to the Gateway user, not A)
    for dv in dash["devices"]:
        if dv["user_id"] == uid:
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
    # (find Dad by id — the protected Gateway user is seeded first)
    assert next(u for u in dash["users"] if u["id"] == uid)["used_gb"] == pytest.approx(6.0)


def test_guest_defaults(client):
    """Guest mode is off by default with a 1 GB guest allowance, a default
    guest limit of 2, no default guest speed cap and stop-new off."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/guest")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "quota_gb": 1.0,
                        "limit": 2, "speed_limit_mbps": 0.0,
                        "stop_new": False}


def test_guest_enable_and_quota(client):
    """POST /api/guest toggles the flag and/or the allowance independently."""
    c, _, _ = client
    _login(c)
    r = c.post("/api/guest", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "quota_gb": 1.0,
                        "limit": 2, "speed_limit_mbps": 0.0,
                        "stop_new": False}

    r = c.post("/api/guest", json={"quota_gb": 5})
    assert r.json() == {"enabled": True, "quota_gb": 5.0,
                        "limit": 2, "speed_limit_mbps": 0.0,
                        "stop_new": False}

    r = c.post("/api/guest", json={"enabled": False})
    assert r.json() == {"enabled": False, "quota_gb": 5.0,
                        "limit": 2, "speed_limit_mbps": 0.0,
                        "stop_new": False}


def test_guest_limit_and_stop_new(client):
    """POST /api/guest also sets the guest-limit cap and the
    stop-new-connections gate independently."""
    c, _, _ = client
    _login(c)
    r = c.post("/api/guest", json={"limit": 3, "stop_new": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "quota_gb": 1.0,
                        "limit": 3, "speed_limit_mbps": 0.0,
                        "stop_new": True}

    r = c.post("/api/guest", json={"limit": 1})
    assert r.json()["limit"] == 1
    assert r.json()["stop_new"] is True  # stop_new untouched by a limit edit

    r = c.post("/api/guest", json={"stop_new": False})
    assert r.json()["stop_new"] is False
    assert r.json()["limit"] == 1


def test_guest_speed_limit_round_trip(client):
    """POST /api/guest sets the default guest speed cap (Mbps) independently
    of every other guest setting; 0 lifts the cap (unlimited)."""
    c, _, _ = client
    _login(c)
    r = c.post("/api/guest", json={"speed_limit_mbps": 8})
    assert r.status_code == 200, r.text
    assert r.json()["speed_limit_mbps"] == 8.0
    assert r.json()["limit"] == 2          # untouched by a speed edit
    assert r.json()["quota_gb"] == 1.0

    r = c.get("/api/guest")
    assert r.json()["speed_limit_mbps"] == 8.0   # persisted

    r = c.post("/api/guest", json={"speed_limit_mbps": 0})
    assert r.json()["speed_limit_mbps"] == 0.0   # unlimited

    # negative values are rejected by the schema
    r = c.post("/api/guest", json={"speed_limit_mbps": -1})
    assert r.status_code == 422


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
    by_mac = {d["mac"]: d for d in dash["devices"]}
    assert by_mac["aa:bb:cc:dd:ee:91"]["guest"] is True


def test_connected_follows_arp_responders(tmp_path):
    """With the ARP probe running, a leased device is "connected" only if it
    ALSO answered the latest sweep — a lease alone lags reality by up to
    LEASE_HOURS (asleep/off/another-network devices must go grey)."""
    import asyncio
    database = _db.Database(tmp_path / "api-arp.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()

    responders = {"192.168.2.50"}
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(
        database, service, holder,
        active_ips_getter=lambda: set(responders))
    try:
        with TestClient(app) as c:
            _login(c)
            async def _seed():
                g = await database.create_user(
                    name="", quota_mode=_db.QUOTA_FIXED,
                    fixed_gb=1.0, guest=True)
                await database.upsert_device(
                    "aa:bb:cc:dd:ee:94", name="Phone", user_id=g.id)
                await database.set_lease("aa:bb:cc:dd:ee:94", "192.168.2.50")
                await database.upsert_device(
                    "aa:bb:cc:dd:ee:95", name="Tablet", user_id=g.id)
                await database.set_lease("aa:bb:cc:dd:ee:95", "192.168.2.51")
            asyncio.get_event_loop().run_until_complete(_seed())

            dash = c.get("/api/dashboard").json()
            by_mac = {d["mac"]: d for d in dash["devices"]}
            # answering the sweep => blue LED; leased-but-silent => grey
            assert by_mac["aa:bb:cc:dd:ee:94"]["connected"] is True
            assert by_mac["aa:bb:cc:dd:ee:95"]["connected"] is False

            # the phone stops answering (asleep) => grey on the next payload
            responders.clear()
            dash2 = c.get("/api/dashboard").json()
            by_mac2 = {d["mac"]: d for d in dash2["devices"]}
            assert by_mac2["aa:bb:cc:dd:ee:94"]["connected"] is False
    finally:
        asyncio.get_event_loop().run_until_complete(database.close())


def test_disabled_user_surfaces_in_dashboard(client):
    """A disabled user (a new device's onboarding lock) shows quota_mode=
    disabled, 0 GB, blocked — the admin sees at a glance which users still
    need a shared/fixed assignment."""
    c, db, _ = client
    _login(c)
    import asyncio
    async def _seed():
        u = await db.create_user("New", _db.QUOTA_DISABLED, 0.0)
        await db.upsert_device("aa:bb:cc:dd:ee:99", name="New Phone",
                               user_id=u.id)
    asyncio.get_event_loop().run_until_complete(_seed())

    dash = c.get("/api/dashboard").json()
    user = next(u for u in dash["users"] if u["name"] == "New")
    assert user["quota_mode"] == "disabled"
    assert user["allowance_gb"] == 0.0
    assert user["blocked"] is True
    dev = next(d for d in dash["devices"] if d["mac"] == "aa:bb:cc:dd:ee:99")
    assert dev["block_state"] == "quota"
    # assigning shared via the API (what the modal does) unblocks the user
    r = c.patch(f"/api/users/{user['id']}",
                json={"quota_mode": _db.QUOTA_AUTO, "fixed_gb": None})
    assert r.status_code == 200, r.text
    dash2 = c.get("/api/dashboard").json()
    user2 = next(u for u in dash2["users"] if u["id"] == user["id"])
    assert user2["quota_mode"] == "auto"
    assert user2["blocked"] is False


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
    # every non-protected user is a guest (the Gateway user is protected)
    assert all(u["guest"] for u in dash["users"] if not u["protected"])


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

    # guest + Dad + the always-seeded protected Gateway user
    assert c.get("/api/dashboard").json()["total_users"] == 3
    r = c.post("/api/reset-month")
    assert r.status_code == 200, r.text
    dash = c.get("/api/dashboard").json()
    # the guest is gone; Dad + the protected Gateway user remain
    assert dash["total_users"] == 2
    assert next(u for u in dash["users"] if not u["protected"])["name"] == "Dad"
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
    password). Sensitive-data hardening: the stored password is NEVER shipped
    — ``pppoe_password`` is masked and ``pppoe_has_password`` tells the UI a
    value exists. The endpoint never 500s."""
    c, _, _ = client
    _login(c)
    r = c.get("/api/wan")
    assert r.status_code == 200
    assert r.json() == {"pppoe_user": "", "pppoe_password": "********",
                        "pppoe_has_password": False, "wan_if": ""}
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
            "pppoe_user": "", "pppoe_password": "********",
            "pppoe_has_password": False, "wan_if": "",
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
    _login_wan(c)
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
        _login_wan(c)
        # save creds first
        r = c.post("/api/wan", json={"topology": "wan", "pppoe_user": "u@isp",
                                     "pppoe_password": "s3cret"})
        assert r.status_code == 200, r.text
        # a LAN revert carries no creds -> they must survive
        r = c.post("/api/wan", json={"topology": "lan"})
        assert r.status_code == 200, r.text
        assert c.get("/api/wan").json()["pppoe_user"] == "u@isp"
        # the stored secret is NEVER shipped — masked + a presence flag instead
        assert c.get("/api/wan").json()["pppoe_password"] == "********"
        assert c.get("/api/wan").json()["pppoe_has_password"] is True
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
        _login_wan(c)
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
        _login_wan(c)
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


def test_wan_renew_restarts_pppoe(tmp_path):
    """v24: POST /api/wan/renew invokes the wired renew callback (the gateway's
    _renew_wan_ip) and returns its restart result. Only allowed while WAN mode
    is active AND ppp0 is up."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "wan", "configured": "wan", "source": "dashboard",
        "pending": "wan", "ppp0": "up", "ppp_ip": "1.2.3.4", "ppp_peer": "",
    }))
    calls: list[bool] = []

    async def _renew():
        calls.append(True)
        return {"restarted": True, "state": "active", "detail": "dialed"}

    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, wan_renew=_renew)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan/renew")
        assert r.status_code == 200, r.text
        assert r.json() == {"restarted": True, "state": "active",
                            "detail": "dialed"}
    assert calls == [True]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_renew_requires_wan_and_ppp0_up(tmp_path):
    """v24: a renewal is refused while ppp0 is down (nothing to renew into) or
    WAN mode isn't active (no PPPoE line) — 409, and the callback never runs."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew-gate.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    calls: list[bool] = []

    async def _renew():
        calls.append(True)
        return {"restarted": True, "state": "active", "detail": ""}

    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, wan_renew=_renew)

    def _wan(**overrides):
        base = {"topology": "wan", "configured": "wan", "source": "dashboard",
                "pending": "wan", "ppp0": "up", "ppp_ip": "1.2.3.4",
                "ppp_peer": ""}
        base.update(overrides)
        return base

    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        holder.swap(EngineSnapshot(wan_status=_wan(ppp0="down")))
        r = c.post("/api/wan/renew")
        assert r.status_code == 409
        assert "ppp0 is down" in r.json()["detail"]
        holder.swap(EngineSnapshot(wan_status=_wan(topology="lan", ppp0="n/a")))
        r = c.post("/api/wan/renew")
        assert r.status_code == 409
        assert "not active" in r.json()["detail"]
        # no snapshot yet ({} -> not wan) is also refused
        holder.swap(EngineSnapshot())
        r = c.post("/api/wan/renew")
        assert r.status_code == 409
    assert calls == [], "the callback must never run when the gates refuse"
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_renew_requires_callback(tmp_path):
    """No renew callback wired (degraded boot / tests without a gateway) -> 503."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew-no-cb.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "wan", "configured": "wan", "source": "dashboard",
        "pending": "wan", "ppp0": "up", "ppp_ip": "1.2.3.4", "ppp_peer": "",
    }))
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)  # wan_renew=None
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan/renew")
        assert r.status_code == 503
        assert "degraded boot" in r.json()["detail"]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_renew_failure_is_500(tmp_path):
    """A callback that raises (e.g. systemctl missing) -> HTTP 500 with the cause."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew-fail.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(wan_status={
        "topology": "wan", "configured": "wan", "source": "dashboard",
        "pending": "wan", "ppp0": "up", "ppp_ip": "1.2.3.4", "ppp_peer": "",
    }))

    async def _renew():
        raise RuntimeError("systemctl not found")

    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, wan_renew=_renew)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        r = c.post("/api/wan/renew")
        assert r.status_code == 500
        assert "WAN renew failed" in r.json()["detail"]
        assert "systemctl not found" in r.json()["detail"]
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_renew_requires_auth(tmp_path):
    """The renew endpoints sit behind the same session gate as the rest of /api."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew-auth.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder, wan_renew=lambda: {})
    with TestClient(app) as c:
        assert c.post("/api/wan/renew").status_code == 401
        assert c.post("/api/wan/renew-config",
                      json={"enabled": True, "minutes": 15}).status_code == 401
    asyncio.get_event_loop().run_until_complete(database.close())


def test_wan_renew_config_round_trip(tmp_path):
    """v24: POST /api/wan/renew-config persists the auto-renew schedule with the
    minutes clamped to the 5-minute floor and returns the stored config."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew-cfg.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        # below the floor -> clamped up to 5
        r = c.post("/api/wan/renew-config", json={"enabled": True, "minutes": 2})
        assert r.status_code == 200, r.text
        assert r.json() == {"enabled": True, "minutes": 5, "last": ""}
        # a longer interval is kept
        r = c.post("/api/wan/renew-config", json={"enabled": False, "minutes": 30})
        assert r.json() == {"enabled": False, "minutes": 30, "last": ""}
        # persisted, not just returned
        async def _read():
            return (await database.get_setting("wan_ip_renew_enabled", None),
                    await database.get_setting("wan_ip_renew_minutes", None))
        enabled, minutes = asyncio.get_event_loop().run_until_complete(_read())
        assert (enabled, minutes) == ("0", "30")
    asyncio.get_event_loop().run_until_complete(database.close())


def test_dashboard_surfaces_renew_schedule(tmp_path):
    """The renew schedule rides the wan_status snapshot into both /api/wan and
    the dashboard payload, so the WAN tab shows the toggle + last-renewed line
    without a separate query."""
    import asyncio
    database = _db.Database(tmp_path / "wan-renew-keys.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    base = {"topology": "wan", "configured": "wan", "source": "dashboard",
            "pending": "wan", "ppp0": "up", "ppp_ip": "1.2.3.4", "ppp_peer": "",
            "renew_enabled": True, "renew_minutes": 20,
            "renew_last": "2026-08-15T00:00:00+00:00"}
    holder.swap(EngineSnapshot(wan_status=base))
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        data = c.get("/api/wan").json()
        assert data["renew_enabled"] is True
        assert data["renew_minutes"] == 20
        assert data["renew_last"] == "2026-08-15T00:00:00+00:00"
        wan = c.get("/api/dashboard").json()["wan"]
        assert wan["renew_enabled"] is True
        assert wan["renew_minutes"] == 20
        assert wan["renew_last"] == "2026-08-15T00:00:00+00:00"
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


# ---------------------------------------------------------------------------
# protected "Gateway" user + delete -> MAC blacklist (deny list)
# ---------------------------------------------------------------------------

def test_gateway_user_seeded_and_protected(client):
    """connect() seeds a protected Gateway user + the box's device: the user
    can be edited (allowance, block) but never deleted, and its device is
    marked ``gateway`` with an empty vendor (the sentinel MAC resolves to
    "XEROX CORPORATION" in oui.txt otherwise)."""
    c, _, _ = client
    _login(c)
    dash = c.get("/api/dashboard").json()
    gwu = next(u for u in dash["users"] if u.get("protected"))
    assert gwu["name"] == "Gateway"
    assert gwu["guest"] is False
    assert gwu["allowance_gb"] == 1.0
    gwd = next(d for d in dash["devices"] if d.get("gateway"))
    assert gwd["mac"] == "00:00:00:00:00:00"
    assert gwd["vendor"] == ""
    assert gwd["name"] == "Gateway box"

    # the protected user cannot be deleted
    r = c.delete(f"/api/users/{gwu['id']}")
    assert r.status_code == 400, r.text
    # ...but its allowance can be dropped to 0 (cuts the box's own internet)
    r = c.patch(f"/api/users/{gwu['id']}", json={"fixed_gb": 0})
    assert r.status_code == 200
    assert next(u for u in c.get("/api/dashboard").json()["users"]
                if u.get("protected"))["allowance_gb"] == 0.0


def test_gateway_payload_reports_desired_vs_programmed_block(client):
    """The dashboard's ``gateway`` object separates the box's DESIRED block
    (the Gateway user's resolved state) from what the engine actually pushed
    to the kernel (blocked_programmed / engine_available). This is what lets
    the UI show "Blocked but NOT cut at the kernel" — the exact failure the
    user hit — instead of silently claiming enforcement."""
    c, _, _ = client
    _login(c)
    dash = c.get("/api/dashboard").json()
    gwu = next(u for u in dash["users"] if u.get("protected"))
    # holder is a bare SnapshotHolder -> engine state is the default
    # (gateway_blocked=None = never programmed, engine_available=True).
    assert dash["gateway"]["blocked_desired"] is False
    assert dash["gateway"]["blocked_programmed"] is None
    assert dash["gateway"]["engine_available"] is True

    # Block the box in the UI (user-level admin cut, resolved at render time).
    assert c.patch(f"/api/users/{gwu['id']}", json={"block": True}).status_code == 200
    dash = c.get("/api/dashboard").json()
    assert dash["gateway"]["blocked_desired"] is True
    # ...but the engine never programmed the set -> the UI would show a warning.
    assert dash["gateway"]["blocked_programmed"] is None


def test_gateway_payload_engine_programmed_but_ui_free(tmp_path):
    """A programmed cut with a free UI toggle: the box is cut at the kernel
    even though the Gateway user card reads unblocked — the reconciliation
    (desired False vs programmed True) is the reverse of the stale-engine
    case and is surfaced the same way."""
    import asyncio
    database = _db.Database(tmp_path / "gw.db")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot(gateway_blocked=True, engine_available=True))
    service = QuotaService(database, timezone="Africa/Cairo")
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)
    try:
        with TestClient(app) as c:
            _login(c)
            dash = c.get("/api/dashboard").json()
            assert dash["gateway"]["blocked_desired"] is False
            assert dash["gateway"]["blocked_programmed"] is True
            assert dash["gateway"]["engine_available"] is True
    finally:
        asyncio.get_event_loop().run_until_complete(database.close())


def test_gateway_device_cannot_be_recreated_or_reassigned(client):
    """The box's sentinel MAC is reserved: no create, no reassign to a user."""
    c, _, _ = client
    _login(c)
    r = c.post("/api/devices", json={"mac": "00:00:00:00:00:00"})
    assert r.status_code == 400, r.text
    dash = c.get("/api/dashboard").json()
    gwd = next(d for d in dash["devices"] if d.get("gateway"))
    u2 = c.post("/api/users", json={"name": "Dad"}).json()["id"]
    r = c.patch(f"/api/devices/{gwd['id']}", json={"user_id": u2})
    assert r.status_code == 400, r.text
    # deleting the box's device is also refused (the user owns it)
    r = c.delete(f"/api/devices/{gwd['id']}")
    assert r.status_code == 400, r.text


def test_delete_device_blacklists_mac(client):
    """A manual DELETE of a device blacklists its MAC (permanent deny list):
    run.py never auto-registers it again while it stays connected, and the
    Network-tab blacklist is the only way back in."""
    import asyncio
    c, db, _ = client
    _login(c)
    g = asyncio.get_event_loop().run_until_complete(db.create_user(
        name="", quota_mode=_db.QUOTA_FIXED, fixed_gb=1.0, guest=True))
    dev = asyncio.get_event_loop().run_until_complete(db.upsert_device(
        "aa:bb:cc:dd:ee:99", name="Phone", user_id=g.id))
    assert asyncio.get_event_loop().run_until_complete(
        db.get_mac_list("deny")) == []

    r = c.delete(f"/api/devices/{dev.id}")
    assert r.status_code == 200, r.text
    assert asyncio.get_event_loop().run_until_complete(
        db.get_mac_list("deny")) == ["aa:bb:cc:dd:ee:99"]


def test_delete_user_blacklists_its_macs(client):
    """Deleting a USER blacklists every device MAC it owned."""
    import asyncio
    c, db, _ = client
    _login(c)
    g = asyncio.get_event_loop().run_until_complete(db.create_user(
        name="", quota_mode=_db.QUOTA_FIXED, fixed_gb=1.0, guest=True))
    asyncio.get_event_loop().run_until_complete(db.upsert_device(
        "aa:bb:cc:dd:ee:98", user_id=g.id))
    r = c.delete(f"/api/users/{g.id}")
    assert r.status_code == 200, r.text
    assert asyncio.get_event_loop().run_until_complete(
        db.get_mac_list("deny")) == ["aa:bb:cc:dd:ee:98"]


def test_delete_normal_user_blacklists_its_macs(client):
    """A NORMAL user's devices are blacklisted too (no guest-only carve-out):
    deleting the user removes the cards AND the kernel keeps blocking the
    still-connected devices."""
    import asyncio
    c, db, _ = client
    _login(c)
    u = asyncio.get_event_loop().run_until_complete(db.create_user(
        name="Dad", quota_mode=_db.QUOTA_FIXED, fixed_gb=20.0))
    asyncio.get_event_loop().run_until_complete(db.upsert_device(
        "aa:bb:cc:dd:ee:97", name="Phone", user_id=u.id))
    r = c.delete(f"/api/users/{u.id}")
    assert r.status_code == 200, r.text
    assert asyncio.get_event_loop().run_until_complete(
        db.get_mac_list("deny")) == ["aa:bb:cc:dd:ee:97"]
    # the device row is gone but the MAC stays blacklisted
    assert asyncio.get_event_loop().run_until_complete(
        db.get_device(mac="aa:bb:cc:dd:ee:97")) is None
    # a deleted user's MAC is hidden from the dashboard and the report
    dash = c.get("/api/dashboard").json()
    assert all(d["mac"] != "aa:bb:cc:dd:ee:97" for d in dash["devices"])
    assert all(dev["mac"] != "aa:bb:cc:dd:ee:97"
               for u in dash["users"] for dev in u["devices"])
    holder = SnapshotHolder()
    with _client_from(create_app(db, QuotaService(db, timezone="Africa/Cairo"),
                                 holder,
                                 report_config=ReportConfig(
                                     enabled=True, allow_client_subnet=True,
                                     allowed_ips=[],
                                     client_subnet="192.168.2.0/24")),
                      "192.168.2.9") as rc:
        report = rc.get("/api/report")
        assert report.status_code == 200, report.text
        assert all(d["mac"] != "aa:bb:cc:dd:ee:97"
                   for u in report.json()["users"] for d in u["devices"])


def test_unblacklist_restores_device(client):
    """Removing a MAC from the deny list (Network tab) unblocks it: the device
    card reappears in the dashboard."""
    import asyncio
    c, db, _ = client
    _login(c)
    u = asyncio.get_event_loop().run_until_complete(db.create_user(
        name="Dad", quota_mode=_db.QUOTA_FIXED, fixed_gb=20.0))
    asyncio.get_event_loop().run_until_complete(db.upsert_device(
        "aa:bb:cc:dd:ee:96", name="Phone", user_id=u.id))
    assert c.delete(f"/api/users/{u.id}").status_code == 200
    assert asyncio.get_event_loop().run_until_complete(
        db.get_mac_list("deny")) == ["aa:bb:cc:dd:ee:96"]

    # un-blacklist via POST /api/mac-lists (an empty deny list)
    r = c.post("/api/mac-lists", json={"deny": []})
    assert r.status_code == 200, r.text
    assert asyncio.get_event_loop().run_until_complete(
        db.get_mac_list("deny")) == []


def test_blacklisted_device_visible_in_mac_lists_api(client):
    """A deleted device's MAC surfaces in GET /api/mac-lists (the Network-tab
    blacklist), which is the ONLY place it appears."""
    import asyncio
    c, db, _ = client
    _login(c)
    u = asyncio.get_event_loop().run_until_complete(db.create_user(
        name="Dad", quota_mode=_db.QUOTA_FIXED, fixed_gb=20.0))
    asyncio.get_event_loop().run_until_complete(db.upsert_device(
        "aa:bb:cc:dd:ee:95", name="Phone", user_id=u.id))
    assert c.delete(f"/api/users/{u.id}").status_code == 200
    lists = c.get("/api/mac-lists").json()
    assert lists["deny"] == ["aa:bb:cc:dd:ee:95"]


def test_speed_cap_edit_triggers_immediate_shaping_sync(tmp_path):
    """A Network-tab save or a device/user cap edit schedules an immediate
    shaper re-sync — the tc tree changes in the kernel right away instead of
    waiting up to 15 s for the next maintenance tick (the "needs a page
    refresh" lag)."""
    import asyncio
    import time
    database = _db.Database(tmp_path / "api.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())

    fired = []

    async def fake_sync():
        fired.append("sync")

    app = create_app(database, service, holder, shaping_sync=fake_sync)
    with TestClient(app) as c:
        _login(c)
        # every speed-affecting write must schedule a kernel re-sync:
        # 1 network settings save
        r = c.post("/api/network", json={"enabled": True, "total_down_mbps": 100})
        assert r.status_code == 200
        # 2 + 3 two device creates (each carries caps)
        d1 = c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:01",
                                          "name": "Phone",
                                          "quota_mode": "fixed",
                                          "fixed_gb": 10.0}).json()["id"]
        c.post("/api/devices", json={"mac": "aa:bb:cc:dd:ee:02",
                                     "name": "Tablet",
                                     "quota_mode": "fixed",
                                     "fixed_gb": 10.0})
        # 4 a device cap edit
        assert c.patch(f"/api/devices/{d1}",
                       json={"limit_down_mbps": 2}).status_code == 200
        # 5 a user create carrying an aggregate cap
        uid = c.post("/api/users", json={"name": "Mom", "quota_mode": "auto",
                                         "limit_down_mbps": 50}).json()["id"]
        # 6 a user cap edit
        assert c.patch(f"/api/users/{uid}",
                       json={"limit_up_mbps": 10}).status_code == 200

        # create_task is fire-and-forget on the portal loop; give it a moment
        # to drain before asserting (the HTTP responses return first).
        deadline = time.monotonic() + 2
        while len(fired) < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(fired) >= 6, fired

    asyncio.get_event_loop().run_until_complete(database.close())



# ---------------------------------------------------------------------------
# milestone page (public, on-demand) + source-IP-gated report
# ---------------------------------------------------------------------------

def _client_from(app, ip):
    """A TestClient that presents ``ip`` as its source address — sets
    ``request.client.host`` for the report/milestone IP logic."""
    return TestClient(app, client=(ip, 50000))


def _seed_milestone_user(d, svc, name, allowance_gb, used_gb, ip):
    """Fixed user + one device with a DHCP lease at ``ip`` + `used_gb` today."""
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt
    TZ2 = _ZI("Africa/Cairo")
    async def _inner():
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        u = await d.create_user(name, _db.QUOTA_FIXED, allowance_gb)
        dev = await d.upsert_device(f"de:ad:be:ef:{abs(hash(name)) % 0xffff:04x}",
                                    name="Phone", user_id=u.id)
        await svc.recompute_allowances()
        today = _dt.now(TZ2).date().isoformat()
        await d.add_usage(dev.id, today, int(used_gb * GB), 0)
        await d.set_lease(dev.mac, ip)
        return u, dev
    return _inner


def test_milestone_api_public_from_leased_device(tmp_path):
    """GET /api/milestone needs no session: the device's source IP resolves to
    its user and the payload carries the per-device breakdown."""
    import asyncio
    database = _db.Database(tmp_path / "ms.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    asyncio.get_event_loop().run_until_complete(
        _seed_milestone_user(database, service, "Mom", 40.0, 20.8,
                             "192.168.2.55")())
    app = create_app(database, service, holder)
    with _client_from(app, "192.168.2.55") as c:
        r = c.get("/api/milestone")
        assert r.status_code == 200
        data = r.json()
        assert data["recognized"] is True
        assert data["user"]["name"] == "Mom"
        assert data["user"]["percent"] > 50
        # JSON serializes int keys to strings: "50"/"75"/"100"
        ms = data["user"]["milestones"]
        assert ms["50"]["crossed"] is True
        assert ms["50"]["pending"] is True
        assert ms["75"]["crossed"] is False
        # per-device breakdown has the exact bytes
        assert len(data["devices"]) == 1
        dv = data["devices"][0]
        assert dv["name"] == "Phone"
        assert dv["device_used_gb"] > 20 and dv["device_used_gb"] < 21
    asyncio.get_event_loop().run_until_complete(database.close())


def test_milestone_api_unrecognized_ip(tmp_path):
    """A device with no lease gets a friendly 'unrecognized' payload, not an
    error."""
    import asyncio
    database = _db.Database(tmp_path / "ms.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)
    with _client_from(app, "192.168.2.99") as c:
        data = c.get("/api/milestone").json()
        assert data["recognized"] is False
        assert data["user"] is None
    asyncio.get_event_loop().run_until_complete(database.close())


def test_milestone_notify_marks_once(tmp_path):
    """POST /api/milestone/notify (no session) acknowledges a pending milestone;
    a re-read shows it notified + no longer pending."""
    import asyncio
    database = _db.Database(tmp_path / "ms.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    user, _ = asyncio.get_event_loop().run_until_complete(
        _seed_milestone_user(database, service, "Mom", 40.0, 20.8,
                             "192.168.2.55")())
    app = create_app(database, service, holder)
    with _client_from(app, "192.168.2.55") as c:
        assert c.post("/api/milestone/notify",
                      json={"user_id": user.id,
                            "milestone": 50}).status_code == 200
        data = c.get("/api/milestone").json()
        assert data["user"]["milestones"]["50"]["notified"] is True
        assert data["user"]["milestones"]["50"]["pending"] is False
    asyncio.get_event_loop().run_until_complete(database.close())


def test_vpn_share_toggle_via_network(client):
    """The Network-tab VPN-share switch persists through /api/network and
    surfaces the applied relay status (None status when no gateway is wired —
    the degraded boot path)."""
    c, _, _ = client
    _login(c)
    n = c.get("/api/network").json()
    assert n["vpn_share"] == {"enabled": False, "interface": ""}

    r = c.post("/api/network", json={"vpn_share": True})
    assert r.status_code == 200
    vs = r.json()["vpn_share"]
    assert vs["enabled"] is True
    # switch persists across requests (the DB setting, not a memory toggle)
    assert c.get("/api/network").json()["vpn_share"]["enabled"] is True

    r = c.post("/api/network", json={"vpn_share": False})
    assert r.json()["vpn_share"]["enabled"] is False
    assert r.json()["vpn_share"]["interface"] == ""


def test_decline_random_macs_via_network(client):
    """The random-MAC gate rides /api/network: the switch persists, a partial
    POST leaves shaping untouched, and the one-shot "existing" sweep runs in
    the same call (the flag itself resets; the gate stays on)."""
    c, _, _ = client
    _login(c)
    assert c.get("/api/network").json()["decline_random_macs"] is False

    r = c.post("/api/network", json={"decline_random_macs": True})
    assert r.status_code == 200
    assert r.json()["decline_random_macs"] is True
    # persists across requests (the DB setting, not a memory toggle)
    assert c.get("/api/network").json()["decline_random_macs"] is True

    # a partial POST with the sweep flag does not touch the shaping totals
    r = c.post("/api/network", json={"decline_random_macs": True,
                                     "decline_random_macs_existing": True})
    assert r.status_code == 200
    assert r.json()["decline_random_macs"] is True
    n = c.get("/api/network").json()
    assert n["enabled"] is False
    assert n["decline_random_macs"] is True

    r = c.post("/api/network", json={"decline_random_macs": False})
    assert r.json()["decline_random_macs"] is False
    assert c.get("/api/network").json()["decline_random_macs"] is False


def test_user_exempt_from_quota(client):
    """A user created with exempt_quota is never quota-blocked even when over
    their allowance (the dashboard resolves through user_quota_blocked and
    carries the flag); clearing the flag re-arms the quota gate; the /report
    payload carries the flag too."""
    import asyncio
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    c, db, _ = client
    _login(c)
    # open a period so usage registers against real allowances
    asyncio.get_event_loop().run_until_complete(
        db.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1)))
    svc = QuotaService(db, timezone="Africa/Cairo")
    asyncio.get_event_loop().run_until_complete(svc.open_period())
    asyncio.get_event_loop().run_until_complete(svc.recompute_allowances())

    uid = c.post("/api/users", json={"name": "Unlimited",
                                     "quota_mode": "fixed",
                                     "fixed_gb": 5.0,
                                     "exempt_quota": True}).json()["id"]
    uid2 = c.post("/api/users", json={"name": "Normal", "quota_mode": "fixed",
                                      "fixed_gb": 5.0}).json()["id"]

    def user_view(i):
        return next(x for x in c.get("/api/dashboard").json()["users"]
                    if x["id"] == i)

    assert user_view(uid)["exempt_quota"] is True
    assert user_view(uid2)["exempt_quota"] is False

    # give each a device + 6 GB of usage against a 5 GB allowance
    today = _dt.now(_tz.utc).date().isoformat()
    dev1 = asyncio.get_event_loop().run_until_complete(
        db.upsert_device("02:00:00:00:00:01", name="Laptop", user_id=uid))
    dev2 = asyncio.get_event_loop().run_until_complete(
        db.upsert_device("02:00:00:00:00:02", name="Phone", user_id=uid2))
    asyncio.get_event_loop().run_until_complete(
        db.add_usage(dev1.id, today, int(6.0 * GB), 0))
    asyncio.get_event_loop().run_until_complete(
        db.add_usage(dev2.id, today, int(6.0 * GB), 0))

    # exempt survives the over-usage; the normal user is quota-blocked
    assert user_view(uid)["quota_blocked"] is False
    assert user_view(uid2)["quota_blocked"] is True

    # PATCH clears the flag — the quota gate re-arms for the same usage
    r = c.patch(f"/api/users/{uid}", json={"exempt_quota": False})
    assert r.status_code == 200
    assert user_view(uid)["exempt_quota"] is False
    assert user_view(uid)["quota_blocked"] is True

    # /report surfaces the flag (the report page renders the same math)
    holder = SnapshotHolder()
    with _client_from(create_app(db, QuotaService(db, timezone="Africa/Cairo"),
                                 holder), "192.168.2.9") as rc:
        rp = rc.get("/api/report")
        if rp.status_code == 200:
            ru = next(x for x in rp.json().get("users", [])
                      if x.get("id") == uid)
            assert "exempt_quota" in ru


def test_milestone_page_is_public(tmp_path):
    """GET /milestone serves the HTML page with no session cookie."""
    import asyncio
    database = _db.Database(tmp_path / "ms.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)
    with _client_from(app, "192.168.2.55") as c:
        r = c.get("/milestone")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert b"Quota" in r.content
        # shares the retuned stylesheet; pin the cache-bust so the theme
        # actually reaches this page (browser-cached ?v=41 would show the
        # pre-obsidian sheet).
        assert "assets/styles.css?v=49" in r.text
    asyncio.get_event_loop().run_until_complete(database.close())



def test_report_gated_by_source_ip(tmp_path):
    """/api/report: client-subnet IP -> 200, outside IP -> 403, explicit
    allow-list entry -> 200."""
    import asyncio
    database = _db.Database(tmp_path / "rep.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder,
                     report_config=ReportConfig(
                         enabled=True, allow_client_subnet=True,
                         allowed_ips=["192.168.1.10"],
                         client_subnet="192.168.2.0/24"))

    # managed client subnet admitted
    with _client_from(app, "192.168.2.77") as c:
        r = c.get("/api/report")
        assert r.status_code == 200
        data = r.json()
        assert "bundle" in data and "users" in data and "logs" in data
        assert "events" in data

    # explicit allow-list entry admitted (not on the client subnet)
    with _client_from(app, "192.168.1.10") as c:
        assert c.get("/api/report").status_code == 200

    # anything else is denied
    with _client_from(app, "8.8.8.8") as c:
        assert c.get("/api/report").status_code == 403
    asyncio.get_event_loop().run_until_complete(database.close())


def test_report_page_respects_gate(tmp_path):
    """GET /report HTML page: allowed source -> 200 text/html, outside -> 403."""
    import asyncio
    database = _db.Database(tmp_path / "rep.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder,
                     report_config=ReportConfig(
                         enabled=True, allow_client_subnet=True,
                         allowed_ips=[], client_subnet="192.168.2.0/24"))
    with _client_from(app, "192.168.2.77") as c:
        r = c.get("/report")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert b"Consumption report" in r.content
        assert "assets/styles.css?v=49" in r.text
    with _client_from(app, "8.8.8.8") as c:
        assert c.get("/report").status_code == 403
    asyncio.get_event_loop().run_until_complete(database.close())


def test_report_disabled_denies_everyone(tmp_path):
    """report.enabled=false -> /api/report 403 for every source."""
    import asyncio
    database = _db.Database(tmp_path / "rep.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder,
                     report_config=ReportConfig(
                         enabled=False, allow_client_subnet=True,
                         allowed_ips=["192.168.1.10"],
                         client_subnet="192.168.2.0/24"))
    for ip in ("192.168.2.77", "192.168.1.10"):
        with _client_from(app, ip) as c:
            assert c.get("/api/report").status_code == 403
    asyncio.get_event_loop().run_until_complete(database.close())


# ---------------------------------------------------------------------------
# DNS browsing history (GET /api/history/{device_id})
# ---------------------------------------------------------------------------

def _now_str() -> str:
    """Local "%Y-%m-%d %H:%M" bucket (matches MINUTE_FMT / the endpoint's
    since_minute)."""
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M")


def _prev_minute(minute: str) -> str:
    """The previous minute bucket (a distinct bucket for multi-minute tests)."""
    from datetime import datetime as _dt, timedelta as _td
    prev = _dt.strptime(minute, "%Y-%m-%d %H:%M") - _td(minutes=1)
    return prev.strftime("%Y-%m-%d %H:%M")


def _hours_ago(minute: str, hours: int) -> str:
    """A bucket ``hours`` before ``minute`` — e.g. old enough to fall OUTSIDE
    a 1-hour look-back window (the endpoint uses ``now - hours(window)``)."""
    from datetime import datetime as _dt, timedelta as _td
    old = _dt.strptime(minute, "%Y-%m-%d %H:%M") - _td(hours=hours)
    return old.strftime("%Y-%m-%d %H:%M")


def _seed_history_device(d, svc, name, ip):
    """A fixed user + one device with a DHCP lease at ``ip``."""
    async def _inner():
        u = await d.create_user(name, _db.QUOTA_FIXED, 10.0)
        dev = await d.upsert_device(
            f"de:ad:be:ef:{abs(hash(name)) % 0xffff:04x}",
            name="Phone", user_id=u.id)
        await d.set_lease(dev.mac, ip)
        return dev.id
    return asyncio.get_event_loop().run_until_complete(_inner())


def test_history_endpoint_requires_auth(client):
    c, _, _ = client
    assert c.get("/api/history/1").status_code == 401


def test_wifi_ssids_endpoint_requires_auth(client):
    """The SSID picker for the access-label field is admin-only (it leaks the
    household's network names)."""
    c, _, _ = client
    assert c.get("/api/wifi/ssids").status_code == 401


def test_wifi_ssids_unconfigured_defaults_to_empty(tmp_path):
    """Without an injected probe getter the endpoint reports the feature as
    not configured — the UI chip just falls back, no 500."""
    database = _db.Database(tmp_path / "ssids.db")
    service = QuotaService(database, timezone="Africa/Cairo")

    async def _init():
        await database.connect()
    asyncio.get_event_loop().run_until_complete(_init())

    app = create_app(database, service, SnapshotHolder())
    try:
        with TestClient(app) as c:
            _login(c)
            assert c.get("/api/wifi/ssids").json() == {
                "available": False, "error": "not configured",
                "ssids": [], "ssid_by_mac": {}, "wireless_macs": []}
    finally:
        asyncio.get_event_loop().run_until_complete(database.close())


def test_history_returns_top_domains_and_activity(client):
    c, database, service = client
    _login(c)
    dev_id = _seed_history_device(database, service, "hist-dev", "192.168.2.77")
    now_minute = _now_str()
    run = asyncio.get_event_loop().run_until_complete
    run(database.batch_add_dns_history([
        (dev_id, now_minute, "example.com", 4),
        (dev_id, now_minute, "other.net", 2),
        (dev_id, _prev_minute(now_minute), "example.com", 3),
    ]))
    r = c.get(f"/api/history/{dev_id}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["device_id"] == dev_id
    assert data["total_queries"] == 9
    assert data["top_domains"][0]["domain"] == "example.com"
    assert data["top_domains"][0]["hits"] == 7
    assert data["top_domains"][1]["domain"] == "other.net"
    # activity + recent use the wire key the JS renders with
    assert all("bucket_minute" in a for a in data["activity"])
    assert all("bucket_minute" in r_ for r_ in data["recent"])
    assert data["recent"][0]["domain"] == "example.com"


def test_history_404_unknown_device(client):
    c, _, _ = client
    _login(c)
    assert c.get("/api/history/999999").status_code == 404


def test_history_window_and_limit_params(client):
    c, database, service = client
    _login(c)
    dev_id = _seed_history_device(database, service, "hist-win", "192.168.2.78")
    now_minute = _now_str()
    old_bucket = _hours_ago(now_minute, 2)  # outside a 1 h look-back
    run = asyncio.get_event_loop().run_until_complete
    run(database.batch_add_dns_history([
        (dev_id, old_bucket, "a.com", 1),
        (dev_id, old_bucket, "b.com", 1),
        (dev_id, old_bucket, "c.com", 1),
    ]))
    # a 1-hour window excludes the 2-hour-old bucket entirely
    r = c.get(f"/api/history/{dev_id}?window=1&limit=2")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["window_hours"] == 1
    assert data["total_queries"] == 0
    # a wide window + small limit caps the top-domains list
    r = c.get(f"/api/history/{dev_id}?window=336&limit=2")
    data = r.json()
    assert data["total_queries"] == 3
    assert len(data["top_domains"]) == 2


def test_history_all_devices_aggregates(client):
    """device_id=\"all\" returns a household-wide aggregate: combined top
    domains + total, and every recent row stamped with its owning device_id
    (the frontend badges them with [name]). Per-device rows stay unattributed."""
    c, database, service = client
    _login(c)
    dev1 = _seed_history_device(database, service, "hist-all-a", "192.168.2.80")
    dev2 = _seed_history_device(database, service, "hist-all-b", "192.168.2.81")
    now_minute = _now_str()
    run = asyncio.get_event_loop().run_until_complete
    run(database.batch_add_dns_history([
        (dev1, now_minute, "example.com", 4),
        (dev1, _prev_minute(now_minute), "example.com", 2),
        (dev2, now_minute, "example.com", 3),
        (dev2, now_minute, "other.net", 1),
    ]))
    r = c.get("/api/history/all")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["device_id"] == "all"
    assert data["total_queries"] == 10
    assert data["top_domains"][0]["domain"] == "example.com"
    assert data["top_domains"][0]["hits"] == 9
    assert data["top_domains"][1]["hits"] == 1
    assert {x["device_id"] for x in data["recent"]} == {dev1, dev2}
    assert all("device_id" in x for x in data["recent"])
    # per-device contract is unchanged: no device_id key on individual rows
    solo = c.get(f"/api/history/{dev1}")
    assert solo.status_code == 200, solo.text
    sdata = solo.json()
    assert sdata["total_queries"] == 6
    assert all("device_id" not in x for x in sdata["recent"])


def test_history_device_0_is_all(client):
    """device_id=0 is an alias for the household aggregate (the backend's
    second documented sentinel next to \"all\")."""
    c, database, service = client
    _login(c)
    dev_id = _seed_history_device(database, service, "hist-zero", "192.168.2.82")
    now_minute = _now_str()
    run = asyncio.get_event_loop().run_until_complete
    run(database.batch_add_dns_history([
        (dev_id, now_minute, "example.com", 3),
    ]))
    r = c.get("/api/history/0")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["device_id"] == "all"
    assert data["total_queries"] == 3


# -- software updates (Admin tab) ----------------------------------------------

def test_updates_unwired_404_and_snapshot_none(client):
    """Without an updater wired (tests / degraded boot) the endpoints 404 and
    the snapshot carries update=None — the UI hides the card gracefully."""
    c, _, _ = client
    _login(c)
    assert c.get("/api/updates").status_code == 404
    assert c.post("/api/updates/check").status_code == 404
    assert c.get("/api/dashboard").json()["update"] is None


def test_updates_round_trip(tmp_path):
    """The Admin-tab update flow: settings toggle, force-check finds a newer
    release, the changelog rides the state, and the install path runs."""
    from quota.updater import Updater

    RELEASE = {
        "tag_name": "v0.3.1",
        "assets": [{"name": "quota-manager_0.3.1_all.deb",
                    "browser_download_url": "https://example/deb"}],
    }
    CHANGELOG = (
        "# Changelog\n\n## [0.3.1]\n### Fixed\n- the bug.\n\n"
        "## [0.2.0]\n### Added\n- old stuff.\n"
    )

    database = _db.Database(tmp_path / "upd.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())

    installed = []
    up = Updater(
        database, current_version="0.2.0",
        fetch_json=lambda url, timeout=20: RELEASE,
        fetch_text=lambda url, timeout=20: CHANGELOG,
        download=lambda url, dest, timeout=120: None,
        run_command=lambda argv, timeout=15: installed.append(argv) or (0, ""))
    app = create_app(database, service, holder, updater=up)
    try:
        with TestClient(app) as c:
            _login(c)
            # current state, default switches
            st = c.get("/api/updates").json()
            assert st["current_version"] == "0.2.0"
            assert st["available"] is False
            assert st["enabled"] is True
            assert st["auto_install"] is False
            # settings round-trip (partial saves keep the other field)
            r = c.post("/api/updates", json={"auto_install": True})
            assert r.status_code == 200, r.text
            assert r.json()["auto_install"] is True
            assert c.post("/api/updates", json={"enabled": False}
                          ).json()["enabled"] is False
            # back on + auto-install off so the check below does NOT race the
            # background install (that path is covered in test_updater.py)
            assert c.post("/api/updates", json={"enabled": True,
                                                "auto_install": False}
                          ).status_code == 200
            # force a check -> newer release + changelog
            r = c.post("/api/updates/check")
            assert r.status_code == 200, r.text
            st = r.json()
            assert st["available"] is True
            assert st["latest_version"] == "0.3.1"
            assert [x["version"] for x in st["changelog"]] == ["0.3.1"]
            # the snapshot carries the same availability
            assert c.get("/api/dashboard").json()["update"]["available"] is True
            # manual install: the endpoint runs the download + systemd-run path
            r = c.post("/api/updates/install")
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True
            import time as _t
            for _ in range(50):
                if installed:
                    break
                _t.sleep(0.02)
            assert installed and installed[0][0] == "systemd-run"
            st = c.get("/api/updates").json()
            assert st["available"] is False  # latest cleared after install
            assert st["last_install"]
    finally:
        asyncio.get_event_loop().run_until_complete(database.close())
