"""SQLite persistence layer (single file, async via aiosqlite).

Schema overview
---------------
users          -- every person: quota mode, allowance, enforcement state.
devices        -- every known MAC: name, owning user, per-device override.
leases         -- current/known DHCP leases (mac <-> ip).
mac_lists      -- operator MAC whitelist/blacklist ('allow' / 'deny').
bundle_config  -- single row: total_gb, reset_day, current period snapshot.
usage_daily    -- append-only per-device daily byte totals.
settings       -- key/value store (admin password hash, flags).
events         -- audit log (blocks, top-ups, config changes).

The monthly allowance lives on the USER (not the device). A user's usage is
the sum of their devices' usage; when a user exceeds their allowance every
device they own is blocked together.

All byte counters are stored as integers (bytes). Conversion to GB happens in
the API/service layer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("quota.db")

import aiosqlite

from quota.engine import GATEWAY_MAC

# ---------------------------------------------------------------------------
# Domain enums (kept as plain strings so the schema stays simple)
# ---------------------------------------------------------------------------

QUOTA_FIXED = "fixed"
QUOTA_AUTO = "auto"
QUOTA_DISABLED = "disabled"  # onboarding lock: 0 GB until the admin assigns
                             # shared (auto) or fixed; always quota-blocked

BLOCK_OK = "ok"          # allowed, within quota
BLOCK_QUOTA = "quota"    # exceeded monthly allowance
BLOCK_ADMIN = "admin_off"  # manually switched off by admin

# Domain-rule scope + action enums (see quota/dns_rules.py for the same
# constants used by the dnsmasq renderer — kept independent so db.py never
# has to import the renderer module).
DNS_SCOPE_GLOBAL = "global"
DNS_SCOPE_USER = "user"
DNS_SCOPE_DEVICE = "device"

DNS_ACTION_BLOCK = "block"
DNS_ACTION_ALLOW = "allow"
DNS_ACTION_REDIRECT = "redirect"


# ---------------------------------------------------------------------------
# Row models
# ---------------------------------------------------------------------------

@dataclass
class Device:
    id: int
    mac: str
    name: str = ""
    quota_mode: str = QUOTA_AUTO
    fixed_gb: Optional[float] = None
    block_state: str = BLOCK_OK
    created_at: float = 0.0
    #: Per-period top-up GB granted by the admin (added to the computed share
    #: on top of the fixed/auto allowance). Survives allowance recomputes and
    #: is reset when the quota period rolls over.
    topup_gb: float = 0.0
    #: Owning user (every device belongs to a user; quota lives on the user).
    user_id: Optional[int] = None
    #: Per-device override: when true this device is exempt from its user's
    #: quota block (an explicit admin_off block still wins).
    bypass: bool = False
    #: Per-device internet speed caps in Mbps (0 = unlimited). Enforced by the
    #: tc shaper (quota/shaping.py).
    limit_down_mbps: float = 0.0
    limit_up_mbps: float = 0.0
    #: Per-device upstream DNS server override (empty = inherit the user's
    #: override, or the gateway's default upstreams if neither is set).
    #: Rendered as a tag-restricted dnsmasq `server=` line — see
    #: quota/dns_rules.py.
    dns_server: str = ""
    #: Which NIC the device was last seen on (``ip neigh`` dev — e.g. "eth0",
    #: "wlan0"). Auto-learned by run.py each maintenance tick; empty when
    #: ``ip`` is unavailable (non-Linux / degraded boot). Drives the device
    #: card's WiFi/LAN tag (labels come from config.yaml's ``network.
    #: interface_tags``).
    source_interface: str = ""
    #: Router-side access label auto-learned by the WiFi probe
    #: (quota/wifi_probe.py + run.py): "WiFi · <SSID>" when the box's monitor
    #: card hears the device on the air, "LAN" once a leased device has been
    #: quiet past the grace period. Empty = not yet classified.
    access_interface: str = ""
    #: Manual pin: what the admin typed in the device modal ("WiFi · MyNet",
    #: "LAN1", ...). Wins over the auto label; empty = let the probe decide.
    access_override: str = ""


@dataclass
class User:
    id: int
    name: str = ""
    quota_mode: str = QUOTA_AUTO
    fixed_gb: Optional[float] = None
    #: 'ok' | 'admin_off' only — the per-user 'quota' state is derived from
    #: usage vs allowance and is never persisted (see service.evaluate_blocks).
    block_state: str = BLOCK_OK
    #: Per-period top-up GB granted to this user (added to their allowance).
    topup_gb: float = 0.0
    created_at: float = 0.0
    #: Guest account — auto-registered for a new device while guest mode is on.
    #: Guests get a fixed quota and are deleted when the quota period resets.
    guest: bool = False
    #: Protected user (the "Gateway" box account): seeded at connect, cannot
    #: be deleted by the admin, and the box's own internet usage is charged
    #: here. Editable like any other user — setting its allowance to 0 cuts
    #: the box's internet while clients keep working.
    protected: bool = False
    #: Exempt from quota: the user is never quota-blocked no matter how much
    #: they use. Admin cuts (user/device level) still apply — exemption only
    #: lifts the usage-vs-allowance gate. Per-device ``bypass`` is redundant
    #: for an exempt user's devices (their quota gate is already open).
    exempt_quota: bool = False
    #: Per-user aggregate internet speed cap in Mbps (0 = unlimited): all of a
    #: user's devices share this ceiling. Enforced by the Linux tc shaper.
    limit_down_mbps: float = 0.0
    limit_up_mbps: float = 0.0
    #: Milestone-notification flags: once the user's usage crosses a threshold
    #: (50%/75%/100% of their allowance), the milestone page marks it notified
    #: so it is only surfaced ONCE. Reset when the quota period rolls.
    notified_50: bool = False
    notified_75: bool = False
    notified_100: bool = False
    #: Per-user DNS-history retention in days; None = the global
    #: ``history.retention_days`` default applies.
    history_days: Optional[int] = None
    #: Per-user upstream DNS server override, inherited by every device of
    #: this user that has no override of its own (empty = no override).
    dns_server: str = ""


@dataclass
class DomainRule:
    """One domain-filtering rule: block/allow/redirect a domain (+ every
    subdomain — dnsmasq's match already covers those), scoped globally, to a
    user (fanned out to all their devices at render time), or to a single
    device. See quota/dns_rules.py for how this becomes dnsmasq config."""

    id: int
    scope: str = DNS_SCOPE_GLOBAL          # 'global' | 'user' | 'device'
    scope_id: Optional[int] = None         # user_id / device_id; None for global
    action: str = DNS_ACTION_BLOCK         # 'block' | 'allow' | 'redirect'
    domain: str = ""                       # normalized, no leading "*."
    target_ip: Optional[str] = None        # only meaningful for 'redirect'
    enabled: bool = True
    #: 'manual' (admin-entered) | 'preset:<preset_id>:<scope>:<scope_id>'
    #: (from an enabled blocklist preset) | 'import' (pasted hosts/
    #: AdBlock-Plus text).
    source: str = "manual"
    created_at: float = 0.0


@dataclass
class Lease:
    mac: str
    ip: str
    lease_start: float = 0.0
    lease_end: float = 0.0


@dataclass
class Bundle:
    total_gb: float = 140.0
    reset_day: int = 1
    #: How the monthly period is bounded: ``"renew_day"`` (the configured
    #: ``reset_day`` — the ISP's expected renew day) or ``"end_of_month"``
    #: (a calendar-month bill: the period runs 1st -> 1st and the configured
    #: ``reset_day`` is ignored).
    period_type: str = "renew_day"
    # Snapshot of allowances computed at period start (json dict user_id->gb).
    allowances: dict[int, float] = field(default_factory=dict)
    period_start: str = ""   # ISO date of current period start
    period_end: str = ""     # ISO date of next reset

    @property
    def effective_reset_day(self) -> int:
        """The reset day actually driving the period grid. An end-of-month
        bill lets the ISP's month-end day win too (some close on the 25th or
        28th, not the calendar end): a configured day drives the reset as-is,
        and 0 falls back to the calendar end (1st of the next month)."""
        if self.period_type == "end_of_month":
            return self.reset_day if self.reset_day > 0 else 1
        return self.reset_day


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
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
    access_interface TEXT NOT NULL DEFAULT '',
    access_override  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS leases (
    mac         TEXT PRIMARY KEY,
    ip          TEXT NOT NULL,
    lease_start REAL NOT NULL,
    lease_end   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bundle_config (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    total_gb     REAL NOT NULL,
    reset_day    INTEGER NOT NULL,
    period_type  TEXT NOT NULL DEFAULT 'renew_day',
    allowances   TEXT NOT NULL DEFAULT '{}',
    period_start TEXT NOT NULL DEFAULT '',
    period_end   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS usage_daily (
    device_id INTEGER NOT NULL,
    date      TEXT NOT NULL,
    up_bytes  INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, date)
);

-- Per-device DNS browsing history (what each device queries, per minute
-- bucket). Fed from dnsmasq's query log by quota/dnslog.py; pruned hourly
-- per-user by their history_days retention (NULL = the global default).
-- No FK (app-layer delete in delete_device, matching the codebase style);
-- device_id is NOT in the bucket index because it leads the PK.
CREATE TABLE IF NOT EXISTS dns_history (
    device_id     INTEGER NOT NULL,
    bucket_minute TEXT    NOT NULL,   -- local "%Y-%m-%d %H:%M" (lexicographic == chronological)
    domain        TEXT    NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, bucket_minute, domain)
);
CREATE INDEX IF NOT EXISTS idx_dns_history_bucket ON dns_history(bucket_minute);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    level     TEXT NOT NULL DEFAULT 'info',
    device_id INTEGER,
    user_id   INTEGER,
    message   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
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
    history_days     INTEGER,             -- NULL = global history.retention_days
    dns_server       TEXT NOT NULL DEFAULT ''
);

-- MAC whitelist/blacklist (quota/service.py): 'allow' = never quota-blocked
-- (even over allowance), 'deny' = always blocked (even when the user is fine).
-- Both are enforced at render time via resolve_device_state — no device rows
-- are touched, so removing a MAC from a list restores instantly. The 'deny'
-- list doubles as the permanent blacklist that a manual user/device DELETE
-- writes to: a blacklisted MAC never auto-registers and stays kernel-blocked
-- even with no device row, until the admin removes it in the Network tab.
CREATE TABLE IF NOT EXISTS mac_lists (
    mac        TEXT NOT NULL,
    kind       TEXT NOT NULL,  -- 'allow' | 'deny'
    created_at REAL NOT NULL,
    PRIMARY KEY (mac, kind)
);

-- Domain-level DNS filtering (quota/dns_rules.py renders these into
-- generated dnsmasq config). scope_id is a user_id or device_id depending on
-- `scope`, and NULL for scope='global'.
--
-- scope_key exists ONLY so the UNIQUE constraint below actually behaves like
-- an upsert key: SQLite treats every NULL as DISTINCT from every other NULL,
-- so a naive UNIQUE(scope, scope_id, domain, action) never collides for two
-- 'global' rows (scope_id is NULL on both) — re-submitting the same global
-- rule silently INSERTs a duplicate instead of updating the existing one.
-- scope_key is a NOT NULL stand-in (COALESCE(scope_id, 0)) so two 'global'
-- rows for the same domain/action DO collide and the upsert in
-- create_domain_rule fires correctly. 0 is never a real user/device id
-- (AUTOINCREMENT starts at 1), so it cannot collide with a genuine scope_id.
CREATE TABLE IF NOT EXISTS domain_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL DEFAULT 'global',
    scope_id   INTEGER,
    scope_key  INTEGER GENERATED ALWAYS AS (COALESCE(scope_id, 0)) STORED,
    action     TEXT NOT NULL DEFAULT 'block',
    domain     TEXT NOT NULL,
    target_ip  TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at REAL NOT NULL,
    UNIQUE(scope, scope_key, domain, action)
);

-- One row per built-in blocklist preset (quota/dns_rules.py:PRESETS) that
-- has ever been enabled somewhere, so the dashboard can show its state
-- (enabled/disabled, how many domains, when it was last refreshed) without
-- re-fetching the source. Enabling a preset also inserts domain_rules rows
-- (source='preset:<preset_id>:<scope>:<scope_id>') for its compiled domain
-- list; disabling it, or re-enabling it at a DIFFERENT scope, deletes those
-- rows again (see api/app.py's enable_dns_preset — the old-scope purge is
-- what prevents an orphaned rule set from a scope change).
CREATE TABLE IF NOT EXISTS dns_presets (
    preset_id    TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 0,
    scope        TEXT NOT NULL DEFAULT 'global',
    scope_id     INTEGER,
    domain_count INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT 0
);
"""


