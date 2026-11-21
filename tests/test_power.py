"""System > Power: turning the whole gateway off from the dashboard."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota import power
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.service import QuotaService


def test_schedule_runs_the_script_detached_after_a_delay(tmp_path):
    script = tmp_path / "gateway_off.sh"
    script.write_text("#!/bin/sh\n")
    calls = []
    out = power.schedule_gateway_off(False, script=script,
                                     run_command=lambda a: calls.append(a) or (0, ""))
    assert out == {"scheduled": True, "poweroff": False, "delay_s": power.DELAY_S}
    argv = calls[0]
    assert argv[0] == "systemd-run" and "--collect" in argv
    assert f"--on-active={power.DELAY_S}" in argv
    assert argv[-2:] == ["/bin/bash", str(script)]


def test_schedule_passes_poweroff(tmp_path):
    script = tmp_path / "gateway_off.sh"
    script.write_text("#!/bin/sh\n")
    calls = []
    power.schedule_gateway_off(True, script=script,
                               run_command=lambda a: calls.append(a) or (0, ""))
    assert calls[0][-1] == "poweroff"


def test_schedule_failure_raises(tmp_path):
    script = tmp_path / "gateway_off.sh"
    script.write_text("#!/bin/sh\n")
    with pytest.raises(RuntimeError, match="no systemd"):
        power.schedule_gateway_off(script=script, run_command=lambda a: (1, "no systemd"))
    with pytest.raises(RuntimeError, match="not found"):
        power.schedule_gateway_off(script=tmp_path / "missing.sh",
                                   run_command=lambda a: (0, ""))


def test_shipped_script_exists_and_stops_everything():
    text = power.SCRIPT.read_text()
    for unit in ("quota-wan-ppp", "dnsmasq", "quota-gateway"):
        assert unit in text
    assert "disable --now" in text
    assert "nft delete table inet" in text


def _app(tmp_path):
    database = _db.Database(tmp_path / "power.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot())
    asyncio.new_event_loop().run_until_complete(database.connect())
    return create_app(database, service, holder), database


def test_api_requires_login(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(power, "schedule_gateway_off", lambda *a, **k: called.append(a))
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/system/off", json={}).status_code == 401
    assert called == []


def test_api_schedules_turn_off(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(power, "schedule_gateway_off",
                        lambda poweroff=False: called.append(poweroff) or
                        {"scheduled": True, "poweroff": poweroff, "delay_s": 3})
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        r = c.post("/api/system/off", json={"poweroff": True})
        assert r.status_code == 200, r.text
        assert r.json()["poweroff"] is True
    assert called == [True]


def test_api_reports_failure(tmp_path, monkeypatch):
    def boom(poweroff=False):
        raise RuntimeError("systemd-run: command not found")
    monkeypatch.setattr(power, "schedule_gateway_off", boom)
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        r = c.post("/api/system/off", json={})
        assert r.status_code == 500
        assert "command not found" in r.json()["detail"]
