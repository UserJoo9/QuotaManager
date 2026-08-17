"""v2 migration: a legacy device-only DB is upgraded in place by connect().

The pre-user schema had devices with no ``user_id``/``bypass`` columns, an
events table without ``user_id``, no ``users`` table at all, and bundle
allowances keyed by MAC. This test builds that legacy DB by hand, then lets
``Database.connect()`` run its idempotent ALTERs + backfill and asserts the
upgrade: every legacy device gets its own user (carrying name, quota mode,
fixed GB and top-up), and stale MAC-keyed allowances are dropped by
``get_bundle``'s int-key coercion.
"""

from __future__ import annotations

import asyncio

import aiosqlite

from quota import db as _db

LEGACY_SCHEMA = """
CREATE TABLE devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mac         TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    quota_mode  TEXT NOT NULL DEFAULT 'auto',
    fixed_gb    REAL,
    block_state TEXT NOT NULL DEFAULT 'ok',
    created_at  REAL NOT NULL,
    topup_gb    REAL NOT NULL DEFAULT 0
);

CREATE TABLE leases (
    mac         TEXT PRIMARY KEY,
    ip          TEXT NOT NULL,
    lease_start REAL NOT NULL,
    lease_end   REAL NOT NULL
);

CREATE TABLE bundle_config (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    total_gb     REAL NOT NULL,
    reset_day    INTEGER NOT NULL,
    allowances   TEXT NOT NULL DEFAULT '{}',
    period_start TEXT NOT NULL DEFAULT '',
    period_end   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE usage_daily (
    device_id INTEGER NOT NULL,
    date      TEXT NOT NULL,
    up_bytes  INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, date)
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    level     TEXT NOT NULL DEFAULT 'info',
    device_id INTEGER,
    message   TEXT NOT NULL
);
"""


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_connect_migrates_legacy_db(tmp_path):
    path = tmp_path / "legacy.db"

    async def build_legacy():
        conn = await aiosqlite.connect(path)
        await conn.executescript(LEGACY_SCHEMA)
        await conn.execute(
            "INSERT INTO devices (mac, name, quota_mode, fixed_gb, "
            "block_state, created_at, topup_gb) VALUES (?,?,?,?,?,?,?)",
            ("aa:bb:cc:dd:ee:01", "Phone", "fixed", 20.0, "ok", 1000.0, 5.0))
        await conn.execute(
            "INSERT INTO devices (mac, name, quota_mode, fixed_gb, "
            "block_state, created_at, topup_gb) VALUES (?,?,?,?,?,?,?)",
            ("aa:bb:cc:dd:ee:02", "Laptop", "auto", None, "admin_off", 1001.0, 0.0))
        await conn.execute(
            "INSERT INTO bundle_config (id, total_gb, reset_day, allowances, "
            "period_start, period_end) VALUES (1, 100, 1, "
            "'{\"aa:bb:cc:dd:ee:01\": 25.0}', '2026-08-01', '2026-09-01')")
        await conn.execute(
            "INSERT INTO usage_daily (device_id, date, up_bytes, down_bytes) "
            "VALUES (1, '2026-08-01', 100, 200)")
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('session_token', 'abc')")
        await conn.execute(
            "INSERT INTO events (ts, level, device_id, message) "
            "VALUES (1000.0, 'info', 1, 'legacy event')")
        await conn.commit()
        await conn.close()

    run(build_legacy())

    d = _db.Database(path)

    async def migrate():
        await d.connect()
        # v2 tables/columns exist (Phone + Laptop + the seeded Gateway user)
        users = await d.list_users()
        assert len(users) == 3, "every legacy device must get its own user"
        phone = next(u for u in users if u.name == "Phone")
        assert phone.quota_mode == _db.QUOTA_FIXED
        assert phone.fixed_gb == 20.0
        assert phone.topup_gb == 5.0, "per-device top-up must carry over"
        laptop = next(u for u in users if u.name == "Laptop")
        assert laptop.block_state == _db.BLOCK_ADMIN, \
            "legacy manual block must be preserved on the user"

        # devices now reference their user
        dev1 = await d.get_device(mac="aa:bb:cc:dd:ee:01")
        assert dev1 is not None and dev1.user_id == phone.id
        dev2 = await d.get_device(mac="aa:bb:cc:dd:ee:02")
        assert dev2 is not None and dev2.user_id == laptop.id
        assert dev2.bypass is False

        # legacy data survived
        usage = await d.get_usage(dev1.id)
        assert usage["up_bytes"] == 100 and usage["down_bytes"] == 200
        assert await d.get_setting("session_token") == "abc"
        events = await d.list_events(10)
        assert any("Migrated 2 device(s) to per-user quotas" in e["message"]
                   for e in events)

        # stale MAC-keyed allowances are dropped by the int-key coercion
        b = await d.get_bundle()
        assert b.allowances == {}, "MAC-keyed pre-v2 allowances must be dropped"

        # idempotent: reconnecting does not add more users
        await d.close()
        await d.connect()
        assert len(await d.list_users()) == 3
    try:
        run(migrate())
    finally:
        run(d.close())