class Database:
    """Async wrapper over the single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
        except (PermissionError, OSError) as exc:
            raise RuntimeError(
                f"cannot open database {self.path}: {exc}. Ensure the directory "
                "is writable — on the Linux gateway run the app as root (or "
                "chown /var/lib/quota-gateway to the service user)") from exc
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        # Legacy cleanup: the old guest-deletion `suppressed_macs` table (v20,
        # pre-blacklist) is orphaned — delete->deny-list replaced it and nothing
        # references it anymore. Idempotent; a fresh DB simply has no such table.
        await self._conn.execute("DROP TABLE IF EXISTS suppressed_macs")
        # Lightweight migration: topup_gb (per-device top-up persistence) was
        # added after the first release; ALTER is a no-op when it already exists.
        try:
            await self._conn.execute(
                "ALTER TABLE devices ADD COLUMN topup_gb REAL NOT NULL DEFAULT 0")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # v2 users migration: devices are grouped under a user and the monthly
        # allowance lives on the user. Idempotent — ALTER no-ops on already-
        # migrated DBs, and _backfill_users only touches rows still unowned.
        try:
            await self._conn.execute(
                "ALTER TABLE devices ADD COLUMN user_id INTEGER")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        try:
            await self._conn.execute(
                "ALTER TABLE devices ADD COLUMN bypass INTEGER NOT NULL DEFAULT 0")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        try:
            await self._conn.execute(
                "ALTER TABLE events ADD COLUMN user_id INTEGER")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # v10 guest mode: users carry a guest flag (auto-registered guests are
        # deleted when the quota period resets). ALTER no-ops when present.
        try:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN guest INTEGER NOT NULL DEFAULT 0")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # v11 speed shaping: per-device + per-user internet speed caps in Mbps
        # (0 = unlimited), consumed by the Linux tc shaper (quota/shaping.py).
        # ALTER no-ops when already present (fresh SCHEMA includes them).
        for table in ("devices", "users"):
            for col in ("limit_down_mbps", "limit_up_mbps"):
                try:
                    await self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} "
                        "REAL NOT NULL DEFAULT 0")
                    await self._conn.commit()
                except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
                    pass
        # v20 protected users: the Gateway box account cannot be deleted. ALTER
        # no-ops when already present (fresh SCHEMA includes it).
        try:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN protected "
                "INTEGER NOT NULL DEFAULT 0")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # quota exemption: a user that is never quota-blocked no matter their
        # usage (admin cuts still apply). ALTER no-ops on existing DBs.
        try:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN exempt_quota "
                "INTEGER NOT NULL DEFAULT 0")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # v21 milestone flags: per-user consumption-threshold notices (50/75/100
        # % of allowance). Fresh SCHEMA includes them; ALTER no-ops on existing
        # DBs (default 0 = not yet notified).
        for col in ("notified_50", "notified_75", "notified_100"):
            try:
                await self._conn.execute(
                    f"ALTER TABLE users ADD COLUMN {col} "
                    "INTEGER NOT NULL DEFAULT 0")
                await self._conn.commit()
            except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
                pass
        # dns-history retention override per user (NULL = the global
        # history.retention_days default). ALTER no-ops on existing DBs.
        try:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN history_days INTEGER")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # DNS filtering: per-user/per-device upstream DNS-server override
        # column. ALTER no-ops when already present (fresh SCHEMA includes
        # it); domain_rules/dns_presets are brand-new tables created by the
        # executescript above, no ALTER needed for those, but a domain_rules
        # table created by a PRE-FIX build (missing scope_key) does need a
        # one-time rebuild — see _migrate_domain_rules_scope_key.
        for table in ("devices", "users"):
            try:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN dns_server "
                    "TEXT NOT NULL DEFAULT ''")
                await self._conn.commit()
            except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
                pass
        # Per-device source-interface tag (WiFi/LAN): which NIC the device was
        # last seen on, auto-learned from the kernel neighbor table. ALTER
        # no-ops when already present (fresh SCHEMA includes it).
        try:
            await self._conn.execute(
                "ALTER TABLE devices ADD COLUMN source_interface "
                "TEXT NOT NULL DEFAULT ''")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        # Router-side access label (WiFi probe auto-learn + manual override).
        # ALTER no-ops when already present (fresh SCHEMA includes both).
        for column in ("access_interface", "access_override"):
            try:
                await self._conn.execute(
                    f"ALTER TABLE devices ADD COLUMN {column} "
                    "TEXT NOT NULL DEFAULT ''")
                await self._conn.commit()
            except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
                pass
        # Bundle period type: 'renew_day' (configured reset_day) or
        # 'end_of_month' (calendar-month bill — resets on the 1st whatever the
        # configured day says). ALTER no-ops when already present (fresh
        # SCHEMA includes it).
        try:
            await self._conn.execute(
                "ALTER TABLE bundle_config ADD COLUMN period_type "
                "TEXT NOT NULL DEFAULT 'renew_day'")
            await self._conn.commit()
        except Exception:  # noqa: BLE001  (duplicate column on existing DBs)
            pass
        await self._migrate_domain_rules_scope_key()
        await self._backfill_users()
        await self._seed_gateway()
        await self._conn.commit()

    async def _migrate_domain_rules_scope_key(self) -> None:
        """One-time repair for a ``domain_rules`` table created by a
        pre-fix build of the DNS-filtering feature.

        The original ``UNIQUE(scope, scope_id, domain, action)`` constraint
        never actually deduplicated global-scope rows: SQLite treats every
        NULL as distinct from every other NULL, so ``scope_id IS NULL``
        never collided and re-submitting the same global rule silently
        inserted a duplicate row instead of updating the existing one. The
        fix adds a NOT NULL ``scope_key`` (``COALESCE(scope_id, 0)``) and
        uniques on that instead (see the ``domain_rules`` table comment in
        SCHEMA). A brand-new DB already gets the fixed table from
        ``executescript`` above, so this is a no-op for it; only a DB that
        already has the OLD shape gets rebuilt here, collapsing any
        duplicates the old constraint let through (keeping the newest row
        per scope/scope_id/domain/action).
        """
        row = await self._fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='domain_rules'")
        if row is None:
            return  # nothing to migrate yet — SCHEMA creates the fixed table
        cols = await self.conn.execute_fetchall("PRAGMA table_info(domain_rules)")
        if any(c["name"] == "scope_key" for c in cols):
            return  # already the fixed shape
        log.warning("domain_rules predates the scope_key fix — rebuilding "
                   "the table and dropping any duplicate global rules the "
                   "old NULL-scope upsert bug let through")
        await self._conn.executescript("""
            ALTER TABLE domain_rules RENAME TO domain_rules_old;
            CREATE TABLE domain_rules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                scope      TEXT NOT NULL DEFAULT 'global',
                scope_id   INTEGER,
                scope_key  INTEGER GENERATED ALWAYS AS (COALESCE(scope_id, 0)) STORED,
                action     TEXT NOT NULL DEFAULT 'block',
                domain     TEXT NOT NULL,
                target_ip  TEXT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                source     TEXT NOT NULL DEFAULT 'manual',
                created_at REAL NOT NULL,
                UNIQUE(scope, scope_key, domain, action)
            );
        """)
        await self._conn.execute("""
            INSERT INTO domain_rules
                (scope, scope_id, action, domain, target_ip, enabled, source, created_at)
            SELECT scope, scope_id, action, domain, target_ip, enabled, source, created_at
            FROM domain_rules_old
            WHERE id IN (
                SELECT MAX(id) FROM domain_rules_old
                GROUP BY scope, COALESCE(scope_id, 0), domain, action
            )
        """)
        await self._conn.execute("DROP TABLE domain_rules_old")
        await self._conn.commit()

    async def _backfill_users(self) -> None:
        """Give every device without a user its own user (v2 migration).

        Each legacy device becomes a single-device user carrying over its name,
        quota mode, fixed GB and any per-device top-up. An admin manual block
        is preserved on the new user (the device keeps its own too). Idempotent:
        only rows with ``user_id IS NULL`` are touched, so this runs every boot
        and is a no-op once the migration is complete.
        """
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM devices WHERE user_id IS NULL")
        for row in rows:
            user_state = (BLOCK_ADMIN if row["block_state"] == BLOCK_ADMIN
                          else BLOCK_OK)
            cur = await self.conn.execute(
                "INSERT INTO users (name, quota_mode, fixed_gb, block_state, "
                "topup_gb, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["name"], row["quota_mode"], row["fixed_gb"], user_state,
                 row["topup_gb"] or 0.0, row["created_at"]))
            await self.conn.execute(
                "UPDATE devices SET user_id=? WHERE id=?", (cur.lastrowid, row["id"]))
        if rows:
            await self.conn.commit()
            await self.add_event(
                f"Migrated {len(rows)} device(s) to per-user quotas", "info")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._conn

    async def _fetch_one(self, sql: str, args: tuple[Any, ...] = ()) -> Any | None:
        """Execute a query and return the first row (or None)."""
        cursor = await self.conn.execute(sql, args)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    # -- devices -----------------------------------------------------------

    async def upsert_device(self, mac: str, name: str = "",
                            quota_mode: str = QUOTA_AUTO,
                            fixed_gb: float | None = None,
                            user_id: int | None = None,
                            user_name: str | None = None,
                            guest: bool = False,
                            limit_down_mbps: float = 0.0,
                            limit_up_mbps: float = 0.0) -> Device:
        """Register/update a device. A brand-new MAC with no ``user_id`` gets
        its own managed user (``user_name`` or the device name, same quota), so
        auto-discovered DHCP devices are quota-managed immediately. Existing
        rows keep their user_id and block_state (only name/quota are
        refreshed). ``guest=True`` flags the auto-created user as a guest
        account (fixed quota, deleted on period reset)."""
        mac = mac.lower()
        now = time.time()
        existing = await self._fetch_one(
            "SELECT id FROM devices WHERE mac=?", (mac,))
        if existing is not None:
            await self.conn.execute(
                "UPDATE devices SET name=?, quota_mode=?, fixed_gb=?, "
                "limit_down_mbps=?, limit_up_mbps=? WHERE mac=?",
                (name, quota_mode, fixed_gb,
                 limit_down_mbps, limit_up_mbps, mac))
            await self.conn.commit()
        else:
            if user_id is None:
                user_id = (await self.create_user(
                    name=user_name or name, quota_mode=quota_mode,
                    fixed_gb=fixed_gb, guest=guest)).id
            await self.conn.execute(
                """INSERT INTO devices (mac, name, quota_mode, fixed_gb, block_state, created_at, user_id, limit_down_mbps, limit_up_mbps)
                   VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?)""",
                (mac, name, quota_mode, fixed_gb, now, user_id,
                 limit_down_mbps, limit_up_mbps))
            await self.conn.commit()
        row = await self._fetch_one("SELECT * FROM devices WHERE mac=?", (mac,))
        return _row_to_device(row)  # type: ignore[arg-type]

    async def get_device(self, device_id: int | None = None,
                         mac: str | None = None) -> Device | None:
        if device_id is not None:
            row = await self._fetch_one(
                "SELECT * FROM devices WHERE id=?", (device_id,))
        else:
            row = await self._fetch_one(
                "SELECT * FROM devices WHERE mac=?", (mac or "",))
        return _row_to_device(row) if row else None

    async def get_device_by_ip(self, ip: str) -> Device | None:
        row = await self._fetch_one(
            """SELECT d.* FROM devices d JOIN leases l ON d.mac = l.mac
               WHERE l.ip = ?""", (ip,))
        return _row_to_device(row) if row else None

    async def list_devices(self, user_id: int | None = None) -> list[Device]:
        if user_id is None:
            rows = await self.conn.execute_fetchall(
                "SELECT * FROM devices ORDER BY name COLLATE NOCASE")
        else:
            rows = await self.conn.execute_fetchall(
                "SELECT * FROM devices WHERE user_id=? ORDER BY name COLLATE NOCASE",
                (user_id,))
        return [_row_to_device(r) for r in rows]

    async def update_device(self, device_id: int, **fields: Any) -> Device | None:
        allowed = {"name", "quota_mode", "fixed_gb", "block_state",
                   "user_id", "bypass", "limit_down_mbps", "limit_up_mbps",
                   "dns_server", "source_interface", "access_interface",
                   "access_override"}
        sets, args = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                args.append(value)
        if not sets:
            return await self.get_device(device_id)
        args.append(device_id)
        await self.conn.execute(
            f"UPDATE devices SET {', '.join(sets)} WHERE id=?", args)
        await self.conn.commit()
        return await self.get_device(device_id)

    async def delete_device(self, device_id: int,
                            deny_list_mac: bool = False) -> None:
        """Delete a device. When ``deny_list_mac`` is set, its MAC is added to
        the deny list (permanent blacklist) FIRST, so run.py never auto-
        registers it again while it stays connected and the kernel keeps
        blocking it even without a device row. Removal is manual: deleting the
        MAC from the deny list in the Network tab unblocks it. The month-reset
        path never sets the flag — a returning guest after a reset registers
        fresh."""
        row = await self._fetch_one(
            "SELECT mac, user_id FROM devices WHERE id=?", (device_id,))
        if deny_list_mac and row is not None:
            await self.add_mac_list("deny", [row["mac"]])
        await self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
        # app-layer FK cleanup: the device's DNS history dies with it (usage
        # rows are TTL-bounded by the quota period instead).
        await self.conn.execute(
            "DELETE FROM dns_history WHERE device_id=?", (device_id,))
        # Device-scoped domain rules are meaningless once the device is gone
        # (their dnsmasq tag would never be applied to anything again).
        await self.delete_domain_rules_by_scope(DNS_SCOPE_DEVICE, device_id)
        await self.conn.commit()

    async def set_device_state(self, device_id: int, state: str) -> None:
        await self.update_device(device_id, block_state=state)

    async def clear_topups(self) -> None:
        """Reset all top-ups when a new quota period opens."""
        await self.conn.execute("UPDATE users SET topup_gb = 0")
        await self.conn.execute("UPDATE devices SET topup_gb = 0")
        await self.conn.commit()

    async def reset_milestone_flags(self) -> None:
        """Clear every user's milestone-notification flags (period roll).

        A fresh quota period re-arms the 50/75/100% notices so each threshold
        is surfaced again in the new month. Idempotent — safe to call on any
        period open/manual reset.
        """
        await self.conn.execute(
            "UPDATE users SET notified_50=0, notified_75=0, notified_100=0")
        await self.conn.commit()

    async def clear_usage(self, since_date: str) -> None:
        """Delete usage rows from a date onward.

        Used by the manual "Reset month" action. Usage is stored day-granular
        (one row per device/date), so a reset on a day that already has usage
        could otherwise never drop the period counter below that day's total —
        the button would appear to do nothing. History before ``since_date`` is
        preserved.
        """
        await self.conn.execute(
            "DELETE FROM usage_daily WHERE date >= ?", (since_date,))
        await self.conn.commit()

    # -- users --------------------------------------------------------------

    async def create_user(self, name: str = "", quota_mode: str = QUOTA_AUTO,
                          fixed_gb: float | None = None,
                          block_state: str = BLOCK_OK,
                          guest: bool = False,
                          protected: bool = False,
                          limit_down_mbps: float = 0.0,
                          limit_up_mbps: float = 0.0,
                          exempt_quota: bool = False) -> User:
        """Insert a user (no devices). Used by the API, by new-device
        auto-registration, by the v2 migration backfill, and by
        :meth:`_seed_gateway` (``protected=True`` for the Gateway account)."""
        cur = await self.conn.execute(
            "INSERT INTO users (name, quota_mode, fixed_gb, block_state, "
            "topup_gb, created_at, guest, protected, exempt_quota, "
            "limit_down_mbps, limit_up_mbps) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (name, quota_mode, fixed_gb, block_state, time.time(),
             int(guest), int(protected), int(exempt_quota),
             limit_down_mbps, limit_up_mbps))
        await self.conn.commit()
        row = await self._fetch_one("SELECT * FROM users WHERE id=?",
                                    (cur.lastrowid,))
        return _row_to_user(row)  # type: ignore[arg-type]

    async def get_user(self, user_id: int) -> User | None:
        row = await self._fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
        return _row_to_user(row) if row else None

    async def list_users(self) -> list[User]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM users ORDER BY name COLLATE NOCASE")
        return [_row_to_user(r) for r in rows]

    async def update_user(self, user_id: int, **fields: Any) -> User | None:
        allowed = {"name", "quota_mode", "fixed_gb", "block_state",
                   "limit_down_mbps", "limit_up_mbps",
                   "notified_50", "notified_75", "notified_100",
                   "history_days", "dns_server", "exempt_quota"}
        sets, args = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                args.append(value)
        if not sets:
            return await self.get_user(user_id)
        args.append(user_id)
        await self.conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
        await self.conn.commit()
        return await self.get_user(user_id)

    async def delete_user(self, user_id: int, cascade: bool = True,
                          deny_list_macs: bool = False) -> int:
        """Delete a user (cascade removes their devices + usage rows). When
        ``deny_list_macs`` is set, EVERY device MAC is added to the deny list
        (permanent blacklist) first, so run.py never auto-registers those
        devices again while they stay connected and the kernel keeps blocking
        them even without device rows. Removal is manual: deleting the MACs
        from the deny list in the Network tab unblocks them. The month-reset
        path never sets the flag. Returns how many devices were removed."""
        rows = await self.conn.execute_fetchall(
            "SELECT id, mac FROM devices WHERE user_id=?", (user_id,))
        if rows and not cascade:
            raise ValueError(
                "user still has devices; reassign or delete them first")
        for r in rows:
            if deny_list_macs:
                await self.add_mac_list("deny", [r["mac"]])
            await self.conn.execute(
                "DELETE FROM usage_daily WHERE device_id=?", (r["id"],))
            await self.conn.execute("DELETE FROM devices WHERE id=?", (r["id"],))
            await self.delete_domain_rules_by_scope(DNS_SCOPE_DEVICE, r["id"])
        await self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        await self.conn.commit()
        await self.delete_domain_rules_by_scope(DNS_SCOPE_USER, user_id)
        return len(rows)

    async def add_topup_user(self, user_id: int, extra_gb: float) -> None:
        """Accumulate a per-user top-up (survives allowance recomputes)."""
        await self.conn.execute(
            "UPDATE users SET topup_gb = topup_gb + ? WHERE id=?",
            (extra_gb, user_id))
        await self.conn.commit()

    async def count_guest_users(self) -> int:
        """How many guest user accounts currently exist (blocked or not).
        Feeds the guest-limit check: when the limit is reached, a brand-new
        device is still registered as a guest but immediately admin-blocked,
        so MAC-changing spam stops getting a fresh allowance."""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS n FROM users WHERE guest=1")
        return int(row["n"]) if row else 0

    async def delete_guest_users(self) -> int:
        """Delete every guest user (cascade removes their devices + usage).
        Called when a quota period resets — a new period starts with no guests.
        Returns how many guest users were removed."""
        rows = await self.conn.execute_fetchall(
            "SELECT id FROM users WHERE guest=1")
        for r in rows:
            await self.delete_user(r["id"], cascade=True)
        return len(rows)

    async def set_guest_fixed_gb(self, gb: float) -> None:
        """Re-apply the guest quota to every existing guest user (their
        allowance changes immediately, not just for future guests)."""
        await self.conn.execute(
            "UPDATE users SET fixed_gb=? WHERE guest=1", (gb,))
        await self.conn.commit()

    async def _seed_gateway(self) -> None:
        """Idempotently create the protected "Gateway" user + the box's device.

        The gateway machine's own internet consumption is charged to this user
        (default 1 GB fixed allowance), so the box's traffic is INSIDE the
        quota math — and the admin can cut the box's own internet by setting
        the allowance to 0 (or using the block toggle). The user is
        ``protected``: it cannot be deleted (API 400 + no delete button), only
        edited. Anchored on the GATEWAY_MAC device so a re-seed (every boot)
        never duplicates either row.
        """
        if await self._fetch_one("SELECT 1 FROM devices WHERE mac=?",
                                 (GATEWAY_MAC,)):
            return  # already seeded
        row = await self._fetch_one("SELECT id FROM users WHERE protected=1")
        if row is not None:
            user_id = row["id"]
        else:
            user_id = (await self.create_user(
                name="Gateway", quota_mode=QUOTA_FIXED, fixed_gb=1.0,
                protected=True)).id
        await self.conn.execute(
            "INSERT INTO devices (mac, name, quota_mode, fixed_gb, block_state, "
            "created_at, user_id) VALUES (?, 'Gateway box', 'fixed', 1.0, "
            "'ok', ?, ?)",
            (GATEWAY_MAC, time.time(), user_id))
        await self.conn.commit()

    # -- MAC whitelist/blacklist --------------------------------------------

    async def get_mac_list(self, kind: str) -> list[str]:
        """All MACs in the allow/deny list (lowercased, sorted)."""
        row = await self.conn.execute_fetchall(
            "SELECT mac FROM mac_lists WHERE kind=? ORDER BY mac", (kind,))
        return [r["mac"] for r in row]

    async def add_mac_list(self, kind: str, macs: list[str]) -> None:
        """Add MACs to the allow/deny list (additive — existing entries are
        kept). Lowercased + idempotent; used by manual deletes to permanently
        blacklist a device's MAC."""
        kind = kind if kind in ("allow", "deny") else "allow"
        cleaned = sorted({m.lower().strip() for m in macs if m and m.strip()})
        if not cleaned:
            return
        await self.conn.executemany(
            "INSERT OR IGNORE INTO mac_lists (mac, kind, created_at) "
            "VALUES (?, ?, ?)",
            [(m, kind, time.time()) for m in cleaned])
        await self.conn.commit()

    async def set_mac_list(self, kind: str, macs: list[str]) -> None:
        """Replace the whole allow/deny list. Lowercased + idempotent."""
        kind = kind if kind in ("allow", "deny") else "allow"
        cleaned = sorted({m.lower().strip() for m in macs if m and m.strip()})
        await self.conn.execute("DELETE FROM mac_lists WHERE kind=?", (kind,))
        await self.conn.executemany(
            "INSERT OR IGNORE INTO mac_lists (mac, kind, created_at) "
            "VALUES (?, ?, ?)",
            [(m, kind, time.time()) for m in cleaned])
        await self.conn.commit()

    async def mac_lists(self) -> dict[str, list[str]]:
        """Both lists: ``{"allow": [...], "deny": [...]}``."""
        return {"allow": await self.get_mac_list("allow"),
                "deny": await self.get_mac_list("deny")}

    # -- leases ------------------------------------------------------------

    async def upsert_lease(self, mac: str, ip: str, lease_hours: int) -> Lease:
        now = time.time()
        end = now + lease_hours * 3600
        await self.conn.execute(
            """INSERT INTO leases (mac, ip, lease_start, lease_end)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip,
                 lease_start=excluded.lease_start, lease_end=excluded.lease_end""",
            (mac.lower(), ip, now, end),
        )
        await self.conn.commit()
        return Lease(mac=mac.lower(), ip=ip, lease_start=now, lease_end=end)

    async def get_ip_for_mac(self, mac: str) -> str | None:
        row = await self._fetch_one("SELECT ip FROM leases WHERE mac=?", (mac.lower(),))
        return row[0] if row else None

    async def get_mac_for_ip(self, ip: str) -> str | None:
        row = await self._fetch_one("SELECT mac FROM leases WHERE ip=?", (ip,))
        return row[0] if row else None

    async def list_leases(self) -> list[Lease]:
        rows = await self.conn.execute_fetchall("SELECT * FROM leases")
        return [
            Lease(mac=r["mac"], ip=r["ip"],
                  lease_start=r["lease_start"], lease_end=r["lease_end"])
            for r in rows
        ]

    async def delete_lease(self, mac: str) -> None:
        await self.conn.execute("DELETE FROM leases WHERE mac=?", (mac.lower(),))
        await self.conn.commit()

    async def set_lease(self, mac: str, ip: str) -> None:
        """Direct assignment (used by static ip<->mac mapping)."""
        now = time.time()
        await self.conn.execute(
            """INSERT INTO leases (mac, ip, lease_start, lease_end)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip""",
            (mac.lower(), ip, now, now + 86400),
        )
        await self.conn.commit()

    # -- bundle config ------------------------------------------------------

    async def get_bundle(self) -> Bundle:
        row = await self._fetch_one("SELECT * FROM bundle_config WHERE id=1")
        if row is None:
            return Bundle()
        # Coerce keys to int and skip stale entries (a pre-v2 DB holds a
        # MAC-keyed dict; those keys are dropped and the snapshot is rewritten
        # on the next allowance recompute).
        allowances: dict[int, float] = {}
        try:
            raw = json.loads(row["allowances"] or "{}")
            for k, v in raw.items():
                try:
                    allowances[int(k)] = float(v)
                except (ValueError, TypeError):
                    pass  # stale MAC-keyed entry from before the users migration
        except json.JSONDecodeError:
            allowances = {}
        return Bundle(
            total_gb=float(row["total_gb"]),
            reset_day=int(row["reset_day"]),
            period_type=row["period_type"] or "renew_day",
            allowances=allowances,
            period_start=row["period_start"] or "",
            period_end=row["period_end"] or "",
        )

    async def set_bundle(self, bundle: Bundle) -> None:
        await self.conn.execute(
            """INSERT INTO bundle_config (id, total_gb, reset_day, period_type, allowances, period_start, period_end)
               VALUES (1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET total_gb=excluded.total_gb,
                 reset_day=excluded.reset_day, period_type=excluded.period_type,
                 allowances=excluded.allowances,
                 period_start=excluded.period_start, period_end=excluded.period_end""",
            (bundle.total_gb, bundle.reset_day, bundle.period_type,
             json.dumps(bundle.allowances), bundle.period_start, bundle.period_end),
        )
        await self.conn.commit()

    # -- usage ----------------------------------------------------------------

    async def add_usage(self, device_id: int, date: str,
                        up_bytes: int, down_bytes: int) -> None:
        await self.conn.execute(
            """INSERT INTO usage_daily (device_id, date, up_bytes, down_bytes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(device_id, date) DO UPDATE SET
                 up_bytes = up_bytes + excluded.up_bytes,
                 down_bytes = down_bytes + excluded.down_bytes""",
            (device_id, date, up_bytes, down_bytes),
        )
        await self.conn.commit()

    async def get_usage(self, device_id: int, since_date: str = "") -> dict[str, int]:
        """Return {up_bytes, down_bytes, total_bytes} for a device since a date."""
        if since_date:
            rows = await self.conn.execute_fetchall(
                "SELECT up_bytes, down_bytes FROM usage_daily WHERE device_id=? AND date>=?",
                (device_id, since_date))
        else:
            rows = await self.conn.execute_fetchall(
                "SELECT up_bytes, down_bytes FROM usage_daily WHERE device_id=?",
                (device_id,))
        up = sum(r["up_bytes"] for r in rows)
        down = sum(r["down_bytes"] for r in rows)
        return {"up_bytes": up, "down_bytes": down, "total_bytes": up + down}

    async def get_period_usage(self) -> dict[int, dict[str, int]]:
        """Aggregate usage since period_start, keyed by device_id."""
        bundle = await self.get_bundle()
        since = bundle.period_start or ""
        rows = await self.conn.execute_fetchall(
            "SELECT device_id, SUM(up_bytes) up, SUM(down_bytes) down FROM usage_daily "
            "WHERE date>=? GROUP BY device_id", (since,))
        return {r["device_id"]: {"up": r["up"], "down": r["down"]} for r in rows}

    async def get_period_usage_by_user(self) -> dict[int, dict[str, int]]:
        """Aggregate usage since period_start, keyed by user_id."""
        bundle = await self.get_bundle()
        since = bundle.period_start or ""
        rows = await self.conn.execute_fetchall(
            "SELECT d.user_id user_id, SUM(u.up_bytes) up, SUM(u.down_bytes) down "
            "FROM usage_daily u JOIN devices d ON u.device_id = d.id "
            "WHERE u.date>=? GROUP BY d.user_id", (since,))
        out: dict[int, dict[str, int]] = {}
        for r in rows:
            if r["user_id"] is None:
                continue  # orphaned device (should not happen post-migration)
            out[r["user_id"]] = {"up": r["up"], "down": r["down"]}
        return out

    # -- DNS browsing history ----------------------------------------------

    async def batch_add_dns_history(
            self, rows: list[tuple[int, str, str, int]]) -> None:
        """Accumulate (device_id, minute, domain, count) buckets in one commit.

        Rows with the same (device_id, minute, domain) merge their counts — the
        tailer drains per-tick events into per-minute buckets, and two drains
        can land in the same bucket before the minute rolls. One commit for the
        whole batch (the tick fires at most ~once per 15 s, so a commit per
        drain is fine).
        """
        await self.conn.executemany(
            """INSERT INTO dns_history (device_id, bucket_minute, domain, count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(device_id, bucket_minute, domain) DO UPDATE SET
                 count = count + excluded.count""",
            rows)
        await self.conn.commit()

    async def get_dns_history(self, device_id: int | None, since_minute: str,
                              limit: int = 100) -> dict[str, Any]:
        """DNS history since ``since_minute`` for one device (or all devices).

        ``device_id=None`` aggregates across every device (the household
        view); a device id scopes to that device. Returns ``top_domains``
        (domain -> total hits, most-hit first), ``activity`` (minute bucket ->
        hits, oldest first, hourly-rolled client-side), ``recent`` (the latest
        bucket lines, newest first, carrying the owning ``device_id`` so the
        UI can badge each row) and ``total`` (all hits in the window, for the
        header and share percentages).
        """
        if device_id is None:
            scope, params = "", (since_minute,)
        else:
            scope, params = "device_id=? AND ", (device_id, since_minute)
        top = await self.conn.execute_fetchall(
            "SELECT domain, SUM(count) hits FROM dns_history "
            f"WHERE {scope}bucket_minute>=? "
            "GROUP BY domain ORDER BY hits DESC, domain LIMIT ?",
            params + (limit,))
        activity = await self.conn.execute_fetchall(
            "SELECT bucket_minute minute, SUM(count) hits FROM dns_history "
            f"WHERE {scope}bucket_minute>=? "
            "GROUP BY bucket_minute ORDER BY bucket_minute",
            params)
        recent = await self.conn.execute_fetchall(
            "SELECT bucket_minute minute, domain, count, device_id FROM dns_history "
            f"WHERE {scope}bucket_minute>=? "
            "ORDER BY bucket_minute DESC, count DESC, domain LIMIT ?",
            params + (limit,))
        total = await self._fetch_one(
            "SELECT COALESCE(SUM(count), 0) hits FROM dns_history "
            f"WHERE {scope}bucket_minute>=?",
            params)
        return {
            "top_domains": [{"domain": r["domain"], "hits": r["hits"]}
                            for r in top],
            "activity": [{"minute": r["minute"], "hits": r["hits"]}
                         for r in activity],
            "recent": [{"minute": r["minute"], "domain": r["domain"],
                        "count": r["count"], "device_id": r["device_id"]}
                       for r in recent],
            "total": int(total[0]) if total else 0,
        }

    async def prune_dns_history(self, user_id: int, before_minute: str) -> int:
        """Delete ONE user's history rows older than ``before_minute``.

        Scoped per user because cutoffs differ (each user's ``history_days``,
        NULL = the global default): an unscoped ``bucket_minute < ?`` delete
        called once per user would let the shortest cutoff wipe rows belonging
        to a user with a longer retention. ``bucket_minute`` is a local
        ``"%Y-%m-%d %H:%M"`` string, so the comparison is chronological.
        Returns the rowcount. Called once per user on the hourly prune gate —
        a handful of DELETEs, one commit each.
        """
        cur = await self.conn.execute(
            "DELETE FROM dns_history WHERE bucket_minute < ? AND device_id IN "
            "(SELECT id FROM devices WHERE user_id=?)",
            (before_minute, user_id))
        await self.conn.commit()
        return cur.rowcount

    # -- settings -----------------------------------------------------------

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._fetch_one("SELECT value FROM settings WHERE key=?", (key,))
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value))
        await self.conn.commit()

    async def delete_setting(self, key: str) -> None:
        await self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        await self.conn.commit()

    # -- events / audit -----------------------------------------------------

    async def add_event(self, message: str, level: str = "info",
                        device_id: int | None = None,
                        user_id: int | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO events (ts, level, device_id, user_id, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), level, device_id, user_id, message))
        await self.conn.commit()

    async def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def count_matching_events(self, pattern: str,
                                    seconds: float) -> int:
        """Count events whose ``message`` matches ``pattern`` (LIKE, e.g.
        ``"Failed login%"``) newer than ``seconds`` ago.

        Feeds the dashboard's security alerting banner (failed logins in the
        last hour) without shipping the raw rows to the client.
        """
        cutoff = time.time() - seconds
        rows = await self.conn.execute_fetchall(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE message LIKE ? AND ts > ?",
            (pattern, cutoff))
        return int(rows[0]["n"]) if rows else 0

    async def prune_events(self, before_ts: float) -> int:
        """Delete audit rows older than ``before_ts`` (unix seconds).

        The events table is append-only and otherwise UNBOUNDED — the only
        real disk-growth risk on a gateway that runs for months. Called on the
        maintenance tick's hourly gate. Returns the rowcount removed.
        """
        cur = await self.conn.execute(
            "DELETE FROM events WHERE ts < ?", (before_ts,))
        await self.conn.commit()
        return cur.rowcount

    # -- domain rules (DNS filtering) ----------------------------------------

    async def create_domain_rule(self, scope: str, action: str, domain: str,
                                 scope_id: int | None = None,
                                 target_ip: str | None = None,
                                 enabled: bool = True,
                                 source: str = "manual") -> DomainRule:
        """Insert (or update, if the same scope/scope_id/domain/action
        combination already exists) one domain rule. The upsert targets
        ``(scope, scope_key, domain, action)`` — NOT ``scope_id`` directly —
        because SQLite treats every NULL as distinct, and scope_id is NULL
        for every global rule; see the ``domain_rules`` table comment in
        SCHEMA and ``_migrate_domain_rules_scope_key``."""
        now = time.time()
        await self.conn.execute(
            """INSERT INTO domain_rules
                 (scope, scope_id, action, domain, target_ip, enabled, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_key, domain, action) DO UPDATE SET
                 target_ip=excluded.target_ip, enabled=excluded.enabled,
                 source=excluded.source""",
            (scope, scope_id, action, domain, target_ip, int(enabled),
             source, now))
        await self.conn.commit()
        row = await self._fetch_one(
            "SELECT * FROM domain_rules WHERE scope=? AND scope_id IS ? "
            "AND domain=? AND action=?", (scope, scope_id, domain, action))
        return _row_to_domain_rule(row)  # type: ignore[arg-type]

    async def create_domain_rules_bulk(
            self, scope: str, action: str, domains: list[str],
            scope_id: int | None = None, source: str = "manual") -> int:
        """Insert many domain rules in ONE transaction via ``executemany``.

        Enabling a preset can mean 100k+ domains (e.g. the ads-tracking
        list); doing an individual INSERT + commit per row in a Python loop
        freezes SQLite for minutes and blocks the event loop for the whole
        request. This does one ``executemany`` + one commit instead — same
        shape as ``batch_add_dns_history``. Returns the number of domains
        submitted (duplicates upsert in place, same as
        :meth:`create_domain_rule`, so the count is submissions, not
        necessarily new rows).
        """
        if not domains:
            return 0
        now = time.time()
        rows = [(scope, scope_id, action, d, None, 1, source, now)
               for d in domains]
        await self.conn.executemany(
            """INSERT INTO domain_rules
                 (scope, scope_id, action, domain, target_ip, enabled, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_key, domain, action) DO UPDATE SET
                 enabled=excluded.enabled, source=excluded.source""",
            rows)
        await self.conn.commit()
        return len(rows)

    async def get_domain_rule(self, rule_id: int) -> DomainRule | None:
        row = await self._fetch_one(
            "SELECT * FROM domain_rules WHERE id=?", (rule_id,))
        return _row_to_domain_rule(row) if row else None

    async def list_domain_rules(self, scope: str | None = None,
                                scope_id: int | None = None,
                                enabled_only: bool = False) -> list[DomainRule]:
        """List rules, optionally filtered to one scope (and scope_id).
        With no filter, returns every rule at every scope — what the
        renderer (quota/dns_rules.py) needs to build the full config."""
        sql = "SELECT * FROM domain_rules WHERE 1=1"
        args: list[Any] = []
        if scope is not None:
            sql += " AND scope=?"
            args.append(scope)
            if scope_id is not None:
                sql += " AND scope_id=?"
                args.append(scope_id)
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY id"
        rows = await self.conn.execute_fetchall(sql, args)
        return [_row_to_domain_rule(r) for r in rows]

    async def update_domain_rule(self, rule_id: int, **fields: Any) -> DomainRule | None:
        allowed = {"enabled", "target_ip", "domain", "action"}
        sets, args = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                args.append(int(value) if key == "enabled" else value)
        if not sets:
            return await self.get_domain_rule(rule_id)
        args.append(rule_id)
        await self.conn.execute(
            f"UPDATE domain_rules SET {', '.join(sets)} WHERE id=?", args)
        await self.conn.commit()
        return await self.get_domain_rule(rule_id)

    async def delete_domain_rule(self, rule_id: int) -> None:
        await self.conn.execute("DELETE FROM domain_rules WHERE id=?", (rule_id,))
        await self.conn.commit()

    async def delete_domain_rules_by_source(self, source: str) -> int:
        """Delete every rule with an exact ``source`` match (used to remove
        a disabled preset's generated rules, or purge a preset's OLD
        scope's rules when it is re-enabled somewhere else — see
        api/app.py's enable_dns_preset). Returns the count removed."""
        rows = await self.conn.execute_fetchall(
            "SELECT id FROM domain_rules WHERE source=?", (source,))
        await self.conn.execute(
            "DELETE FROM domain_rules WHERE source=?", (source,))
        await self.conn.commit()
        return len(rows)

    async def delete_domain_rules_by_scope(self, scope: str, scope_id: int | None) -> int:
        """Delete every rule at one scope (used when a user/device is
        deleted, so their per-scope rules don't linger orphaned)."""
        rows = await self.conn.execute_fetchall(
            "SELECT id FROM domain_rules WHERE scope=? AND scope_id IS ?",
            (scope, scope_id))
        await self.conn.execute(
            "DELETE FROM domain_rules WHERE scope=? AND scope_id IS ?",
            (scope, scope_id))
        await self.conn.commit()
        return len(rows)

    # -- DNS blocklist presets ------------------------------------------------

    async def get_preset_state(self, preset_id: str) -> dict[str, Any] | None:
        row = await self._fetch_one(
            "SELECT * FROM dns_presets WHERE preset_id=?", (preset_id,))
        return dict(row) if row else None

    async def list_preset_states(self) -> list[dict[str, Any]]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM dns_presets ORDER BY preset_id")
        return [dict(r) for r in rows]

    async def set_preset_state(self, preset_id: str, enabled: bool,
                               scope: str = DNS_SCOPE_GLOBAL,
                               scope_id: int | None = None,
                               domain_count: int = 0) -> None:
        await self.conn.execute(
            """INSERT INTO dns_presets (preset_id, enabled, scope, scope_id,
                 domain_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(preset_id) DO UPDATE SET
                 enabled=excluded.enabled, scope=excluded.scope,
                 scope_id=excluded.scope_id, domain_count=excluded.domain_count,
                 updated_at=excluded.updated_at""",
            (preset_id, int(enabled), scope, scope_id, domain_count, time.time()))
        await self.conn.commit()

    async def delete_preset_state(self, preset_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM dns_presets WHERE preset_id=?", (preset_id,))
        await self.conn.commit()


def _row_to_device(row: Any) -> Device:
    return Device(
        id=row["id"],
        mac=row["mac"],
        name=row["name"] or "",
        quota_mode=row["quota_mode"],
        fixed_gb=row["fixed_gb"],
        block_state=row["block_state"],
        created_at=row["created_at"],
        topup_gb=float(row["topup_gb"] or 0.0),
        user_id=row["user_id"],
        bypass=bool(row["bypass"]),
        limit_down_mbps=float(row["limit_down_mbps"] or 0.0),
        limit_up_mbps=float(row["limit_up_mbps"] or 0.0),
        dns_server=row["dns_server"] or "",
        source_interface=row["source_interface"] or "",
        access_interface=row["access_interface"] or "",
        access_override=row["access_override"] or "",
    )


def _row_to_user(row: Any) -> User:
    return User(
        id=row["id"],
        name=row["name"] or "",
        quota_mode=row["quota_mode"],
        fixed_gb=row["fixed_gb"],
        block_state=row["block_state"],
        topup_gb=float(row["topup_gb"] or 0.0),
        created_at=row["created_at"],
        guest=bool(row["guest"]),
        protected=bool(row["protected"]),
        exempt_quota=bool(row["exempt_quota"]),
        limit_down_mbps=float(row["limit_down_mbps"] or 0.0),
        limit_up_mbps=float(row["limit_up_mbps"] or 0.0),
        notified_50=bool(row["notified_50"]),
        notified_75=bool(row["notified_75"]),
        notified_100=bool(row["notified_100"]),
        history_days=row["history_days"],
        dns_server=row["dns_server"] or "",
    )


def _row_to_domain_rule(row: Any) -> DomainRule:
    return DomainRule(
        id=row["id"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        action=row["action"],
        domain=row["domain"],
        target_ip=row["target_ip"],
        enabled=bool(row["enabled"]),
        source=row["source"],
        created_at=row["created_at"],
    )
