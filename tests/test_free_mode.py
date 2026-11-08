"""Free mode: when everyone is out of quota and no pack exists, the box opens
for all; new GB closes it again and nothing used while open is deducted."""
from __future__ import annotations

import datetime as _dt

import pytest

from quota import db as _db
from quota.service import GB
from tests.test_quota_service import QuotaService, TZ, make_clock, run


@pytest.fixture
def database(tmp_path):
    d = _db.Database(tmp_path / "q.db")

    async def _connect():
        await d.connect()
        return d
    return _connect


def clock():
    return make_clock(_dt.datetime(2026, 8, 15, 12, 0, tzinfo=TZ))


async def _two_users(d, svc):
    # 1 GB goes to the protected Gateway, 20 GB are split: 10 GB each
    await d.set_bundle(_db.Bundle(total_gb=21.0, reset_day=1))
    a = await d.create_user("A", _db.QUOTA_AUTO)
    b = await d.create_user("B", _db.QUOTA_AUTO)
    da = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=a.id)
    db_ = await d.upsert_device("AA:AA:AA:AA:AA:02", "B", user_id=b.id)
    await svc.open_period()
    return a, b, da, db_


async def _state(d, dev):
    return (await d.get_device(dev.id)).block_state


def test_everyone_out_opens_the_box(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=clock())
        a, b, da, dbb = await _two_users(d, svc)
        await d.add_usage(da.id, "2026-08-15", int(11 * GB), 0)
        await svc.evaluate_blocks()
        assert await _state(d, da) == _db.BLOCK_QUOTA      # B still has GB: A is cut
        assert not svc.free_mode
        await d.add_usage(dbb.id, "2026-08-15", int(11 * GB), 0)
        await svc.evaluate_blocks()
        assert svc.free_mode
        assert await _state(d, da) == _db.BLOCK_OK
        assert await _state(d, dbb) == _db.BLOCK_OK
        snap = await svc.snapshot_state()
        assert not snap[da.mac]["blocked"] and not snap[dbb.mac]["blocked"]
        await d.close()
    run(scenario())


def test_pack_closes_it_again_and_free_usage_is_not_deducted(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=clock())
        a, b, da, dbb = await _two_users(d, svc)
        await d.add_usage(da.id, "2026-08-15", int(10 * GB), 0)
        await d.add_usage(dbb.id, "2026-08-15", int(10 * GB), 0)
        await svc.evaluate_blocks()
        assert svc.free_mode
        # 5 GB used while the box is open
        await d.add_usage(da.id, "2026-08-15", int(5 * GB), 0)
        await d.add_usage(dbb.id, "2026-08-15", int(5 * GB), 0)
        used = (await d.get_period_usage_by_user())[a.id]
        assert abs(used["up"] / GB - 10) < 0.01             # not counted
        # a 10 GB pack for A only
        await svc.add_recharge_pack(10.0, target_type="user", target_id=a.id)
        await svc.sync_recharges()
        await svc.evaluate_blocks()
        assert not svc.free_mode
        assert await _state(d, dbb) == _db.BLOCK_QUOTA      # B is cut again
        assert await _state(d, da) == _db.BLOCK_OK          # A has the whole pack
        pack = (await d.list_recharges(active_only=True))[0]
        assert pack.remaining_gb == 10.0                    # nothing was eaten
        await d.close()
    run(scenario())


def test_setting_off_keeps_the_old_behaviour(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=clock())
        a, b, da, dbb = await _two_users(d, svc)
        await d.set_setting("free_when_exhausted", "0")
        await d.add_usage(da.id, "2026-08-15", int(11 * GB), 0)
        await d.add_usage(dbb.id, "2026-08-15", int(11 * GB), 0)
        await svc.evaluate_blocks()
        assert not svc.free_mode
        assert await _state(d, da) == _db.BLOCK_QUOTA
        await d.close()
    run(scenario())


def test_unmetered_or_exempt_people_do_not_count_and_admin_cut_stays(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=clock())
        a, b, da, dbb = await _two_users(d, svc)
        await d.add_usage(da.id, "2026-08-15", int(11 * GB), 0)
        await d.add_usage(dbb.id, "2026-08-15", int(11 * GB), 0)
        await d.update_user(b.id, block_state=_db.BLOCK_ADMIN)   # admin cut: not "available"
        await svc.evaluate_blocks()
        assert svc.free_mode                      # A is the only metered person and is out
        snap = await svc.snapshot_state()
        assert not snap[da.mac]["blocked"]
        assert snap[dbb.mac]["blocked"]           # the admin cut is never lifted
        await d.close()
    run(scenario())


def test_new_period_ends_free_mode_and_manual_reset_too(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=clock())
        a, b, da, dbb = await _two_users(d, svc)
        await d.add_usage(da.id, "2026-08-15", int(10 * GB), 0)
        await d.add_usage(dbb.id, "2026-08-15", int(10 * GB), 0)
        await svc.evaluate_blocks()
        assert svc.free_mode
        await d.add_usage(da.id, "2026-08-31", int(3 * GB), 0)      # used while open
        # the period rolls over (Sept 1): fresh allowances, nobody is out
        svc._clock = make_clock(_dt.datetime(2026, 9, 1, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        await svc.evaluate_blocks()
        assert not svc.free_mode
        assert await _state(d, da) == _db.BLOCK_OK and await _state(d, dbb) == _db.BLOCK_OK
        used = (await d.get_period_usage_by_user()).get(a.id, {"up": 0, "down": 0})
        assert used["up"] == 0
        # exhaust again in the new month, then the manual "new period" button
        await d.add_usage(da.id, "2026-09-02", int(10 * GB), 0)
        await d.add_usage(dbb.id, "2026-09-02", int(10 * GB), 0)
        svc._clock = make_clock(_dt.datetime(2026, 9, 2, 12, 0, tzinfo=TZ))
        await svc.evaluate_blocks()
        assert svc.free_mode
        await svc.reset_month()
        await svc.evaluate_blocks()
        assert not svc.free_mode
        await d.close()
    run(scenario())
