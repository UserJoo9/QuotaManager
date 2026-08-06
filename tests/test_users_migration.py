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
        # v2 tables/columns exist
        users = await d.list_users()
        assert len(users) == 2, "every legacy device must get its own user"
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
        assert len(await d.list_users()) == 2
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
