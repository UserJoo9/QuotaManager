"""FastAPI application: REST API + WebSocket push + static UI.

Built by :func:`create_app`, which takes the dependencies (database, quota
service, engine snapshot holder) so it can be tested without real hardware.
Authentication is a single admin password (PBKDF2-hashed in ``settings``);
the UI gets a signed session cookie. WebSocket clients receive a full snapshot
on connect, then the app pushes refreshed snapshots on a timer.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from api.schemas import (BundleUpdate, DeviceCreate, DeviceUpdate, LoginRequest,
                         PasswordUpdate, TopUpRequest)
from core import timeutil
from quota import db as _db
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.service import GB, QuotaService

log = logging.getLogger("quota.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE_NAME = "qmsession"
SESSION_TTL_SEC = 60 * 60 * 24 * 7  # 7 days


# ---------------------------------------------------------------------------
# Auth helpers (PBKDF2 via stdlib)
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


async def _ensure_admin_password(db: _db.Database) -> None:
    stored = await db.get_setting("admin_password")
    if not stored:
        default = os.environ.get("QUOTA_ADMIN_PASSWORD", "admin")
        await db.set_setting("admin_password", _hash_password(default))
        log.warning("admin password created with default — change it in Settings")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    database: _db.Database,
    service: QuotaService,
    holder: SnapshotHolder,
    now_provider: Optional[Callable[[], _dt.datetime]] = None,
) -> FastAPI:
    app = FastAPI(title="Quota Manager", version="1.0.0",
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    def _now() -> _dt.datetime:
        return now_provider() if now_provider else _dt.datetime.now().astimezone()

    async def _require_auth(request: Request) -> None:
        """FastAPI dependency: every admin route (and the WS handshake) needs a
        valid session cookie. Without it a quota-blocked device could POST
        /api/devices/{id}/topup and unblock itself — the product's whole point.
        """
        token = request.cookies.get(COOKIE_NAME, "")
        stored = await database.get_setting("session_token", "")
        if not token or not stored or not hmac.compare_digest(token, stored):
            raise HTTPException(401, "not logged in")

    # -- serialization helper ------------------------------------------------

    def _device_view(dev: _db.Device, usage: dict[int, dict[str, int]],
                     leases: dict[str, str], allowances: dict[str, float],
                     live: EngineSnapshot) -> dict[str, Any]:
        used = usage.get(dev.id, {"up": 0, "down": 0})
        used_gb = (used["up"] + used["down"]) / GB
        live_c = live.counters_for(dev.mac)
        allowance = allowances.get(dev.mac, 0.0)
        return {
            "id": dev.id,
            "mac": dev.mac,
            "name": dev.name,
            "ip": leases.get(dev.mac, ""),
            "quota_mode": dev.quota_mode,
            "allowance_gb": allowance,
            "used_gb": round(used_gb, 3),
            "live_up": live_c.up,
            "live_down": live_c.down,
            "block_state": dev.block_state,
            "blocked": dev.block_state != _db.BLOCK_OK,
            "percent": round(used_gb / allowance * 100, 1) if allowance > 0 else 0.0,
        }

    # -- dashboard --------------------------------------------------------------

    async def _dashboard_payload() -> dict[str, Any]:
        bundle = await database.get_bundle()
        devices = await database.list_devices()
        usage = await database.get_period_usage()
        leases = {l.mac: l.ip for l in await database.list_leases()}
        allowances = bundle.allowances
        live = holder.get()

        total_used = sum((u["up"] + u["down"]) / GB for u in usage.values())
        days_left = timeutil.days_remaining(_now(), bundle.reset_day)
        return {
            "bundle_source": await database.get_setting("bundle_source", "config"),
            "bundle": {
                "total_gb": bundle.total_gb,
                "used_gb": round(total_used, 3),
                "remaining_gb": round(max(0.0, bundle.total_gb - total_used), 3),
                "reset_day": bundle.reset_day,
                "period_start": bundle.period_start,
                "period_end": bundle.period_end,
                "days_left": days_left,
            },
            "devices": [_device_view(d, usage, leases, allowances, live)
                        for d in devices],
            "total_devices": len(devices),
            "blocked_count": sum(1 for d in devices
                                 if d.block_state != _db.BLOCK_OK),
            "ts": _now().isoformat(),
        }

    # -- REST routes ----------------------------------------------------------

    @app.get("/api/dashboard", dependencies=[Depends(_require_auth)])
    async def dashboard() -> dict[str, Any]:
        return await _dashboard_payload()

    @app.get("/api/devices", dependencies=[Depends(_require_auth)])
    async def list_devices() -> list[dict[str, Any]]:
        bundle = await database.get_bundle()
        devices = await database.list_devices()
        usage = await database.get_period_usage()
        leases = {l.mac: l.ip for l in await database.list_leases()}
        live = holder.get()
        return [_device_view(d, usage, leases, bundle.allowances, live)
                for d in devices]

    @app.post("/api/devices", status_code=201, dependencies=[Depends(_require_auth)])
    async def create_device(body: DeviceCreate) -> dict[str, Any]:
        mac = body.mac.strip().lower()
        if not mac:
            raise HTTPException(400, "mac is required")
        dev = await database.upsert_device(mac, body.name, body.quota_mode,
                                           body.fixed_gb)
        await service.recompute_allowances()
        await database.add_event(f"Device added: {body.name or mac}", "info", dev.id)
        return {"id": dev.id, "mac": dev.mac}

    @app.patch("/api/devices/{device_id}", dependencies=[Depends(_require_auth)])
    async def update_device(device_id: int, body: DeviceUpdate) -> dict[str, Any]:
        dev = await database.get_device(device_id)
        if dev is None:
            raise HTTPException(404, "device not found")
        fields: dict[str, Any] = {}
        if body.name is not None:
            fields["name"] = body.name
        if body.quota_mode is not None:
            fields["quota_mode"] = body.quota_mode
        if body.fixed_gb is not None:
            fields["fixed_gb"] = body.fixed_gb
        if fields:
            await database.update_device(device_id, **fields)
        if body.block is not None:
            await service.set_admin_block(device_id, body.block)
        await service.recompute_allowances()
        return {"id": device_id, "updated": True}

    @app.delete("/api/devices/{device_id}", dependencies=[Depends(_require_auth)])
    async def delete_device(device_id: int) -> dict[str, Any]:
        dev = await database.get_device(device_id)
        await database.delete_device(device_id)
        await database.add_event(
            f"Device removed: {dev.name if dev else device_id}", "warn")
        return {"id": device_id, "deleted": True}

    @app.post("/api/devices/{device_id}/topup", dependencies=[Depends(_require_auth)])
    async def topup(device_id: int, body: TopUpRequest) -> dict[str, Any]:
        result = await service.top_up(device_id, body.extra_gb)
        if result is None:
            raise HTTPException(404, "device not found")
        return result

    @app.get("/api/usage/{device_id}", dependencies=[Depends(_require_auth)])
    async def usage_series(device_id: int, since: str = "") -> list[dict[str, Any]]:
        if not since:
            since = (await database.get_bundle()).period_start or ""
        return await database.get_usage_series(device_id, since)

    @app.get("/api/usage", dependencies=[Depends(_require_auth)])
    async def usage_all(since: str = "") -> list[dict[str, Any]]:
        if not since:
            since = (await database.get_bundle()).period_start or ""
        return await database.get_usage_series(None, since)

    @app.get("/api/events", dependencies=[Depends(_require_auth)])
    async def events(limit: int = 30) -> list[dict[str, Any]]:
        return await database.list_events(limit)

    @app.get("/api/bundle", dependencies=[Depends(_require_auth)])
    async def get_bundle() -> dict[str, Any]:
        b = await database.get_bundle()
        return {"total_gb": b.total_gb, "reset_day": b.reset_day,
                "period_start": b.period_start, "period_end": b.period_end,
                "allowances": b.allowances}

    @app.post("/api/bundle", dependencies=[Depends(_require_auth)])
    async def set_bundle(body: BundleUpdate) -> dict[str, Any]:
        # Escape hatch: explicitly return bundle ownership to config.yaml so it
        # is re-applied on the next restart (see Gateway._seed_bundle_from_cfg).
        if body.bundle_source == "config":
            await database.delete_setting("bundle_source")
            await database.add_event(
                "Bundle ownership returned to config.yaml (applies on next "
                "restart)", "warn")
            return {"bundle_source": "config", "note": "re-applies on restart"}
        # A dashboard edit/recharge makes the dashboard the bundle owner, so
        # config.yaml stops overriding these values on restart (see
        # Gateway._seed_bundle_from_cfg).
        await database.set_setting("bundle_source", "dashboard")
        b = await database.get_bundle()
        if body.add_gb is not None:
            # ISP re-charge: add to the current bundle, never roll the period.
            result = await service.recharge(body.add_gb)
            b = await database.get_bundle()
            return {"total_gb": b.total_gb, "reset_day": b.reset_day,
                    "added_gb": result["added_gb"]}
        if body.total_gb is not None:
            b.total_gb = body.total_gb
        if body.reset_day is not None:
            b.reset_day = body.reset_day
        await database.set_bundle(b)
        await service.recompute_allowances()
        await database.add_event(
            f"Bundle updated: {b.total_gb:g} GB, reset day {b.reset_day}", "warn")
        return {"total_gb": b.total_gb, "reset_day": b.reset_day}

    @app.post("/api/reset-month", dependencies=[Depends(_require_auth)])
    async def reset_month() -> dict[str, Any]:
        await service.reset_month()
        return {"ok": True}

    # -- auth -----------------------------------------------------------------

    @app.post("/api/login")
    async def login(body: LoginRequest, response: Response) -> dict[str, Any]:
        await _ensure_admin_password(database)
        stored = await database.get_setting("admin_password")
        if not _verify_password(body.password, stored):
            raise HTTPException(401, "invalid password")
        token = secrets.token_hex(24)
        await database.set_setting("session_token", token)
        response.set_cookie(COOKIE_NAME, token, httponly=True,
                            samesite="lax", max_age=SESSION_TTL_SEC)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME, "")
        stored = await database.get_setting("session_token", "")
        if not token or not stored or not hmac.compare_digest(token, stored):
            return {"authenticated": False}
        return {"authenticated": True}

    @app.post("/api/password")
    async def change_password(request: Request, body: PasswordUpdate) -> dict[str, Any]:
        # Must be a valid logged-in session — a wrong password is a 400, not a
        # 401: the client maps 401 to "session expired" and logs the user out.
        token = request.cookies.get(COOKIE_NAME, "")
        stored_token = await database.get_setting("session_token", "")
        if not token or not stored_token or not hmac.compare_digest(token, stored_token):
            raise HTTPException(401, "not logged in")
        stored = await database.get_setting("admin_password")
        if not _verify_password(body.current, stored):
            raise HTTPException(400, "current password incorrect")
        await database.set_setting("admin_password", _hash_password(body.new))
        await database.add_event("Admin password changed", "warn")
        return {"ok": True}

    # -- websocket ------------------------------------------------------------

    active_ws: set[WebSocket] = set()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # Authenticate the handshake like the REST routes: without a valid
        # session cookie the socket is closed before a single snapshot leaks.
        token = ws.cookies.get(COOKIE_NAME, "")
        stored = await database.get_setting("session_token", "")
        if not token or not stored or not hmac.compare_digest(token, stored):
            await ws.close(code=4401)
            return
        await ws.accept()
        active_ws.add(ws)
        try:
            await ws.send_json({"type": "snapshot", "data": await _dashboard_payload()})
            while True:
                # server-push every 5s; client sends keepalives too
                import asyncio
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                    if msg == "ping":
                        await ws.send_json({"type": "pong"})
                except asyncio.TimeoutError:
                    pass
                await ws.send_json({"type": "snapshot",
                                    "data": await _dashboard_payload()})
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            pass
        finally:
            active_ws.discard(ws)

    async def _push_loop() -> None:
        import asyncio
        while True:
            await asyncio.sleep(5)
            if not active_ws:
                continue
            payload = await _dashboard_payload()
            for ws in list(active_ws):
                try:
                    await ws.send_json({"type": "snapshot", "data": payload})
                except Exception:  # noqa: BLE001
                    active_ws.discard(ws)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # startup
        await _ensure_admin_password(database)
        await service.ensure_period()
        import asyncio
        push_task = asyncio.get_running_loop().create_task(_push_loop())
        try:
            yield
        finally:
            push_task.cancel()

    app.router.lifespan_context = _lifespan

    # static UI
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")

    return app
