"""Policy Scheduler — time-based policy evaluation.

Evaluates which policies are active at a given time, handling:
- Day-of-week schedules
- Time ranges
- Date ranges (start/end)
- Timezone handling
- Recurring schedules
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

log = logging.getLogger("quota.policy_scheduler")


class PolicyScheduler:
    """Evaluates policy schedules."""

    def __init__(self, timezone: str = "") -> None:
        self.tz = timezone

    def _now(self) -> _dt.datetime:
        if self.tz:
            import zoneinfo
            try:
                return _dt.datetime.now(zoneinfo.ZoneInfo(self.tz))
            except Exception:  # noqa: BLE001
                pass
        return _dt.datetime.now().astimezone()

    def is_active(self, schedule: dict[str, Any]) -> bool:
        """Check if a schedule is currently active."""
        if not schedule.get("enabled", False):
            return True  # no schedule = always active

        now = self._now()

        # Date range check
        start_date = schedule.get("start_date", "")
        end_date = schedule.get("end_date", "")
        if start_date:
            try:
                if now.date() < _dt.date.fromisoformat(start_date):
                    return False
            except ValueError:
                pass
        if end_date:
            try:
                if now.date() > _dt.date.fromisoformat(end_date):
                    return False
            except ValueError:
                pass

        # Day of week check
        days = schedule.get("days", [])
        if days:
            if now.weekday() not in days:
                return False

        # Time range check
        time_ranges = schedule.get("time_ranges", [])
        if time_ranges:
            active = False
            for tr in time_ranges:
                if len(tr) < 4:
                    continue
                start = now.replace(hour=tr[0], minute=tr[1], second=0, microsecond=0)
                end = now.replace(hour=tr[2], minute=tr[3], second=0, microsecond=0)
                if end < start:
                    end += _dt.timedelta(days=1)
                if start <= now <= end:
                    active = True
                    break
            if not active:
                return False

        return True

    def next_change(self, schedule: dict[str, Any]) -> _dt.datetime | None:
        """When does this schedule next change state? (for UI countdown)."""
        if not schedule.get("enabled", False):
            return None
        now = self._now()
        # Find next transition
        # This is a simplified version; full implementation would compute
        # exact transition times
        return None