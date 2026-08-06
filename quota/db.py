"""SQLite persistence layer (single file, async via aiosqlite).

Schema overview
---------------
users          -- every person: quota mode, allowance, enforcement state.
devices        -- every known MAC: name, owning user, per-device override.
leases         -- current/known DHCP leases (mac <-> ip).
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
    #: Per-user aggregate internet speed cap in Mbps (0 = unlimited): all of a
    #: user's devices share this ceiling. Enforced by the Linux tc shaper.
    limit_down_mbps: float = 0.0
    limit_up_mbps: float = 0.0

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
    limit_down_mbps  REAL NOT NULL DEFAULT 0,
    limit_up_mbps    REAL NOT NULL DEFAULT 0
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
        await self._backfill_users()
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

    async def delete_device(self, device_id: int) -> None:
        await self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
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
                          limit_down_mbps: float = 0.0,
                          limit_up_mbps: float = 0.0) -> User:
        """Insert a user (no devices). Used by the API, by new-device
        auto-registration, and by the v2 migration backfill."""
        cur = await self.conn.execute(
            "INSERT INTO users (name, quota_mode, fixed_gb, block_state, "
            "topup_gb, created_at, guest, limit_down_mbps, limit_up_mbps) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (name, quota_mode, fixed_gb, block_state, time.time(),
             int(guest), limit_down_mbps, limit_up_mbps))
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
                   "limit_down_mbps", "limit_up_mbps"}
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

    async def delete_user(self, user_id: int, cascade: bool = True) -> int:
        """Delete a user (cascade removes their devices + usage rows).
        Returns how many devices were removed with them."""
        rows = await self.conn.execute_fetchall(
            "SELECT id FROM devices WHERE user_id=?", (user_id,))
        if rows and not cascade:
            raise ValueError(
                "user still has devices; reassign or delete them first")
        for r in rows:
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
        limit_down_mbps=float(row["limit_down_mbps"] or 0.0),
        limit_up_mbps=float(row["limit_up_mbps"] or 0.0),
    )