def test_bundle_allowances_drop_stale_mac_keys(tmp_path):
    """get_bundle must silently ignore non-int allowance keys left over from a
    pre-v2 DB even after a partial migration."""
    path = tmp_path / "mixed.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        # seed the single bundle_config row, then drop a mixed-key snapshot into it
        run(d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1)))
        run(d._conn.execute(
            "UPDATE bundle_config SET allowances=? WHERE id=1",
            ('{"aa:bb:cc:dd:ee:09": 3.0, "7": 12.5}',)))
        run(d._conn.commit())
        b = run(d.get_bundle())
        assert b.allowances == {7: 12.5}, \
            "int keys survive, stale MAC keys are dropped"
    finally:
        run(d.close())


# --------------------------------------------------------------------------- #
# protected "Gateway" seed + delete -> MAC blacklist (deny list)
# --------------------------------------------------------------------------- #

def test_gateway_seed_is_idempotent(tmp_path):
    """connect() seeds the protected Gateway user + box device exactly once —
    a re-connect (every boot) never duplicates either row."""
    from quota.engine import GATEWAY_MAC

    path = tmp_path / "seed.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        users = run(d.list_users())
        gateway = next(u for u in users if getattr(u, "protected", False))
        assert gateway.name == "Gateway"
        assert gateway.quota_mode == _db.QUOTA_FIXED
        assert gateway.fixed_gb == 1.0
        box = run(d.get_device(mac=GATEWAY_MAC))
        assert box is not None and box.name == "Gateway box"
        assert box.user_id == gateway.id
        n_users, n_devices = len(users), len(run(d.list_devices()))

        # reconnect (a restart) must not duplicate anything
        run(d.close())
        run(d.connect())
        assert len(run(d.list_users())) == n_users
        assert len(run(d.list_devices())) == n_devices
    finally:
        run(d.close())


def test_add_mac_list_roundtrip(tmp_path):
    """add_mac_list: additive + case-normalized + idempotent (unlike the
    replace-all set_mac_list, entries accumulate and duplicates are dropped)."""
    path = tmp_path / "mac.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        run(d.add_mac_list("deny", ["AA:BB:CC:DD:EE:10"]))
        assert run(d.get_mac_list("deny")) == ["aa:bb:cc:dd:ee:10"]
        # additive: a second add keeps the first entry
        run(d.add_mac_list("deny", ["aa:bb:cc:dd:ee:11"]))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:10", "aa:bb:cc:dd:ee:11"]
        # idempotent re-add
        run(d.add_mac_list("deny", ["AA:BB:CC:DD:EE:10"]))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:10", "aa:bb:cc:dd:ee:11"]
        # blanks are dropped; the allow list is untouched
        run(d.add_mac_list("deny", ["  ", ""]))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:10", "aa:bb:cc:dd:ee:11"]
        assert run(d.get_mac_list("allow")) == []
    finally:
        run(d.close())


