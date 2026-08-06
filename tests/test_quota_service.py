"""Unit tests for the quota service (pure logic, no network/hardware)."""

from __future__ import annotations

import asyncio
import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from core import timeutil
from quota import db as _db
from quota.service import GB, QuotaService

TZ = ZoneInfo("Africa/Cairo")


def make_clock(now: _dt.datetime) -> callable:
    def _clock() -> _dt.datetime:
        return now
    return _clock


@pytest.fixture
def database(tmp_path):
    """In-memory-ish Database on a temp file, closed after the test."""
    d = _db.Database(tmp_path / "q.db")

    async def _connect():
        await d.connect()
        return d
    return _connect


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# timeutil: month-boundary math
# ---------------------------------------------------------------------------

def test_period_bounds_reset_day_1():
    now = _dt.datetime(2026, 8, 2, 15, tzinfo=TZ)
    start, end = timeutil.period_bounds(now, 1)
    assert start == _dt.datetime(2026, 8, 1, tzinfo=TZ)
    assert end == _dt.datetime(2026, 9, 1, tzinfo=TZ)


def test_period_bounds_reset_day_28_clamps_short_month():
    now = _dt.datetime(2026, 2, 25, tzinfo=TZ)  # Feb 2026 = 28 days
    start, end = timeutil.period_bounds(now, 28)
    assert start == _dt.datetime(2026, 2, 28, tzinfo=TZ)
    assert end == _dt.datetime(2026, 3, 28, tzinfo=TZ)


def test_period_bounds_december_rollover():
    now = _dt.datetime(2026, 12, 15, tzinfo=TZ)
    start, end = timeutil.period_bounds(now, 1)
    assert start == _dt.datetime(2026, 12, 1, tzinfo=TZ)
    assert end == _dt.datetime(2027, 1, 1, tzinfo=TZ)


def test_days_remaining():
    now = _dt.datetime(2026, 8, 2, tzinfo=TZ)
    assert timeutil.days_remaining(now, 1) == 30  # Aug 2 -> Sep 1


def test_next_reset():
    now = _dt.datetime(2026, 8, 2, tzinfo=TZ)
    assert timeutil.next_reset(now, 1) == _dt.datetime(2026, 9, 1, tzinfo=TZ)


# ---------------------------------------------------------------------------
# allowance math
# ---------------------------------------------------------------------------

