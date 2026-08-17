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
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from api.schemas import (BundleUpdate, DeviceAccessUpdate, DeviceCreate,
                         DeviceUpdate, DnsImportRequest, DnsPresetEnable,
                         DnsQuickRule,
                         DnsServerUpdate, DomainRuleCreate, DomainRuleUpdate,
                         GuestUpdate, LoginRequest, MacListsUpdate,
                         MilestoneNotify,
                         NetworkUpdate, PasswordUpdate, SetupComplete,
                         TopUpRequest, UpdateSettings, UserCreate, UserUpdate,
                         WanRenewConfig, WanTest, WanUpdate)
from core import timeutil
from quota import db as _db
from quota import dns_rules as _dns_rules
from quota.engine import GATEWAY_MAC, EngineSnapshot, SnapshotHolder
from quota.service import GB, QuotaService
from quota.vendor import vendor_for
from quota.version import __version__

log = logging.getLogger("quota.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE_NAME = "qmsession"
SESSION_TTL_SEC = 60 * 60 * 24 * 7  # 7 days


def _normalize_domains(raw_domains: set[str]) -> tuple[list[str], int]:
    """Run quota.dns_rules.normalize_pattern over a whole domain set,
    dropping anything unenforceable rather than failing the batch.

    Pulled out to module level (not a route-local closure) so it can be
    handed to ``asyncio.to_thread`` — a large preset/import (the
    ads-tracking preset alone is 100k+ domains) is real CPU work worth
    moving off the event loop, not just the network fetch that precedes it.
    Returns (normalized_domains, skipped_count).
    """
    domains: list[str] = []
    skipped = 0
    for raw in raw_domains:
        try:
            domains.append(_dns_rules.normalize_pattern(raw))
        except ValueError:
            skipped += 1
    return domains, skipped


def _read_log_tail(path: str | Path | None, limit: int = 300) -> dict[str, Any]:
    """Tail of the gateway's rotating log file, newest lines first.

    The frontend "System Logs" console (on the Admin page) is fed from here. Missing/unreadable file
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

#: PBKDF2-HMAC-SHA256 work factor for NEW password hashes (OWASP 2023+
#: recommends >= 600k; 200k was the pre-v0.2.1 default). The stored hash
#: records its own iteration count, so old hashes keep verifying at their
#: original cost and are re-hashed on the next successful login.
PBKDF2_ITERATIONS = 600_000

#: login throttling: max failed attempts per source IP within the window,
#: then HTTP 429 for the rest of the window. In-memory (a LAN admin box);
#: the session cookie and PBKDF2 already gate the account itself.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SEC = 300.0


def _hash_password(password: str, salt: bytes | None = None,
                   iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{salt.hex()}${iterations}${dk.hex()}"


def _verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Verify ``password`` against a stored hash.

    Returns (valid, needs_rehash). Stored formats:
      ``salt$iterations$dk``  — current; the recorded iteration count is used
      ``salt$dk``             — legacy pre-v0.2.1 (200k); valid at 200k, but
                                the caller should re-hash at the new default
    """
    try:
        parts = stored.split("$")
        if len(parts) == 3:
            salt_hex, iters, dk_hex = parts
            iterations = int(iters)
        elif len(parts) == 2:
            salt_hex, dk_hex = parts
            iterations = 200_000
        else:
            return False, False
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        valid = hmac.compare_digest(dk.hex(), dk_hex)
        return valid, valid and iterations < PBKDF2_ITERATIONS
    except (ValueError, AttributeError):
        return False, False


class _LoginRateLimiter:
    """In-memory per-IP failed-login throttle.

    Tracks failed attempts + the window start; when the attempt budget is
    exhausted within the window every further attempt from that IP is denied
    (429) until the window rolls. Successes reset the budget (a wrong-password
    guesser hammering the LAN box is the threat; a legitimate user typing the
    right password is not).
    """

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._window_start: dict[str, float] = {}

    def check(self, ip: str) -> bool:
        """May this IP attempt a login now? (True = allowed.)"""
        if ip not in self._fails:
            return True
        if time.monotonic() - self._window_start.get(ip, 0.0) > LOGIN_WINDOW_SEC:
            del self._fails[ip]
            self._window_start.pop(ip, None)
            return True
        return self._fails[ip] < LOGIN_MAX_FAILURES

    def fail(self, ip: str) -> None:
        if ip not in self._window_start:
            self._window_start[ip] = time.monotonic()
        self._fails[ip] = self._fails.get(ip, 0) + 1

    def success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._window_start.pop(ip, None)


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
    dns_apply: Optional[Callable[[], object]] = None,
    vpn_apply: Optional[Callable[[], object]] = None,
    vpn_status_getter: Optional[Callable[[], dict]] = None,
    wan_renew: Optional[Callable[[], object]] = None,
    interface_tags: Optional[dict[str, str]] = None,
    shaping_state_getter: Optional[Callable[[], dict]] = None,
    wifi_probe_getter: Optional[Callable[[], dict]] = None,
    active_ips_getter: Optional[Callable[[], Optional[set[str]]]] = None,
    stop_new_sync: Optional[Callable[[], object]] = None,
    decline_random_sync: Optional[Callable[[], object]] = None,
    updater: object | None = None,
) -> FastAPI:
    app = FastAPI(title="Quota Manager", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    #: NIC name -> human label for the per-device WiFi/LAN tag (config.yaml
    #: network.interface_tags); an unknown NIC falls back to its raw name.
    _interface_tags: dict[str, str] = interface_tags or {}

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

    def _schedule_dns_apply() -> None:
        """Apply a domain-rule / DNS-server edit to dnsmasq right away (no
        15 s tick wait). Fire-and-forget, same pattern as
        ``_schedule_shaping_sync``; ``dns_apply`` is run.py's callback and is
        a no-op when unset (tests / degraded boot)."""
        if dns_apply is None:
            return
        try:
            asyncio.create_task(dns_apply())
        except RuntimeError:  # no running event loop (should not happen in a route)
            pass

    def _schedule_vpn_apply() -> None:
        """Apply a VPN-share toggle in the kernel right away (no 15 s tick
        wait): policy routing into/out of the tunnel + the gateway-meter
        suspension. Fire-and-forget, same pattern as
        ``_schedule_shaping_sync``; ``vpn_apply`` is run.py's callback and is
        a no-op when unset (tests / degraded boot)."""
        if vpn_apply is None:
            return
        try:
            asyncio.create_task(vpn_apply())
        except RuntimeError:  # no running event loop (should not happen in a route)
            pass

    def _schedule_stop_new_sync() -> None:
        """Apply a STOP-NEW-CONNECTIONS toggle to dnsmasq right away (no 15 s
        tick wait): write/clear the DHCP-refusal fragment + restart dnsmasq.
        Fire-and-forget, same pattern as ``_schedule_dns_apply``;
        ``stop_new_sync`` is run.py's callback and is a no-op when unset
        (tests / degraded boot)."""
        if stop_new_sync is None:
            return
        try:
            asyncio.create_task(stop_new_sync())
        except RuntimeError:  # no running event loop (should not happen in a route)
            pass

    def _schedule_decline_random_sync() -> None:
        """Apply a Decline-random-MACs toggle to dnsmasq right away (no 15 s
        tick wait): write/clear the DHCP-refusal fragment + restart dnsmasq.
        Fire-and-forget, same pattern as ``_schedule_stop_new_sync``;
        ``decline_random_sync`` is run.py's callback and is a no-op when
        unset (tests / degraded boot)."""
        if decline_random_sync is None:
            return
        try:
            asyncio.create_task(decline_random_sync())
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
                     dusage: dict[str, int] | None = None,
                     active_ips: Optional[set[str]] = None) -> dict[str, Any]:
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
            # connected NOW? The DHCP lease alone lags reality (dnsmasq keeps
            # leases for the whole LEASE_HOURS after a disconnect), so when the
            # gateway's ARP probe is running the device must ALSO have answered
            # its latest sweep — otherwise the LED goes grey despite the lease.
            "connected": (
                dev.mac in leases
                and (active_ips is None
                     or (leases.get(dev.mac) or "") in active_ips)),
            # owning user is a guest account (guest-mode auto-registration)
            "guest": bool(user.guest) if user else False,
            "quota_mode": uv["quota_mode"] if uv else dev.quota_mode,
            "fixed_gb": uv["fixed_gb"] if uv else dev.fixed_gb,
            "bypass": dev.bypass,
            # per-device internet speed caps (Mbps, 0 = unlimited)
            "limit_down_mbps": float(dev.limit_down_mbps or 0.0),
            "limit_up_mbps": float(dev.limit_up_mbps or 0.0),
            # per-device upstream DNS-server override (empty = inherit)
            "dns_server": dev.dns_server or "",
            # which NIC the device was last seen on (ip neigh) + its display
            # label — the WiFi/LAN chip on the device card. Empty interface =
            # unknown/offline (no tag rendered). The box-side label comes
            # ONLY from an explicit network.interface_tags mapping — never a
            # guess: every client arrives on the same wired NIC (eth0), so an
            # unmapped name says nothing about WiFi vs LAN (that verdict
            # belongs to the router-side access_interface probe).
            "source_interface": dev.source_interface or "",
            "interface_label": _interface_tags.get(dev.source_interface) or "",
            # ROUTER-side access label: "WiFi · <SSID>" / "LAN" learned from
            # the passive radio probe (quota.wifi_probe.py), or the admin's
            # manual pin. The manual override always wins; ``access_interface``
            # below is the DISPLAY label (override || auto), while the raw
            # auto value stays in dev.access_interface for the UI.
            "access_override": dev.access_override or "",
            "access_interface": (
                dev.access_override or dev.access_interface or ""),
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

    async def _vpn_share_payload() -> dict[str, Any]:
        """Switch + live kernel state for the WS snapshot (mirrors /api/network)."""
        cfg = await service.get_vpn_config()
        if vpn_status_getter is not None:
            try:
                cfg["status"] = vpn_status_getter()
            except Exception:  # noqa: BLE001 — a status probe must never 500 the panel
                cfg["status"] = {"state": "error", "message": "status probe failed"}
        return cfg

    async def _dashboard_payload() -> dict[str, Any]:
        bundle = await database.get_bundle()
        users = await database.list_users()
        devices = await database.list_devices()
        usage_by_user = await database.get_period_usage_by_user()
        usage_by_device = await database.get_period_usage()
        leases = {l.mac: l.ip for l in await database.list_leases()}
        active_ips = active_ips_getter() if active_ips_getter else None
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
            quota_blocked = service.user_quota_blocked(u, allowance, used_gb)
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
                # exempt users are never quota-blocked (admin cuts still apply)
                "exempt_quota": bool(u.exempt_quota),
                # per-user aggregate speed caps (Mbps, 0 = unlimited)
                "limit_down_mbps": float(u.limit_down_mbps or 0.0),
                "limit_up_mbps": float(u.limit_up_mbps or 0.0),
                # DNS-history retention override (days); None = global default
                "history_days": u.history_days,
                # per-user upstream DNS-server override (empty = inherit)
                "dns_server": u.dns_server or "",
                "allowance_gb": round(allowance, 3),
                "used_gb": round(used_gb, 3),
                "percent": round(used_gb / allowance * 100, 1) if allowance > 0 else 0.0,
                "blocked": admin_blocked or quota_blocked,
                "block_state": state,
                "quota_blocked": quota_blocked,
                "devices": [],
            }

        devices_view: list[dict[str, Any]] = []
        allow_set = set(await database.get_mac_list("allow"))
        deny_set = set(await database.get_mac_list("deny"))
        for d in devices:
            # Blacklisted MACs are hidden from Management: they live ONLY in
            # the Network-tab blacklist (a delete wrote them there, or the
            # admin typed them in). Un-blacklisting restores the card.
            if d.mac in deny_set:
                continue
            user = next((x for x in users if x.id == d.user_id), None)
            uv = user_views.get(d.user_id)
            state = service.resolve_device_state(
                user, d, uv["quota_blocked"] if uv else False,
                allow_listed=d.mac in allow_set,
                deny_listed=d.mac in deny_set)
            dev_view = _device_view(
                d, user, uv, leases, live, state,
                usage_by_device.get(d.id, {"up": 0, "down": 0}),
                active_ips)
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
        days_left = timeutil.days_remaining(_now(), bundle.effective_reset_day)
        return {
            "bundle_source": await database.get_setting("bundle_source", "config"),
            "bundle": {
                "total_gb": bundle.total_gb,
                "used_gb": round(total_used, 3),
                "remaining_gb": round(max(0.0, bundle.total_gb - total_used), 3),
                "reset_day": bundle.reset_day,
                "period_type": bundle.period_type,
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
            # The VPN-share switch + live kernel state ride the WS snapshot so
            # the Network-tab status stays current (the toggle auto-saves, but
            # the "Waiting for the gateway to apply…" text must advance to the
            # applied state on its own — it can't wait for a manual refresh).
            # status is None in tests / degraded boot, matching /api/network.
            "vpn_share": await _vpn_share_payload(),
            # Live shaping engine state for the Network preview: whether tc is
            # available and whether the kernel tree matches the last save
            # ("applying…" while a rebuild is queued). None in tests / when
            # the app was built without the run.py callback.
            "shaping": (shaping_state_getter() if shaping_state_getter is not None
                        else None),
            # Top-level internet reachability (probed every 15 s tick) so the
            # top-bar indicator can read it without digging into wan_status;
            # None = not probed yet (pre-first-tick).
            "internet": (live.wan_status or {}).get("internet"),
            "total_devices": len(devices),
            "total_users": len(users),
            "blocked_count": sum(1 for dv in devices_view if dv["blocked"]),
            "version": __version__,
            # GitHub self-update state for the Admin-tab card + the
            # "Update available" notification (None = not wired — tests /
            # degraded boot; the JS treats it as "no updater").
            "update": (await updater.state()) if updater is not None else None,
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
        deny_set = set(await database.get_mac_list("deny"))
        allowance = ms["allowance_gb"]
        device_rows = []
        for d in devices:
            # a blacklisted device of this user is not a household device
            # anymore — the Network-tab blacklist is the only place it shows
            if d.mac in deny_set:
                continue
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
    async def milestone_notify(body: MilestoneNotify,
                               request: Request) -> dict[str, Any]:
        """Mark a crossed milestone as notified (the page's acknowledge).

        No session required — a household device acknowledging its own usage
        notice. The requester is resolved by source IP and must OWN the user
        row it is acknowledging (a sibling device cannot clear another user's
        milestone pills); unrecognized sources are denied. Unknown/duplicate
        milestones are harmless: the service validates the value and setting
        an already-notified flag is a no-op.
        """
        host = request.client.host if request.client else ""
        dev = await database.get_device_by_ip(host)
        if dev is None or dev.user_id is None:
            raise HTTPException(403, "unknown requester")
        if dev.user_id != body.user_id:
            raise HTTPException(403, "not your user")
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
            quota_blocked = service.user_quota_blocked(u, allowance, used_gb)
            admin_blocked = u.block_state == _db.BLOCK_ADMIN
            user_rows.append({
                "id": u.id,
                "name": u.name,
                "quota_mode": u.quota_mode,
                "protected": bool(u.protected),
                "guest": bool(u.guest),
                "exempt_quota": bool(u.exempt_quota),
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

        deny_set = set(await database.get_mac_list("deny"))
        # One lease pass for the whole loop — querying the lease file per
        # device (N+1 subprocess reads) made a large household's report slow.
        leases = {l.mac: l.ip for l in await database.list_leases()}
        for d in devices:
            # Blacklisted MACs are hidden from the report too — the Network-tab
            # blacklist is the only place they appear.
            if d.mac in deny_set:
                continue
            urow = by_uid.get(d.user_id)
            dusage = usage_by_device.get(d.id, {"up": 0, "down": 0})
            dused_gb = (dusage.get("up", 0) + dusage.get("down", 0)) / GB
            allowance = urow["allowance_gb"] if urow else 0.0
            dev_row = {
                "id": d.id,
                "name": d.name,
                "mac": d.mac,
                "ip": leases.get(d.mac, ""),
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
                "period_type": bundle.period_type,
                "period_start": bundle.period_start,
                "period_end": bundle.period_end,
            },
            "users": user_rows,
            "events": await database.list_events(50),
            # whole-file read — off the event loop, see /api/logs
            "logs": await asyncio.to_thread(_read_log_tail, log_path, 200),
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
        # Filter-status annotation: for each domain actually seen, resolve
        # what the LIVE dnsmasq config would do with it right now (block/
        # allow/redirect/none) — see quota/dns_rules.resolve_domain_status,
        # which mirrors dnsmasq's own longest-domain-match + scope
        # precedence exactly, so this is never an approximation. A specific
        # device also gets its owning user's scope considered (a user-level
        # rule reaches it); the "all devices" aggregate only has global-scope
        # rules to go on, since no single device context applies to it.
        rules = await database.list_domain_rules(enabled_only=True)
        status_user_id = dev.user_id if did is not None else None
        top_domains = []
        for t in hist["top_domains"]:
            status, _rule = _dns_rules.resolve_domain_status(
                t["domain"], rules, did, status_user_id)
            top_domains.append({**t, "status": status})
        owner_cache: dict[int, int | None] = {}  # device_id -> user_id, memoized
        for item in recent:
            # An aggregate row may belong to a DIFFERENT device than `did`
            # (did is None here) — resolve against that row's own device/user.
            row_did = item.get("device_id", did)
            row_uid = status_user_id
            if did is None and row_did is not None:
                if row_did not in owner_cache:
                    owner = await database.get_device(row_did)
                    owner_cache[row_did] = owner.user_id if owner else None
                row_uid = owner_cache[row_did]
            status, _rule = _dns_rules.resolve_domain_status(
                item["domain"], rules, row_did, row_uid)
            item["status"] = status
        return {
            "device_id": "all" if did is None else did,
            "window_hours": window,
            "total_queries": hist["total"],
            "top_domains": top_domains,
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

    @app.post("/api/wan/renew", dependencies=[Depends(_require_auth)])
    async def renew_wan() -> dict[str, Any]:
        """Renew the WAN public IP NOW (the WAN-tab Restart button).

        Restarts the box's PPPoE dial (``quota-wan-ppp``), which tears the
        session down and re-dials — on a metered Egyptian line the ISP assigns
        a fresh public IP to the new session, the same effect as restarting
        the router. Internet drops for a few seconds while ppp0 comes back up,
        and the auto-renew schedule's countdown restarts from now.

        Rejects 409 while ppp0 is down (the internet isn't working — nothing
        to renew into) or when WAN mode isn't active (no PPPoE line), and 503
        when no renew callback is wired (degraded boot / tests without a
        gateway).
        """
        if wan_renew is None:
            raise HTTPException(503, "no WAN renew callback wired (degraded boot)")
        st = dict(holder.get().wan_status or {})
        if st.get("topology") != "wan":
            raise HTTPException(409, "WAN mode is not active — there is no "
                                     "PPPoE line to renew")
        if st.get("ppp0") != "up":
            raise HTTPException(409, "ppp0 is down — the internet isn't "
                                     "working, nothing to renew")
        try:
            result = await wan_renew()
        except Exception as exc:  # noqa: BLE001
            log.error("WAN renew failed: %s", exc)
            raise HTTPException(500, f"WAN renew failed: {exc}")
        return {"restarted": bool(result.get("restarted")),
                "state": result.get("state", "unknown"),
                "detail": result.get("detail", "")}

    @app.post("/api/wan/renew-config", dependencies=[Depends(_require_auth)])
    async def set_wan_renew(body: WanRenewConfig) -> dict[str, Any]:
        """Set the WAN auto-renew schedule: ``enabled`` arms the periodic
        PPPoE re-dial and ``minutes`` is its interval (clamped to a 5-minute
        floor — every renewal drops internet briefly). Returns the stored
        config (``{enabled, minutes, last}``)."""
        return await service.set_wan_renew_config(body.enabled, body.minutes)

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
        # Deleting a device blacklists its MAC (permanent deny list): it does
        # not re-register while still connected, the kernel keeps blocking it
        # even without a device row, and the Network-tab blacklist is the only
        # way back in (remove the MAC there to unblock + re-register).
        await database.delete_device(device_id, deny_list_mac=True)
        await database.add_event(
            f"Device removed: {dev.name or dev.mac} — MAC blacklisted "
            f"({dev.mac})", "warn")
        return {"id": device_id, "deleted": True}

    @app.post("/api/devices/{device_id}/topup", dependencies=[Depends(_require_auth)])
    async def topup(device_id: int, body: TopUpRequest) -> dict[str, Any]:
        result = await service.top_up(device_id, body.extra_gb)
        if result is None:
            raise HTTPException(404, "device not found")
        return result

    @app.post("/api/devices/{device_id}/access", dependencies=[Depends(_require_auth)])
    async def set_device_access(device_id: int,
                                body: DeviceAccessUpdate) -> dict[str, Any]:
        """Pin (or clear) the router-side access label shown on the device
        card: "WiFi · <SSID>", "LAN1", ... — whatever the admin types. The
        passive probe's auto label (``access_interface``) keeps updating in
        the background; the override just wins the display. Empty string
        clears the pin."""
        dev = await database.update_device(
            device_id, access_override=body.override.strip())
        if dev is None:
            raise HTTPException(404, "device not found")
        return {"id": device_id,
                "access_override": dev.access_override,
                "access_interface": dev.access_override or dev.access_interface}

    @app.get("/api/wifi/ssids", dependencies=[Depends(_require_auth)])
    async def get_wifi_ssids() -> dict[str, Any]:
        """SSIDs the passive probe currently hears — the device modal's
        access-label picker. Degrades to an empty list when the probe is off
        or the box has no monitor card."""
        if wifi_probe_getter is None:
            return {"available": False, "ssids": [], "error": "not configured",
                    "ssid_by_mac": {}, "wireless_macs": []}
        return wifi_probe_getter()

    # -- users (a person owns devices; the quota lives on the user) ----------

    @app.get("/api/users", dependencies=[Depends(_require_auth)])
    async def list_users() -> list[dict[str, Any]]:
        return (await _dashboard_payload())["users"]

    @app.post("/api/users", status_code=201, dependencies=[Depends(_require_auth)])
    async def create_user(body: UserCreate) -> dict[str, Any]:
        user = await database.create_user(
            body.name, body.quota_mode, body.fixed_gb,
            limit_down_mbps=body.limit_down_mbps or 0.0,
            limit_up_mbps=body.limit_up_mbps or 0.0,
            exempt_quota=bool(body.exempt_quota or False))
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
                    "limit_down_mbps", "limit_up_mbps", "history_days",
                    "exempt_quota"):
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
        # Deleting a user blacklists every device MAC it owned (permanent deny
        # list): none re-register while still connected, the kernel keeps
        # blocking them even without device rows, and the Network-tab
        # blacklist is the only way back in. Month-reset cleanup never sets
        # this flag.
        removed = await database.delete_user(user_id, cascade=True,
                                             deny_list_macs=True)
        await database.add_event(
            f"User removed: {user.name or user_id} ({removed} device(s) — "
            f"MACs blacklisted)", "warn")
        await service.recompute_allowances()
        return {"id": user_id, "deleted": True, "devices_removed": removed}

    @app.post("/api/users/{user_id}/topup", dependencies=[Depends(_require_auth)])
    async def topup_user(user_id: int, body: TopUpRequest) -> dict[str, Any]:
        result = await service.top_up_user(user_id, body.extra_gb)
        if result is None:
            raise HTTPException(404, "user not found")
        return result

    @app.patch("/api/users/{user_id}/dns", dependencies=[Depends(_require_auth)])
    async def set_user_dns(user_id: int, body: DnsServerUpdate) -> dict[str, Any]:
        user = await database.get_user(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        await database.update_user(user_id, dns_server=body.dns_server.strip())
        await database.add_event(
            f"DNS server for user '{user.name or user_id}' set to "
            f"{body.dns_server.strip() or '(inherit default)'}", "info",
            user_id=user_id)
        _schedule_dns_apply()
        return {"id": user_id, "dns_server": body.dns_server.strip()}

    @app.patch("/api/devices/{device_id}/dns", dependencies=[Depends(_require_auth)])
    async def set_device_dns(device_id: int, body: DnsServerUpdate) -> dict[str, Any]:
        dev = await database.get_device(device_id)
        if dev is None:
            raise HTTPException(404, "device not found")
        await database.update_device(device_id, dns_server=body.dns_server.strip())
        await database.add_event(
            f"DNS server for device '{dev.name or dev.mac}' set to "
            f"{body.dns_server.strip() or '(inherit default)'}", "info",
            device_id)
        _schedule_dns_apply()
        return {"id": device_id, "dns_server": body.dns_server.strip()}

    # -- domain filtering (blacklist / allow-list / custom hosts) -----------

    def _rule_view(rule: _db.DomainRule) -> dict[str, Any]:
        return {
            "id": rule.id, "scope": rule.scope, "scope_id": rule.scope_id,
            "action": rule.action, "domain": rule.domain,
            "target_ip": rule.target_ip, "enabled": rule.enabled,
            "source": rule.source, "created_at": rule.created_at,
        }

    @app.get("/api/dns/rules", dependencies=[Depends(_require_auth)])
    async def list_dns_rules(scope: Optional[str] = None,
                             scope_id: Optional[int] = None) -> list[dict[str, Any]]:
        rules = await database.list_domain_rules(scope, scope_id)
        return [_rule_view(r) for r in rules]

    @app.post("/api/dns/rules", status_code=201, dependencies=[Depends(_require_auth)])
    async def create_dns_rule(body: DomainRuleCreate) -> dict[str, Any]:
        if body.scope != "global" and body.scope_id is None:
            raise HTTPException(400, "scope_id is required for a user/device rule")
        if body.action == "redirect" and not body.target_ip:
            raise HTTPException(400, "target_ip is required for a redirect rule")
        try:
            domain = _dns_rules.normalize_pattern(body.domain)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if body.scope == "user" and await database.get_user(body.scope_id) is None:
            raise HTTPException(404, "user not found")
        if body.scope == "device" and await database.get_device(body.scope_id) is None:
            raise HTTPException(404, "device not found")
        rule = await database.create_domain_rule(
            body.scope, body.action, domain, scope_id=body.scope_id,
            target_ip=body.target_ip, enabled=body.enabled, source="manual")
        await database.add_event(
            f"DNS rule added: {body.action} {domain} ({body.scope})", "info")
        _schedule_dns_apply()
        return _rule_view(rule)

    @app.patch("/api/dns/rules/{rule_id}", dependencies=[Depends(_require_auth)])
    async def update_dns_rule(rule_id: int, body: DomainRuleUpdate) -> dict[str, Any]:
        rule = await database.get_domain_rule(rule_id)
        if rule is None:
            raise HTTPException(404, "rule not found")
        fields: dict[str, Any] = {}
        if body.enabled is not None:
            fields["enabled"] = body.enabled
        if body.target_ip is not None:
            fields["target_ip"] = body.target_ip
        if fields:
            await database.update_domain_rule(rule_id, **fields)
        _schedule_dns_apply()
        return {"id": rule_id, "updated": True}

    @app.delete("/api/dns/rules/{rule_id}", dependencies=[Depends(_require_auth)])
    async def delete_dns_rule(rule_id: int) -> dict[str, Any]:
        rule = await database.get_domain_rule(rule_id)
        if rule is None:
            raise HTTPException(404, "rule not found")
        await database.delete_domain_rule(rule_id)
        await database.add_event(f"DNS rule removed: {rule.action} {rule.domain}", "info")
        _schedule_dns_apply()
        return {"id": rule_id, "deleted": True}

    @app.post("/api/dns/rules/quick", status_code=201, dependencies=[Depends(_require_auth)])
    async def quick_dns_rule(body: DnsQuickRule) -> dict[str, Any]:
        """One-click block/allow for a domain a device has actually queried
        (the History tab's per-domain buttons). Narrower than the general
        rule-create endpoint on purpose: only ``global``/``device`` scope,
        matching the two choices the History UI actually offers."""
        if body.scope == "device":
            if body.device_id is None:
                raise HTTPException(400, "device_id is required for scope=device")
            if await database.get_device(body.device_id) is None:
                raise HTTPException(404, "device not found")
        try:
            domain = _dns_rules.normalize_pattern(body.domain)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        scope_id = body.device_id if body.scope == "device" else None
        rule = await database.create_domain_rule(
            body.scope, body.action, domain, scope_id=scope_id, source="manual")
        await database.add_event(
            f"DNS rule added from history: {body.action} {domain} "
            f"({body.scope})", "info")
        _schedule_dns_apply()
        return _rule_view(rule)

    @app.post("/api/dns/import", dependencies=[Depends(_require_auth)])
    async def import_dns_rules(body: DnsImportRequest) -> dict[str, Any]:
        """Paste raw hosts-format or AdBlock-Plus-format text -> domain_rules
        rows, in ONE bulk insert (see database.create_domain_rules_bulk —
        a per-row insert+commit loop is what made preset-enable freeze on a
        large list; a pasted import can be just as large)."""
        if body.scope != "global" and body.scope_id is None:
            raise HTTPException(400, "scope_id is required for a user/device rule")
        raw_domains = _dns_rules.compile_source_text(body.text, body.format)
        # Parsing + per-domain normalization is pure CPU with no I/O, but a
        # very large paste (tens of thousands of lines) is still enough work
        # to be worth moving off the event loop, same reasoning as the
        # preset-enable path below.
        domains, skipped = await asyncio.to_thread(
            _normalize_domains, raw_domains)
        created = await database.create_domain_rules_bulk(
            body.scope, body.action, domains, scope_id=body.scope_id,
            source="import")
        await database.add_event(
            f"Imported {created} domain rule(s) ({body.action}, {skipped} skipped)",
            "info")
        _schedule_dns_apply()
        return {"created": created, "skipped": skipped}

    @app.get("/api/dns/presets", dependencies=[Depends(_require_auth)])
    async def list_dns_presets() -> list[dict[str, Any]]:
        states = {s["preset_id"]: s for s in await database.list_preset_states()}
        out = []
        for preset in _dns_rules.PRESETS.values():
            state = states.get(preset.id, {})
            out.append({
                "id": preset.id, "name": preset.name,
                "description": preset.description,
                "enabled": bool(state.get("enabled", 0)),
                "scope": state.get("scope", "global"),
                "scope_id": state.get("scope_id"),
                "domain_count": state.get("domain_count", 0),
                "updated_at": state.get("updated_at", 0),
            })
        return out

    @app.post("/api/dns/presets/{preset_id}/enable", dependencies=[Depends(_require_auth)])
    async def enable_dns_preset(preset_id: str, body: DnsPresetEnable) -> dict[str, Any]:
        preset = _dns_rules.PRESETS.get(preset_id)
        if preset is None:
            raise HTTPException(404, "unknown preset")
        if body.scope != "global" and body.scope_id is None:
            raise HTTPException(400, "scope_id is required for a user/device preset")
        # Fetching runs in a thread — it's a blocking network call (urllib)
        # and must never stall the event loop / WebSocket push.
        try:
            raw_domains = await asyncio.to_thread(_dns_rules.fetch_preset, preset)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"failed to fetch preset sources: {exc}") from None
        # Normalization of a 100k+ domain list (ads-tracking) is real CPU
        # work — off the event loop too, so a large preset never stalls the
        # WS push or another request while it enables.
        domains, _skipped = await asyncio.to_thread(_normalize_domains, raw_domains)

        # Scope-change leak fix: if this preset was already enabled at a
        # DIFFERENT scope, its old rules are orphaned unless purged here —
        # set_preset_state below only remembers the NEW scope, so the old
        # scope's source_tag would otherwise never be reachable again.
        prior = await database.get_preset_state(preset_id)
        if prior and prior.get("enabled") and (
                prior.get("scope") != body.scope
                or prior.get("scope_id") != body.scope_id):
            old_tag = (f"preset:{preset_id}:{prior['scope']}:"
                      f"{prior.get('scope_id') or 0}")
            removed = await database.delete_domain_rules_by_source(old_tag)
            log.info("DNS preset %s moved scope (%s/%s -> %s/%s): purged "
                    "%d rule(s) from the old scope", preset_id,
                    prior.get("scope"), prior.get("scope_id"),
                    body.scope, body.scope_id, removed)

        source_tag = f"preset:{preset_id}:{body.scope}:{body.scope_id or 0}"
        # Replace any previous rules from this exact preset+scope (a refresh
        # re-enables cleanly instead of accumulating stale domains forever).
        await database.delete_domain_rules_by_source(source_tag)
        count = await database.create_domain_rules_bulk(
            body.scope, "block", domains, scope_id=body.scope_id,
            source=source_tag)
        await database.set_preset_state(
            preset_id, True, scope=body.scope, scope_id=body.scope_id,
            domain_count=count)
        await database.add_event(
            f"DNS preset enabled: {preset.name} ({count} domains, {body.scope})",
            "info")
        _schedule_dns_apply()
        return {"id": preset_id, "enabled": True, "domain_count": count}

    @app.post("/api/dns/presets/{preset_id}/disable", dependencies=[Depends(_require_auth)])
    async def disable_dns_preset(preset_id: str, body: DnsPresetEnable) -> dict[str, Any]:
        if preset_id not in _dns_rules.PRESETS:
            raise HTTPException(404, "unknown preset")
        source_tag = f"preset:{preset_id}:{body.scope}:{body.scope_id or 0}"
        removed = await database.delete_domain_rules_by_source(source_tag)
        await database.set_preset_state(
            preset_id, False, scope=body.scope, scope_id=body.scope_id,
            domain_count=0)
        await database.add_event(
            f"DNS preset disabled: {preset_id} ({removed} rules removed)", "info")
        _schedule_dns_apply()
        return {"id": preset_id, "enabled": False, "removed": removed}

    @app.post("/api/dns/apply", dependencies=[Depends(_require_auth)])
    async def apply_dns_now() -> dict[str, Any]:
        """Force an immediate dnsmasq regeneration + reload (normally
        automatic after every rule/preset/DNS-server edit above)."""
        if dns_apply is None:
            return {"applied": False, "reason": "dns manager not wired"}
        await dns_apply()
        return {"applied": True}

    @app.get("/api/logs", dependencies=[Depends(_require_auth)])
    async def logs(limit: int = 300) -> dict[str, Any]:
        """Tail of the gateway log file (newest first) for the Admin-page
        System Logs console. The read is a whole-file scan — off the event
        loop so a big log never stalls the API."""
        return await asyncio.to_thread(_read_log_tail, log_path, limit)

    @app.get("/api/bundle", dependencies=[Depends(_require_auth)])
    async def get_bundle() -> dict[str, Any]:
        b = await database.get_bundle()
        return {"total_gb": b.total_gb, "reset_day": b.reset_day,
                "period_type": b.period_type,
                "period_start": b.period_start, "period_end": b.period_end,
                "allowances": b.allowances}

    async def _apply_bundle_values(total_gb: float | None,
                                   reset_day: int | None,
                                   period_type: str | None = None) -> None:
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
        if period_type is not None:
            if period_type not in ("renew_day", "end_of_month"):
                raise HTTPException(
                    400, "period_type must be 'renew_day' or 'end_of_month'")
            b.period_type = period_type
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
                    "period_type": b.period_type,
                    "added_gb": result["added_gb"]}
        await _apply_bundle_values(body.total_gb, body.reset_day,
                                   body.period_type)
        b = await database.get_bundle()
        return {"total_gb": b.total_gb, "reset_day": b.reset_day,
                "period_type": b.period_type}

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
                "total_gb": b.total_gb, "reset_day": b.reset_day,
                "period_type": b.period_type}

    @app.post("/api/setup/complete", dependencies=[Depends(_require_auth)])
    async def complete_setup(body: SetupComplete) -> dict[str, Any]:
        # Auth'd like /api/password: a wrong current password is a 400, not a
        # 401 (the client maps 401 to "session expired" and logs out).
        if body.new_password is not None:
            stored = await database.get_setting("admin_password")
            valid, _ = _verify_password(body.current_password or "", stored)
            if not valid:
                raise HTTPException(400, "current password incorrect")
            await database.set_setting("admin_password",
                                       _hash_password(body.new_password))
            await database.add_event("Admin password changed", "warn")
        # Only apply the bundle when a value was given — a password-only save
        # must not take bundle ownership from config.yaml (see _apply_bundle_values).
        if body.total_gb is not None or body.reset_day is not None \
                or body.period_type is not None:
            await _apply_bundle_values(body.total_gb, body.reset_day,
                                       body.period_type)
        await service.mark_setup_complete()
        await database.add_event("First-run setup completed", "info")
        return {"ok": True}

    # -- guest mode ------------------------------------------------------------

    @app.get("/api/guest", dependencies=[Depends(_require_auth)])
    async def get_guest() -> dict[str, Any]:
        return {"enabled": await service.is_guest_mode(),
                "quota_gb": await service.guest_quota_gb(),
                "limit": await service.guest_limit(),
                "speed_limit_mbps": await service.guest_speed_limit_mbps(),
                "stop_new": await service.stop_new_connections()}

    @app.post("/api/guest", dependencies=[Depends(_require_auth)])
    async def set_guest(body: GuestUpdate) -> dict[str, Any]:
        if body.enabled is not None:
            await service.set_guest_mode(body.enabled)
        if body.quota_gb is not None:
            await service.set_guest_quota(body.quota_gb)
        if body.limit is not None:
            await service.set_guest_limit(body.limit)
        if body.speed_limit_mbps is not None:
            await service.set_guest_speed_limit(body.speed_limit_mbps)
        if body.stop_new is not None:
            await service.set_stop_new_connections(body.stop_new)
            _schedule_stop_new_sync()
        return {"enabled": await service.is_guest_mode(),
                "quota_gb": await service.guest_quota_gb(),
                "limit": await service.guest_limit(),
                "speed_limit_mbps": await service.guest_speed_limit_mbps(),
                "stop_new": await service.stop_new_connections()}

    # -- speed shaping (Network tab) ------------------------------------------

    @app.get("/api/network", dependencies=[Depends(_require_auth)])
    async def get_network() -> dict[str, Any]:
        result = await service.get_shaping_config()
        # VPN share rides the same Network-tab payload: the switch (persisted),
        # the pinned tunnel interface, and the LIVE status from the kernel-side
        # manager (None in tests / degraded boot — the UI shows just the
        # persisted switch).
        result["vpn_share"] = await service.get_vpn_config()
        if vpn_status_getter is not None:
            try:
                result["vpn_share"]["status"] = vpn_status_getter()
            except Exception:  # noqa: BLE001 — a status probe must never 500 the panel
                result["vpn_share"]["status"] = {"state": "error",
                                                 "message": "status probe failed"}
        result["decline_random_macs"] = await service.decline_random_macs()
        return result

    @app.post("/api/network", dependencies=[Depends(_require_auth)])
    async def set_network(body: NetworkUpdate) -> dict[str, Any]:
        result = await service.set_shaping(
            enabled=body.enabled,
            total_down_mbps=body.total_down_mbps,
            total_up_mbps=body.total_up_mbps,
            aqm=body.aqm,
            lan_rate_mbps=body.lan_rate_mbps)
        # Apply to the kernel NOW — no 15 s wait for the maintenance tick, so a
        # saved Network-tab change is enforced immediately (no page refresh).
        _schedule_shaping_sync()
        # VPN-share toggle: persist the switch, then fire the immediate kernel
        # reconcile (policy routing + the gateway-meter suspension) the same
        # way shaping is applied right away.
        if body.vpn_share is not None:
            await service.set_vpn_share(body.vpn_share)
            _schedule_vpn_apply()
        # Random-MAC gate: persist the switch; the optional one-shot sweep
        # ("also for old devices already joined") cuts existing randomized
        # devices in the same call.
        if body.decline_random_macs is not None:
            await service.set_decline_random_macs(
                body.decline_random_macs,
                also_existing=bool(body.decline_random_macs_existing))
            _schedule_decline_random_sync()
        result["vpn_share"] = await service.get_vpn_config()
        if vpn_status_getter is not None:
            try:
                result["vpn_share"]["status"] = vpn_status_getter()
            except Exception:  # noqa: BLE001
                result["vpn_share"]["status"] = {"state": "error",
                                                 "message": "status probe failed"}
        result["decline_random_macs"] = await service.decline_random_macs()
        return result

    # -- MAC whitelist / blacklist ----------------------------------------------

    @app.get("/api/mac-lists", dependencies=[Depends(_require_auth)])
    async def get_mac_lists() -> dict[str, Any]:
        return await service.mac_lists()

    @app.post("/api/mac-lists", dependencies=[Depends(_require_auth)])
    async def set_mac_lists(body: MacListsUpdate) -> dict[str, Any]:
        if body.allow is not None:
            await service.set_mac_list("allow", body.allow)
        if body.deny is not None:
            await service.set_mac_list("deny", body.deny)
        return await service.mac_lists()

    # -- software updates (Admin tab) ------------------------------------------

    def _updater_or_404():
        if updater is None:
            raise HTTPException(404, "update manager not wired")
        return updater

    @app.get("/api/updates", dependencies=[Depends(_require_auth)])
    async def get_updates() -> dict[str, Any]:
        """Current self-update state: version, latest, availability, the
        per-version changelog, and the enabled/auto-install switches."""
        return await _updater_or_404().state()

    @app.post("/api/updates", dependencies=[Depends(_require_auth)])
    async def set_updates(body: UpdateSettings) -> dict[str, Any]:
        """Toggle the 24 h check and/or auto-install. A partial save leaves the
        other setting untouched. Turning the check ON clears a stale last-error
        so the card starts clean."""
        u = _updater_or_404()
        if body.enabled is not None:
            await u.set_enabled(body.enabled)
        if body.auto_install is not None:
            await database.set_setting("updates_auto_install",
                                       "1" if body.auto_install else "")
        return await u.state()

    @app.post("/api/updates/check", dependencies=[Depends(_require_auth)])
    async def check_updates() -> dict[str, Any]:
        """Force a GitHub release check now (no 24 h wait)."""
        return await _updater_or_404().check_now()

    @app.post("/api/updates/install", dependencies=[Depends(_require_auth)])
    async def install_updates() -> dict[str, Any]:
        """Download + install the latest .deb right away."""
        return await _updater_or_404().install_latest()

    # -- auth -----------------------------------------------------------------

    login_limiter = _LoginRateLimiter()

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request,
                    response: Response) -> dict[str, Any]:
        await _ensure_admin_password(database)
        host = request.client.host if request.client else ""
        if not login_limiter.check(host):
            raise HTTPException(429, "too many failed logins — try again later")
        stored = await database.get_setting("admin_password")
        valid, needs_rehash = _verify_password(body.password, stored)
        if not valid:
            login_limiter.fail(host)
            raise HTTPException(401, "invalid password")
        login_limiter.success(host)
        if needs_rehash:
            # Legacy 200k hash verified — upgrade it to the current work
            # factor so the stored secret keeps pace with the default.
            await database.set_setting("admin_password",
                                       _hash_password(body.password))
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
        valid, _ = _verify_password(body.current, stored)
        if not valid:
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
            # One snapshot on connect so the panel renders instantly; the
            # periodic pushes come from the single shared _push_loop below
            # (one payload build per 5 s for ALL sockets — N sockets never
            # trigger N builds, each a ~30-query DB round-trip).
            await ws.send_json({"type": "snapshot", "data": await _dashboard_payload()})
            while True:
                # keepalive + disconnect detection; the payload push is the
                # push loop's job
                msg = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
        except asyncio.TimeoutError:
            pass  # nothing to read, push loop keeps the client fresh
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
