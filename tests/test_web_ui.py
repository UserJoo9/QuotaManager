"""Web UI smoke tests: FastAPI must serve the glassmorphism dashboard."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.service import QuotaService


@pytest.fixture
def client(tmp_path):
    database = _db.Database(tmp_path / "ui.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    asyncio.get_event_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        yield c
    asyncio.get_event_loop().run_until_complete(database.close())


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    for needle in ("Quota Manager", "assets/styles.css",
                   "assets/app.js", "assets/chart.umd.js"):
        assert needle in r.text


def test_recharge_ui_elements_present(client):
    """The dashboard exposes 'Bundle recharged' and a 0-friendly reset day."""
    r = client.get("/")
    assert "Bundle recharged" in r.text
    assert "set-recharge" in r.text
    assert 'id="recharge-btn"' in r.text
    # reset-day input now accepts 0 (manual)
    assert 'id="set-reset-day" min="0" max="28"' in r.text

    r = client.get("/assets/app.js")
    assert "submitRecharge" in r.text
    assert "add_gb" in r.text
    assert "→ manual" in r.text          # reset_day=0 period rendering
    assert "days_left < 0" in r.text


def test_assets_served(client):
    r = client.get("/assets/styles.css")
    assert r.status_code == 200
    assert "backdrop-filter" in r.text

    r = client.get("/assets/app.js")
    assert r.status_code == 200
    assert "WebSocket" in r.text

    r = client.get("/assets/chart.umd.js")
    assert r.status_code == 200
    assert "Chart.js" in r.text
