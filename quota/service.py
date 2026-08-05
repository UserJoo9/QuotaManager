"""Quota domain service: budget math, month roll-over, enforcement state.

This module is deliberately pure — no pydivert, no network. It talks to the
:class:`~quota.db.Database` only, which makes the entire quota logic unit-testable
offline.

Allowance model (hybrid)
------------------------
Each device is either ``fixed`` (admin assigns GB) or ``auto``. At the start of
a quota period, auto devices equally share whatever remains of the bundle after
fixed allocations:

    fixed_total = sum(fixed_gb for fixed devices)
    remaining   = max(0, total_gb - fixed_total)
    auto_share  = remaining / count(auto devices)
    allowance(i)= fixed_gb(i)            if mode == fixed
                = auto_share             if mode == auto

A device is blocked when its period usage >= allowance, or when the admin has
set ``block_state`` to ``admin_off``.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Optional

from core import timeutil
from quota import db as _db

log = logging.getLogger("quota.service")

GB = 1024 ** 3


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

    async def compute_allowances(self) -> dict[str, float]:
        """Compute per-MAC allowances using the hybrid model above.

        Each device's allowance = its fixed/auto share PLUS any top-up GB the
        admin granted this period (``dev.topup_gb``). Persisting top-ups in the
        devices table (not the snapshot dict) is what lets a top-up survive
        ``recompute_allowances`` — otherwise the next device edit, bundle
        change, or new-device auto-registration silently wiped it and re-blocked
        the device the admin had just unblocked.
        """
        devices = await self.db.list_devices()
        fixed_total = sum((d.fixed_gb or 0.0) for d in devices
                          if d.quota_mode == _db.QUOTA_FIXED)
        auto_devices = [d for d in devices if d.quota_mode == _db.QUOTA_AUTO]
        remaining = max(0.0, (await self.db.get_bundle()).total_gb - fixed_total)
        auto_share = remaining / len(auto_devices) if auto_devices else 0.0

        allowances: dict[str, float] = {}
        for d in devices:
            base = d.fixed_gb if d.quota_mode == _db.QUOTA_FIXED else auto_share
            base = base if base is not None else 0.0
            allowances[d.mac] = round(base + (d.topup_gb or 0.0), 3)
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
        log.info("allowances recomputed (%d devices)", len(bundle.allowances))

    async def ensure_period(self) -> None:
        """Roll the period if stale, open if missing.

        ``reset_day <= 0`` disables the automatic roll: the period is opened
        once (on first boot) and afterwards only advances via an explicit
        admin action (:meth:`reset_month`).
        """
        bundle = await self.db.get_bundle()
        now = self._now()
        if bundle.reset_day <= 0:
            if not bundle.period_start:
                await self.open_period()
            return
        start, _ = timeutil.period_bounds(now, bundle.reset_day)
        if bundle.period_start == start.date().isoformat():
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

    async def evaluate_blocks(self) -> list[dict[str, Any]]:
        """Recompute every device's block state from usage vs allowance.

        Returns a list of changes: ``{device_id, mac, state, changed}``.
        Admin manual blocks are never overridden.
        """
        bundle = await self.db.get_bundle()
        allowances = bundle.allowances
        devices = await self.db.list_devices()
        period_usage = await self.db.get_period_usage()
        changes: list[dict[str, Any]] = []

        for dev in devices:
            if dev.block_state == _db.BLOCK_ADMIN:
                continue  # admin manual override stays until lifted
            usage = period_usage.get(dev.id, {"up": 0, "down": 0})
            used_gb = (usage["up"] + usage["down"]) / GB
            allowance_gb = allowances.get(dev.mac, -1.0)
            # A device with no assigned allowance (absent from the snapshot, or
            # an auto share that rounded to 0.0 under an over-subscribed bundle)
            # must NOT be quota-blocked at 0 usage. ``allowance_gb > 0`` treats
            # those as unmetered until the admin assigns a real allowance.
            new_state = _db.BLOCK_QUOTA if allowance_gb > 0 and used_gb >= allowance_gb else _db.BLOCK_OK
            if new_state != dev.block_state:
                await self.db.set_device_state(dev.id, new_state)
                changes.append({
                    "device_id": dev.id, "mac": dev.mac,
                    "state": new_state, "changed": True,
                })
        return changes

    async def snapshot_state(self) -> dict[str, dict[str, Any]]:
        """Produce the per-device view used by the packet engine and the UI.

        ``blocked`` is the single source of truth for enforcement.
        """
        devices = await self.db.list_devices()
        leases = {l.mac: l.ip for l in await self.db.list_leases()}
        usage = await self.db.get_period_usage()
        allowances = (await self.db.get_bundle()).allowances
        out: dict[str, dict[str, Any]] = {}
        for dev in devices:
            u = usage.get(dev.id, {"up": 0, "down": 0})
            used_gb = (u["up"] + u["down"]) / GB
            allowance = allowances.get(dev.mac, 0.0)
            out[dev.mac] = {
                "ip": leases.get(dev.mac, ""),
                "name": dev.name,
                "mode": dev.quota_mode,
                "allowance_gb": allowance,
                "used_gb": used_gb,
                "blocked": dev.block_state != _db.BLOCK_OK,
                "block_state": dev.block_state,
            }
        return out

    # -- admin operations ------------------------------------------------------

    async def top_up(self, device_id: int, extra_gb: float) -> Optional[dict[str, Any]]:
        """Increase a device's allowance by ``extra_gb`` for this period.

        The top-up is stored on the device row (``topup_gb``), so a later
        ``recompute_allowances``/``open_period`` rebuild of the snapshot does
        not discard it — the dashboard top-up flow survives.
        """
        if extra_gb <= 0:
            raise ValueError("extra_gb must be positive")
        dev = await self.db.get_device(device_id)
        if dev is None:
            return None
        await self.db.add_topup(device_id, extra_gb)
        await self.recompute_allowances()  # effective allowance = share + topup
        if dev.block_state == _db.BLOCK_QUOTA:
            await self.db.set_device_state(dev.id, _db.BLOCK_OK)
        bundle = await self.db.get_bundle()
        allowance = bundle.allowances.get(dev.mac, 0.0)
        await self.db.add_event(
            f"Top-up +{extra_gb:g} GB for '{dev.name}'", "warn", dev.id)
        return {"device_id": device_id, "mac": dev.mac, "allowance_gb": allowance}

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

    async def reset_month(self) -> None:
        """Force an early period roll-over (admin action or first boot)."""
        await self.open_period()
        await self.db.add_event("Monthly quota period reset", "info")