def test_allowances_all_auto_share_equally(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # bundle 100 GB, 2 auto USERS -> 50 each
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        u1 = await d.create_user("A", _db.QUOTA_AUTO)
        u2 = await d.create_user("B", _db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        assert allowances == {u1.id: 50.0, u2.id: 50.0}
        await d.close()
    run(scenario())


def test_allowances_fixed_then_remainder_to_auto(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        parent = await d.create_user("Parent", _db.QUOTA_FIXED, 40.0)
        kid_a = await d.create_user("KidA", _db.QUOTA_AUTO)
        kid_b = await d.create_user("KidB", _db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        # remaining = 100 - 40 = 60 -> 30 each
        assert allowances[parent.id] == 40.0
        assert allowances[kid_a.id] == 30.0
        assert allowances[kid_b.id] == 30.0
        await d.close()
    run(scenario())


def test_allowances_fixed_exceeds_bundle_no_negative(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=20.0, reset_day=1))
        await d.create_user("X", _db.QUOTA_FIXED, 50.0)
        y = await d.create_user("Y", _db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        assert allowances[y.id] == 0.0  # never negative
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# period open / roll-over
# ---------------------------------------------------------------------------

def test_ensure_period_opens_on_first_run(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
        await svc.ensure_period()
        bundle = await d.get_bundle()
        assert bundle.period_start == "2026-08-01"
        assert bundle.period_end == "2026-09-01"
        await d.close()
    run(scenario())


def test_ensure_period_rolls_at_boundary(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 7, 20, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-07-01"

        # advance the clock past the boundary
        svc._clock = make_clock(_dt.datetime(2026, 8, 1, 0, 0, tzinfo=TZ))
        await svc.ensure_period()
        bundle = await d.get_bundle()
        assert bundle.period_start == "2026-08-01"
        assert bundle.period_end == "2026-09-01"
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# enforcement state
# ---------------------------------------------------------------------------

def test_block_when_usage_exceeds_allowance(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=1.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=u.id)
        await svc.open_period()  # allowance = 1 GB (single auto user)
        # use 1.5 GB
        await d.add_usage(dev.id, "2026-08-01", int(1.5 * GB), 0)
        changes = await svc.evaluate_blocks()
        assert len(changes) == 1
        assert changes[0]["state"] == _db.BLOCK_QUOTA
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_QUOTA
        await d.close()
    run(scenario())


def test_no_block_under_allowance(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=u.id)
        await svc.open_period()
        await d.add_usage(dev.id, "2026-08-01", int(0.5 * GB), 0)
        assert await svc.evaluate_blocks() == []
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


def test_admin_block_never_overridden(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=1.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=u.id)
        await d.set_device_state(dev.id, _db.BLOCK_ADMIN)
        await svc.open_period()
        await d.add_usage(dev.id, "2026-08-01", 10 * GB, 0)
        # even though over-quota, admin_off must persist
        changes = await svc.evaluate_blocks()
        assert changes == []
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_ADMIN
        await d.close()
    run(scenario())


def test_top_up_clears_quota_block(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=1.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=u.id)
        await svc.open_period()
        await d.add_usage(dev.id, "2026-08-01", int(1.5 * GB), 0)
        await svc.evaluate_blocks()
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_QUOTA
        # a device-level top-up raises the OWNING USER's allowance
        result = await svc.top_up(dev.id, 5.0)
        assert result is not None
        assert result["user_id"] == u.id
        assert result["allowance_gb"] >= 6.0
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


def test_snapshot_state_shape(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("Phone", _db.QUOTA_FIXED, 5.0)
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Phone", user_id=u.id)
        await svc.open_period()
        snap = await svc.snapshot_state()
        phone = snap["aa:aa:aa:aa:aa:01"]
        assert phone["mode"] == _db.QUOTA_FIXED
        assert phone["allowance_gb"] == 5.0
        assert phone["blocked"] is False
        assert phone["name"] == "Phone"
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# per-user model: one person owns several devices
# ---------------------------------------------------------------------------

def test_user_admin_block_cuts_all_devices_without_touching_rows(database):
    """A user-level admin cut reaches every device, but is never written to
    device rows (lossless — clearing the cut restores all devices)."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        d2 = await d.upsert_device("AA:AA:AA:AA:AA:02", "p2", user_id=u.id)
        await svc.open_period()
        assert await svc.evaluate_blocks() == []

        await svc.set_admin_block_user(u.id, True)
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["block_state"] == _db.BLOCK_ADMIN
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True
        assert snap["aa:aa:aa:aa:aa:02"]["blocked"] is True
        # no device rows touched: the fan-out is resolved, not persisted
        assert (await d.get_device(d1.id)).block_state == _db.BLOCK_OK
        assert (await d.get_device(d2.id)).block_state == _db.BLOCK_OK

        await svc.set_admin_block_user(u.id, False)
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is False
        assert snap["aa:aa:aa:aa:aa:02"]["blocked"] is False
        await d.close()
    run(scenario())


def test_user_quota_block_fans_out_to_all_devices(database):
    """Usage summed across a user's devices; one over-quota user cuts all of
    them, and each device reports the user's aggregate usage."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "p2", user_id=u.id)
        await svc.open_period()  # user allowance = 10 GB
        await d.add_usage(d1.id, "2026-08-01", int(10.5 * GB), 0)  # over the USER cap
        changes = await svc.evaluate_blocks()
        assert {c["mac"] for c in changes} == {"aa:aa:aa:aa:aa:01", "aa:aa:aa:aa:aa:02"}
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True
        assert snap["aa:aa:aa:aa:aa:02"]["blocked"] is True
        # usage aggregates per USER (the phone that used nothing is cut too)
        assert snap["aa:aa:aa:aa:aa:02"]["used_gb"] == pytest.approx(10.5)
        await d.close()
    run(scenario())


def test_device_bypass_exempts_from_user_quota(database):
    """A per-device bypass keeps one device online while its user is
    quota-blocked; an explicit per-device admin block still wins over bypass."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "p2", user_id=u.id)
        await svc.open_period()
        await d.add_usage(d1.id, "2026-08-01", int(10.5 * GB), 0)
        await svc.evaluate_blocks()
        assert (await d.get_device(d1.id)).block_state == _db.BLOCK_QUOTA

        await d.update_device(d1.id, bypass=True)
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is False, "bypass exempts"
        assert snap["aa:aa:aa:aa:aa:02"]["blocked"] is True, "sibling still blocked"

        # explicit per-device admin cut wins over bypass
        await d.set_device_state(d1.id, _db.BLOCK_ADMIN)
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# reset_day = 0  (no automatic reset; bundle is recharged mid-month)
# ---------------------------------------------------------------------------

def test_period_bounds_reset_day_0():
    now = _dt.datetime(2026, 8, 2, 15, tzinfo=TZ)
    start, end = timeutil.period_bounds(now, 0)
    assert start == now
    assert end == now + _dt.timedelta(days=1)
    assert timeutil.days_remaining(now, 0) == -1


def test_ensure_period_reset_day_0_opens_once_and_never_rolls(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=0))
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-08-02"
        assert b.period_end == ""  # no scheduled reset

        # later the same month: no roll
        svc._clock = make_clock(_dt.datetime(2026, 8, 20, tzinfo=TZ))
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-08-02"

        # next month: STILL no roll (must be manual via reset_month)
        svc._clock = make_clock(_dt.datetime(2026, 9, 5, tzinfo=TZ))
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-08-02"
        await d.close()
    run(scenario())


def test_reset_month_still_rolls_when_reset_day_0(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=0))
        await svc.ensure_period()
        svc._clock = make_clock(_dt.datetime(2026, 9, 5, tzinfo=TZ))
        await svc.reset_month()
        b = await d.get_bundle()
        assert b.period_start == "2026-09-05"
        await d.close()
    run(scenario())


def test_reset_month_mid_month_restarts_from_today(database):
    """Manual reset must start a fresh period TODAY even when the period
    already opened this month (reset_day>0) — the bug that made the button a
    silent no-op on the deployed gateway."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 5, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Phone", _db.QUOTA_AUTO)
        await svc.ensure_period()               # opened on Aug 1
        dev = await d.get_device(mac="aa:aa:aa:aa:aa:01")
        await d.add_usage(dev.id, "2026-08-03", 10_000_000_000, 0)

        svc._clock = make_clock(_dt.datetime(2026, 8, 5, tzinfo=TZ))
        await svc.reset_month()
        b = await d.get_bundle()
        assert b.period_start == "2026-08-05"   # counters restart from today
        assert b.period_end == "2026-09-01"     # next natural boundary
        # usage recorded before today is no longer part of the period
        assert await d.get_period_usage() == {}
        await d.close()
    run(scenario())


def test_reset_month_not_undone_by_ensure_period(database):
    """A mid-month manual reset must survive the maintenance loop until the
    next natural boundary (period_start is after the boundary, not equal)."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 5, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await svc.ensure_period()
        await svc.reset_month()                 # period_start = 2026-08-05
        # later same month AND the next month before the boundary: no roll
        svc._clock = make_clock(_dt.datetime(2026, 8, 20, tzinfo=TZ))
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-08-05"
        svc._clock = make_clock(_dt.datetime(2026, 8, 31, 23, 59, tzinfo=TZ))
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-08-05"
        # once the boundary passes, the next automatic roll takes over
        svc._clock = make_clock(_dt.datetime(2026, 9, 1, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-09-01"
        await d.close()
    run(scenario())


def test_reset_month_same_day_zeroes_usage(database):
    """reset_day=0 (manual mode): usage recorded on the reset day itself must
    be zeroed, or the counter can never drop below that day's total — the
    deployed symptom of resetting many times and still seeing 6.02 GB."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 4, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=0))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Phone", _db.QUOTA_AUTO)
        await svc.ensure_period()               # period opens today (Aug 4)
        dev = await d.get_device(mac="aa:aa:aa:aa:aa:01")
        await d.add_usage(dev.id, "2026-08-04", 6_000_000_000, 0)  # 6 GB today

        await svc.reset_month()                 # reset today
        b = await d.get_bundle()
        assert b.period_start == "2026-08-04"
        assert await d.get_period_usage() == {}  # today's 6 GB is gone — the fix
        await d.close()
    run(scenario())


def test_reset_month_zeroes_period_but_keeps_history(database):
    """clear_usage deletes only rows since the OLD period start — usage from
    before the period (historical) survives a manual reset."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 3, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=0))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Phone", _db.QUOTA_AUTO)
        await svc.ensure_period()               # period opened Aug 3
        dev = await d.get_device(mac="aa:aa:aa:aa:aa:01")
        await d.add_usage(dev.id, "2026-08-02", 1_000_000_000, 0)  # pre-period
        await d.add_usage(dev.id, "2026-08-03", 2_000_000_000, 0)  # in-period

        svc._clock = make_clock(_dt.datetime(2026, 8, 4, tzinfo=TZ))
        await svc.reset_month()                 # reset the next day
        assert (await d.get_bundle()).period_start == "2026-08-04"
        assert await d.get_period_usage() == {}  # in-period rows gone
        # the pre-period row survives (history preserved)
        rows = await d.conn.execute_fetchall(
            "SELECT date, up_bytes FROM usage_daily WHERE device_id=?",
            (dev.id,))
        assert [tuple(r) for r in rows] == [("2026-08-02", 1_000_000_000)]
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# bundle recharge (ISP top-up mid-month)
# ---------------------------------------------------------------------------

def test_recharge_grows_bundle_and_auto_shares(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=140.0, reset_day=1))
        fixed = await d.create_user("Fixed TV", _db.QUOTA_FIXED, 30.0)
        auto1 = await d.create_user("Auto1", _db.QUOTA_AUTO)
        auto2 = await d.create_user("Auto2", _db.QUOTA_AUTO)
        await svc.open_period()
        b = await d.get_bundle()
        assert b.allowances[auto1.id] == 55.0  # (140-30)/2
        period_start_before = b.period_start

        # ISP re-charge adds 50 GB -> auto share grows, fixed untouched
        result = await svc.recharge(50.0)
        b = await d.get_bundle()
        assert b.total_gb == 190.0
        assert result["added_gb"] == 50.0
        assert b.allowances[fixed.id] == 30.0       # fixed unchanged
        assert b.allowances[auto1.id] == 80.0       # (190-30)/2
        assert b.allowances[auto2.id] == 80.0
        assert b.period_start == period_start_before            # period NOT rolled
        await d.close()
    run(scenario())


def test_recompute_allowances_keeps_period_start(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await svc.open_period()
        start_before = (await d.get_bundle()).period_start
        late = await d.create_user("Late joiner", _db.QUOTA_AUTO)
        await svc.recompute_allowances()
        b = await d.get_bundle()
        assert b.period_start == start_before
        assert late.id in b.allowances
        assert b.period_end == "2026-09-01"
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# guest mode (period-scoped auto-registered fixed users)
# ---------------------------------------------------------------------------

def test_guest_settings_round_trip(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        assert await svc.is_guest_mode() is False   # default off
        assert await svc.guest_quota_gb() == 1.0    # default 1 GB

        await svc.set_guest_mode(True)
        assert await svc.is_guest_mode() is True
        await svc.set_guest_quota(3.5)
        assert await svc.guest_quota_gb() == 3.5
        await svc.set_guest_mode(False)
        assert await svc.is_guest_mode() is False
        # the quota survives disabling guest mode
        assert await svc.guest_quota_gb() == 3.5
        await d.close()
    run(scenario())


def test_shaping_settings_round_trip(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # defaults: off, no totals, AQM on
        cfg = await svc.get_shaping_config()
        assert cfg == {"enabled": False, "total_down_mbps": 0.0,
                       "total_up_mbps": 0.0, "aqm": True}

        # partial update — only the fields passed change
        cfg = await svc.set_shaping(enabled=True, total_down_mbps=100,
                                    total_up_mbps=20)
        assert cfg["enabled"] is True
        assert cfg["total_down_mbps"] == 100.0
        assert cfg["total_up_mbps"] == 20.0
        assert cfg["aqm"] is True           # untouched

        cfg = await svc.set_shaping(aqm=False)
        assert cfg["aqm"] is False          # totals + enabled survive
        assert cfg["enabled"] is True
        assert cfg["total_down_mbps"] == 100.0

        # negative values clamp to 0
        cfg = await svc.set_shaping(total_up_mbps=-5)
        assert cfg["total_up_mbps"] == 0.0

        # disable: master switch off, settings retained
        cfg = await svc.set_shaping(enabled=False)
        assert cfg["enabled"] is False
        assert cfg["total_down_mbps"] == 100.0
        await d.close()
    run(scenario())


def test_guest_quota_clamped_to_min(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await svc.set_guest_quota(0.01)
        assert await svc.guest_quota_gb() == 0.1    # never below the floor
        await d.close()
    run(scenario())


def test_guest_is_a_fixed_user_with_own_allowance(database):
    """A guest is an ordinary fixed user (1 GB by default); the auto user's
    share is the remainder after guests take their GB off the top.

    Guests are created AFTER the period opens (open_period wipes guests), so
    this exercises the steady-state math."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()          # a fresh period starts with no guests
        g = await d.create_user("", _db.QUOTA_FIXED, 1.0, guest=True)
        auto = await d.create_user("Dad", _db.QUOTA_AUTO)
        await svc.recompute_allowances()  # now the guest is in the period
        allowances = (await d.get_bundle()).allowances
        assert allowances[g.id] == 1.0        # guest takes its own slice
        assert allowances[auto.id] == 99.0    # auto gets the remainder
        await d.close()
    run(scenario())


def test_set_guest_quota_updates_existing_guest(database):
    """Changing the guest quota applies to guests already registered."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        g = await d.create_user("", _db.QUOTA_FIXED, 1.0, guest=True)
        await svc.recompute_allowances()
        assert (await d.get_user(g.id)).fixed_gb == 1.0

        await svc.set_guest_quota(4.0)
        assert (await d.get_user(g.id)).fixed_gb == 4.0
        assert (await d.get_bundle()).allowances[g.id] == 4.0
        await d.close()
    run(scenario())


def test_reset_month_deletes_guest_users(database):
    """A manual reset wipes guests that were present in the period."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 5, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=0))
        await svc.ensure_period()        # opens the period first (no guests yet)
        g = await d.create_user("", _db.QUOTA_FIXED, 1.0, guest=True)
        await d.upsert_device("AA:AA:AA:AA:AA:71", "Phone", user_id=g.id)
        await d.create_user("Dad", _db.QUOTA_FIXED, 20.0)
        await svc.recompute_allowances()
        assert len(await d.list_users()) == 2       # guest present before reset

        await svc.reset_month()
        users = await d.list_users()
        assert [u.name for u in users] == ["Dad"]       # guest wiped, Dad kept
        # the guest's device went with it (cascade)
        devs = await d.list_devices()
        assert [dev.mac for dev in devs] == []
        await d.close()
    run(scenario())


def test_open_period_deletes_guest_users_on_roll(database):
    """The AUTOMATIC month roll also starts with zero guests."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 7, 20, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.ensure_period()        # opens the July period
        g = await d.create_user("", _db.QUOTA_FIXED, 1.0, guest=True)
        await d.upsert_device("AA:AA:AA:AA:AA:72", "Phone", user_id=g.id)
        await svc.recompute_allowances()
        assert len(await d.list_users()) == 1       # guest lives in July

        # roll into August (open_period deletes the guests of the old period)
        svc._clock = make_clock(_dt.datetime(2026, 8, 1, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        assert await d.list_users() == []               # guest gone
        assert await d.list_devices() == []             # cascade removed the device
        await d.close()
    run(scenario())


def test_upsert_device_auto_creates_user(database):
    """A device registered without a user (DHCP auto-discover / manual add
    with user_id=None) owns a brand-new user carrying its name + quota."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        dev = await d.upsert_device("AA:AA:AA:AA:AA:07", "Phone", _db.QUOTA_FIXED, 20.0)
        assert dev.user_id is not None
        u = await d.get_user(dev.user_id)
        assert u is not None
        assert u.name == "Phone"
        assert u.quota_mode == _db.QUOTA_FIXED
        assert u.fixed_gb == 20.0
        await svc.open_period()
        assert (await d.get_bundle()).allowances[u.id] == 20.0
        await d.close()
    run(scenario())


def test_user_topup_aggregates(database):
    """Top-up is a USER-level grant: every device of the user benefits."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "p2", user_id=u.id)
        await svc.open_period()  # 10 GB
        await d.add_usage(d1.id, "2026-08-01", int(10.5 * GB), 0)
        await svc.evaluate_blocks()
        assert (await d.get_device(d1.id)).block_state == _db.BLOCK_QUOTA

        result = await svc.top_up_user(u.id, 5.0)
        assert result is not None
        assert result["allowance_gb"] >= 15.0
        # quota fan-out cleared on ALL of the user's devices
        for dev in await d.list_devices():
            assert dev.block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())
