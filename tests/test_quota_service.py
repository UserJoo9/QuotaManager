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
        # bundle 100 GB, 2 auto devices -> 50 each
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "B", _db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        assert allowances == {
            "aa:aa:aa:aa:aa:01": 50.0,
            "aa:aa:aa:aa:aa:02": 50.0,
        }
        await d.close()
    run(scenario())


def test_allowances_fixed_then_remainder_to_auto(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Parent", _db.QUOTA_FIXED, 40.0)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "KidA", _db.QUOTA_AUTO)
        await d.upsert_device("AA:AA:AA:AA:AA:03", "KidB", _db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        # remaining = 100 - 40 = 60 -> 30 each
        assert allowances["aa:aa:aa:aa:aa:01"] == 40.0
        assert allowances["aa:aa:aa:aa:aa:02"] == 30.0
        assert allowances["aa:aa:aa:aa:aa:03"] == 30.0
        await d.close()
    run(scenario())


def test_allowances_fixed_exceeds_bundle_no_negative(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=20.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "X", _db.QUOTA_FIXED, 50.0)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "Y", _db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        assert allowances["aa:aa:aa:aa:aa:02"] == 0.0  # never negative
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
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
        await svc.open_period()  # allowance = 1 GB
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
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
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
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
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
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
        await svc.open_period()
        await d.add_usage(dev.id, "2026-08-01", int(1.5 * GB), 0)
        await svc.evaluate_blocks()
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_QUOTA
        result = await svc.top_up(dev.id, 5.0)
        assert result is not None
        assert result["allowance_gb"] >= 6.0
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


def test_snapshot_state_shape(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Phone", _db.QUOTA_FIXED, 5.0)
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


# ---------------------------------------------------------------------------
# bundle recharge (ISP top-up mid-month)
# ---------------------------------------------------------------------------

def test_recharge_grows_bundle_and_auto_shares(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=140.0, reset_day=1))
        await d.upsert_device("AA:AA:AA:AA:AA:01", "Fixed TV", _db.QUOTA_FIXED, 30.0)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "Auto1", _db.QUOTA_AUTO)
        await d.upsert_device("AA:AA:AA:AA:AA:03", "Auto2", _db.QUOTA_AUTO)
        await svc.open_period()
        b = await d.get_bundle()
        assert b.allowances["aa:aa:aa:aa:aa:02"] == 55.0  # (140-30)/2
        period_start_before = b.period_start

        # ISP re-charge adds 50 GB -> auto share grows, fixed untouched
        result = await svc.recharge(50.0)
        b = await d.get_bundle()
        assert b.total_gb == 190.0
        assert result["added_gb"] == 50.0
        assert b.allowances["aa:aa:aa:aa:aa:01"] == 30.0       # fixed unchanged
        assert b.allowances["aa:aa:aa:aa:aa:02"] == 80.0       # (190-30)/2
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
        await d.upsert_device("AA:AA:AA:AA:AA:09", "Late joiner", _db.QUOTA_AUTO)
        await svc.recompute_allowances()
        b = await d.get_bundle()
        assert b.period_start == start_before
        assert "aa:aa:aa:aa:aa:09" in b.allowances
        assert b.period_end == "2026-09-01"
        await d.close()
    run(scenario())
