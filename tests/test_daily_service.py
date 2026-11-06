"""Daily quota wired into the service (fake clock, real sqlite)."""
from __future__ import annotations

import asyncio
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


def at(day: int, hour: int = 12):
    return make_clock(_dt.datetime(2026, 8, day, hour, 0, tzinfo=TZ))


async def _setup(d, svc, *, total=70.0, **user_kw):
    """A bundle (Aug, reset day 1) with a single auto user owning one device."""
    await d.set_bundle(_db.Bundle(total_gb=total + 1.0, reset_day=1))
    u = await d.create_user("A", _db.QUOTA_AUTO)
    dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=u.id)
    await svc.open_period()
    if user_kw:
        await d.update_user(u.id, **user_kw)
    return u, dev


def test_off_by_default_never_blocks(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="spread")
        await d.add_usage(dev.id, "2026-08-15", int(30 * GB), 0)
        # master switch is off -> only the monthly quota counts
        assert await svc.daily_state() == {}
        await svc.evaluate_blocks()
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


def test_spread_blocks_at_cap_and_unblocks_after_midnight(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="spread")
        await d.set_setting("daily_quota_enabled", "1")
        # 70 GB, Aug 15..31 = 17 days left -> 4.1176 GB today
        st = (await svc.daily_state())[u.id]
        assert abs(st["cap_gb"] - 70 / 17) < 0.01
        await d.add_usage(dev.id, "2026-08-15", int(4.5 * GB), 0)
        assert (await svc.daily_state())[u.id]["blocked"]
        await svc.evaluate_blocks()
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_QUOTA
        snap = (await svc.snapshot_state())[dev.mac]
        assert snap["blocked"] and snap["daily_blocked"]
        # next day: a fresh cap from what is left -> unblocked
        svc._clock = at(16, 0)
        await svc.evaluate_blocks()
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_OK
        st2 = (await svc.daily_state())[u.id]
        assert abs(st2["cap_gb"] - (70 - 4.5) / 16) < 0.01
        await d.close()
    run(scenario())


def test_cap_is_frozen_for_the_day(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="spread")
        await d.set_setting("daily_quota_enabled", "1")
        c1 = (await svc.daily_state())[u.id]["cap_gb"]
        await d.add_usage(dev.id, "2026-08-15", int(2 * GB), 0)
        c2 = (await svc.daily_state())[u.id]["cap_gb"]
        assert c1 == c2   # using the internet never raises today's cap
        await d.close()
    run(scenario())


def test_enabling_mid_day_deducts_usage_and_keeps_todays_share(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="spread")
        await d.add_usage(dev.id, "2026-08-15", int(8 * GB), 0)   # before enabling
        await d.set_setting("daily_quota_enabled", "1")
        st = (await svc.daily_state())[u.id]
        assert abs(st["cap_gb"] - (8 + 62 / 17)) < 0.01
        assert not st["blocked"]          # nobody is cut by enabling
        await d.close()
    run(scenario())


def test_fixed_mode_and_next_day_carry(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="fixed", daily_gb=5.0,
                              daily_carry="next_day")
        await d.set_setting("daily_quota_enabled", "1")
        assert (await svc.daily_state())[u.id]["cap_gb"] == 5.0
        await d.add_usage(dev.id, "2026-08-15", int(2 * GB), 0)
        svc._clock = at(16, 1)
        assert abs((await svc.daily_state())[u.id]["cap_gb"] - 8.0) < 0.01  # 5 + 3 carried
        await d.close()
    run(scenario())


def test_device_booster_and_bypass_are_not_daily_blocked(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="fixed", daily_gb=1.0)
        await d.set_setting("daily_quota_enabled", "1")
        await svc.daily_state()
        await d.add_usage(dev.id, "2026-08-15", int(2 * GB), 0)
        await d.update_device(dev.id, bypass=True)
        await svc.daily_state()
        await svc.evaluate_blocks()
        assert (await d.get_device(dev.id)).block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