def test_delete_device_deny_lists_mac(tmp_path):
    """delete_device(..., deny_list_mac=True) blacklists the MAC for ANY owner
    (guest or normal) — the month-reset path never does."""
    path = tmp_path / "denydev.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        g = run(d.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                              fixed_gb=1.0, guest=True))
        gdev = run(d.upsert_device("aa:bb:cc:dd:ee:20", "Phone", user_id=g.id))
        run(d.delete_device(gdev.id, deny_list_mac=True))
        assert run(d.get_mac_list("deny")) == ["aa:bb:cc:dd:ee:20"]

        # a NON-guest (normal user) delete blacklists too
        n = run(d.create_user(name="Dad", quota_mode=_db.QUOTA_FIXED,
                              fixed_gb=20.0))
        ndev = run(d.upsert_device("aa:bb:cc:dd:ee:21", "Laptop", user_id=n.id))
        run(d.delete_device(ndev.id, deny_list_mac=True))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:20", "aa:bb:cc:dd:ee:21"]

        # without the flag the MAC is NOT blacklisted
        n3 = run(d.create_user(name="Mom", quota_mode=_db.QUOTA_FIXED,
                               fixed_gb=20.0))
        ndev3 = run(d.upsert_device("aa:bb:cc:dd:ee:25", user_id=n3.id))
        run(d.delete_device(ndev3.id))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:20", "aa:bb:cc:dd:ee:21"]
    finally:
        run(d.close())


def test_delete_user_deny_lists_all_macs(tmp_path):
    """delete_user(..., deny_list_macs=True) blacklists EVERY device MAC it
    owned, guest or normal."""
    path = tmp_path / "denyuser.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        g2 = run(d.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                               fixed_gb=1.0, guest=True))
        run(d.upsert_device("aa:bb:cc:dd:ee:22", user_id=g2.id))
        run(d.upsert_device("aa:bb:cc:dd:ee:23", user_id=g2.id))
        run(d.delete_user(g2.id, cascade=True, deny_list_macs=True))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:22", "aa:bb:cc:dd:ee:23"]

        # a normal user delete with the flag blacklists too
        n2 = run(d.create_user(name="Mom", quota_mode=_db.QUOTA_FIXED,
                               fixed_gb=20.0))
        run(d.upsert_device("aa:bb:cc:dd:ee:24", user_id=n2.id))
        run(d.delete_user(n2.id, cascade=True, deny_list_macs=True))
        assert run(d.get_mac_list("deny")) == [
            "aa:bb:cc:dd:ee:22", "aa:bb:cc:dd:ee:23", "aa:bb:cc:dd:ee:24"]
    finally:
        run(d.close())


def test_count_guest_users(tmp_path):
    """count_guest_users tallies guest accounts (feeds the guest-limit gate)."""
    path = tmp_path / "count.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        assert run(d.count_guest_users()) == 0
        run(d.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                          fixed_gb=1.0, guest=True))
        run(d.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                          fixed_gb=1.0, guest=True))
        run(d.create_user(name="Dad", quota_mode=_db.QUOTA_FIXED,
                          fixed_gb=20.0))
        assert run(d.count_guest_users()) == 2   # normal users don't count
    finally:
        run(d.close())


def test_delete_guest_users_never_blacklists(tmp_path):
    """The month-reset path (delete_guest_users) NEVER writes deny-list rows —
    a returning guest after a reset re-registers fresh."""
    path = tmp_path / "denyreset.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        g = run(d.create_user(name="", quota_mode=_db.QUOTA_FIXED,
                              fixed_gb=1.0, guest=True))
        run(d.upsert_device("aa:bb:cc:dd:ee:30", user_id=g.id))
        run(d.delete_guest_users())
        assert run(d.get_mac_list("deny")) == []
        # ...and the Gateway seed survives the guest wipe
        assert len(run(d.list_users())) == 1
        assert run(d.list_users())[0].protected is True
    finally:
        run(d.close())


