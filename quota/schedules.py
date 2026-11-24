"""Internet schedules — a pure module (no DB, no clock, no I/O).

A schedule turns the internet OFF for chosen people and devices during chosen
hours of chosen weekdays ("bedtime 23:00-07:00 on school nights").

Vocabulary
----------
* ``window``      ``{days, start, end}``: days are Python weekdays (0 = Monday
                  ... 6 = Sunday), start/end are ``HH:MM``. ``end`` earlier than
                  ``start`` crosses midnight: ``23:00-07:00`` on Sunday runs from
                  Sunday 23:00 to Monday 07:00 (a window belongs to the day it
                  STARTS on). ``end`` = ``24:00`` means "until midnight".
* ``skip_dates``  ``YYYY-MM-DD`` days the schedule takes off (a holiday, Eid):
                  windows that START on such a day do not run.
* ``paused_until`` epoch seconds; a "bonus time" from the dashboard. While it is
                  in the future the schedule does not cut anyone.
* ``users`` / ``devices``  who is cut: every device of each user, plus single
                  devices.

Everything works on plain ``datetime`` values handed in by the caller, so it
can be unit-tested exhaustively.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

MAX_SCHEDULES = 50
MAX_WINDOWS = 20
MAX_SKIP_DATES = 60
MAX_TARGETS = 500
MAX_NAME = 40
MAX_PAUSE_MIN = 24 * 60
DAY_MIN = 24 * 60

_HHMM_RE = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_hhmm(value: str) -> int:
    """``"07:30"`` -> 450 minutes after midnight. ``"24:00"`` -> 1440."""
    m = _HHMM_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"time must be HH:MM, got {value!r}")
    minutes = int(m.group(1)) * 60 + int(m.group(2))
    if minutes > DAY_MIN:
        raise ValueError(f"time must be at most 24:00, got {value!r}")
    return minutes


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Window:
    days: tuple[int, ...]
    start: int  # minutes after midnight, 0..1439
    end: int    # minutes after midnight, 1..1440; < start = crosses midnight

    def to_dict(self) -> dict[str, Any]:
        return {"days": list(self.days), "start": fmt_hhmm(self.start),
                "end": fmt_hhmm(self.end)}


@dataclass(frozen=True)
class Schedule:
    id: int
    name: str
    enabled: bool = True
    users: tuple[int, ...] = ()
    devices: tuple[int, ...] = ()
    windows: tuple[Window, ...] = ()
    skip_dates: tuple[str, ...] = ()
    paused_until: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "users": list(self.users), "devices": list(self.devices),
            "windows": [w.to_dict() for w in self.windows],
            "skip_dates": list(self.skip_dates),
            "paused_until": self.paused_until,
        }


@dataclass
class ScheduleState:
    active: bool = False            # cutting right now
    until: Optional[_dt.datetime] = None       # when the current cut ends
    paused: bool = False            # a window is running but bonus time holds it
    next_start: Optional[_dt.datetime] = None  # next time a cut begins
    next_end: Optional[_dt.datetime] = None    # when that next cut ends
    skipped_today: bool = False     # today is one of the skip dates

    def to_dict(self) -> dict[str, Any]:
        iso = (lambda d: d.isoformat() if d is not None else None)
        ts = (lambda d: d.timestamp() if d is not None else None)
        return {"active": self.active, "paused": self.paused,
                "skipped_today": self.skipped_today,
                "until": iso(self.until), "until_ts": ts(self.until),
                "next_start": iso(self.next_start),
                "next_start_ts": ts(self.next_start),
                "next_end": iso(self.next_end), "next_end_ts": ts(self.next_end)}


def _int_list(raw: Any, what: str, lo: int, hi: int, limit: int) -> tuple[int, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{what} must be a list")
    out: set[int] = set()
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{what} must contain whole numbers")
        if not lo <= v <= hi:
            raise ValueError(f"{what} value {v} is out of range")
        out.add(v)
    if len(out) > limit:
        raise ValueError(f"too many {what} (max {limit})")
    return tuple(sorted(out))


def normalize_window(raw: Any) -> Window:
    if not isinstance(raw, dict):
        raise ValueError("each time window must be an object")
    days = _int_list(raw.get("days"), "days", 0, 6, 7)
    if not days:
        raise ValueError("pick at least one day for each time window")
    start = parse_hhmm(raw.get("start", ""))
    end = parse_hhmm(raw.get("end", ""))
    if start == DAY_MIN:
        raise ValueError("a time window cannot start at 24:00 (use 00:00)")
    if end == 0:
        end = DAY_MIN  # "until 00:00" = until midnight
    if start == end:
        raise ValueError("a time window must not start and end at the same time")
    return Window(days=days, start=start, end=end)


def normalize_dates(raw: Any, today: Optional[_dt.date] = None) -> tuple[str, ...]:
    """Validate ``YYYY-MM-DD`` strings; drop past dates when ``today`` is given."""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("skip dates must be a list")
    out: set[str] = set()
    for v in raw:
        s = str(v).strip()
        if not _DATE_RE.match(s):
            raise ValueError(f"date must be YYYY-MM-DD, got {v!r}")
        try:
            d = _dt.date.fromisoformat(s)
        except ValueError:
            raise ValueError(f"not a real date: {v!r}") from None
        # A window that started yesterday may still be running today, so
        # yesterday's skip stays until tomorrow.
        if today is not None and d < today - _dt.timedelta(days=1):
            continue
        out.add(s)
    if len(out) > MAX_SKIP_DATES:
        raise ValueError(f"too many skip dates (max {MAX_SKIP_DATES})")
    return tuple(sorted(out))


def normalize(raw: Any, sid: int, today: Optional[_dt.date] = None) -> Schedule:
    """Validate one schedule coming from the API or the database."""
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    name = " ".join(str(raw.get("name", "")).split())
    if not name:
        raise ValueError("give the schedule a name")
    if len(name) > MAX_NAME:
        raise ValueError(f"name is too long (max {MAX_NAME} characters)")
    windows_raw = raw.get("windows") or []
    if not isinstance(windows_raw, (list, tuple)):
        raise ValueError("windows must be a list")
    if len(windows_raw) > MAX_WINDOWS:
        raise ValueError(f"too many time windows (max {MAX_WINDOWS})")
    windows = tuple(normalize_window(w) for w in windows_raw)
    if not windows:
        raise ValueError("add at least one time window")
    users = _int_list(raw.get("users"), "users", 1, 2**31, MAX_TARGETS)
    devices = _int_list(raw.get("devices"), "devices", 1, 2**31, MAX_TARGETS)
    if not users and not devices:
        raise ValueError("pick at least one person or device")
    paused = raw.get("paused_until", 0) or 0
    try:
        paused = float(paused)
    except (TypeError, ValueError):
        paused = 0.0
    if paused != paused or paused < 0:  # NaN / negative
        paused = 0.0
    return Schedule(
        id=int(sid), name=name, enabled=bool(raw.get("enabled", True)),
        users=users, devices=devices, windows=windows,
        skip_dates=normalize_dates(raw.get("skip_dates"), today),
        paused_until=paused)


def load(data: Any) -> list[Schedule]:
    """Schedules stored as JSON -> objects. A broken entry is skipped, never
    fatal: one bad row must not stop every other schedule."""
    out: list[Schedule] = []
    if not isinstance(data, list):
        return out
    for raw in data:
        try:
            out.append(normalize(raw, int(raw.get("id", 0))))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


# -- time math -----------------------------------------------------------------

def _at(day: _dt.date, minutes: int, tz: Optional[_dt.tzinfo]) -> _dt.datetime:
    base = _dt.datetime.combine(day, _dt.time(0, 0))
    out = base + _dt.timedelta(minutes=minutes)
    return out.replace(tzinfo=tz) if tz is not None else out


def intervals_starting_on(s: Schedule, day: _dt.date,
                          tz: Optional[_dt.tzinfo] = None
                          ) -> list[tuple[_dt.datetime, _dt.datetime]]:
    """The cut intervals of ``s`` whose start falls on ``day``."""
    if day.isoformat() in s.skip_dates:
        return []
    out = []
    wd = day.weekday()
    for w in s.windows:
        if wd not in w.days:
            continue
        start = _at(day, w.start, tz)
        end_day = day if w.end > w.start else day + _dt.timedelta(days=1)
        end = _at(end_day, w.end, tz)
        out.append((start, end))
    return out


def merged_intervals(s: Schedule, first: _dt.date, days: int,
                     tz: Optional[_dt.tzinfo] = None
                     ) -> list[tuple[_dt.datetime, _dt.datetime]]:
    """Intervals starting on ``first`` .. ``first + days - 1``, sorted, with
    overlapping or touching intervals merged (22:00-24:00 + 00:00-07:00 is one
    cut 22:00-07:00, so the dashboard shows the real end)."""
    raw: list[tuple[_dt.datetime, _dt.datetime]] = []
    for i in range(days):
        raw.extend(intervals_starting_on(s, first + _dt.timedelta(days=i), tz))
    raw.sort()
    merged: list[tuple[_dt.datetime, _dt.datetime]] = []
    for a, b in raw:
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def state(s: Schedule, now: _dt.datetime) -> ScheduleState:
    """Is ``s`` cutting at ``now``, until when, and when does it cut next?"""
    st = ScheduleState(skipped_today=now.date().isoformat() in s.skip_dates)
    if not s.enabled:
        return st
    tz = now.tzinfo
    naive_now = now.replace(tzinfo=None) if tz is not None else now
    # yesterday (a window crossing midnight) .. 8 days ahead (next start)
    spans = merged_intervals(s, naive_now.date() - _dt.timedelta(days=1), 10, None)

    def aware(d: _dt.datetime) -> _dt.datetime:
        return d.replace(tzinfo=tz) if tz is not None else d

    paused = s.paused_until > now.timestamp()
    for a, b in spans:
        if a <= naive_now < b:
            if paused:
                st.paused = True
            else:
                st.active = True
                st.until = aware(b)
            break
    for a, b in spans:
        if a > naive_now:
            st.next_start, st.next_end = aware(a), aware(b)
            break
    return st


def target_device_ids(s: Schedule, devices: Iterable[Any]) -> set[int]:
    """Device ids ``s`` cuts: every device of its users + its single devices."""
    users = set(s.users)
    singles = set(s.devices)
    return {d.id for d in devices
            if d.id in singles or (d.user_id is not None and d.user_id in users)}


@dataclass
class Cut:
    until: _dt.datetime
    schedules: list[dict[str, Any]] = field(default_factory=list)


def resolve(schedules: Iterable[Schedule], devices: Iterable[Any],
            now: _dt.datetime, skip_macs: Iterable[str] = ()) -> dict[str, Cut]:
    """MAC -> the cut that applies right now (latest end wins when several
    schedules overlap)."""
    devices = list(devices)
    skip = {m.lower() for m in skip_macs}
    out: dict[str, Cut] = {}
    for s in schedules:
        st = state(s, now)
        if not st.active or st.until is None:
            continue
        ids = target_device_ids(s, devices)
        for d in devices:
            if d.id not in ids:
                continue
            mac = (d.mac or "").lower()
            if not mac or mac in skip:
                continue
            cut = out.get(mac)
            if cut is None:
                cut = out[mac] = Cut(until=st.until)
            elif st.until > cut.until:
                cut.until = st.until
            cut.schedules.append({"id": s.id, "name": s.name})
    return out
