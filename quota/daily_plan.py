"""Daily quota planner — a pure module (no DB, no clock, no I/O).

A person's MONTHLY allowance stays the hard stop; the daily plan only decides
how fast it may be spent.  Everything here works on plain numbers (GB) and
``datetime.date`` days so it can be unit-tested exhaustively.

Vocabulary
----------
* ``mode``  ``fixed``  - X GB every day.
            ``spread`` - what is left of the month is divided equally over the
                         days that are left (today included).
* ``carry`` what happens to the GB a person did not use today:
            ``next_day`` - they move to tomorrow (and keep rolling forward);
            ``spread``   - they are redistributed over all remaining days.
``period_end`` everywhere is the reset date (first day of the next period).

* ``heavy`` {date: GB} - days the person wants to use more; every other day
            shares what is left equally.

The cap of a day is computed ONCE when the day starts (or when the plan /
allowance changes during the day) and then stays fixed for the rest of that
day, so a person never "earns" more GB by using the internet.  When it is
computed mid-day, what was already used today is deducted from the month and
today's share is still available on top of it (nobody is cut because the plan
was enabled).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

MODE_OFF = "off"
MODE_FIXED = "fixed"
MODE_SPREAD = "spread"
MODES = (MODE_OFF, MODE_FIXED, MODE_SPREAD)

CARRY_NEXT_DAY = "next_day"
CARRY_SPREAD = "spread"
CARRIES = (CARRY_NEXT_DAY, CARRY_SPREAD)

SPLIT_OPEN = "open"      # the devices share the person's pool
SPLIT_EQUAL = "equal"    # fair share: every device gets the same slice
SPLIT_FIXED = "fixed"    # fixed share: a slice per device (weights)
SPLITS = (SPLIT_OPEN, SPLIT_EQUAL, SPLIT_FIXED)

_EPS = 1e-9


@dataclass
class DailySettings:
    mode: str = MODE_OFF
    daily_gb: float = 0.0
    carry: str = CARRY_SPREAD
    split: str = SPLIT_OPEN
    #: ISO date -> GB for days with a bigger amount.
    heavy: dict[str, float] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.mode in (MODE_FIXED, MODE_SPREAD)


def shape_signature(settings: DailySettings) -> str:
    """Identifies the parts of a plan whose edit re-computes today's cap
    immediately (carry policy and heavy days)."""
    heavy = ",".join(f"{k}={v:g}" for k, v in sorted((settings.heavy or {}).items()))
    return f"{settings.carry}|{heavy}"


def parse_day(value: str | _dt.date) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(value)


def days_left(today: _dt.date, period_end: _dt.date | None) -> int:
    """Days remaining in the period, today included (>= 1).

    ``period_end`` is the first day of the NEXT period (the reset date, as
    stored in the bundle), so the last day of the period is the day before.
    """
    if period_end is None:
        return 1
    return max(1, (period_end - today).days)


def _future_heavy(settings: DailySettings, today: _dt.date,
                  period_end: _dt.date | None) -> dict[_dt.date, float]:
    out: dict[_dt.date, float] = {}
    for key, gb in (settings.heavy or {}).items():
        try:
            day = parse_day(key)
        except ValueError:
            continue
        if day < today or (period_end is not None and day >= period_end):
            continue
        if gb and gb > 0:
            out[day] = float(gb)
    return out


def plan_day(settings: DailySettings, *, allowance: float, used_before: float,
             used_today: float, today: _dt.date,
             period_end: _dt.date | None, bank: float = 0.0) -> float:
    """Cap (GB) for ``today``: the most the person may use in this day.

    ``used_before`` is the usage of the period up to (not including) today;
    ``used_today`` what was already used today when the cap is (re)computed.
    ``bank`` is the unused GB carried from earlier days (``next_day`` carry).
    The result never exceeds what is left of the month at the start of today.
    """
    month_left_at_start = max(0.0, allowance - used_before)
    month_left_now = max(0.0, month_left_at_start - used_today)
    bank = max(0.0, min(bank, month_left_now)) if settings.carry == CARRY_NEXT_DAY else 0.0

    heavy = _future_heavy(settings, today, period_end)
    today_heavy = heavy.get(today)

    if settings.mode == MODE_FIXED:
        share = today_heavy if today_heavy is not None else max(0.0, settings.daily_gb)
    else:  # spread
        n = days_left(today, period_end)
        other_heavy = {d: g for d, g in heavy.items() if d != today}
        h_total = sum(other_heavy.values())
        if today_heavy is not None:
            share = today_heavy
        else:
            n_normal = n - len(other_heavy)
            pool = max(0.0, month_left_now - bank - h_total)
            share = pool / n_normal if n_normal > 0 else 0.0
    cap = used_today + share + bank
    return round(max(0.0, min(cap, month_left_at_start)), 4)


def validate_heavy(settings: DailySettings, *, allowance: float,
                   used_before: float, used_today: float, today: _dt.date,
                   period_end: _dt.date | None) -> str | None:
    """Return an error text when the heavy days cannot fit in the month."""
    heavy = _future_heavy(settings, today, period_end)
    left = max(0.0, allowance - used_before - used_today)
    if sum(heavy.values()) > left + _EPS:
        return (f"heavy days need {sum(heavy.values()):.2f} GB but only "
                f"{left:.2f} GB are left in the period")
    if (settings.mode == MODE_SPREAD and period_end is not None):
        n = days_left(today, period_end)
        if n - len(heavy) <= 0 and sum(heavy.values()) < left - _EPS:
            return "every remaining day is marked heavy but GB would be left over"
    return None


def device_caps(person_cap: float, split: str, device_ids: list[int],
                shares: dict[int, float] | None = None) -> dict[int, float]:
    """Per-device caps for today (only devices that HAVE a cap are returned).

    ``open``  -> {} (the person cap alone applies)
    ``equal`` -> every device gets person_cap / number_of_devices
    ``fixed`` -> proportional to the device's fixed share; devices without a
                 share stay open (no individual cap).
    """
    if split == SPLIT_EQUAL and device_ids:
        each = person_cap / len(device_ids)
        return {d: round(each, 4) for d in device_ids}
    if split == SPLIT_FIXED:
        shares = {d: s for d, s in (shares or {}).items()
                  if d in device_ids and s and s > 0}
        total = sum(shares.values())
        if total > 0:
            return {d: round(person_cap * s / total, 4) for d, s in shares.items()}
    return {}


def preview(settings: DailySettings, *, allowance: float, used_before: float,
            used_today: float, today: _dt.date,
            period_end: _dt.date | None, cap_today: float,
            limit: int = 62) -> list[dict]:
    """Projected caps for the rest of the period, assuming each day is used
    exactly up to its cap (a realistic worst case).  Day 0 is today."""
    rows: list[dict] = []
    day = today
    before = used_before
    first = True
    bank = 0.0
    while len(rows) < limit:
        if first:
            cap = cap_today
            spent = max(cap_today, used_today)
        else:
            cap = plan_day(settings, allowance=allowance, used_before=before,
                           used_today=0.0, today=day, period_end=period_end,
                           bank=bank)
            spent = cap
        rows.append({"date": day.isoformat(), "cap_gb": round(cap, 3),
                     "heavy": day.isoformat() in (settings.heavy or {})})
        before += spent
        bank = 0.0
        first = False
        day += _dt.timedelta(days=1)
        if period_end is None or day >= period_end:
            break
        if before >= allowance - _EPS:
            break
    return rows


def settings_from_user(user) -> DailySettings:
    """Build settings from a ``db.User`` row (heavy days are filled by the caller)."""
    mode = getattr(user, "daily_mode", MODE_OFF) or MODE_OFF
    if mode not in MODES:
        mode = MODE_OFF
    carry = getattr(user, "daily_carry", CARRY_SPREAD) or CARRY_SPREAD
    if carry not in CARRIES:
        carry = CARRY_SPREAD
    split = getattr(user, "daily_split", SPLIT_OPEN) or SPLIT_OPEN
    if split not in SPLITS:
        split = SPLIT_OPEN
    return DailySettings(mode=mode, daily_gb=float(getattr(user, "daily_gb", 0.0) or 0.0),
                         carry=carry, split=split)
