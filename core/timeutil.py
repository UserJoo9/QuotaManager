"""Month-boundary math for quota periods.

The monthly bundle resets on a configurable day-of-month (``reset_day``,
1-31). A *period* is the half-open range ``[start, end)`` with ``end`` being
the next reset instant. Days remaining are counted on calendar days (the
reset boundary at 00:00), which is what a user sees on a dashboard.

``reset_day = 0`` means **no automatic reset** (the ISP bundle can be topped
up mid-month instead): periods are opened manually and only advance on an
explicit admin action. All functions below treat ``reset_day <= 0`` as "no
monthly boundary" and return a degenerate single-day window so callers never
divide by a zero-length month.
"""

from __future__ import annotations

import calendar
import datetime as _dt

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: ISO date format used by the dashboard / history tables.
ISO_DATE = "%Y-%m-%d"


def tz_for(tz_name: str) -> ZoneInfo:
    """Resolve a zoneinfo name, falling back to the local system timezone."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("localtime")


def _month_range(year: int, month: int, reset_day: int, tz: ZoneInfo) -> tuple[_dt.datetime, _dt.datetime]:
    """Return (start, end) datetimes for the period containing (year, month).

    The period starts at 00:00 of ``reset_day`` (or the 1st if ``reset_day``
    would overflow the month) and ends at the same instant of the next month.
    """
    start_day = min(reset_day, calendar.monthrange(year, month)[1])
    start = _dt.datetime(year, month, start_day, tzinfo=tz)

    # Advance to the next month.
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end_day = min(reset_day, calendar.monthrange(ny, nm)[1])
    end = _dt.datetime(ny, nm, end_day, tzinfo=tz)
    return start, end


def period_bounds(now: _dt.datetime, reset_day: int) -> tuple[_dt.datetime, _dt.datetime]:
    """Return (start, end) of the quota period containing ``now``.

    With ``reset_day <= 0`` there is no monthly boundary: the period is a
    degenerate single-day window (used only for a fresh open; the service
    layer keeps it open until an admin action).
    """
    if reset_day <= 0:
        return now, now + _dt.timedelta(days=1)
    # The period containing `now` starts at the most recent reset instant AT
    # OR BEFORE `now`. Before this month's reset day the current period began
    # LAST month on the reset day — anchoring to this month's grid instead
    # would skip the current month: with reset day 25 and today the 16th,
    # days-left read 40 and the maintenance loop rolled the period early
    # (zeroing the recorded usage) instead of counting down to the 25th.
    year, month = now.year, now.month
    if now.day < reset_day:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return _month_range(year, month, reset_day, now.tzinfo)


def next_reset(now: _dt.datetime, reset_day: int) -> _dt.datetime:
    """Datetime of the next quota reset strictly after ``now``."""
    if reset_day <= 0:
        return now + _dt.timedelta(days=1)
    return period_bounds(now, reset_day)[1]


def days_remaining(now: _dt.datetime, reset_day: int) -> int:
    """Calendar days from ``now``'s date to the next reset date (min 0).

    Returns ``-1`` for ``reset_day <= 0`` (no scheduled reset) so callers can
    render "manual" instead of a number.
    """
    if reset_day <= 0:
        return -1
    _, end = period_bounds(now, reset_day)
    end_date = end.date()
    delta = (end_date - now.date()).days
    return max(delta, 0)


def is_reset_instant(now: _dt.datetime, reset_day: int) -> bool:
    """True if ``now`` is (exactly) the boundary where a period rolls over."""
    start, _ = period_bounds(now, reset_day)
    return now == start
