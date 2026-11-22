"""Deleting a device or a person never hands its usage back.

Regression: a person deleted one of their devices that had used ~30 GB and the
30 GB came back to the bundle (and to the person's allowance). The bytes left
the ISP bundle when they were used, so they must keep counting.
"""
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


async def _setup(d):
    svc = QuotaService(d, clock=clock())
    await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
    a = await d.create_user("A", _db.QUOTA_AUTO)
    phone = await d.upsert_device("AA:AA:AA:AA:AA:01", "phone", user_id=a.id)
    laptop = await d.upsert_device("AA:AA:AA:AA:AA:02", "laptop", user_id=a.id)
    await svc.open_period()
    await d.add_usage(phone.id, "2026-08-15", int(30 * GB), 0)
    await d.add_usage(laptop.id, "2026-08-15", int(5 * GB), 0)
    return svc, a, phone, laptop


def _user_gb(usage, uid):
    u = usage.get(uid, {"up": 0, "down": 0})
    return (u["up"] + u["down"]) / GB


def test_deleting_a_device_keeps_its_usage(database):
    async def scenario():
        d = await database()
        _svc, a, phone, _laptop = await _setup(d)
        assert await d.get_period_usage_total() == int(35 * GB)
        await d.delete_device(phone.id)
        # still counted for the bundle AND for the person who used it
        assert await d.get_period_usage_total() == int(35 * GB)
        assert _user_gb(await d.get_period_usage_by_user(), a.id) == pytest.approx(35)
        assert await d.get_user_usage_on(a.id, "2026-08-15") == int(35 * GB)
        day = await d.get_usage_before_and_on("2026-08-01", "2026-08-15")
        assert day["today"][a.id] == int(35 * GB)
        await d.close()
    run(scenario())


def test_deleting_a_person_keeps_the_bundle_usage(database):
    async def scenario():
        d = await database()
        _svc, a, phone, _laptop = await _setup(d)
        await d.delete_device(phone.id)            # removed first, then the person
        await d.delete_user(a.id, cascade=True)
        assert await d.get_period_usage_total() == int(35 * GB)
        assert a.id not in await d.get_period_usage_by_user()
        await d.close()
    run(scenario())


def test_dashboard_bundle_used_survives_device_delete(database):
    async def scenario():
        d = await database()
        svc, _a, phone, _laptop = await _setup(d)
        before = await d.get_period_usage_total()
        await d.delete_device(phone.id)
        await svc.evaluate_blocks()
        assert await d.get_period_usage_total() == before
        await d.close()
    run(scenario())


def test_free_mode_usage_still_excluded(database):
    async def scenario():
        d = await database()
        _svc, a, phone, _laptop = await _setup(d)
        await d.conn.execute(
            "INSERT INTO free_usage (device_id, date, up_bytes, down_bytes) VALUES (?,?,?,?)",
            (phone.id, "2026-08-15", int(10 * GB), 0))
        await d.conn.commit()
        assert await d.get_period_usage_total() == int(25 * GB)
        await d.delete_device(phone.id)
        assert await d.get_period_usage_total() == int(25 * GB)
        assert _user_gb(await d.get_period_usage_by_user(), a.id) == pytest.approx(25)
        await d.close()
    run(scenario())


def test_calibration_counts_deleted_devices(database):
    """Calibrating a person's usage works on their total, deleted devices
    included: "set to 20 GB" means 20 GB in total, "set to 0" really zeroes."""
    async def scenario():
        d = await database()
        svc, a, phone, laptop = await _setup(d)
        await d.delete_device(phone.id)                 # 30 GB stay with A
        await svc.spoof_consumption(a.id, "set", 20, log_event=False)
        assert _user_gb(await d.get_period_usage_by_user(), a.id) == pytest.approx(20, abs=1e-6)
        await svc.spoof_consumption(a.id, "set", 0, log_event=False)
        assert _user_gb(await d.get_period_usage_by_user(), a.id) == pytest.approx(0, abs=1e-6)
        assert await d.get_period_usage_total() == 0
        await d.close()
    run(scenario())
