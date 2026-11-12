"""Backup & restore system for Quota Manager.

Creates SQLite database snapshots as JSON files, with full CRUD over backup
records stored in the `backup_jobs` table. Backups are stored under
`~/.quota-manager/backups/` (or $QUOTA_BACKUP_DIR) and can be restored on
demand. All operations are audited via the existing policy_audit_log table.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from quota.db import Database, list_policies, get_policy, delete_policy, save_policy, audit_policy_log

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BACKUP_DIR = Path(os.environ.get("QUOTA_BACKUP_DIR", str(Path.home() / ".quota-manager" / "backups")))


def _ensure_backup_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

async def init_backup_tables(db: Database) -> None:
    """Create backup_jobs table if it doesn't exist."""
    await db.conn.execute("""
        CREATE TABLE IF NOT EXISTS backup_jobs (
            backup_id   TEXT PRIMARY KEY,
            backup_type TEXT NOT NULL DEFAULT 'full',
            status      TEXT NOT NULL DEFAULT 'pending',  -- pending, done, failed
            path        TEXT NOT NULL,
            size_bytes  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            created_by  TEXT NOT NULL DEFAULT 'admin',
            note        TEXT NOT NULL DEFAULT ''
        )
    """)
    await db.conn.commit()


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

async def create_backup(db: Database, backup_type: str = "full", created_by: str = "admin",
                        note: str = "") -> dict[str, Any]:
    """Create a full JSON backup of all policy-related tables.

    Returns a dict with backup_id, path, size_bytes, status.
    """
    await init_backup_tables(db)
    import uuid
    import datetime as _dt

    backup_id = f"bak_{uuid.uuid4().hex[:12]}"
    now = _dt.datetime.now().astimezone().isoformat()
    _ensure_backup_dir()
    backup_path = BACKUP_DIR / f"{backup_id}.json"

    try:
        # Dump all relevant tables
        tables = ["policies", "policy_audit_log", "admin_roles", "admin_user_roles",
                  "role_permissions", "device_groups", "device_group_members", "alerts"]

        data: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            try:
                cur = await db.conn.execute(f"SELECT * FROM {table}")
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]
                data[table] = [
                    {cols[i]: row[i] for i in range(len(cols))}
                    for row in rows
                ]
            except Exception:  # table may not exist yet
                data[table] = []

        payload = {
            "backup_id": backup_id,
            "backup_type": backup_type,
            "created_at": now,
            "created_by": created_by,
            "note": note,
            "tables": data,
        }

        backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        size_bytes = backup_path.stat().st_size

        # Record in backup_jobs
        await db.conn.execute(
            """INSERT INTO backup_jobs (backup_id, backup_type, status, path, size_bytes,
                                         created_at, created_by, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (backup_id, backup_type, "done", str(backup_path), size_bytes, now, created_by, note),
        )
        await db.conn.commit()

        await audit_policy_log(db, "backup_create", backup_id, "system",
                               actor=created_by,
                               details={"backup_type": backup_type, "size_bytes": size_bytes, "path": str(backup_path)})

        return {
            "backup_id": backup_id,
            "backup_type": backup_type,
            "status": "done",
            "path": str(backup_path),
            "size_bytes": size_bytes,
            "created_at": now,
            "created_by": created_by,
            "note": note,
        }
    except Exception as e:  # noqa: BLE001
        await db.conn.execute(
            """INSERT INTO backup_jobs (backup_id, backup_type, status, path, size_bytes,
                                         created_at, created_by, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (backup_id, backup_type, "failed", str(backup_path), 0, now, created_by,
             f"error: {e}"),
        )
        await db.conn.commit()
        raise


async def list_backups(db: Database) -> list[dict[str, Any]]:
    """List all backup jobs, newest first."""
    await init_backup_tables(db)
    cur = await db.conn.execute(
        "SELECT backup_id, backup_type, status, path, size_bytes, created_at, created_by, note "
        "FROM backup_jobs ORDER BY created_at DESC"
    )
    rows = await cur.fetchall()
    return [
        {
            "backup_id": r[0],
            "backup_type": r[1],
            "status": r[2],
            "path": r[3],
            "size_bytes": r[4],
            "created_at": r[5],
            "created_by": r[6],
            "note": r[7],
        }
        for r in rows
    ]


async def delete_backup(db: Database, backup_id: str, created_by: str = "admin") -> bool:
    """Delete a backup job and its file. Returns True if deleted."""
    await init_backup_tables(db)
    cur = await db.conn.execute("SELECT path FROM backup_jobs WHERE backup_id = ?", (backup_id,))
    row = await cur.fetchone()
    if not row:
        return False

    path = row[0]
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass

    cur = await db.conn.execute("DELETE FROM backup_jobs WHERE backup_id = ?", (backup_id,))
    await db.conn.commit()
    deleted = cur.rowcount > 0

    if deleted:
        await audit_policy_log(db, "backup_delete", backup_id, "system",
                               actor=created_by, details={"path": path})

    return deleted


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #

async def restore_backup(db: Database, backup_id: str, created_by: str = "admin") -> dict[str, Any]:
    """Restore all tables from a backup file.

    Replaces existing data in all backed-up tables with the backup contents.
    Returns a summary dict.
    """
    await init_backup_tables(db)
    import datetime as _dt

    cur = await db.conn.execute("SELECT path, status FROM backup_jobs WHERE backup_id = ?", (backup_id,))
    row = await cur.fetchone()
    if not row:
        raise ValueError(f"Backup not found: {backup_id}")

    path, status = row[0], row[1]
    if status != "done":
        raise ValueError(f"Backup {backup_id} is not in 'done' status (current: {status})")

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Backup file not found: {path}")

    payload = json.loads(p.read_text(encoding="utf-8"))
    tables = payload.get("tables", {})

    now = _dt.datetime.now().astimezone().isoformat()

    # Get list of existing tables
    cur = await db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {r[0] for r in await cur.fetchall()}

    # Clear existing data in all tables then re-insert (only if table exists)
    for table in ["alerts", "device_group_members", "device_groups", "role_permissions",
                  "admin_user_roles", "admin_roles", "policy_audit_log", "policies"]:
        if table in tables and table in existing_tables:
            await db.conn.execute(f"DELETE FROM {table}")

    restored_counts: dict[str, int] = {}
    for table, rows in tables.items():
        if not rows:
            restored_counts[table] = 0
            continue
        if table not in existing_tables:
            continue  # skip tables that don't exist yet

        # Build INSERT dynamically
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        for row in rows:
            values = [row[c] for c in cols]
            await db.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})",
                values,
            )
        restored_counts[table] = len(rows)

    await db.conn.commit()

    await audit_policy_log(db, "backup_restore", backup_id, "system",
                           actor=created_by,
                           details={"restored_tables": restored_counts, "path": path})

    return {
        "backup_id": backup_id,
        "status": "restored",
        "restored_at": now,
        "restored_by": created_by,
        "tables": restored_counts,
    }