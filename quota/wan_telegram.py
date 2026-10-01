"""Telegram notification trigger for WAN public IP changes.

Enables remote management from outside home by notifying the gateway admin
on Telegram whenever the WAN public IP changes (e.g. daily ISP lease reset
or PPPoE dial restart).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import urllib.request

log = logging.getLogger("quota.wan_telegram")


def mask_bot_token(token: str) -> str:
    """Mask a Telegram bot token for safe display in the UI (e.g. 123456789:ABC...wxyz)."""
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) <= 12:
        return "********"
    colon_idx = token.find(":")
    if colon_idx != -1 and colon_idx + 4 < len(token):
        prefix = token[:colon_idx + 4]
        suffix = token[-4:]
        return f"{prefix}...{suffix}"
    return f"{token[:4]}...{token[-4:]}"


def format_wan_ip_message(
    ip: str,
    web_port: int = 8080,
    web_proto: str = "http",
    wan_exposed: bool = True,
    interface: str = "ppp0",
    is_test: bool = False,
) -> str:
    """Format an informative, clickable HTML message for Telegram."""
    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "🧪 <b>[TEST] QuotaManager WAN Notification</b>" if is_test else "🌐 <b>QuotaManager — WAN IP Changed!</b>"

    if wan_exposed:
        fw_status = (
            f"✅ <b>Remote Web Access:</b> OPEN on port {web_port}\n"
            f"🔗 <b>Dashboard URL:</b>\n{web_proto}://{ip}:{web_port}"
        )
    else:
        fw_status = (
            "⚠️ <b>Remote Web Access:</b> BLOCKED in Firewall\n"
            "<i>(Enable 'WAN Web Access' in Firewall tab to access remotely)</i>"
        )

    msg = (
        f"{title}\n\n"
        f"📡 <b>Public IP:</b> <code>{ip}</code>\n"
        f"🕒 <b>Timestamp:</b> {now_str}\n"
        f"📊 <b>Interface:</b> {interface}\n\n"
        f"{fw_status}\n\n"
        f"<i>QuotaManager Gateway Protection</i>"
    )
    return msg


async def send_telegram_message(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Send an HTML message via Telegram Bot API."""
    token = (bot_token or "").strip()
    chat = (chat_id or "").strip()
    if not token or not chat:
        return False, "Bot token and Chat ID are required"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        import httpx  # type: ignore[import-untyped]
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                return True, "Message sent successfully"
            err_desc = data.get("description", f"HTTP {resp.status_code}")
            return False, f"Telegram API error: {err_desc}"
    except ImportError:
        # Fallback to urllib in thread
        def _urllib_send() -> tuple[bool, str]:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "QuotaManager/1.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("ok"):
                        return True, "Message sent successfully"
                    return False, f"Telegram API error: {data.get('description', '')}"
            except Exception as e:
                return False, f"Network error: {e}"

        return await asyncio.to_thread(_urllib_send)
    except Exception as e:
        return False, f"Network error: {e}"


async def get_current_public_ip(effective_topology: str = "wan") -> str:
    """Detect current public IP.
    In WAN mode with ppp0 up, reads ppp0 local address.
    Falls back to external IP check if ppp0 is absent or empty."""
    if effective_topology == "wan":
        try:
            from quota.topology import detect_ppp
            link = await asyncio.to_thread(detect_ppp, "ppp0")
            if link.get("state") == "up" and link.get("local"):
                return str(link["local"]).strip()
        except Exception as e:
            log.debug("detect_ppp check error: %s", e)

    # External probe fallback
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            if resp.status_code == 200:
                data = resp.json()
                if "ip" in data:
                    return str(data["ip"]).strip()
    except Exception:
        pass

    try:
        def _fetch_icanhazip() -> str:
            req = urllib.request.Request("https://icanhazip.com", headers={"User-Agent": "QuotaManager"})
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                return resp.read().decode("utf-8").strip()

        text = await asyncio.to_thread(_fetch_icanhazip)
        if text:
            return text
    except Exception:
        pass

    return ""
