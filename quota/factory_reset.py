#!/usr/bin/env python3
"""Factory reset of the gateway database (System > Power > Factory reset).

Runs OUTSIDE the app, started by ``systemd-run`` while ``quota-gateway`` is
stopped, so nothing else has the database open. Standard library only: it is
executed as a plain script (``python3 factory_reset.py --db PATH``), not
imported from the package.

Steps: stop quota-gateway -> back the database up next to it
(``backups/quota-<time>.db``) -> wipe -> start quota-gateway. The app
re-creates every default on start (admin password ``admin``, bundle, ...).

What survives:
- always: the network mode (LAN/WAN), the PPPoE login and the WAN card, so a
  reset never cuts the home off the internet;
- with ``--keep-users``: people, devices, fixed IPs, the bundle and this
  period's usage, plus the dashboard login (password, 2FA, passkeys, HTTPS).

Everything else returns to how a fresh install starts: firewall, DNS rules and
presets, VPN nodes, guest settings, speed limits, shared disks, update and
display preferences, the event log and DNS history.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sqlite3
import subprocess
import sys

#: settings kept by every reset — losing them would cut the home off.
NETWORK_SETTINGS = ("topology", "topology_source", "pppoe_user", "pppoe_password", "wan_if")

#: tables kept with --keep-users.
KEEP_USERS_TABLES = frozenset({
    "users", "devices", "static_leases", "bundle_config", "bundle_recharges",
    "usage_daily", "lan_usage_daily", "free_usage", "daily_caps",
    # owner of each deleted device: without it, keeping usage_daily would give
    # a deleted device's usage back to the bundle (quota/db.py removed_devices)
    "removed_devices",
})

#: settings kept with --keep-users (exact keys, or prefixes ending in "_").
KEEP_USERS_SETTINGS = (
    "admin_password", "admin_password_default", "totp_", "session_token",
    "passkeys", "tls_", "bundle_source",
)


def _keep_setting(key: str, keep_users: bool) -> bool:
    if key in NETWORK_SETTINGS:
        return True
    if not keep_users:
        return False
    return any(key.startswith(p) if p.endswith("_") else key == p
               for p in KEEP_USERS_SETTINGS)


def backup_database(db_path: str, now: _dt.datetime | None = None) -> str:
    """Copy the database (consistent snapshot) to ``<dir>/backups/``."""
    now = now or _dt.datetime.now()
    folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"quota-{now:%Y%m%d-%H%M%S}.db")
    src = sqlite3.connect(db_path)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def reset_database(db_path: str, keep_users: bool = False) -> dict[str, list[str]]:
    """Wipe the database in place; returns which tables were cleared/kept."""
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        cleared, kept = [], []
        for table in tables:
            if table == "settings":
                continue
            if keep_users and table in KEEP_USERS_TABLES:
                kept.append(table)
                continue
            con.execute(f'DELETE FROM "{table}"')
            cleared.append(table)
        if "settings" in tables:
            keys = [r[0] for r in con.execute("SELECT key FROM settings")]
            for key in keys:
                if not _keep_setting(key, keep_users):
                    con.execute("DELETE FROM settings WHERE key = ?", (key,))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return {"cleared": cleared, "kept": kept}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--keep-users", action="store_true")
    ap.add_argument("--cred-dir", default="/etc/quota-gateway/smb-credentials",
                    help="network-folder logins, removed with the shares")
    args = ap.parse_args(argv)
    subprocess.run(["systemctl", "stop", "quota-gateway"], check=False)
    try:
        backup = backup_database(args.db)
        print(f"[factory-reset] backup: {backup}")
        result = reset_database(args.db, keep_users=args.keep_users)
        # Shared disks/folders are settings too: the share list is gone from
        # the DB, so drop the saved logins; the app unmounts and clears the
        # Samba config itself when it starts with an empty list.
        shutil.rmtree(args.cred_dir, ignore_errors=True)
        print(f"[factory-reset] cleared {', '.join(result['cleared']) or '-'}; "
              f"kept {', '.join(result['kept']) or 'network settings only'}")
    finally:
        subprocess.run(["systemctl", "start", "quota-gateway"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
