"""System > Power > Factory reset: what is wiped and what always survives."""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota import factory_reset as fr
from quota import power
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.service import QuotaService


def _seed(path):
    database = _db.Database(path)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(database.connect())
    con = database  # schema created by connect()
    loop.run_until_complete(database.close())
    del con
    c = sqlite3.connect(path)
    c.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", [
        ("topology", "wan"), ("topology_source", "dashboard"), ("pppoe_user", "u@isp"),
        ("admin_password", "hash"), ("totp_secret", "s"), ("passkeys", "[]"),
        ("firewall_config", "{}"), ("vpn_auto_connect", "1"), ("disk_shares", "[]")])
    c.execute("INSERT INTO events(ts, message, level) VALUES (1, 'x', 'info')")
    c.commit()
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    return tables


def _settings(path):
    c = sqlite3.connect(path)
    try:
        return dict(c.execute("SELECT key, value FROM settings"))
    finally:
        c.close()


def test_full_reset_keeps_only_the_network_mode(tmp_path):
    db = tmp_path / "quota.db"
    _seed(db)
    out = fr.reset_database(str(db), keep_users=False)
    assert "users" in out["cleared"] and "events" in out["cleared"] and out["kept"] == []
    assert _settings(db) == {"topology": "wan", "topology_source": "dashboard",
                             "pppoe_user": "u@isp"}


def test_keep_users_keeps_people_usage_and_login(tmp_path):
    db = tmp_path / "quota.db"
    _seed(db)
    out = fr.reset_database(str(db), keep_users=True)
    assert {"users", "devices", "usage_daily", "bundle_config", "removed_devices"} <= set(out["kept"])
    assert "events" in out["cleared"] and "domain_rules" in out["cleared"]
    kept = _settings(db)
    assert {"topology", "pppoe_user", "admin_password", "totp_secret", "passkeys"} <= set(kept)
    assert "firewall_config" not in kept and "vpn_auto_connect" not in kept
    assert "disk_shares" not in kept


def test_backup_is_a_full_copy(tmp_path):
    db = tmp_path / "quota.db"
    _seed(db)
    dest = fr.backup_database(str(db), now=dt.datetime(2026, 11, 21, 8, 30, 0))
    assert dest.endswith("backups/quota-20261121-083000.db")
    assert _settings(dest)["admin_password"] == "hash"


def test_main_stops_backs_up_wipes_and_starts(tmp_path, monkeypatch):
    db = tmp_path / "quota.db"
    _seed(db)
    calls = []
    monkeypatch.setattr(fr.subprocess, "run", lambda argv, check=False: calls.append(argv))
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "PC").write_text("username=a\npassword=b\n")
    assert fr.main(["--db", str(db), "--keep-users", "--cred-dir", str(creds)]) == 0
    assert not creds.exists()                       # shared-folder logins go too
    assert calls == [["systemctl", "stop", "quota-gateway"],
                     ["systemctl", "start", "quota-gateway"]]
    assert list((tmp_path / "backups").iterdir())
    assert "admin_password" in _settings(db)


def test_schedule_factory_reset_and_restart(tmp_path):
    db = tmp_path / "quota.db"
    db.write_text("")
    calls = []
    run = lambda a: calls.append(a) or (0, "")
    power.schedule_factory_reset(str(db), keep_users=True, run_command=run)
    assert calls[-1][:2] == ["systemd-run", "--unit=quota-factory-reset"]
    assert calls[-1][-3:] == ["--db", str(db.resolve()), "--keep-users"]
    power.schedule_restart(False, run_command=run)
    assert calls[-1][-3:] == ["systemctl", "restart", "quota-gateway"]
    power.schedule_restart(True, run_command=run)
    assert calls[-1][-2:] == ["systemctl", "reboot"]
    with pytest.raises(RuntimeError, match="database not found"):
        power.schedule_factory_reset(str(tmp_path / "nope.db"), run_command=run)


def test_api_factory_reset_needs_the_confirm_word(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(power, "schedule_factory_reset",
                        lambda path, keep: called.append((path, keep)) or
                        {"scheduled": True, "keep_users": keep, "delay_s": 3})
    database = _db.Database(tmp_path / "api.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot())
    asyncio.new_event_loop().run_until_complete(database.connect())
    with TestClient(create_app(database, service, holder)) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        assert c.post("/api/system/factory-reset", json={"keep_users": True}).status_code == 422
        assert c.post("/api/system/factory-reset",
                      json={"keep_users": True, "confirm": "reset"}).status_code == 422
        r = c.post("/api/system/factory-reset", json={"keep_users": True, "confirm": "RESET"})
        assert r.status_code == 200 and r.json()["keep_users"] is True
    assert called == [(str(tmp_path / "api.db"), True)]
