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


_cached_loop = None
def _get_loop():
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop

def run(coro):
    return _get_loop().run_until_complete(coro)


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


def test_domain_rules_modern_schema_survives_restart_without_rebuilding(tmp_path, caplog):
    """The generated scope_key is hidden from table_info; recognizing it via
    table_xinfo prevents every restart from rebuilding the table and changing
    retained row IDs.
    """
    path = tmp_path / "modern-domain-rules.db"
    d = _db.Database(path)
    run(d.connect())
    try:
        run(d.conn.execute(
            "INSERT INTO domain_rules (id, scope, action, domain, created_at) "
            "VALUES (47, 'global', 'block', 'example.com', 1.0)"))
        run(d.conn.commit())
        original_id = 47
    finally:
        run(d.close())

    d = _db.Database(path)
    try:
        caplog.set_level("WARNING", logger="quota.db")
        run(d.connect())
        row = run(d._fetch_one("SELECT id FROM domain_rules WHERE domain=?",
                               ("example.com",)))
        assert row["id"] == original_id
        assert not any("predates the scope_key fix" in record.message
                       for record in caplog.records)
        assert run(d._fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='domain_rules_old'")) is None
    finally:
        run(d.close())


def test_domain_rules_legacy_migration_keeps_retained_ids_and_latest_duplicate(tmp_path):
    """Legacy duplicates collapse to the newest row while retained rows keep
    their original IDs and all migration work is transactionally applied.
    """
    path = tmp_path / "legacy-domain-rules.db"

    async def build_legacy_domain_rules():
        conn = await aiosqlite.connect(path)
        await conn.execute("""CREATE TABLE domain_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'global', scope_id INTEGER,
            action TEXT NOT NULL DEFAULT 'block', domain TEXT NOT NULL,
            target_ip TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual', created_at REAL NOT NULL,
            UNIQUE(scope, scope_id, domain, action))""")
        await conn.executemany(
            "INSERT INTO domain_rules "
            "(id, scope, scope_id, action, domain, target_ip, enabled, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(5, "global", None, "block", "duplicate.example", None, 1, "old", 1.0),
             (9, "global", None, "block", "duplicate.example", None, 0, "new", 2.0),
             (17, "device", 12, "allow", "kept.example", None, 1, "manual", 3.0)])
        # Simulate a deleted high-ID row: sqlite_sequence intentionally keeps
        # the old AUTOINCREMENT high-water mark at 1000.
        await conn.execute(
            "UPDATE sqlite_sequence SET seq=1000 WHERE name='domain_rules'")
        await conn.commit()
        await conn.close()

    run(build_legacy_domain_rules())
    d = _db.Database(path)
    try:
        run(d.connect())
        rows = run(d.conn.execute_fetchall(
            "SELECT id, scope, scope_id, domain, enabled, source "
            "FROM domain_rules ORDER BY id"))
        assert [(r["id"], r["domain"], r["enabled"], r["source"]) for r in rows] == [
            (9, "duplicate.example", 0, "new"),
            (17, "kept.example", 1, "manual")]
        assert run(d._fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='domain_rules_old'")) is None
        run(d.conn.execute(
            "INSERT INTO domain_rules (scope, action, domain, created_at) "
            "VALUES ('global', 'block', 'after-migration.example', 4.0)"))
        run(d.conn.commit())
        new_row = run(d._fetch_one(
            "SELECT id FROM domain_rules WHERE domain=?",
            ("after-migration.example",)))
        assert new_row["id"] == 1001
    finally:
        run(d.close())


def test_domain_rules_migration_failure_rolls_back_table_rename(tmp_path):
    """A copy failure leaves the original table and rows in place, with no
    half-migrated table name for the next boot to mistake as complete.
    """
    path = tmp_path / "broken-domain-rules.db"

    async def build_invalid_legacy_domain_rules():
        conn = await aiosqlite.connect(path)
        await conn.execute("""CREATE TABLE domain_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'global', scope_id INTEGER,
            action TEXT NOT NULL DEFAULT 'block', domain TEXT,
            target_ip TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual', created_at REAL NOT NULL,
            UNIQUE(scope, scope_id, domain, action))""")
        await conn.execute(
            "INSERT INTO domain_rules (id, scope, scope_id, action, domain, created_at) "
            "VALUES (23, 'global', NULL, 'block', NULL, 1.0)")
        await conn.commit()
        await conn.close()

    run(build_invalid_legacy_domain_rules())
    d = _db.Database(path)
    try:
        try:
            run(d.connect())
        except Exception as exc:
            assert "NOT NULL constraint failed" in str(exc)
        else:
            raise AssertionError("invalid legacy row should fail the copy")
    finally:
        run(d.close())

    async def inspect_after_failure():
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        names = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('domain_rules', 'domain_rules_old') ORDER BY name")
        rows = await conn.execute_fetchall("SELECT id, domain FROM domain_rules")
        await conn.close()
        return {row["name"] for row in names}, [(row["id"], row["domain"]) for row in rows]

    names, rows = run(inspect_after_failure())
    assert names == {"domain_rules"}
    assert rows == [(23, None)]


def test_domain_rules_migration_residue_unknown_shape_kept_and_startup_continues(tmp_path):
    """An interrupted migration's old-table residue of an unknown shape is
    preserved untouched, and the gateway still starts (a gateway that does
    not start counts and blocks nobody); the admin gets an event.
    """
    path = tmp_path / "residue-domain-rules.db"

    async def build_residue():
        conn = await aiosqlite.connect(path)
        await conn.execute("CREATE TABLE domain_rules_old (id INTEGER, domain TEXT)")
        await conn.execute(
            "INSERT INTO domain_rules_old VALUES (31, 'recover.example')")
        await conn.commit()
        await conn.close()

    run(build_residue())
    d = _db.Database(path)
    try:
        run(d.connect())
        events = run(d.conn.execute_fetchall("SELECT message FROM events"))
        assert any("domain_rules_old" in e["message"] for e in events)
    finally:
        run(d.close())

    async def read_residue():
        conn = await aiosqlite.connect(path)
        row = await (await conn.execute(
            "SELECT id, domain FROM domain_rules_old")).fetchone()
        await conn.close()
        return row

    assert run(read_residue()) == (31, "recover.example")


def test_domain_rules_partial_migration_residue_preserves_both_tables(tmp_path):
    """If a partial copy and an unknown-shape renamed source coexist, startup
    continues without deleting rows from either table.
    """
    path = tmp_path / "partial-residue-domain-rules.db"

    async def build_partial_residue():
        conn = await aiosqlite.connect(path)
        await conn.execute("""CREATE TABLE domain_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'global', scope_id INTEGER,
            scope_key INTEGER GENERATED ALWAYS AS (COALESCE(scope_id, 0)) STORED,
            action TEXT NOT NULL DEFAULT 'block', domain TEXT NOT NULL,
            target_ip TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual', created_at REAL NOT NULL,
            UNIQUE(scope, scope_key, domain, action))""")
        await conn.execute(
            "INSERT INTO domain_rules (id, scope, action, domain, created_at) "
            "VALUES (41, 'global', 'block', 'partial.example', 1.0)")
        await conn.execute("CREATE TABLE domain_rules_old (id INTEGER, domain TEXT)")
        await conn.execute(
            "INSERT INTO domain_rules_old VALUES (42, 'source.example')")
        await conn.commit()
        await conn.close()

    run(build_partial_residue())
    d = _db.Database(path)
    try:
        run(d.connect())
    finally:
        run(d.close())

    async def read_tables():
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        current = await conn.execute_fetchall("SELECT id, domain FROM domain_rules")
        old = await conn.execute_fetchall(
            "SELECT id, domain FROM domain_rules_old")
        await conn.close()
        return ([(row["id"], row["domain"]) for row in current],
                [(row["id"], row["domain"]) for row in old])

    assert run(read_tables()) == ([(41, "partial.example")], [(42, "source.example")])


def test_domain_rules_interrupted_rebuild_is_recovered_on_start(tmp_path):
    """Regression: an older build's every-restart rebuild could be cut by a
    power loss right after RENAME + CREATE, leaving the rules in
    domain_rules_old and an empty fixed table. Startup must put them back
    (ids kept, newest duplicate wins, rows already present win) instead of
    crashing or losing them.
    """
    path = tmp_path / "interrupted-domain-rules.db"

    async def build():
        conn = await aiosqlite.connect(path)
        await conn.execute("""CREATE TABLE domain_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'global', scope_id INTEGER,
            scope_key INTEGER GENERATED ALWAYS AS (COALESCE(scope_id, 0)) STORED,
            action TEXT NOT NULL DEFAULT 'block', domain TEXT NOT NULL,
            target_ip TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual', created_at REAL NOT NULL,
            UNIQUE(scope, scope_key, domain, action))""")
        await conn.execute(
            "INSERT INTO domain_rules (id, scope, action, domain, created_at) "
            "VALUES (7, 'global', 'block', 'kept.example', 1.0)")
        await conn.execute("""CREATE TABLE domain_rules_old (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, scope_id INTEGER,
            action TEXT, domain TEXT, target_ip TEXT, enabled INTEGER,
            source TEXT, created_at REAL)""")
        rows = [(3, "global", None, "block", "ads.example", None, 1, "manual", 1.0),
                (5, "global", None, "block", "ads.example", None, 1, "manual", 2.0),
                (7, "global", None, "block", "clash.example", None, 1, "manual", 3.0),
                (9, "global", None, "allow", "school.example", None, 1, "manual", 4.0)]
        await conn.executemany(
            "INSERT INTO domain_rules_old VALUES (?,?,?,?,?,?,?,?,?)", rows)
        await conn.commit()
        await conn.close()

    run(build())
    d = _db.Database(path)
    try:
        run(d.connect())
        got = run(d.conn.execute_fetchall(
            "SELECT id, domain FROM domain_rules ORDER BY id"))
        assert [(r["id"], r["domain"]) for r in got] == [
            (5, "ads.example"), (7, "kept.example"), (9, "school.example")]
        left = run(d.conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE name='domain_rules_old'"))
        assert left == []
        seq = run(d.conn.execute_fetchall(
            "SELECT seq FROM sqlite_sequence WHERE name='domain_rules'"))
        assert seq[0]["seq"] >= 9
    finally:
        run(d.close())
    # a second start is a no-op
    d = _db.Database(path)
    try:
        run(d.connect())
        n = run(d.conn.execute_fetchall("SELECT COUNT(*) AS n FROM domain_rules"))
        assert n[0]["n"] == 3
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
