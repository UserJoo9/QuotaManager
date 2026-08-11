"""FastAPI application: REST API + WebSocket push + static UI.

Built by :func:`create_app`, which takes the dependencies (database, quota
service, engine snapshot holder) so it can be tested without real hardware.
Authentication is a single admin password (PBKDF2-hashed in ``settings``);
the UI gets a signed session cookie. WebSocket clients receive a full snapshot
on connect, then the app pushes refreshed snapshots on a timer.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from api.schemas import (BundleUpdate, DeviceCreate, DeviceUpdate, GuestUpdate,
                         LoginRequest, MilestoneNotify, NetworkUpdate,
                         PasswordUpdate, SetupComplete, TopUpRequest,
                         UserCreate, UserUpdate, WanTest, WanUpdate)
from core import timeutil
from quota import db as _db
from quota.engine import GATEWAY_MAC, EngineSnapshot, SnapshotHolder
from quota.service import GB, QuotaService
from quota.vendor import vendor_for
from quota.version import __version__

log = logging.getLogger("quota.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE_NAME = "qmsession"
SESSION_TTL_SEC = 60 * 60 * 24 * 7  # 7 days


def _read_log_tail(path: str | Path | None, limit: int = 300) -> dict[str, Any]:
    """Tail of the gateway's rotating log file, newest lines first.

    The frontend "System logs" tab is fed from here. Missing/unreadable file
    (e.g. before the gateway has written anything) degrades to an empty tail —
    never an error page. ``limit`` is clamped to 2000.
    """
    limit = max(1, min(int(limit), 2000))
    lines: list[str] = []
    if path:
        try:
            lines = Path(path).read_text(encoding="utf-8",
                                         errors="replace").splitlines()
        except OSError:
            lines = []
    return {"lines": lines[-limit:][::-1],
            "path": str(path) if path else "",
            "total": len(lines),
            "truncated": len(lines) > limit}


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
    log_path: str | Path | None = None,
    topology_manager: object | None = None,
    shaping_sync: Optional[Callable[[], object]] = None,
    report_config: object | None = None,
) -> FastAPI:
    app = FastAPI(title="Quota Manager", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    def _now() -> _dt.datetime:
        return now_provider() if now_provider else _dt.datetime.now().astimezone()

    def _schedule_shaping_sync() -> None:
        """Apply a speed-limit edit in the kernel right away (no 15 s tick
        wait). Fire-and-forget: the HTTP response returns first, the shaper
        reconciles in the background. ``shaping_sync`` is run.py's callback;
        without one (tests/degraded boot) this is a no-op."""
        if shaping_sync is None:
            return
        try:
            asyncio.create_task(shaping_sync())
        except RuntimeError:  # no running event loop (should not happen in a route)
            pass

    async def _require_auth(request: Request) -> None:
        """FastAPI dependency: every admin route (and the WS handshake) needs a
        valid session cookie. Without it a quota-blocked device could POST
        /api/devices/{id}/topup and unblock itself — the product's whole point.
        """
        token = request.cookies.get(COOKIE_NAME, "")
        stored = await database.get_setting("session_token", "")
        if not token or not stored or not hmac.compare_digest(token, stored):
            raise HTTPException(401, "not logged in")

    # -- report IP gate (source-IP whitelist for /report) ---------------------

    def _ip_in_network(ip: str, cidr: str) -> bool:
        """Is ``ip`` inside a CIDR (or equal to a bare IP)? Malformed entries
        never match — a bad config value must deny, not allow."""
        try:
            return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return False

    async def _require_report_ip(request: Request) -> None:
        """FastAPI dependency: only admit requesters whose source IP is on the
        report whitelist (managed client subnet and/or the explicit
        ``report.allowed_ips`` list). Everything else -> 403.

        Deliberately no session cookie required — this is the on-demand internal
        view for the household's own devices. ``report.enabled: false`` (or no
        report_config wired) denies every source.
        """
        if report_config is None or not getattr(report_config, "enabled", False):
            raise HTTPException(403, "report access denied")
        host = request.client.host if request.client else ""
        allowed = [
            entry for entry in getattr(report_config, "allowed_ips", []) or []
        ]
        client_subnet = (getattr(report_config, "client_subnet", "") or "").strip()
        if getattr(report_config, "allow_client_subnet", True) and client_subnet:
            allowed.append(client_subnet)
        if not allowed or not any(_ip_in_network(host, e) for e in allowed):
            raise HTTPException(403, "report access denied")

    # -- serialization helper ------------------------------------------------

    def _device_view(dev: _db.Device, user: _db.User | None,
                     uv: dict[str, Any], leases: dict[str, str],
                     live: EngineSnapshot, state: str,
                     dusage: dict[str, int] | None = None) -> dict[str, Any]:
        """Device card. allowance/used/percent are the USER's aggregates (all of
        a user's devices report the same), ``state`` is the resolved block state
        (service.resolve_device_state) so a user cut reaches every device.
        ``dusage`` is THIS device's own period usage (``get_period_usage``),
        surfaced as ``device_used_gb``/``device_up_gb``/``device_down_gb`` so the
        UI can show each device's consumption within its user."""
        used_gb = uv["used_gb"] if uv else 0.0
        allowance = uv["allowance_gb"] if uv else 0.0
        live_c = live.counters_for(dev.mac)
        dusage = dusage or {"up": 0, "down": 0}
        dused_gb = (dusage.get("up", 0) + dusage.get("down", 0)) / GB
        return {
            "id": dev.id,
            "mac": dev.mac,
            "name": dev.name,
            # The gateway sentinel MAC would otherwise resolve to a real OUI
            # ("XEROX CORPORATION"); the box has no vendor.
            "vendor": "" if dev.mac == GATEWAY_MAC else vendor_for(dev.mac),
            # The box's own device (protected "Gateway" user) — the UI shows a
            # badge and hides its block/delete controls (controlled via its user).
            "gateway": dev.mac == GATEWAY_MAC,
            "user_id": dev.user_id,
            "user_name": user.name if user else "",
            "ip": leases.get(dev.mac, ""),
            # currently has a live DHCP lease? (dnsmasq prunes leases on
            # disconnect)
            "connected": dev.mac in leases,
            # owning user is a guest account (guest-mode auto-registration)
            "guest": bool(user.guest) if user else False,
            "quota_mode": uv["quota_mode"] if uv else dev.quota_mode,
            "fixed_gb": uv["fixed_gb"] if uv else dev.fixed_gb,
            "bypass": dev.bypass,
            # per-device internet speed caps (Mbps, 0 = unlimited)
            "limit_down_mbps": float(dev.limit_down_mbps or 0.0),
            "limit_up_mbps": float(dev.limit_up_mbps or 0.0),
            "allowance_gb": allowance,
            "used_gb": used_gb,
            "live_up": live_c.up,
            "live_down": live_c.down,
            "block_state": state,
            "blocked": state != _db.BLOCK_OK,
            "percent": round(used_gb / allowance * 100, 1) if allowance > 0 else 0.0,
            # this device's OWN consumption this period (not the user aggregate)
            "device_used_gb": round(dused_gb, 3),
            "device_percent": round(dused_gb / allowance * 100, 1) if allowance > 0 else 0.0,
            "device_up_gb": round(dusage.get("up", 0) / GB, 3),
            "device_down_gb": round(dusage.get("down", 0) / GB, 3),
        }

    # -- dashboard --------------------------------------------------------------

    async def _dashboard_payload() -> dict[str, Any]:
        bundle = await database.get_bundle()
        users = await database.list_users()
        devices = await database.list_devices()
        usage_by_user = await database.get_period_usage_by_user()
        usage_by_device = await database.get_period_usage()
        leases = {l.mac: l.ip for l in await database.list_leases()}
        allowances = bundle.allowances
        live = holder.get()

        # Per-user aggregate views (allowance + usage + resolved block state).
        user_views: dict[int, dict[str, Any]] = {}
        for u in users:
            usage = usage_by_user.get(u.id, {"up": 0, "down": 0})
            used_gb = (usage["up"] + usage["down"]) / GB
            allowance = allowances.get(u.id, 0.0)
            # quota_blocked_for special-cases protected users: an allowance of
            # 0 cuts the box IMMEDIATELY (the engine's `allowance > 0` guard
            # would otherwise treat 0 as "unmetered").
            quota_blocked = service.quota_blocked_for(u, allowance, used_gb)
            admin_blocked = u.block_state == _db.BLOCK_ADMIN
            state = (_db.BLOCK_ADMIN if admin_blocked
                     else (_db.BLOCK_QUOTA if quota_blocked else _db.BLOCK_OK))
            user_views[u.id] = {
                "id": u.id, "name": u.name, "quota_mode": u.quota_mode,
                "fixed_gb": u.fixed_gb,
                "guest": bool(u.guest),
                # protected users (the Gateway user) are permanent: editable but
                # never deletable — the UI hides the delete control.
                "protected": bool(u.protected),
                # per-user aggregate speed caps (Mbps, 0 = unlimited)
                "limit_down_mbps": float(u.limit_down_mbps or 0.0),
                "limit_up_mbps": float(u.limit_up_mbps or 0.0),
                # DNS-history retention override (days); None = global default
                "history_days": u.history_days,
                "allowance_gb": round(allowance, 3),
                "used_gb": round(used_gb, 3),
                "percent": round(used_gb / allowance * 100, 1) if allowance > 0 else 0.0,
                "blocked": admin_blocked or quota_blocked,
                "block_state": state,
                "quota_blocked": quota_blocked,
                "devices": [],
            }

        devices_view: list[dict[str, Any]] = []
        for d in devices:
            user = next((x for x in users if x.id == d.user_id), None)
            uv = user_views.get(d.user_id)
            state = service.resolve_device_state(
                user, d, uv["quota_blocked"] if uv else False)
            dev_view = _device_view(
                d, user, uv, leases, live, state,
                usage_by_device.get(d.id, {"up": 0, "down": 0}))
            devices_view.append(dev_view)
            if uv is not None:
                uv["devices"].append(dev_view)

        # What the box's own block toggle resolves to (the protected Gateway
        # user's user-level view — the same resolve_device_state the tick uses).
        gw_view = next((v for v in user_views.values() if v.get("protected")),
                       None)
        gw_desired = bool(gw_view["blocked"]) if gw_view else False

        total_used = sum((u["up"] + u["down"]) / GB
                         for u in usage_by_user.values())
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
            "users": [user_views[u.id] for u in users],
            "devices": devices_view,
            "rogue": [
                {"ip": r.ip, "mac": r.mac, "vendor": r.vendor, "online": r.online}
                for r in live.rogue
            ],
            # The box's OWN enforcement: what the protected Gateway user's
            # resolved state WANTS (blocked_desired) vs what the engine last
            # pushed to the kernel (blocked_programmed — None = never
            # programmed / engine off). The Gateway card shows a warning when
            # they disagree, so "Blocked in the UI but the box still reaches the
            # internet" (a stale engine / failed set program) is visible instead
            # of silent.
            "gateway": {
                "blocked_desired": gw_desired,
                "blocked_programmed": getattr(live, "gateway_blocked", None),
                "engine_available": bool(
                    getattr(live, "engine_available", True)),
            },
            "wan": live.wan_status,
            # Top-level internet reachability (probed every 15 s tick) so the
            # top-bar indicator can read it without digging into wan_status;
            # None = not probed yet (pre-first-tick).
            "internet": (live.wan_status or {}).get("internet"),
            "total_devices": len(devices),
            "total_users": len(users),
            "blocked_count": sum(1 for dv in devices_view if dv["blocked"]),
            "version": __version__,
            "ts": _now().isoformat(),
        }

    # -- milestone page (public, on-demand) -----------------------------------

    async def _milestone_payload(request: Request) -> dict[str, Any]:
        """The requesting device's user's consumption + per-device breakdown.

        Public by design: the milestone page is how a household device learns
        its own progress toward the quota cap — no admin session. The device is
        resolved by its source IP (a current DHCP lease); without one it gets a
        friendly "unrecognized" payload instead of an error.
        """
        host = request.client.host if request.client else ""
        dev = await database.get_device_by_ip(host)
        if dev is None or dev.user_id is None:
            return {"recognized": False, "user": None, "devices": []}
        user = await database.get_user(dev.user_id)
        if user is None or user.protected:
            return {"recognized": False, "user": None, "devices": []}
        ms = (await service.milestone_state()).get(user.id)
        if ms is None:
            return {"recognized": False, "user": None, "devices": []}
        # per-device breakdown: exact bytes per device for THIS user
        usage_by_device = await database.get_period_usage()
        devices = await database.list_devices(user_id=user.id)
        allowance = ms["allowance_gb"]
        device_rows = []
        for d in devices:
            dusage = usage_by_device.get(d.id, {"up": 0, "down": 0})
            dused_gb = (dusage.get("up", 0) + dusage.get("down", 0)) / GB
            device_rows.append({
                "id": d.id,
                "name": d.name,
                "mac": d.mac,
                "device_used_gb": round(dused_gb, 3),
                "device_up_gb": round(dusage.get("up", 0) / GB, 3),
                "device_down_gb": round(dusage.get("down", 0) / GB, 3),
                "device_percent": round(
                    dused_gb / allowance * 100, 1) if allowance > 0 else 0.0,
            })
        return {
            "recognized": True,
            "user": {
                "id": user.id,
                "name": user.name,
                "allowance_gb": ms["allowance_gb"],
                "used_gb": ms["used_gb"],
                "percent": ms["percent"],
                "milestones": ms["milestones"],
            },
            "devices": device_rows,
        }

    @app.get("/api/milestone", response_model=None)
    async def milestone_api(request: Request) -> dict[str, Any]:
        return await _milestone_payload(request)

    @app.post("/api/milestone/notify", response_model=None)
    async def milestone_notify(body: MilestoneNotify) -> dict[str, Any]:
        """Mark a crossed milestone as notified (the page's acknowledge).

        No session required — a household device acknowledging its own usage
        notice. Unknown/duplicate milestones are harmless: the service validates
        the value and setting an already-notified flag is a no-op.
        """
        await service.mark_milestone_notified(body.user_id, body.milestone)
        return {"ok": True}

    # -- on-demand report (source-IP gated) -----------------------------------

    async def _report_payload() -> dict[str, Any]:
        """Read-only consumption report: exact bytes/quota per user and device,
        plus events + log tail. No session; gated by ``_require_report_ip``."""
        bundle = await database.get_bundle()
        users = await database.list_users()
        devices = await database.list_devices()
        usage_by_user = await database.get_period_usage_by_user()
        usage_by_device = await database.get_period_usage()
        allowances = bundle.allowances
        live = holder.get()

        total_used = sum((u["up"] + u["down"]) / GB
                         for u in usage_by_user.values())

        user_rows = []
        for u in users:
            usage = usage_by_user.get(u.id, {"up": 0, "down": 0})
            used_gb = (usage["up"] + usage["down"]) / GB
            allowance = allowances.get(u.id, 0.0)
            quota_blocked = service.quota_blocked_for(u, allowance, used_gb)
            admin_blocked = u.block_state == _db.BLOCK_ADMIN
            user_rows.append({
                "id": u.id,
                "name": u.name,
                "quota_mode": u.quota_mode,
                "protected": bool(u.protected),
                "guest": bool(u.guest),
                "allowance_gb": round(allowance, 3),
                "used_gb": round(used_gb, 3),
                "used_bytes": int(usage.get("up", 0) + usage.get("down", 0)),
                "percent": round(used_gb / allowance * 100, 1) if allowance > 0 else 0.0,
                "blocked": admin_blocked or quota_blocked,
                "block_state": (_db.BLOCK_ADMIN if admin_blocked
                                else (_db.BLOCK_QUOTA if quota_blocked
                                      else _db.BLOCK_OK)),
                "devices": [],
            })
        by_uid = {u["id"]: u for u in user_rows}

        for d in devices:
            urow = by_uid.get(d.user_id)
            dusage = usage_by_device.get(d.id, {"up": 0, "down": 0})
            dused_gb = (dusage.get("up", 0) + dusage.get("down", 0)) / GB
            allowance = urow["allowance_gb"] if urow else 0.0
            dev_row = {
                "id": d.id,
                "name": d.name,
                "mac": d.mac,
                "ip": next((l.ip for l in await database.list_leases()
                            if l.mac == d.mac), ""),
                "device_used_gb": round(dused_gb, 3),
                "device_up_gb": round(dusage.get("up", 0) / GB, 3),
                "device_down_gb": round(dusage.get("down", 0) / GB, 3),
                "device_percent": round(
                    dused_gb / allowance * 100, 1) if allowance > 0 else 0.0,
            }
            if urow is not None:
                urow["devices"].append(dev_row)

        return {
            "generated_at": _now().isoformat(),
            "bundle": {
                "total_gb": bundle.total_gb,
                "used_gb": round(total_used, 3),
                "used_bytes": int(sum(u["up"] + u["down"]
                                      for u in usage_by_user.values())),
                "remaining_gb": round(max(0.0, bundle.total_gb - total_used), 3),
                "reset_day": bundle.reset_day,
                "period_start": bundle.period_start,
                "period_end": bundle.period_end,
            },
            "users": user_rows,
            "events": await database.list_events(50),
            "logs": _read_log_tail(log_path, 200),
            "wan": live.wan_status or {},
            "version": __version__,
        }

    @app.get("/api/report", dependencies=[Depends(_require_report_ip)],
             response_model=None)
    async def report_api() -> dict[str, Any]:
        return await _report_payload()

    @app.get("/report", dependencies=[Depends(_require_report_ip)],
             response_class=FileResponse)
    async def report_page() -> FileResponse:
        """The reporting dashboard HTML. Gated by source IP (no admin session),
        so a whitelisted machine can open it on demand."""
        page = WEB_DIR / "report.html"
        return FileResponse(str(page))

    @app.get("/milestone", response_class=FileResponse)
    async def milestone_page() -> FileResponse:
        """The household milestone page — public, on-demand."""
        page = WEB_DIR / "milestone.html"
        return FileResponse(str(page))

    # -- REST routes ----------------------------------------------------------

    @app.get("/api/dashboard", dependencies=[Depends(_require_auth)])
    async def dashboard() -> dict[str, Any]:
        return await _dashboard_payload()

    @app.get("/api/rogue", dependencies=[Depends(_require_auth)])
    async def rogue() -> list[dict[str, Any]]:
        """Active LAN hosts that are NOT known DHCP devices (static-IP bypassers).

        Sourced from the same snapshot the dashboard payload uses, so the WS
        push and this endpoint never disagree.
        """
        return (await _dashboard_payload())["rogue"]

    @app.get("/api/history/{device_id}", dependencies=[Depends(_require_auth)])
    async def device_history(device_id: int | str, window: int = 24,
                             limit: int = 100) -> dict[str, Any]:
        """A device's DNS browsing history — top domains, activity, recent.

        ``device_id`` is a device id, or the sentinels ``"all"`` / ``0`` for a
        household-wide aggregate across every device (combined top domains,
        activity and total, with each recent row carrying its ``device_id``
        so the UI can badge it). ``window`` (hours, default 24, clamped 1-336)
        is the look-back; rows are per-minute buckets from the ``dns_history``
        table (fed from dnsmasq's query log). ``limit`` (default 100, clamped
        1-500) caps the top/recent lists. Bandwidth is NOT duplicated here —
        the History tab reads live/per-period bytes from the cached dashboard
        payload.
        """
        window = max(1, min(int(window), 336))
        limit = max(1, min(int(limit), 500))
        if device_id == "all" or device_id == "0" or device_id == 0:
            did = None
        else:
            try:
                did = int(device_id)
            except (TypeError, ValueError):
                raise HTTPException(404, "device not found")
            dev = await database.get_device(did)
            if dev is None:
                raise HTTPException(404, "device not found")
        since_minute = (_now() - _dt.timedelta(hours=window)
                        ).strftime("%Y-%m-%d %H:%M")
        hist = await database.get_dns_history(did, since_minute, limit)
        # "minute" -> "bucket_minute" on the wire so activity and recent use
        # the same key the JS renders with. The aggregate view also carries
        # each row's owning device_id for the [name] badges.
        recent = [{"bucket_minute": r["minute"], "domain": r["domain"],
                   "count": r["count"]} for r in hist["recent"]]
        if did is None:
            for item, r in zip(recent, hist["recent"]):
                item["device_id"] = r["device_id"]
        return {
            "device_id": "all" if did is None else did,
            "window_hours": window,
            "total_queries": hist["total"],
            "top_domains": hist["top_domains"],
            "activity": [{"bucket_minute": a["minute"], "count": a["hits"]}
                         for a in hist["activity"]],
            "recent": recent,
        }

    @app.get("/api/wan", dependencies=[Depends(_require_auth)])
    async def get_wan() -> dict[str, Any]:
        """Live WAN-mode status: effective topology, who owns it (config /
        dashboard), a pending dashboard toggle, the ppp0 link state, and the
        saved PPPoE credentials so the WAN tab can prefill them.

        Sourced from the same snapshot the dashboard payload uses, so the WS
        push and this endpoint never disagree (may be ``{}`` before the first
        maintenance tick, exactly like ``rogue``). The credentials are read from
        the DB settings here — they are deliberately NOT in the WS snapshot, so
        a password is only ever served to this explicit GET.
        """
        status = dict((await _dashboard_payload()).get("wan") or {})
        status["pppoe_user"] = await database.get_setting("pppoe_user", "")
        status["pppoe_password"] = await database.get_setting("pppoe_password", "")
        status["wan_if"] = await database.get_setting("wan_if", "")
        return status

    @app.post("/api/wan", dependencies=[Depends(_require_auth)])
    async def set_wan(body: WanUpdate) -> dict[str, Any]:
        """Apply WAN mode ("lan" | "wan") LIVE from the panel (v19).

        Rewrites config.yaml + the DB setting together (they can never
        disagree), runs the runtime applier (NIC + dnsmasq + PPPoE dial), and
        schedules a detached self-restart — no setup script, no terminal. The
        response carries the CURRENT live status (the in-memory topology is
        unchanged until the restart) plus ``restart_scheduled``.
        """
        if body.topology not in ("lan", "wan"):
            raise HTTPException(400, "topology must be 'lan' or 'wan'")
        st = dict(holder.get().wan_status or {})
        st.setdefault("topology", "lan")
        st.setdefault("ppp0", "n/a")
        st.setdefault("ppp_ip", "")
        st.setdefault("ppp_peer", "")
        if topology_manager is None:
            # No applier wired (tests / degraded boot): fall back to v18 — persist
            # the preference so it wins on the next restart. The credentials are
            # remembered too so the WAN tab keeps its prefilled values.
            await database.set_setting("topology_source", "dashboard")
            await database.set_setting("topology", body.topology)
            # Only non-empty creds are saved — a body with empty fields (a LAN
            # revert posts {topology: "lan"} only) must preserve the saved ones
            # for the prefill, not erase them.
            for key, value in (("pppoe_user", body.pppoe_user or ""),
                               ("pppoe_password", body.pppoe_password or ""),
                               ("wan_if", body.wan_if or "")):
                if value:
                    await database.set_setting(key, value)
            await database.add_event(
                f"WAN topology set to {body.topology} (applies on next restart)",
                "warn")
            st["source"] = "dashboard"
            st["configured"] = body.topology
            st["pending"] = body.topology
            st["applies_on_restart"] = True
            return st
        try:
            result = await topology_manager.apply(
                body.topology,
                pppoe_user=body.pppoe_user or "",
                pppoe_password=body.pppoe_password or "",
                wan_if=body.wan_if or "")
        except RuntimeError as exc:
            log.error("WAN apply failed: %s", exc)
            raise HTTPException(500, f"topology apply failed: {exc}")
        st["source"] = "dashboard"
        st["configured"] = body.topology
        st["pending"] = body.topology
        st["applies_on_restart"] = True
        st["restart_scheduled"] = bool(result.get("restart_scheduled"))
        st["script_output"] = result.get("script_output", "")
        return st

    @app.post("/api/wan/test", dependencies=[Depends(_require_auth)])
    async def test_wan(body: WanTest) -> dict[str, Any]:
        """Test the PPPoE line with the entered credentials WITHOUT applying
        anything (v19.1): a throwaway dial on ppp200 that reports whether the
        ISP accepts the user/password and whether an internet connection comes
        up. No config.yaml write, no DB write, no topology change — the running
        gateway is untouched. Returns the parsed test result or an HTTP 500
        with the script's output on failure (script missing, pppd absent).
        """
        if topology_manager is None:
            raise HTTPException(503, "no topology manager wired (degraded boot)")
        try:
            return await topology_manager.test_pppoe(
                pppoe_user=body.pppoe_user or "",
                pppoe_password=body.pppoe_password or "",
                wan_if=body.wan_if or "")
        except RuntimeError as exc:
            log.error("PPPoE test failed: %s", exc)
            raise HTTPException(500, f"PPPoE test failed: {exc}")

    @app.get("/api/devices", dependencies=[Depends(_require_auth)])
    async def list_devices() -> list[dict[str, Any]]:
        return (await _dashboard_payload())["devices"]

    @app.post("/api/devices", status_code=201, dependencies=[Depends(_require_auth)])
    async def create_device(body: DeviceCreate) -> dict[str, Any]:
        mac = body.mac.strip().lower()
        if not mac:
            raise HTTPException(400, "mac is required")
        if mac == GATEWAY_MAC:
            raise HTTPException(400, "the gateway box MAC is reserved — it "
                                "cannot be re-created")
        # user_id=None => upsert_device auto-creates a user carrying
        # body.user_name (or the device name) + quota.
        dev = await database.upsert_device(mac, body.name, body.quota_mode,
                                           body.fixed_gb, body.user_id,
                                           body.user_name,
                                           limit_down_mbps=body.limit_down_mbps or 0.0,
                                           limit_up_mbps=body.limit_up_mbps or 0.0)
        await service.recompute_allowances()
        await database.add_event(f"Device added: {body.name or mac}", "info", dev.id)
        _schedule_shaping_sync()  # the new device's caps land in tc immediately
        return {"id": dev.id, "mac": dev.mac, "user_id": dev.user_id}

    @app.patch("/api/devices/{device_id}", dependencies=[Depends(_require_auth)])
    async def update_device(device_id: int, body: DeviceUpdate) -> dict[str, Any]:
        dev = await database.get_device(device_id)
        if dev is None:
            raise HTTPException(404, "device not found")
        if dev.mac == GATEWAY_MAC and body.user_id is not None:
            raise HTTPException(400, "the gateway box device cannot be moved "
                                "to another user")
        fields: dict[str, Any] = {}
        if body.name is not None:
            fields["name"] = body.name
        if body.quota_mode is not None:
            fields["quota_mode"] = body.quota_mode
        if body.fixed_gb is not None:
            fields["fixed_gb"] = body.fixed_gb
        if body.user_id is not None:
            fields["user_id"] = body.user_id
        if body.bypass is not None:
            fields["bypass"] = body.bypass
        # Speed caps are per-device (NOT forwarded to the user, unlike quota):
        # a device with its own limit keeps it even when its user has none.
        if body.limit_down_mbps is not None:
            fields["limit_down_mbps"] = body.limit_down_mbps
        if body.limit_up_mbps is not None:
            fields["limit_up_mbps"] = body.limit_up_mbps
        if fields:
            await database.update_device(device_id, **fields)
        # Quota lives on the USER now: a quota edit through a device card is
        # forwarded to the owning user (the device row keeps an inert mirror).
        if dev.user_id is not None and (body.quota_mode is not None
                                        or body.fixed_gb is not None):
            ufields: dict[str, Any] = {}
            if body.quota_mode is not None:
                ufields["quota_mode"] = body.quota_mode
            if body.fixed_gb is not None:
                ufields["fixed_gb"] = body.fixed_gb
            await database.update_user(dev.user_id, **ufields)
        if body.block is not None:
            await service.set_admin_block(device_id, body.block)
        await service.recompute_allowances()
        if body.user_id is not None and body.user_id != dev.user_id:
            await database.add_event(
                f"Device '{dev.name or dev.mac}' moved to user #{body.user_id}",
                "info", device_id)
        # Cap edits must reach tc now, not on the next 15 s maintenance tick.
        _schedule_shaping_sync()
        return {"id": device_id, "updated": True}

    @app.delete("/api/devices/{device_id}", dependencies=[Depends(_require_auth)])
    async def delete_device(device_id: int) -> dict[str, Any]:
        dev = await database.get_device(device_id)
        if dev is None:
            raise HTTPException(404, "device not found")
        if dev.mac == GATEWAY_MAC:
            raise HTTPException(400, "the gateway box device cannot be deleted")
        # Deleting a guest's device records its MAC as suppressed so it does
        # not re-register while it is still connected (run.py _persist_lease
        # skips suppressed MACs). A normal device delete does not suppress.
        await database.delete_device(device_id, suppress_guest_mac=True)
        await database.add_event(
            f"Device removed: {dev.name or dev.mac}", "warn")
        return {"id": device_id, "deleted": True}

    @app.post("/api/devices/{device_id}/topup", dependencies=[Depends(_require_auth)])
    async def topup(device_id: int, body: TopUpRequest) -> dict[str, Any]:
        result = await service.top_up(device_id, body.extra_gb)
        if result is None:
            raise HTTPException(404, "device not found")
        return result

    # -- users (a person owns devices; the quota lives on the user) ----------

    @app.get("/api/users", dependencies=[Depends(_require_auth)])
    async def list_users() -> list[dict[str, Any]]:
        return (await _dashboard_payload())["users"]

    @app.post("/api/users", status_code=201, dependencies=[Depends(_require_auth)])
    async def create_user(body: UserCreate) -> dict[str, Any]:
        user = await database.create_user(
            body.name, body.quota_mode, body.fixed_gb,
            limit_down_mbps=body.limit_down_mbps or 0.0,
            limit_up_mbps=body.limit_up_mbps or 0.0)
        await service.recompute_allowances()
        await database.add_event(
            f"User added: {body.name or 'unnamed'}", "info", user_id=user.id)
        _schedule_shaping_sync()  # the user's aggregate cap lands in tc now
        return {"id": user.id, "name": user.name}

    @app.patch("/api/users/{user_id}", dependencies=[Depends(_require_auth)])
    async def update_user(user_id: int, body: UserUpdate) -> dict[str, Any]:
        user = await database.get_user(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        fields: dict[str, Any] = {}
        for key in ("name", "quota_mode", "fixed_gb",
                    "limit_down_mbps", "limit_up_mbps", "history_days"):
            value = getattr(body, key)
            if value is not None:
                fields[key] = value
        if fields:
            await database.update_user(user_id, **fields)
        if body.block is not None:
            await service.set_admin_block_user(user_id, body.block)
        await service.recompute_allowances()
        _schedule_shaping_sync()  # the user's aggregate cap lands in tc now
        return {"id": user_id, "updated": True}

    @app.delete("/api/users/{user_id}", dependencies=[Depends(_require_auth)])
    async def delete_user(user_id: int) -> dict[str, Any]:
        user = await database.get_user(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if getattr(user, "protected", False):
            raise HTTPException(400, "the protected Gateway user cannot be "
                                "deleted — edit it instead")
        # Deleting a guest user records its devices' MACs as suppressed so they
        # do not re-register while still connected (run.py _persist_lease skips
        # suppressed MACs). Month-reset cleanup never sets this flag.
        removed = await database.delete_user(user_id, cascade=True,
                                             suppress_guest_macs=True)
        await database.add_event(
            f"User removed: {user.name or user_id} ({removed} device(s))", "warn")
        await service.recompute_allowances()
        return {"id": user_id, "deleted": True, "devices_removed": removed}

    @app.post("/api/users/{user_id}/topup", dependencies=[Depends(_require_auth)])
    async def topup_user(user_id: int, body: TopUpRequest) -> dict[str, Any]:
        result = await service.top_up_user(user_id, body.extra_gb)
        if result is None:
            raise HTTPException(404, "user not found")
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

    @app.get("/api/logs", dependencies=[Depends(_require_auth)])
    async def logs(limit: int = 300) -> dict[str, Any]:
        """Tail of the gateway log file (newest first) for the System logs tab."""
        return _read_log_tail(log_path, limit)

    @app.get("/api/bundle", dependencies=[Depends(_require_auth)])
    async def get_bundle() -> dict[str, Any]:
        b = await database.get_bundle()
        return {"total_gb": b.total_gb, "reset_day": b.reset_day,
                "period_start": b.period_start, "period_end": b.period_end,
                "allowances": b.allowances}

    async def _apply_bundle_values(total_gb: float | None,
                                   reset_day: int | None) -> None:
        """Apply a bundle edit from the dashboard (both /api/bundle and the
        first-run welcome flow). Sets ``bundle_source=dashboard`` so config.yaml
        stops overriding these values on restart (see
        Gateway._seed_bundle_from_cfg). Only fields that are present are
        written — a password-only save never takes bundle ownership."""
        await database.set_setting("bundle_source", "dashboard")
        b = await database.get_bundle()
        if total_gb is not None:
            b.total_gb = total_gb
        if reset_day is not None:
            b.reset_day = reset_day
        await database.set_bundle(b)
        await service.recompute_allowances()
        await database.add_event(
            f"Bundle updated: {b.total_gb:g} GB, reset day {b.reset_day}", "warn")

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
        if body.add_gb is not None:
            # ISP re-charge: add to the current bundle, never roll the period.
            # (add_gb keeps the dashboard as bundle owner, like a plain edit.)
            await database.set_setting("bundle_source", "dashboard")
            result = await service.recharge(body.add_gb)
            b = await database.get_bundle()
            return {"total_gb": b.total_gb, "reset_day": b.reset_day,
                    "added_gb": result["added_gb"]}
        await _apply_bundle_values(body.total_gb, body.reset_day)
        b = await database.get_bundle()
        return {"total_gb": b.total_gb, "reset_day": b.reset_day}

    @app.post("/api/reset-month", dependencies=[Depends(_require_auth)])
    async def reset_month() -> dict[str, Any]:
        await service.reset_month()
        return {"ok": True}

    # -- first-run setup (welcome panel) ---------------------------------------

    @app.get("/api/setup", dependencies=[Depends(_require_auth)])
    async def get_setup() -> dict[str, Any]:
        """One-time welcome state: complete + the current bundle for prefill."""
        b = await database.get_bundle()
        return {"setup_complete": await service.is_setup_complete(),
                "total_gb": b.total_gb, "reset_day": b.reset_day}

    @app.post("/api/setup/complete", dependencies=[Depends(_require_auth)])
    async def complete_setup(body: SetupComplete) -> dict[str, Any]:
        # Auth'd like /api/password: a wrong current password is a 400, not a
        # 401 (the client maps 401 to "session expired" and logs out).
        if body.new_password is not None:
            stored = await database.get_setting("admin_password")
            if not _verify_password(body.current_password or "", stored):
                raise HTTPException(400, "current password incorrect")
            await database.set_setting("admin_password",
                                       _hash_password(body.new_password))
            await database.add_event("Admin password changed", "warn")
        # Only apply the bundle when a value was given — a password-only save
        # must not take bundle ownership from config.yaml (see _apply_bundle_values).
        if body.total_gb is not None or body.reset_day is not None:
            await _apply_bundle_values(body.total_gb, body.reset_day)
        await service.mark_setup_complete()
        await database.add_event("First-run setup completed", "info")
        return {"ok": True}

    # -- guest mode ------------------------------------------------------------

    @app.get("/api/guest", dependencies=[Depends(_require_auth)])
    async def get_guest() -> dict[str, Any]:
        return {"enabled": await service.is_guest_mode(),
                "quota_gb": await service.guest_quota_gb()}

    @app.post("/api/guest", dependencies=[Depends(_require_auth)])
    async def set_guest(body: GuestUpdate) -> dict[str, Any]:
        if body.enabled is not None:
            await service.set_guest_mode(body.enabled)
        if body.quota_gb is not None:
            await service.set_guest_quota(body.quota_gb)
        return {"enabled": await service.is_guest_mode(),
                "quota_gb": await service.guest_quota_gb()}

    # -- speed shaping (Network tab) ------------------------------------------

    @app.get("/api/network", dependencies=[Depends(_require_auth)])
    async def get_network() -> dict[str, Any]:
        return await service.get_shaping_config()

    @app.post("/api/network", dependencies=[Depends(_require_auth)])
    async def set_network(body: NetworkUpdate) -> dict[str, Any]:
        result = await service.set_shaping(
            enabled=body.enabled,
            total_down_mbps=body.total_down_mbps,
            total_up_mbps=body.total_up_mbps,
            aqm=body.aqm)
        # Apply to the kernel NOW — no 15 s wait for the maintenance tick, so a
        # saved Network-tab change is enforced immediately (no page refresh).
        _schedule_shaping_sync()
        return result

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
