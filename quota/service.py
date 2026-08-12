"""Quota domain service: budget math, month roll-over, enforcement state.

This module is deliberately pure — no network. It talks to the
:class:`~quota.db.Database` only, which makes the entire quota logic unit-testable
offline.

Allowance model (per user)
--------------------------
Each USER is either ``fixed`` (admin assigns GB) or ``auto``. At the start of a
quota period, auto users equally share whatever remains of the bundle after
fixed allocations:

    fixed_total = sum(fixed_gb for fixed users)
    remaining   = max(0, total_gb - fixed_total)
    auto_share  = remaining / count(auto users)
    allowance(i)= fixed_gb(i)            if mode == fixed
                = auto_share             if mode == auto

A user's usage is the sum of every device they own. A user is quota-blocked
when their period usage >= their allowance, or admin-blocked via
``set_admin_block_user``. Enforcement stays per-MAC: every device of a blocked
user is resolved blocked in :meth:`snapshot_state` (a per-device ``bypass``
exempts a single device from the user's QUOTA block only — an explicit
``admin_off`` on either the user or the device always wins).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Optional

from core import timeutil
from quota import db as _db

log = logging.getLogger("quota.service")

GB = 1024 ** 3

#: Consumption-milestone thresholds (percent of the user's allowance). When a
#: user's usage crosses one, the milestone page surfaces it and marks it
#: notified so each threshold is shown once per period.
MILESTONES = (50, 75, 100)


class QuotaService:
    def __init__(self, database: _db.Database, timezone: str = "",
                 clock: Any = None) -> None:
        self.db = database
        self.tz = timeutil.tz_for(timezone) if timezone else None
        #: injectable clock (callable returning datetime) for tests
        self._clock = clock

    # -- helpers --------------------------------------------------------------

    def _now(self) -> _dt.datetime:
        if self._clock is not None:
            return self._clock()
        if self.tz is not None:
            return _dt.datetime.now(self.tz)
        return _dt.datetime.now().astimezone()

    async def current_period_dates(self) -> tuple[str, str]:
        bundle = await self.db.get_bundle()
        return bundle.period_start, bundle.period_end

    # -- budget ---------------------------------------------------------------

    async def compute_allowances(self) -> dict[int, float]:
        """Compute per-USER allowances using the hybrid model above.

        Each user's allowance = their fixed/auto share PLUS the per-period
        top-up the admin granted (``users.topup_gb``). Persisting top-ups on
        the user row (not the snapshot dict) is what lets a top-up survive
        ``recompute_allowances`` — otherwise the next user edit, bundle
        change, or new-device auto-registration silently wiped it and re-blocked
        the user the admin had just unblocked.
        """
        users = await self.db.list_users()
        fixed_total = sum((u.fixed_gb or 0.0) for u in users
                          if u.quota_mode == _db.QUOTA_FIXED)
        auto_users = [u for u in users if u.quota_mode == _db.QUOTA_AUTO]
        remaining = max(0.0, (await self.db.get_bundle()).total_gb - fixed_total)
        auto_share = remaining / len(auto_users) if auto_users else 0.0

        allowances: dict[int, float] = {}
        for u in users:
            base = u.fixed_gb if u.quota_mode == _db.QUOTA_FIXED else auto_share
            base = base if base is not None else 0.0
            allowances[u.id] = round(base + (u.topup_gb or 0.0), 3)
        return allowances

    def _next_period_end(self, bundle: _db.Bundle, now: _dt.datetime) -> str:
        """ISO end date of the current period ('' when there is no reset)."""
        if bundle.reset_day <= 0:
            return ""  # no automatic reset — period stays open until admin acts
        return timeutil.period_bounds(now, bundle.reset_day)[1].date().isoformat()

    async def open_period(self) -> None:
        """Open a fresh period: recompute allowances and set ``period_start`` to now.

        Called at startup, at each month roll-over, and by the manual
        "Reset month" action. Idempotent: it rewrites the snapshot but never
        touches historical usage rows. With ``reset_day <= 0`` the period is
        opened once and stays open (no automatic roll).
        """
        bundle = await self.db.get_bundle()
        now = self._now()
        # A top-up is a grant for the CURRENT period — clear it on roll-over so
        # the new month recomputes from the fixed/auto shares only.
        await self.db.clear_topups()
        # A new period starts with no guests: guest accounts are period-scoped.
        await self._clear_guest_users()
        # Milestone notices (50/75/100%) are period-scoped: a fresh period
        # re-arms them so each threshold is surfaced again in the new month.
        await self.db.reset_milestone_flags()
        start, end = timeutil.period_bounds(now, bundle.reset_day)
        bundle.allowances = await self.compute_allowances()
        # reset_day<=0 -> period_bounds returns "today"; period_end stays "".
        bundle.period_start = start.date().isoformat()
        bundle.period_end = end.date().isoformat() if bundle.reset_day > 0 else ""
        await self.db.set_bundle(bundle)
        log.info("quota period opened: %s -> %s (%d allowances)",
                 bundle.period_start, bundle.period_end or "manual",
                 len(bundle.allowances))

    async def recompute_allowances(self) -> None:
        """Refresh allowances + period_end without moving ``period_start``.

        Used after device edits, bundle changes, and mid-month top-ups of the
        bundle itself. Unlike :meth:`open_period`, it never rolls the period,
        so usage already recorded in the current period is preserved.
        """
        bundle = await self.db.get_bundle()
        now = self._now()
        bundle.allowances = await self.compute_allowances()
        bundle.period_end = self._next_period_end(bundle, now)
        await self.db.set_bundle(bundle)
        log.info("allowances recomputed (%d users)", len(bundle.allowances))

    async def ensure_period(self) -> None:
        """Roll the period if stale, open if missing.

        ``reset_day <= 0`` disables the automatic roll: the period is opened
        once (on first boot) and afterwards only advances via an explicit
        admin action (:meth:`reset_month`).

        With a monthly boundary the roll triggers when the recorded
        ``period_start`` is *before* the current boundary — a mid-month manual
        reset leaves ``period_start`` after the boundary, so it stands until
        the next natural boundary passes.
        """
        bundle = await self.db.get_bundle()
        now = self._now()
        if bundle.reset_day <= 0:
            if not bundle.period_start:
                await self.open_period()
            return
        start, _ = timeutil.period_bounds(now, bundle.reset_day)
        if bundle.period_start >= start.date().isoformat():
            return
        await self.open_period()

    async def recharge(self, add_gb: float) -> dict[str, Any]:
        """Add GB to the current bundle (ISP re-charge) and recompute quotas.

        The period itself is untouched — only the total bundle size grows, so
        auto devices pick up a larger share immediately. Returns the updated
        bundle view.
        """
        if add_gb <= 0:
            raise ValueError("add_gb must be positive")
        bundle = await self.db.get_bundle()
        bundle.total_gb = round(bundle.total_gb + add_gb, 3)
        await self.db.set_bundle(bundle)
        await self.recompute_allowances()
        await self.db.add_event(f"Bundle recharged +{add_gb:g} GB", "warn")
        return {
            "total_gb": bundle.total_gb,
            "added_gb": add_gb,
            "allowances": bundle.allowances,
        }

    # -- enforcement state -----------------------------------------------------

    @staticmethod
    def quota_blocked_for(user: _db.User | None, allowance: float,
                          used_gb: float) -> bool:
        """Is the user over their allowance (quota-blocked)?

        A PROTECTED user (the Gateway box account) is blocked once their usage
        reaches their allowance — even when the allowance is 0. That is the
        product behaviour the admin wants: setting the Gateway allowance to 0
        cuts the box's own internet immediately ("0 MB to only connect other
        users"). Every other user keeps the historical guard: an allowance of
        0 means UNMETERED (no limit), so an empty/over-subscribed bundle never
        blocks anyone.
        """
        if user is not None and user.protected:
            return used_gb >= allowance
        return allowance > 0 and used_gb >= allowance

    @staticmethod
    def resolve_device_state(user: _db.User | None, dev: _db.Device,
                             user_quota_blocked: bool) -> str:
        """Resolve a device's effective block state through its owner user.

        Precedence (highest wins):
          1. user admin_off   -> admin_off  (user-level cut covers all devices)
          2. device admin_off -> admin_off  (per-device manual cut)
          3. user quota-block -> quota      (unless the device has ``bypass``)
          4. otherwise        -> ok

        This is the single source of truth for a device's state; it is used by
        the API views and :meth:`snapshot_state` (enforcement map), so a
        user-level cut reaches every one of the user's device MACs. The
        user-level cut is deliberately NOT written to ``devices.block_state`` —
        that would be lossy (you couldn't tell a user-fan-out from a genuine
        per-device toggle, and clearing the user cut would strand devices in
        ``admin_off`` forever).
        """
        if user is not None and user.block_state == _db.BLOCK_ADMIN:
            return _db.BLOCK_ADMIN
        if dev.block_state == _db.BLOCK_ADMIN:
            return _db.BLOCK_ADMIN
        if user_quota_blocked and not dev.bypass:
            return _db.BLOCK_QUOTA
        return _db.BLOCK_OK

    async def _user_quota_map(self, allowances: dict[int, float]) -> dict[int, bool]:
        """user_id -> True when the user is over their allowance.

        Deferred to :meth:`quota_blocked_for` so a PROTECTED user (the Gateway
        box) is blocked at any usage when its allowance is 0 — the admin can
        cut the box's own internet with a 0 GB allowance. Other users keep the
        "no allowance => unmetered" guard.
        """
        users = {u.id: u for u in await self.db.list_users()}
        usage_by_user = await self.db.get_period_usage_by_user()
        out: dict[int, bool] = {}
        for uid, allowance in allowances.items():
            u = usage_by_user.get(uid, {"up": 0, "down": 0})
            used_gb = (u["up"] + u["down"]) / GB
            out[uid] = self.quota_blocked_for(users.get(uid), allowance, used_gb)
        return out

    async def evaluate_blocks(self) -> list[dict[str, Any]]:
        """Recompute block states from per-USER usage vs per-USER allowance.

        Only the QUOTA fan-out is persisted on devices (``block_state='quota'``)
        — the user-level admin cut is resolved at render/enforcement time by
        :meth:`resolve_device_state`, never written to devices. Returns a list
        of persisted changes: ``{device_id, mac, state, changed}``.
        """
        bundle = await self.db.get_bundle()
        allowances = bundle.allowances
        users = {u.id: u for u in await self.db.list_users()}
        devices = await self.db.list_devices()
        user_quota = await self._user_quota_map(allowances)
        changes: list[dict[str, Any]] = []

        for dev in devices:
            user = users.get(dev.user_id)
            if user is None:
                continue  # orphaned device (should not happen post-migration)
            if dev.block_state == _db.BLOCK_ADMIN:
                continue  # per-device manual override stays until lifted
            if user.block_state == _db.BLOCK_ADMIN:
                continue  # user-level cut resolved at render time, not persisted
            new_state = (_db.BLOCK_QUOTA if user_quota.get(dev.user_id, False)
                         else _db.BLOCK_OK)
            if new_state != dev.block_state:
                await self.db.set_device_state(dev.id, new_state)
                changes.append({
                    "device_id": dev.id, "mac": dev.mac,
                    "state": new_state, "changed": True,
                })
        return changes

    async def snapshot_state(self) -> dict[str, dict[str, Any]]:
        """Produce the per-device view used by the packet engine and the UI.

        ``blocked`` is the single source of truth for enforcement: it is
        resolved through the owning user (see :meth:`resolve_device_state`), so
        a user-level admin cut or user quota-block reaches every one of the
        user's device MACs — run.py's blocked-map push is unchanged.
        """
        devices = await self.db.list_devices()
        users = {u.id: u for u in await self.db.list_users()}
        leases = {l.mac: l.ip for l in await self.db.list_leases()}
        usage_by_user = await self.db.get_period_usage_by_user()
        allowances = (await self.db.get_bundle()).allowances
        out: dict[str, dict[str, Any]] = {}
        for dev in devices:
            user = users.get(dev.user_id)
            used_gb, allowance, quota_blocked = 0.0, 0.0, False
            if user is not None:
                u = usage_by_user.get(user.id, {"up": 0, "down": 0})
                used_gb = (u["up"] + u["down"]) / GB
                allowance = allowances.get(user.id, 0.0)
                quota_blocked = self.quota_blocked_for(user, allowance, used_gb)
            state = self.resolve_device_state(user, dev, quota_blocked)
            out[dev.mac] = {
                "ip": leases.get(dev.mac, ""),
                "name": dev.name,
                # the quota lives on the USER — a device's own quota_mode is an
                # inert mirror, so report the user's mode when it has one.
                "mode": user.quota_mode if user is not None else dev.quota_mode,
                # USER-level values (every device of a user reports the same).
                "allowance_gb": allowance,
                "used_gb": round(used_gb, 3),
                "blocked": state != _db.BLOCK_OK,
                "block_state": state,
            }
        return out

    # -- milestone notifications (page-only, per-user) ------------------------

    async def milestone_state(self) -> dict[int, dict[str, Any]]:
        """Per-user milestone-flag state for the milestone page.

        For each NON-protected user returns::

            {"allowance_gb", "used_gb", "percent",
             "milestones": {m: {"crossed", "notified", "pending"}}}

        where ``crossed`` = usage has reached ``m`` % of the allowance,
        ``notified`` = the milestone page already marked it, and ``pending`` =
        crossed but not yet notified (what the page surfaces + acknowledges).
        The protected Gateway account is skipped — its own usage is the admin's
        concern, not a household consumption notice.
        """
        users = {u.id: u for u in await self.db.list_users()}
        usage_by_user = await self.db.get_period_usage_by_user()
        allowances = (await self.db.get_bundle()).allowances
        out: dict[int, dict[str, Any]] = {}
        for u in users.values():
            if u.protected:
                continue
            usage = usage_by_user.get(u.id, {"up": 0, "down": 0})
            used_gb = (usage["up"] + usage["down"]) / GB
            allowance = allowances.get(u.id, 0.0)
            percent = used_gb / allowance * 100 if allowance > 0 else 0.0
            flags = {50: u.notified_50, 75: u.notified_75, 100: u.notified_100}
            milestones: dict[int, dict[str, Any]] = {}
            for m in MILESTONES:
                crossed = percent >= m
                notified = bool(flags[m])
                milestones[m] = {
                    "crossed": crossed,
                    "notified": notified,
                    "pending": crossed and not notified,
                }
            out[u.id] = {
                "allowance_gb": round(allowance, 3),
                "used_gb": round(used_gb, 3),
                "percent": round(percent, 1),
                "milestones": milestones,
            }
        return out

    async def mark_milestone_notified(self, user_id: int, milestone: int) -> None:
        """Mark a crossed milestone as notified (the milestone page's acknowledge).

        Validates ``milestone`` ∈ :data:`MILESTONES`; an unknown value raises
        :class:`ValueError`. Protected users are a no-op — they never appear in
        :meth:`milestone_state`, so nothing is ever surfaced for them.
        """
        if milestone not in MILESTONES:
            raise ValueError(
                f"milestone must be one of {MILESTONES}, got {milestone!r}")
        user = await self.db.get_user(user_id)
        if user is None or user.protected:
            return
        await self.db.update_user(user_id, **{f"notified_{milestone}": True})

    # -- first-run setup ------------------------------------------------------

    #: Settings key for the one-time welcome panel. Fresh installs show it until
    #: the admin confirms the bundle/password; the developer's existing box (any
    #: users already present) never sees it.
    SETUP_COMPLETE_KEY = "setup_complete"

    async def is_setup_complete(self) -> bool:
        """True once the first-run welcome is done.

        Heuristic: the setting is set to "1", OR the DB already has any
        NON-protected users. The protected "Gateway" account is seeded at
        connect, so it must not count — otherwise a genuinely fresh install
        would never show the welcome panel. The non-protected clause keeps an
        existing deployment from ever showing it.
        """
        if (await self.db.get_setting(self.SETUP_COMPLETE_KEY, "")) == "1":
            return True
        return any(not u.protected for u in await self.db.list_users())

    async def mark_setup_complete(self) -> None:
        """Record that the first-run welcome was completed."""
        await self.db.set_setting(self.SETUP_COMPLETE_KEY, "1")

    # -- guest mode ------------------------------------------------------------

    #: Settings keys for guest mode (a toggle + the guest allowance in GB).
    GUEST_MODE_KEY = "guest_mode"
    GUEST_QUOTA_KEY = "guest_quota_gb"

    async def is_guest_mode(self) -> bool:
        """True when newly connected devices auto-register as guests."""
        return (await self.db.get_setting(self.GUEST_MODE_KEY, "0")) == "1"

    async def guest_quota_gb(self) -> float:
        """The allowance granted to each new guest (default 1 GB, min 0.1)."""
        raw = await self.db.get_setting(self.GUEST_QUOTA_KEY, "")
        try:
            return max(0.1, float(raw or 1.0))
        except ValueError:
            return 1.0

    async def set_guest_mode(self, enabled: bool) -> None:
        """Turn guest mode on/off. Existing guests stay; only future new
        devices are affected."""
        await self.db.set_setting(self.GUEST_MODE_KEY, "1" if enabled else "0")
        await self.db.add_event(
            f"Guest mode {'enabled' if enabled else 'disabled'}",
            "warn" if enabled else "info")

    async def set_guest_quota(self, gb: float) -> None:
        """Change the guest allowance. Applied to EVERY existing guest right
        away (not just future ones), then allowances recompute."""
        gb = round(max(0.1, float(gb)), 3)
        await self.db.set_setting(self.GUEST_QUOTA_KEY, str(gb))
        await self.db.set_guest_fixed_gb(gb)
        await self.recompute_allowances()
        await self.db.add_event(f"Guest quota set to {gb:g} GB", "warn")

    async def _clear_guest_users(self) -> None:
        """Delete every guest user + their devices/usage (period reset hook).

        Guests are period-scoped: a new quota period — manual or automatic —
        always starts with zero guests. A freshly returned guest device simply
        re-registers under guest mode.
        """
        n = await self.db.delete_guest_users()
        if n:
            await self.db.add_event(f"Cleared {n} guest device(s)", "info")

    # -- speed shaping (Linux tc) ------------------------------------------------

    #: Settings keys for the Linux tc shaper (quota/shaping.py). The master
    #: toggle, the real line down/up rates in Mbps (0 = not set -> shaper idle),
    #: and the bufferbloat-avoidance (fq_codel) toggle.
    SHAPING_ENABLED_KEY = "shaping_enabled"
    SHAPING_DOWN_KEY = "shaping_total_down_mbps"
    SHAPING_UP_KEY = "shaping_total_up_mbps"
    SHAPING_AQM_KEY = "shaping_aqm"

    async def get_shaping_config(self) -> dict[str, Any]:
        """Current shaping settings (the shaper reads these each maintenance
        tick, so a change takes effect within ~15 s, like blocks/bundle)."""
        enabled = (await self.db.get_setting(self.SHAPING_ENABLED_KEY, "0")) == "1"
        aqm = (await self.db.get_setting(self.SHAPING_AQM_KEY, "1")) == "1"
        try:
            total_down = max(0.0, float(
                await self.db.get_setting(self.SHAPING_DOWN_KEY, "0") or 0))
        except ValueError:
            total_down = 0.0
        try:
            total_up = max(0.0, float(
                await self.db.get_setting(self.SHAPING_UP_KEY, "0") or 0))
        except ValueError:
            total_up = 0.0
        return {"enabled": enabled, "total_down_mbps": total_down,
                "total_up_mbps": total_up, "aqm": aqm}

    # -- VPN share (policy-routing the client subnet into the box's tunnel) ------

    #: Settings keys for the "VPN share" switch (Network tab). The dashboard
    #: toggle drives quota/vpnshare.py's policy routing; ``vpn_share_interface``
    #: pins the auto-detected tunnel so a multi-VPN box does not re-guess on
    #: every restart/reconcile.
    VPN_SHARE_KEY = "vpn_share_enabled"
    VPN_INTERFACE_KEY = "vpn_share_interface"

    async def get_vpn_config(self) -> dict[str, Any]:
        """Current VPN-share settings (the switch + the pinned tunnel)."""
        enabled = (await self.db.get_setting(self.VPN_SHARE_KEY, "0")) == "1"
        iface = (await self.db.get_setting(self.VPN_INTERFACE_KEY, "") or "").strip()
        return {"enabled": enabled, "interface": iface}

    async def set_vpn_share(self, enabled: bool) -> dict[str, Any]:
        """Persist the VPN-share master switch. The kernel routing itself is
        applied by run.py's maintenance loop / immediate callback (same
        pattern as shaping) — this only owns the setting + the event."""
        await self.db.set_setting(self.VPN_SHARE_KEY, "1" if enabled else "0")
        await self.db.add_event(
            "VPN share " + ("enabled — all devices share the box's VPN "
                            "connection (policy routing into the tunnel)"
                            if enabled else
                            "disabled — devices use the direct uplink"),
            "warn" if enabled else "info")
        return await self.get_vpn_config()

    async def set_vpn_interface(self, iface: str) -> None:
        """Pin the auto-detected tunnel interface (called after a successful
        apply so a restart re-applies the same tunnel)."""
        iface = (iface or "").strip()
        await self.db.set_setting(self.VPN_INTERFACE_KEY, iface)

    async def set_shaping(self, enabled: bool | None = None,
                          total_down_mbps: float | None = None,
                          total_up_mbps: float | None = None,
                          aqm: bool | None = None) -> dict[str, Any]:
        """Persist a shaping change. Each non-None field is written; a ``warn``
        event records what changed. No engine call here — the maintenance loop
        consumes the settings next tick (same pattern as guest mode)."""
        changes: list[str] = []
        if enabled is not None:
            await self.db.set_setting(self.SHAPING_ENABLED_KEY,
                                      "1" if enabled else "0")
            changes.append(f"shaping {'enabled' if enabled else 'disabled'}")
        if total_down_mbps is not None:
            total_down_mbps = max(0.0, float(total_down_mbps))
            await self.db.set_setting(self.SHAPING_DOWN_KEY, str(total_down_mbps))
            changes.append(f"total down {total_down_mbps:g} Mbps")
        if total_up_mbps is not None:
            total_up_mbps = max(0.0, float(total_up_mbps))
            await self.db.set_setting(self.SHAPING_UP_KEY, str(total_up_mbps))
            changes.append(f"total up {total_up_mbps:g} Mbps")
        if aqm is not None:
            await self.db.set_setting(self.SHAPING_AQM_KEY, "1" if aqm else "0")
            changes.append("low-latency queues " +
                           ("enabled" if aqm else "disabled"))
        if changes:
            await self.db.add_event("Speed limits: " + ", ".join(changes), "warn")
        return await self.get_shaping_config()

    # -- admin operations ------------------------------------------------------

    async def top_up_user(self, user_id: int, extra_gb: float) -> Optional[dict[str, Any]]:
        """Increase a USER's allowance by ``extra_gb`` for this period.

        The top-up is stored on the user row (``topup_gb``), so a later
        ``recompute_allowances``/``open_period`` rebuild of the snapshot does
        not discard it. Any quota fan-out on the user's devices is cleared
        (per-device admin overrides are untouched).
        """
        if extra_gb <= 0:
            raise ValueError("extra_gb must be positive")
        user = await self.db.get_user(user_id)
        if user is None:
            return None
        await self.db.add_topup_user(user_id, extra_gb)
        await self.recompute_allowances()  # effective allowance = share + topup
        for dev in await self.db.list_devices(user_id=user_id):
            if dev.block_state == _db.BLOCK_QUOTA:
                await self.db.set_device_state(dev.id, _db.BLOCK_OK)
        bundle = await self.db.get_bundle()
        allowance = bundle.allowances.get(user_id, 0.0)
        await self.db.add_event(
            f"Top-up +{extra_gb:g} GB for '{user.name}'", "warn",
            user_id=user_id)
        return {"user_id": user_id, "allowance_gb": allowance}

    async def top_up(self, device_id: int, extra_gb: float) -> Optional[dict[str, Any]]:
        """Device convenience: top up the device's owning user."""
        dev = await self.db.get_device(device_id)
        if dev is None:
            return None
        if dev.user_id is None:
            raise ValueError("device has no user")
        return await self.top_up_user(dev.user_id, extra_gb)

    async def set_admin_block(self, device_id: int, blocked: bool) -> Optional[dict[str, Any]]:
        """Manually enable/disable a device regardless of quota."""
        dev = await self.db.get_device(device_id)
        if dev is None:
            return None
        state = _db.BLOCK_ADMIN if blocked else _db.BLOCK_OK
        await self.db.set_device_state(dev.id, state)
        await self.db.add_event(
            f"{'Blocked' if blocked else 'Unblocked'} '{dev.name}'", "warn", dev.id)
        return {"device_id": device_id, "mac": dev.mac, "block_state": state}

    async def set_admin_block_user(self, user_id: int, blocked: bool) -> Optional[dict[str, Any]]:
        """Manually cut/uncut all of a user's devices at once.

        The user-level ``admin_off`` is applied at render/enforcement time (see
        :meth:`resolve_device_state`), so no device rows are touched — clearing
        the cut restores every device immediately.
        """
        user = await self.db.get_user(user_id)
        if user is None:
            return None
        state = _db.BLOCK_ADMIN if blocked else _db.BLOCK_OK
        await self.db.update_user(user_id, block_state=state)
        await self.db.add_event(
            f"{'Blocked' if blocked else 'Unblocked'} user '{user.name}'",
            "warn", user_id=user_id)
        return {"user_id": user_id, "block_state": state}

    async def reset_month(self) -> None:
        """Force an early period roll-over (admin action).

        A manual reset starts a fresh period **from today** and zeroes the
        usage already recorded this period, so the counter visibly restarts.
        Usage storage is day-granular (one row per device/date), so just moving
        ``period_start`` cannot exclude usage recorded earlier today — the
        button would look dead. History before the period start is kept.
        """
        bundle = await self.db.get_bundle()
        now = self._now()
        await self.db.clear_topups()
        # A manual reset is a fresh period: guests are period-scoped, so they
        # are wiped too (a new guest just re-registers when it reconnects).
        await self._clear_guest_users()
        # Milestone notices are period-scoped too — re-arm for the new period.
        await self.db.reset_milestone_flags()
        old_start = bundle.period_start
        if old_start:
            await self.db.clear_usage(old_start)
        bundle.allowances = await self.compute_allowances()
        bundle.period_start = now.date().isoformat()
        bundle.period_end = self._next_period_end(bundle, now)
        await self.db.set_bundle(bundle)
        await self.db.add_event("Monthly quota period reset", "info")
        log.info("manual reset: period restarted %s (usage zeroed)", bundle.period_start)