def test_equal_split_blocks_only_the_device_over_its_slice(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev1 = await _setup(d, svc, daily_mode="fixed", daily_gb=10.0,
                               daily_split="equal")
        dev2 = await d.upsert_device("AA:AA:AA:AA:AA:02", "B", user_id=u.id)
        await d.set_setting("daily_quota_enabled", "1")
        await svc.daily_state()                                    # day starts: cap 10
        await d.add_usage(dev1.id, "2026-08-15", int(6 * GB), 0)   # slice is 5
        await svc.evaluate_blocks()
        assert (await d.get_device(dev1.id)).block_state == _db.BLOCK_QUOTA
        assert (await d.get_device(dev2.id)).block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


def test_exempt_and_protected_users_are_never_paced(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="fixed", daily_gb=1.0,
                              exempt_quota=True)
        await d.set_setting("daily_quota_enabled", "1")
        assert await svc.daily_state() == {}
        await d.close()
    run(scenario())


def test_spread_without_period_end_is_ignored(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=0))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        await d.upsert_device("AA:AA:AA:AA:AA:01", "A", user_id=u.id)
        await svc.open_period()
        await d.update_user(u.id, daily_mode="spread")
        await d.set_setting("daily_quota_enabled", "1")
        assert await svc.daily_state() == {}
        await d.close()
    run(scenario())


def test_old_database_gets_daily_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"

    async def scenario():
        d = _db.Database(path)
        await d.connect()
        await d.close()
        con = sqlite3.connect(path)
        con.execute("ALTER TABLE users DROP COLUMN daily_mode")
        con.commit()
        con.close()
        d2 = _db.Database(path)
        await d2.connect()
        u = await d2.create_user("X", _db.QUOTA_AUTO)
        assert u.daily_mode == "off" and u.daily_heavy == {}
        await d2.update_user(u.id, daily_heavy={"2026-08-20": 9})
        assert (await d2.get_user(u.id)).daily_heavy == {"2026-08-20": 9.0}
        await d2.close()
    asyncio.run(scenario())


def test_editing_heavy_days_or_carry_applies_immediately_without_extra_gb(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="spread")
        await d.set_setting("daily_quota_enabled", "1")
        base = (await svc.daily_state())[u.id]["cap_gb"]            # 70/17
        await d.add_usage(dev.id, "2026-08-15", int(2 * GB), 0)
        # mark TODAY heavy: the cap changes right now
        await d.update_user(u.id, daily_heavy={"2026-08-15": 12.0})
        assert abs((await svc.daily_state())[u.id]["cap_gb"] - 12.0) < 0.01
        # remove it again, and again: always the same start-of-day basis
        for _ in range(3):
            await d.update_user(u.id, daily_heavy={})
            assert abs((await svc.daily_state())[u.id]["cap_gb"] - base) < 0.01
            await d.update_user(u.id, daily_carry="next_day")
            assert abs((await svc.daily_state())[u.id]["cap_gb"] - base) < 0.01
            await d.update_user(u.id, daily_carry="spread")
        await d.close()
    run(scenario())


def test_editing_after_usage_never_cuts_or_grants(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, clock=at(15))
        u, dev = await _setup(d, svc, daily_mode="spread")
        await d.add_usage(dev.id, "2026-08-15", int(8 * GB), 0)      # before enabling
        await d.set_setting("daily_quota_enabled", "1")
        cap = (await svc.daily_state())[u.id]["cap_gb"]              # 8 + 62/17
        await d.add_usage(dev.id, "2026-08-15", int(1 * GB), 0)
        await d.update_user(u.id, daily_heavy={"2026-08-20": 5.0})   # edit later in the day
        st = (await svc.daily_state())[u.id]
        assert st["cap_gb"] >= 8 + 1 and not st["blocked"]
        assert st["cap_gb"] <= cap + 0.5   # slightly more: 5 GB reserved for another day
        await d.close()
    run(scenario())
