"""SQLite persistence layer (single file, async via aiosqlite).

Schema overview
---------------
users          -- every person: quota mode, allowance, enforcement state.
devices        -- every known MAC: name, owning user, per-device override.
leases         -- current/known DHCP leases (mac <-> ip).
suppressed_macs-- guest device MACs the admin manually deleted: while present
                 they are not auto-registered (cleared once they leave).
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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from quota.engine import GATEWAY_MAC

# ---------------------------------------------------------------------------
# Domain enums (kept as plain strings so the schema stays simple)
# ---------------------------------------------------------------------------

QUOTA_FIXED = "fixed"
QUOTA_AUTO = "auto"

BLOCK_OK = "ok"          # allowed, within quota
BLOCK_QUOTA = "quota"    # exceeded monthly allowance
BLOCK_ADMIN = "admin_off"  # manually switched off by admin


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

    @property
    def is_blocked(self) -> bool:
        return self.block_state != BLOCK_OK


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

    @property
    def is_admin_blocked(self) -> bool:
        return self.block_state == BLOCK_ADMIN


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
    # Snapshot of allowances computed at period start (json dict user_id->gb).
    allowances: dict[int, float] = field(default_factory=dict)
    period_start: str = ""   # ISO date of current period start
    period_end: str = ""     # ISO date of next reset


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
    limit_up_mbps    REAL NOT NULL DEFAULT 0
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
    limit_down_mbps  REAL NOT NULL DEFAULT 0,
    limit_up_mbps    REAL NOT NULL DEFAULT 0,
    notified_50      INTEGER NOT NULL DEFAULT 0,
    notified_75      INTEGER NOT NULL DEFAULT 0,
    notified_100     INTEGER NOT NULL DEFAULT 0,
    history_days     INTEGER              -- NULL = global history.retention_days
);

CREATE TABLE IF NOT EXISTS suppressed_macs (
    mac        TEXT PRIMARY KEY,
    created_at REAL NOT NULL
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
        await self._backfill_users()
        await self._seed_gateway()
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
                   "user_id", "bypass", "limit_down_mbps", "limit_up_mbps"}
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
                            suppress_guest_mac: bool = False) -> None:
        """Delete a device. When ``suppress_guest_mac`` is set AND the device
        belongs to a guest user, its MAC is recorded in ``suppressed_macs`` so
        run.py does not auto-register it again while it stays connected (the
        manual-delete-never-returns rule). The month-reset path calls without
        the flag — a returning guest after a reset re-registers fresh."""
        row = await self._fetch_one(
            "SELECT mac, user_id FROM devices WHERE id=?", (device_id,))
        if suppress_guest_mac and row is not None:
            if row["user_id"] is not None:
                user = await self._fetch_one(
                    "SELECT guest FROM users WHERE id=?", (row["user_id"],))
                if user is not None and user["guest"]:
                    await self.add_suppressed_mac(row["mac"])
        await self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
        # app-layer FK cleanup: the device's DNS history dies with it (usage
        # rows are TTL-bounded by the quota period instead).
        await self.conn.execute(
            "DELETE FROM dns_history WHERE device_id=?", (device_id,))
        await self.conn.commit()

    async def set_device_state(self, device_id: int, state: str) -> None:
        await self.update_device(device_id, block_state=state)

    async def add_topup(self, device_id: int, extra_gb: float) -> None:
        """Accumulate a per-device top-up (survives allowance recomputes)."""
        await self.conn.execute(
            "UPDATE devices SET topup_gb = topup_gb + ? WHERE id=?",
            (extra_gb, device_id))
        await self.conn.commit()

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
                          limit_up_mbps: float = 0.0) -> User:
        """Insert a user (no devices). Used by the API, by new-device
        auto-registration, by the v2 migration backfill, and by
        :meth:`_seed_gateway` (``protected=True`` for the Gateway account)."""
        cur = await self.conn.execute(
            "INSERT INTO users (name, quota_mode, fixed_gb, block_state, "
            "topup_gb, created_at, guest, protected, "
            "limit_down_mbps, limit_up_mbps) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (name, quota_mode, fixed_gb, block_state, time.time(),
             int(guest), int(protected), limit_down_mbps, limit_up_mbps))
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
                   "history_days"}
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
                          suppress_guest_macs: bool = False) -> int:
        """Delete a user (cascade removes their devices + usage rows). When
        ``suppress_guest_macs`` is set AND the user is a guest, their device
        MACs are recorded in ``suppressed_macs`` first, so run.py will not
        auto-register those devices again while they stay connected. Returns
        how many devices were removed with them."""
        user = await self._fetch_one("SELECT guest FROM users WHERE id=?",
                                     (user_id,))
        rows = await self.conn.execute_fetchall(
            "SELECT id, mac FROM devices WHERE user_id=?", (user_id,))
        if rows and not cascade:
            raise ValueError(
                "user still has devices; reassign or delete them first")
        for r in rows:
            if suppress_guest_macs and user is not None and user["guest"]:
                await self.add_suppressed_mac(r["mac"])
            await self.conn.execute(
                "DELETE FROM usage_daily WHERE device_id=?", (r["id"],))
            await self.conn.execute("DELETE FROM devices WHERE id=?", (r["id"],))
        await self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        await self.conn.commit()
        return len(rows)

    async def add_topup_user(self, user_id: int, extra_gb: float) -> None:
        """Accumulate a per-user top-up (survives allowance recomputes)."""
        await self.conn.execute(
            "UPDATE users SET topup_gb = topup_gb + ? WHERE id=?",
            (extra_gb, user_id))
        await self.conn.commit()

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

    # -- suppressed MACs ------------------------------------------------------
    # A manually-deleted guest device's MAC is recorded here so run.py does not
    # auto-register it again while it stays connected. The row is cleared once
    # the MAC drops out of the dnsmasq lease file (device genuinely left), so a
    # later return registers a fresh guest. The month-reset path NEVER writes
    # here — returning guests after a reset re-register normally.

    async def add_suppressed_mac(self, mac: str) -> None:
        """Record a MAC that must not auto-register. Lowercased + idempotent."""
        await self.conn.execute(
            "INSERT INTO suppressed_macs (mac, created_at) VALUES (?, ?) "
            "ON CONFLICT(mac) DO NOTHING",
            (mac.lower(), time.time()))
        await self.conn.commit()

    async def is_mac_suppressed(self, mac: str) -> bool:
        row = await self._fetch_one(
            "SELECT 1 FROM suppressed_macs WHERE mac=?", (mac.lower(),))
        return row is not None

    async def clear_suppressed_macs_not_in(self, keep: set[str]) -> int:
        """Drop suppression rows for MACs no longer on the network.

        ``keep`` is the set of currently-leased MACs (dnsmasq lease file). A
        MAC that left the network loses its suppression, so a future return
        registers fresh. Returns how many rows were cleared."""
        keep_l = {m.lower() for m in keep}
        rows = await self.conn.execute_fetchall("SELECT mac FROM suppressed_macs")
        gone = [r["mac"] for r in rows if r["mac"] not in keep_l]
        if gone:
            ph = ",".join("?" for _ in gone)
            await self.conn.execute(
                f"DELETE FROM suppressed_macs WHERE mac IN ({ph})", gone)
            await self.conn.commit()
        return len(gone)

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

    async def has_bundle(self) -> bool:
        """True if a ``bundle_config`` row exists (seeded/edited before)."""
        row = await self._fetch_one("SELECT 1 FROM bundle_config WHERE id=1")
        return row is not None

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
            allowances=allowances,
            period_start=row["period_start"] or "",
            period_end=row["period_end"] or "",
        )

    async def set_bundle(self, bundle: Bundle) -> None:
        await self.conn.execute(
            """INSERT INTO bundle_config (id, total_gb, reset_day, allowances, period_start, period_end)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET total_gb=excluded.total_gb,
                 reset_day=excluded.reset_day, allowances=excluded.allowances,
                 period_start=excluded.period_start, period_end=excluded.period_end""",
            (bundle.total_gb, bundle.reset_day,
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

    async def get_usage_series(self, device_id: int | None,
                               since_date: str) -> list[dict[str, Any]]:
        """Daily series for the UI chart."""
        if device_id is None:
            rows = await self.conn.execute_fetchall(
                "SELECT date, SUM(up_bytes) up, SUM(down_bytes) down FROM usage_daily "
                "WHERE date>=? GROUP BY date ORDER BY date", (since_date,))
        else:
            rows = await self.conn.execute_fetchall(
                "SELECT date, up_bytes up, down_bytes down FROM usage_daily "
                "WHERE device_id=? AND date>=? ORDER BY date",
                (device_id, since_date))
        return [{"date": r["date"], "up": r["up"], "down": r["down"]} for r in rows]

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
        limit_down_mbps=float(row["limit_down_mbps"] or 0.0),
        limit_up_mbps=float(row["limit_up_mbps"] or 0.0),
        notified_50=bool(row["notified_50"]),
        notified_75=bool(row["notified_75"]),
        notified_100=bool(row["notified_100"]),
        history_days=row["history_days"],
    )
