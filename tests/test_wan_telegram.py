"""Unit tests for Telegram WAN IP Trigger."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from quota.wan_telegram import (
    mask_bot_token,
    format_wan_ip_message,
    send_telegram_message,
    get_current_public_ip,
)


def test_mask_bot_token():
    assert mask_bot_token("") == ""
    assert mask_bot_token("123") == "********"
    # standard bot token format
    token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
    masked = mask_bot_token(token)
    assert masked.startswith("123456789:ABC")
    assert masked.endswith("wxyZ")
    assert "..." in masked


def test_format_wan_ip_message():
    # Exposed WAN
    msg = format_wan_ip_message(
        ip="197.35.10.20",
        web_port=8080,
        web_proto="http",
        wan_exposed=True,
        interface="ppp0",
        is_test=False,
    )
    assert "197.35.10.20" in msg
    assert "http://197.35.10.20:8080" in msg
    assert "OPEN" in msg

    # Blocked WAN
    msg_blocked = format_wan_ip_message(
        ip="197.35.10.20",
        web_port=8080,
        web_proto="http",
        wan_exposed=False,
        interface="ppp0",
        is_test=False,
    )
    assert "BLOCKED" in msg_blocked

    # Test message
    msg_test = format_wan_ip_message(
        ip="197.35.10.20",
        is_test=True,
    )
    assert "[TEST]" in msg_test


@pytest.mark.anyio
async def test_send_telegram_message_validation():
    ok, err = await send_telegram_message("", "", "test text")
    assert not ok
    assert "required" in err.lower()


@pytest.mark.anyio
async def test_send_telegram_message_success():
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_resp

        ok, msg = await send_telegram_message("test_token", "123456", "Hello")
        assert ok
        assert "success" in msg.lower()


@pytest.mark.anyio
async def test_get_current_public_ip_ppp():
    with patch("quota.topology.detect_ppp", return_value={"state": "up", "local": "156.200.1.2"}):
        ip = await get_current_public_ip("wan")
        assert ip == "156.200.1.2"


def test_wan_telegram_api(tmp_path):
    import asyncio
    from api.app import create_app
    from quota import db as _db
    from quota.engine import SnapshotHolder
    from quota.service import QuotaService

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = _db.Database(tmp_path / "test.db")
    loop.run_until_complete(db.connect())
    service = QuotaService(db, timezone="Africa/Cairo")
    holder = SnapshotHolder()

    app = create_app(db, service, holder)
    with TestClient(app) as c:
        # login
        c.post("/api/login", json={"password": "admin"})

        # GET initial
        r = c.get("/api/wan/telegram")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is False
        assert data["has_token"] is False

        # POST save config
        r_save = c.post(
            "/api/wan/telegram",
            json={
                "enabled": True,
                "bot_token": "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ",
                "chat_id": "987654321",
            },
        )
        assert r_save.status_code == 200
        saved = r_save.json()
        assert saved["enabled"] is True
        assert saved["has_token"] is True
        assert saved["chat_id"] == "987654321"
        assert "..." in saved["bot_token"]  # masked!

        # POST test message (mocked)
        with patch("quota.wan_telegram.send_telegram_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = (True, "Message sent successfully")
            r_test = c.post("/api/wan/telegram/test", json={})
            assert r_test.status_code == 200
            assert "sent" in r_test.json()["message"].lower()

    loop.run_until_complete(db.close())