def test_access_columns_auto_migrated(tmp_path):
    """The router-side access label landed AFTER the v2 users migration — a
    pre-access DB (devices without access_interface/access_override) must be
    upgraded in place by connect(), with defaults on existing rows."""
    path = tmp_path / "access.db"
    conn = None
    try:
        async def _build():
            nonlocal conn
            conn = await aiosqlite.connect(path)
            await conn.execute("""
                CREATE TABLE devices (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac              TEXT UNIQUE NOT NULL,
                    name             TEXT NOT NULL DEFAULT '',
                    quota_mode       TEXT NOT NULL DEFAULT 'auto',
                    fixed_gb         REAL,
                    block_state      TEXT NOT NULL DEFAULT 'ok',
                    created_at       REAL NOT NULL,
                    topup_gb         REAL NOT NULL DEFAULT 0,
                    limit_down_mbps  REAL NOT NULL DEFAULT 0,
                    limit_up_mbps    REAL NOT NULL DEFAULT 0,
                    dns_server       TEXT NOT NULL DEFAULT '',
                    source_interface TEXT NOT NULL DEFAULT '',
                    user_id          INTEGER,
                    bypass           INTEGER NOT NULL DEFAULT 0)""")
            await conn.execute(
                "INSERT INTO devices (mac, name, created_at) VALUES (?, ?, ?)",
                ("aa:bb:cc:dd:ee:40", "Old", 1.0))
            await conn.execute("""
                CREATE TABLE leases (
                    mac TEXT PRIMARY KEY, ip TEXT NOT NULL,
                    lease_start REAL NOT NULL, lease_end REAL NOT NULL)""")
            await conn.execute("""
                CREATE TABLE users (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    name             TEXT NOT NULL DEFAULT '',
                    quota_mode       TEXT NOT NULL DEFAULT 'auto',
                    fixed_gb         REAL,
                    block_state      TEXT NOT NULL DEFAULT 'ok',
                    topup_gb         REAL NOT NULL DEFAULT 0,
                    created_at       REAL NOT NULL,
                    guest            INTEGER NOT NULL DEFAULT 0,
                    protected        INTEGER NOT NULL DEFAULT 0,
                    exempt_quota     INTEGER NOT NULL DEFAULT 0,
                    limit_down_mbps  REAL NOT NULL DEFAULT 0,
                    limit_up_mbps    REAL NOT NULL DEFAULT 0,
                    notified_50      INTEGER NOT NULL DEFAULT 0,
                    notified_75      INTEGER NOT NULL DEFAULT 0,
                    notified_100     INTEGER NOT NULL DEFAULT 0,
                    history_days     INTEGER,
                    dns_server       TEXT NOT NULL DEFAULT '')""")
            await conn.commit()
            await conn.close()
            conn = None
        run(_build())
        d = _db.Database(path)
        run(d.connect())
        try:
            dev = run(d.get_device(mac="aa:bb:cc:dd:ee:40"))
            assert dev.access_interface == ""
            assert dev.access_override == ""
            # the migrated columns are writable + readable
            run(d.update_device(dev.id, access_override="LAN1"))
            dev = run(d.get_device(mac="aa:bb:cc:dd:ee:40"))
            assert dev.access_override == "LAN1"
            assert dev.access_interface == ""
            run(d.update_device(dev.id, access_interface="WiFi · MyNet"))
            dev = run(d.get_device(mac="aa:bb:cc:dd:ee:40"))
            assert dev.access_interface == "WiFi · MyNet"
        finally:
            run(d.close())
    finally:
        if conn is not None:
            run(conn.close())
