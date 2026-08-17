"""Unit tests for the quota service (pure logic, no network/hardware)."""

from __future__ import annotations

import asyncio
import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from core import timeutil
from quota import db as _db
from quota.engine import GATEWAY_MAC
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
    now = _dt.datetime(2026, 2, 28, tzinfo=TZ)  # ON the Feb 2026 (28-day) boundary
    start, end = timeutil.period_bounds(now, 28)
    assert start == _dt.datetime(2026, 2, 28, tzinfo=TZ)
    assert end == _dt.datetime(2026, 3, 28, tzinfo=TZ)


def test_period_bounds_reset_day_after_today_spans_previous_month():
    # Today is the 16th, reset day 25: the current period runs from the LAST
    # month's 25th to THIS month's 25th — the grid must never skip the current
    # month (days-left read 40 + a premature roll before the fix).
    now = _dt.datetime(2026, 8, 16, tzinfo=TZ)
    start, end = timeutil.period_bounds(now, 25)
    assert start == _dt.datetime(2026, 7, 25, tzinfo=TZ)
    assert end == _dt.datetime(2026, 8, 25, tzinfo=TZ)
    assert timeutil.days_remaining(now, 25) == 9  # Aug 16 -> Aug 25


def test_period_bounds_reset_day_after_today_january_wraps():
    now = _dt.datetime(2026, 1, 10, tzinfo=TZ)
    start, end = timeutil.period_bounds(now, 25)
    assert start == _dt.datetime(2025, 12, 25, tzinfo=TZ)
    assert end == _dt.datetime(2026, 1, 25, tzinfo=TZ)


def test_period_bounds_reset_day_on_boundary_starts_today():
    now = _dt.datetime(2026, 8, 25, 0, 5, tzinfo=TZ)
    start, end = timeutil.period_bounds(now, 25)
    assert start == _dt.datetime(2026, 8, 25, tzinfo=TZ)
    assert end == _dt.datetime(2026, 9, 25, tzinfo=TZ)
    assert timeutil.days_remaining(now, 25) == 31  # Aug 25 -> Sep 25


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
        # bundle 100 GB, 2 auto USERS -> 49.5 each (the protected Gateway user
        # is always seeded and takes 1.0 fixed off the top)
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        u1 = await d.create_user("A", _db.QUOTA_AUTO)
        u2 = await d.create_user("B", _db.QUOTA_AUTO)
        gw = next(u for u in await d.list_users() if u.protected)
        allowances = await svc.compute_allowances()
        assert allowances == {gw.id: 1.0, u1.id: 49.5, u2.id: 49.5}
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
        # remaining = 100 - 40 - 1.0 (seeded Gateway) = 59 -> 29.5 each
        assert allowances[parent.id] == 40.0
        assert allowances[kid_a.id] == 29.5
        assert allowances[kid_b.id] == 29.5
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
        # 2.0 GB so the auto user still gets 1.0 after the seeded Gateway's 1.0
        await d.set_bundle(_db.Bundle(total_gb=2.0, reset_day=1))
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
        # 2.0 GB so the auto user still gets 1.0 after the seeded Gateway's 1.0
        await d.set_bundle(_db.Bundle(total_gb=2.0, reset_day=1))
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


def test_deny_listed_mac_without_device_row_stays_blocked(database):
    """A deny-listed MAC with a LIVE LEASE but NO device row (the admin
    deleted the device/user — blacklist) must still appear in snapshot_state
    as blocked: run.py turns that into a kernel @blocked entry, so the
    still-connected device's internet stays cut. No allowance is reported and
    no usage ever accrues (run.py skips row-less MACs when draining)."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_FIXED, 5.0)
        dev = await d.upsert_device("AA:AA:AA:AA:AA:07", "p1", user_id=u.id)
        await svc.open_period()
        await d.upsert_lease("aa:aa:aa:aa:aa:07", "192.168.2.77", 24)
        # the delete blacklists the MAC: device row gone, deny entry written
        await d.add_mac_list("deny", ["aa:aa:aa:aa:aa:07"])
        await d.delete_device(dev.id)
        snap = await svc.snapshot_state()
        entry = snap.get("aa:aa:aa:aa:aa:07")
        assert entry is not None, "row-less deny-listed MAC must be in the map"
        assert entry["ip"] == "192.168.2.77"
        assert entry["blocked"] is True
        assert entry["block_state"] == _db.BLOCK_ADMIN
        assert entry["allowance_gb"] == 0.0
        assert entry["used_gb"] == 0.0
        # un-blacklisting removes the row-less entry (a fresh device row
        # re-registers on the next lease tick)
        await d.set_mac_list("deny", [])
        assert "aa:aa:aa:aa:aa:07" not in await svc.snapshot_state()
        await d.close()
    run(scenario())


def test_exempt_user_never_quota_blocked_in_snapshot_state(database):
    """The enforcement map (snapshot_state -> kernel blocked set) must honor
    the user's exempt_quota flag — a user marked "unlimited" is never
    quota-blocked there, while a manual admin cut still resolves."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_FIXED, 5.0, exempt_quota=True)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await svc.open_period()
        await d.add_usage(d1.id, "2026-08-01", int(10.5 * GB), 0)  # way over

        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is False, \
            "exempt user's device must not be quota-blocked in the enforcement map"

        # a manual admin cut still resolves through (exempt lifts quota only)
        await d.set_device_state(d1.id, _db.BLOCK_ADMIN)
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True
        await d.close()
    run(scenario())


def test_allowances_disabled_user_gets_zero_and_claims_no_share(database):
    """A DISABLED user (fresh auto-registered device awaiting the admin's
    shared/fixed assignment) takes NO allowance — not an auto share, nothing
    off the fixed total — until the admin assigns a rule."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        auto = await d.create_user("A", _db.QUOTA_AUTO)
        dis = await d.create_user("D", _db.QUOTA_DISABLED, 0.0)
        allowances = await svc.compute_allowances()
        assert allowances[dis.id] == 0.0
        # the disabled user claims nothing: A gets the full remainder
        # (100 - 1.0 seeded Gateway fixed user)
        assert allowances[auto.id] == 99.0
        # once the admin assigns shared, the pool re-splits normally
        await d.update_user(dis.id, quota_mode=_db.QUOTA_AUTO)
        allowances = await svc.compute_allowances()
        assert allowances[dis.id] == 49.5
        assert allowances[auto.id] == 49.5
        await d.close()
    run(scenario())


def test_user_quota_blocked_disabled_always_cut(database):
    """The onboarding lock is unconditional: a disabled user is quota-blocked
    at ANY usage/allowance — a top-up can't unlock it, the exemption flag
    can't lift it (the admin's positive shared/fixed assignment is the only
    way out)."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        dis = await d.create_user("D", _db.QUOTA_DISABLED, 0.0)
        assert svc.user_quota_blocked(dis, 0.0, 0.0) is True
        assert svc.user_quota_blocked(dis, 5.0, 0.0) is True  # top-up can't unlock
        await d.update_user(dis.id, exempt_quota=True)
        dis = await d.get_user(dis.id)
        assert svc.user_quota_blocked(dis, 0.0, 0.0) is True  # exemption loses
        # assigning the rule ends the lock: shared/fixed behave as before
        await d.update_user(dis.id, quota_mode=_db.QUOTA_AUTO, exempt_quota=False)
        dis = await d.get_user(dis.id)
        assert svc.user_quota_blocked(dis, 5.0, 0.0) is False
        assert svc.user_quota_blocked(dis, 5.0, 6.0) is True
        await d.close()
    run(scenario())


def test_disabled_user_device_cut_in_enforcement_map(database):
    """snapshot_state (the engine's blocked-set source) must kernel-cut a
    disabled user's device even with zero usage — the disabled state is a
    hard block, not a no-allowance "unmetered" corner."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("New", _db.QUOTA_DISABLED, 0.0)
        await d.upsert_device("AA:AA:AA:AA:AA:07", "p1", user_id=u.id)
        await svc.open_period()
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:07"]["blocked"] is True, \
            "disabled user's device must be cut (0 usage, 0 allowance)"

        # the admin assigns shared -> the device comes online (usage under share)
        await d.update_user(u.id, quota_mode=_db.QUOTA_AUTO)
        await svc.recompute_allowances()
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:07"]["blocked"] is False
        await d.close()
    run(scenario())


def test_mac_lists_round_trip_and_normalization(database):
    """set/get mac lists: lowercasing, dedupe, sorting; only the provided
    list is replaced."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        assert await svc.mac_lists() == {"allow": [], "deny": []}

        await svc.set_mac_list("allow", ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66",
                                         "aa:bb:cc:dd:ee:ff"])  # dup + case
        lists = await svc.mac_lists()
        assert lists["allow"] == ["11:22:33:44:55:66", "aa:bb:cc:dd:ee:ff"]
        assert lists["deny"] == []

        # replacing deny leaves allow untouched; blanks are dropped
        await svc.set_mac_list("deny", ["ee:ee:ee:ee:ee:ee", "  ", ""])
        lists = await svc.mac_lists()
        assert lists["deny"] == ["ee:ee:ee:ee:ee:ee"]
        assert lists["allow"] == ["11:22:33:44:55:66", "aa:bb:cc:dd:ee:ff"]

        # clearing a list works
        await svc.set_mac_list("allow", [])
        assert await svc.mac_lists() == {
            "allow": [], "deny": ["ee:ee:ee:ee:ee:ee"]}
        await d.close()
    run(scenario())


def test_mac_deny_list_always_blocks_even_when_user_ok(database):
    """A deny-listed MAC resolves BLOCK_ADMIN in the enforcement map even
    when the user is well under their allowance."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await svc.open_period()

        assert (await svc.snapshot_state())["aa:aa:aa:aa:aa:01"]["blocked"] is False
        await svc.set_mac_list("deny", ["AA:AA:AA:AA:AA:01"])
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True
        assert snap["aa:aa:aa:aa:aa:01"]["block_state"] == _db.BLOCK_ADMIN

        # removing the entry restores instantly (no device row was touched)
        await svc.set_mac_list("deny", [])
        assert (await svc.snapshot_state())["aa:aa:aa:aa:aa:01"]["blocked"] is False
        await d.close()
    run(scenario())


def test_stop_new_refused_macs_rowless_block_in_snapshot(database):
    """A STOP-NEW-refused MAC with a live lease but NO device row gets a
    row-less BLOCK_ADMIN entry in the enforcement map (the kernel keeps
    dropping its just-issued lease until it expires); clearing the refuse
    list restores it. Mirrors the deny-list row-less pass."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        await svc.open_period()
        await d.upsert_lease("AA:BB:CC:DD:EE:99", "192.168.2.199", 1)

        assert (await svc.snapshot_state()).get("aa:bb:cc:dd:ee:99") is None, \
            "no row-less block before the MAC is refused"
        assert await svc.add_refused_mac("AA:BB:CC:DD:EE:99") is True
        assert await svc.add_refused_mac("AA:BB:CC:DD:EE:99") is False, \
            "re-adding a refused MAC must be idempotent"
        snap = await svc.snapshot_state()
        entry = snap.get("aa:bb:cc:dd:ee:99")
        assert entry is not None and entry["blocked"] is True, \
            "the lingering lease of a refused MAC must be kernel-cut"
        assert entry["block_state"] == _db.BLOCK_ADMIN

        await svc.clear_refused_macs()
        assert not await svc.refused_macs()
        assert (await svc.snapshot_state()).get("aa:bb:cc:dd:ee:99") is None, \
            "clearing the refuse list must lift the row-less block"
        await d.close()
    run(scenario())


def test_decline_random_refused_macs_rowless_block_in_snapshot(database):
    """A Decline-random-refused MAC with a live lease but NO device row gets
    a row-less BLOCK_ADMIN entry in the enforcement map (the kernel keeps
    dropping its just-issued lease until it expires); clearing the refuse
    list restores it. Mirrors the STOP-NEW row-less pass."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        await svc.open_period()
        await d.upsert_lease("02:42:AC:11:00:99", "192.168.2.199", 1)

        assert (await svc.snapshot_state()).get("02:42:ac:11:00:99") is None, \
            "no row-less block before the MAC is refused"
        assert await svc.add_refused_random_mac("02:42:AC:11:00:99") is True
        assert await svc.add_refused_random_mac("02:42:AC:11:00:99") is False, \
            "re-adding a refused MAC must be idempotent"
        snap = await svc.snapshot_state()
        entry = snap.get("02:42:ac:11:00:99")
        assert entry is not None and entry["blocked"] is True, \
            "the lingering lease of a refused MAC must be kernel-cut"
        assert entry["block_state"] == _db.BLOCK_ADMIN

        await svc.clear_refused_random_macs()
        assert not await svc.refused_random_macs()
        assert (await svc.snapshot_state()).get("02:42:ac:11:00:99") is None, \
            "clearing the refuse list must lift the row-less block"
        await d.close()
    run(scenario())


def test_mac_allow_list_never_quota_blocked(database):
    """An allow-listed MAC stays online despite its user being way over the
    allowance; an explicit per-device admin cut still wins."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await d.upsert_device("AA:AA:AA:AA:AA:02", "p2", user_id=u.id)
        await svc.open_period()
        await d.add_usage(d1.id, "2026-08-01", int(10.5 * GB), 0)  # over the cap

        await svc.set_mac_list("allow", ["AA:AA:AA:AA:AA:01"])
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is False, \
            "allow-listed device must stay online despite the user's quota block"
        assert snap["aa:aa:aa:aa:aa:02"]["blocked"] is True, \
            "non-listed sibling of the same user is still cut"

        # an explicit per-device admin cut beats the allow list
        await d.set_device_state(d1.id, _db.BLOCK_ADMIN)
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True
        await d.close()
    run(scenario())


def test_mac_deny_list_wins_over_allow_and_bypass(database):
    """Precedence: deny list beats the allow list and a device's bypass."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=10.0, reset_day=1))
        u = await d.create_user("A", _db.QUOTA_AUTO)
        d1 = await d.upsert_device("AA:AA:AA:AA:AA:01", "p1", user_id=u.id)
        await svc.open_period()
        await d.add_usage(d1.id, "2026-08-01", int(10.5 * GB), 0)

        await d.update_device(d1.id, bypass=True)
        await svc.set_mac_list("allow", ["AA:AA:AA:AA:AA:01"])
        await svc.set_mac_list("deny", ["AA:AA:AA:AA:AA:01"])
        snap = await svc.snapshot_state()
        assert snap["aa:aa:aa:aa:aa:01"]["blocked"] is True, \
            "deny list must win over allow list + bypass"
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


def test_changing_reset_day_mid_month_does_not_roll_or_zero_usage(database):
    """Changing the reset day from 1 to 25 while today is the 16th must NOT
    skip the current month: the period stays open until the new period_end
    (Aug 25), the recorded usage keeps counting, and days left is 9. The old
    boundary heuristic read days-left=40 and rolled immediately, dropping the
    current month's usage from the period."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 2, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await svc.ensure_period()                       # opened Aug 1
        dev = await d.upsert_device("AA:AA:AA:AA:AA:01", "A", _db.QUOTA_AUTO)
        await d.add_usage(dev.id, "2026-08-05", 5_000_000_000, 0)

        # the admin changes the reset day to 25 while today is the 16th
        svc._clock = make_clock(_dt.datetime(2026, 8, 16, tzinfo=TZ))
        b = await d.get_bundle()
        b.reset_day = 25
        await d.set_bundle(b)
        await svc.recompute_allowances()                # re-anchors period_end, no roll
        bundle = await d.get_bundle()
        assert bundle.period_start == "2026-08-01"      # current month NOT skipped
        assert bundle.period_end == "2026-08-25"
        assert timeutil.days_remaining(
            _dt.datetime(2026, 8, 16, tzinfo=TZ), 25) == 9
        assert (await d.get_period_usage())[dev.id]["up"] == 5_000_000_000

        # the maintenance loop must not roll it away either
        await svc.ensure_period()
        bundle = await d.get_bundle()
        assert bundle.period_start == "2026-08-01"
        assert (await d.get_period_usage())[dev.id]["up"] == 5_000_000_000

        # only once the new boundary passes does the roll finally happen
        svc._clock = make_clock(_dt.datetime(2026, 8, 25, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-08-25"
        await d.close()
    run(scenario())


def test_ensure_period_reset_day_25_steady_state_never_rolls_mid_month(database):
    """With reset_day=25 in steady state, days 1-24 of the NEXT month belong
    to the same period — the maintenance loop must not re-roll the period
    every day before the boundary."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 25, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=25))
        await svc.ensure_period()                       # opened Aug 25
        assert (await d.get_bundle()).period_start == "2026-08-25"
        for day in (1, 5, 16, 24):
            svc._clock = make_clock(_dt.datetime(2026, 9, day, tzinfo=TZ))
            await svc.ensure_period()
            assert (await d.get_bundle()).period_start == "2026-08-25", \
                f"day {day} rolled the period early"
        svc._clock = make_clock(_dt.datetime(2026, 9, 25, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-09-25"
        await d.close()
    run(scenario())


def test_period_type_end_of_month_honors_configured_day(database):
    """end_of_month: the ISP's month-end day (25th here) drives the reset —
    the day input stays flexible. Never rolls mid-month; rolls on the day."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 16, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=25,
                                      period_type="end_of_month"))
        assert (await d.get_bundle()).effective_reset_day == 25
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-07-25"           # month-end day 25
        assert b.period_end == "2026-08-25"
        assert timeutil.days_remaining(
            _dt.datetime(2026, 8, 16, tzinfo=TZ), 25) == 9
        svc._clock = make_clock(_dt.datetime(2026, 8, 24, 23, 0, tzinfo=TZ))
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-07-25"
        svc._clock = make_clock(_dt.datetime(2026, 8, 25, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-08-25"
        assert b.period_end == "2026-09-25"
        await d.close()
    run(scenario())


def test_period_type_end_of_month_day_zero_uses_calendar_month(database):
    """end_of_month with no configured day (0) falls back to the calendar end:
    the period runs 1st -> 1st and resets automatically."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo",
                           clock=make_clock(_dt.datetime(2026, 8, 16, tzinfo=TZ)))
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=0,
                                      period_type="end_of_month"))
        assert (await d.get_bundle()).effective_reset_day == 1
        await svc.ensure_period()
        b = await d.get_bundle()
        assert b.period_start == "2026-08-01"
        assert b.period_end == "2026-09-01"
        svc._clock = make_clock(_dt.datetime(2026, 9, 1, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        assert (await d.get_bundle()).period_start == "2026-09-01"
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
        assert b.allowances[auto1.id] == 54.5  # (140-30-1.0 Gateway)/2
        period_start_before = b.period_start

        # ISP re-charge adds 50 GB -> auto share grows, fixed untouched
        result = await svc.recharge(50.0)
        b = await d.get_bundle()
        assert b.total_gb == 190.0
        assert result["added_gb"] == 50.0
        assert b.allowances[fixed.id] == 30.0       # fixed unchanged
        assert b.allowances[auto1.id] == 79.5       # (190-30-1.0 Gateway)/2
        assert b.allowances[auto2.id] == 79.5
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


def test_guest_limit_settings_round_trip(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        assert await svc.guest_limit() == 2        # default cap
        assert await svc.stop_new_connections() is False
        assert await svc.guest_speed_limit_mbps() == 0.0   # default unlimited

        await svc.set_guest_limit(4)
        assert await svc.guest_limit() == 4
        await svc.set_guest_limit(1)
        assert await svc.guest_limit() == 1
        await svc.set_guest_limit(0)              # clamped up to the floor
        assert await svc.guest_limit() == 1

        await svc.set_guest_speed_limit(8)
        assert await svc.guest_speed_limit_mbps() == 8.0
        await svc.set_guest_speed_limit(0)        # 0 lifts the cap
        assert await svc.guest_speed_limit_mbps() == 0.0
        await svc.set_guest_speed_limit(-3)       # clamped down to the floor
        assert await svc.guest_speed_limit_mbps() == 0.0

        await svc.set_stop_new_connections(True)
        assert await svc.stop_new_connections() is True
        await svc.set_stop_new_connections(False)
        assert await svc.stop_new_connections() is False
        await d.close()
    run(scenario())


def test_user_quota_blocked_respects_exempt_flag(database):
    """An exempt user is NEVER quota-blocked, whatever their usage — the
    exemption lifts the usage-vs-allowance gate only (manual admin cuts still
    resolve through resolve_device_state). Everyone else keeps the gate,
    including the protected Gateway account."""
    async def scenario():
        d = await database()
        normal = await d.create_user("A", _db.QUOTA_FIXED, 5.0)
        exempt = await d.create_user("E", _db.QUOTA_FIXED, 5.0,
                                     exempt_quota=True)
        assert normal.exempt_quota is False
        assert exempt.exempt_quota is True

        svc = QuotaService(d, timezone="Africa/Cairo")
        # over the allowance: normal blocked, exempt not
        assert svc.user_quota_blocked(normal, 5.0, 6.0) is True
        assert svc.user_quota_blocked(exempt, 5.0, 6.0) is False
        # under the allowance: nobody blocked
        assert svc.user_quota_blocked(normal, 5.0, 4.0) is False
        assert svc.user_quota_blocked(exempt, 5.0, 4.0) is False
        # allowance 0 = unmetered for a normal user; still unmetered when exempt
        assert svc.user_quota_blocked(normal, 0.0, 10.0) is False
        assert svc.user_quota_blocked(exempt, 0.0, 10.0) is False
        # the flag is editable: clearing it re-arms the quota gate
        await d.update_user(exempt.id, exempt_quota=False)
        unexempt = await d.get_user(exempt.id)
        assert unexempt.exempt_quota is False
        assert svc.user_quota_blocked(unexempt, 5.0, 6.0) is True
        await d.close()
    run(scenario())


def test_decline_random_macs_settings_and_is_random_mac(database):
    """The random-MAC gate: the is_random_mac helper (locally-administered
    bit), the settings round-trip, and the one-shot existing sweep that cuts
    already-joined randomized devices (a real-OUI device is never touched)."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # is_random_mac: IEEE's locally-administered bit (0x02 in the first
        # byte) is exactly what OSes set on a randomized/privacy MAC.
        assert svc.is_random_mac("02:42:ac:11:00:02") is True
        assert svc.is_random_mac("52:74:f2:b1:a8:7f") is True
        assert svc.is_random_mac("aa:bb:cc:dd:ee:ff") is True    # 0xaa & 0x02
        assert svc.is_random_mac("00:11:22:33:44:55") is False   # real OUI
        assert svc.is_random_mac("3c:7c:3f:aa:bb:cc") is False   # global only
        # a locally-administered MAC whose OUI IS a registered vendor prefix
        # is a real legacy product (3COM / DEC / Olivetti), never a randomize —
        # the sweep must NOT cut it.
        assert svc.is_random_mac("02:c0:8c:11:22:33") is False
        assert svc.is_random_mac("aa:00:00:12:34:56") is False
        assert svc.is_random_mac("02:aa:3c:01:02:03") is False
        assert svc.is_random_mac("") is False
        assert svc.is_random_mac("garbage") is False

        # settings round-trip
        assert await svc.decline_random_macs() is False
        await svc.set_decline_random_macs(True)
        assert await svc.decline_random_macs() is True
        await svc.set_decline_random_macs(False)
        assert await svc.decline_random_macs() is False

        # one-shot existing sweep: randomized devices cut, real OUIs untouched
        keep = await d.create_user("Keep", _db.QUOTA_FIXED, 10.0)
        cut = await d.create_user("Cut", _db.QUOTA_FIXED, 10.0)
        real_dev = await d.upsert_device("3c:7c:3f:aa:bb:cc", name="real",
                                         user_id=keep.id)
        legacy_dev = await d.upsert_device(
            "02:c0:8c:11:22:33", name="legacy-3com", user_id=keep.id)
        rand_dev = await d.upsert_device("02:42:ac:11:00:02", name="rand",
                                         user_id=cut.id)
        assert rand_dev.block_state == _db.BLOCK_OK
        assert real_dev.block_state == _db.BLOCK_OK
        assert legacy_dev.block_state == _db.BLOCK_OK
        await svc.set_decline_random_macs(True, also_existing=True)
        assert (await d.get_device(rand_dev.id)).block_state == _db.BLOCK_ADMIN, (
            "already-joined randomized device must be cut by the sweep")
        assert (await d.get_device(real_dev.id)).block_state == _db.BLOCK_OK, (
            "a real-OUI device is never touched by the sweep")
        assert (await d.get_device(legacy_dev.id)).block_state == _db.BLOCK_OK, (
            "a registered legacy OUI (locally administered but a real product) "
            "is never touched by the sweep")
        # the gate itself stays on after the sweep
        assert await svc.decline_random_macs() is True
        await d.close()
    run(scenario())


def test_shaping_settings_round_trip(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # defaults: off, no totals, AQM on
        cfg = await svc.get_shaping_config()
        assert cfg == {"enabled": False, "total_down_mbps": 0.0,
                       "total_up_mbps": 0.0, "aqm": True,
                       "lan_rate_mbps": 1000.0}

        # partial update — only the fields passed change
        cfg = await svc.set_shaping(enabled=True, total_down_mbps=100,
                                    total_up_mbps=20)
        assert cfg["enabled"] is True
        assert cfg["total_down_mbps"] == 100.0
        assert cfg["total_up_mbps"] == 20.0
        assert cfg["aqm"] is True           # untouched

        # LAN rate round-trips independently of the WAN totals
        cfg = await svc.set_shaping(lan_rate_mbps=250)
        assert cfg["lan_rate_mbps"] == 250.0
        assert cfg["total_down_mbps"] == 100.0   # WAN untouched
        cfg = await svc.set_shaping(lan_rate_mbps=0)
        assert cfg["lan_rate_mbps"] == 0.0

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


# ---------------------------------------------------------------------------
# WAN public-IP renewal (auto-renew schedule settings)
# ---------------------------------------------------------------------------

def test_wan_renew_config_round_trip(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # defaults: disabled, 15 min, never renewed
        cfg = await svc.get_wan_renew_config()
        assert cfg == {"enabled": False, "minutes": 15, "last": ""}

        cfg = await svc.set_wan_renew_config(True, 30)
        assert cfg["enabled"] is True
        assert cfg["minutes"] == 30
        assert cfg["last"] == ""

        cfg = await svc.set_wan_renew_config(False, 45)
        assert cfg["enabled"] is False
        assert cfg["minutes"] == 45     # interval survives disabling

        await svc.mark_wan_renew()
        cfg = await svc.get_wan_renew_config()
        assert cfg["minutes"] == 45
        assert cfg["last"] != ""        # ISO timestamp recorded
        from datetime import datetime
        datetime.fromisoformat(cfg["last"])  # parses as ISO
        await d.close()
    run(scenario())


def test_wan_renew_minutes_clamped_to_floor(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # the floor: every renewal drops internet, so a typo can't hammer the line
        cfg = await svc.set_wan_renew_config(True, 2)
        assert cfg["minutes"] == 5

        # no upper bound: any longer interval is allowed
        cfg = await svc.set_wan_renew_config(True, 1440)
        assert cfg["minutes"] == 1440

        # a corrupted setting falls back to the default
        await svc.db.set_setting(QuotaService.WAN_RENEW_MINUTES_KEY, "banana")
        cfg = await svc.get_wan_renew_config()
        assert cfg["minutes"] == 15
        await d.close()
    run(scenario())


def test_wan_renew_minutes_static_clamp():
    """The clamp helper: below floor -> floor, garbage -> default, big kept."""
    assert QuotaService._clamp_renew_minutes("1") == 5
    assert QuotaService._clamp_renew_minutes("99999") == 99999
    assert QuotaService._clamp_renew_minutes("") == 15
    assert QuotaService._clamp_renew_minutes("abc") == 15
    assert QuotaService._clamp_renew_minutes(None) == 15


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
        # auto = 100 - 1.0 guest - 1.0 seeded Gateway = 98.0
        assert allowances[auto.id] == 98.0
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
        # guest + Dad + the always-seeded protected Gateway user
        assert len(await d.list_users()) == 3

        await svc.reset_month()
        users = await d.list_users()
        # guest wiped; Dad + the protected Gateway user remain (name-sorted)
        assert [u.name for u in users] == ["Dad", "Gateway"]
        # the guest's device went with it (cascade); the Gateway device remains
        devs = await d.list_devices()
        assert [dev.mac for dev in devs] == [GATEWAY_MAC]
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
        # guest + the always-seeded protected Gateway user
        assert len(await d.list_users()) == 2

        # roll into August (open_period deletes the guests of the old period)
        svc._clock = make_clock(_dt.datetime(2026, 8, 1, 0, 5, tzinfo=TZ))
        await svc.ensure_period()
        assert [u.name for u in await d.list_users()] == ["Gateway"]  # guest gone
        assert [dev.mac for dev in await d.list_devices()] == [GATEWAY_MAC]
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
        # 9.0 auto share (10 - 1.0 seeded Gateway) + 5.0 top-up = 14.0
        assert result["allowance_gb"] >= 14.0
        # quota fan-out cleared on ALL of the user's devices
        for dev in await d.list_devices():
            assert dev.block_state == _db.BLOCK_OK
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# protected "Gateway" user: box accounting + 0-allowance cuts the box
# ---------------------------------------------------------------------------

def test_quota_blocked_for_protected_zero_allowance(database):
    """A PROTECTED user (the Gateway account) is blocked once usage reaches its
    allowance — even at 0. That is the product rule: setting the Gateway to 0
    GB cuts the box's own internet while clients keep working."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        gw_user = next(u for u in await d.list_users()
                       if getattr(u, "protected", False))
        # protected + 0 allowance -> blocked at ANY usage (even 0 bytes)
        assert svc.quota_blocked_for(gw_user, 0.0, 0.0) is True
        assert svc.quota_blocked_for(gw_user, 0.0, 5.0) is True
        # protected + a real allowance -> blocked once reached
        assert svc.quota_blocked_for(gw_user, 1.0, 0.5) is False
        assert svc.quota_blocked_for(gw_user, 1.0, 1.0) is True
        # a NORMAL user with 0 allowance stays UNMETERED (never auto-blocked)
        normal = await d.create_user("Dad", _db.QUOTA_AUTO)
        assert svc.quota_blocked_for(normal, 0.0, 100.0) is False
        assert svc.quota_blocked_for(normal, 5.0, 6.0) is True
        await d.close()
    run(scenario())


def test_is_setup_complete_ignores_gateway_user(database):
    """A fresh install (only the seeded protected Gateway user) is NOT setup-
    complete — the welcome panel must show. Any non-protected user flips it."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        # only the seeded Gateway user exists
        assert [u.name for u in await d.list_users()] == ["Gateway"]
        assert await svc.is_setup_complete() is False
        # adding any normal user completes setup
        await d.create_user("Dad", _db.QUOTA_FIXED, 20.0)
        assert await svc.is_setup_complete() is True
        await d.close()
    run(scenario())


def test_gateway_usage_counts_inside_quota_math(database):
    """The box's own traffic (via its device) flows into total usage and its
    fixed allowance takes its slice off the top — the machine is INSIDE the
    quota calculations, not invisible."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await svc.open_period()
        gw_user = next(u for u in await d.list_users()
                       if getattr(u, "protected", False))
        auto = await d.create_user("Kid", _db.QUOTA_AUTO)
        await svc.recompute_allowances()
        # 50 - 1.0 (Gateway) = 49.0 auto share
        assert (await d.get_bundle()).allowances[auto.id] == 49.0
        # box usage is charged to its own device
        box = await d.get_device(mac=GATEWAY_MAC)
        await d.add_usage(box.id, "2026-08-01", int(0.4 * GB), 0)
        await svc.evaluate_blocks()
        # 0.4 GB used of 1.0 -> box not quota-blocked yet
        assert svc.quota_blocked_for(gw_user, 1.0, 0.4) is False
        await d.close()
    run(scenario())


def test_evaluate_blocks_never_persists_quota_flag_on_gateway(database):
    """The box's own cut is user-resolved at render/enforcement time — a
    persisted ``quota`` block_state on the GATEWAY_MAC device row would
    desync the Gateway card's toggle (the v0.2.1 cosmetic fix)."""
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=50.0, reset_day=1))
        await svc.open_period()
        gw_user = next(u for u in await d.list_users()
                       if getattr(u, "protected", False))
        # drive the Gateway user over its allowance -> resolved blocked
        await d.update_user(gw_user.id, fixed_gb=0.5)
        await svc.recompute_allowances()  # refresh the allowance snapshot
        box = await d.get_device(mac=GATEWAY_MAC)
        await d.add_usage(box.id, "2026-08-01", int(0.9 * GB), 0)
        await svc.evaluate_blocks()
        refreshed = await d.get_device(mac=GATEWAY_MAC)
        assert refreshed.block_state == _db.BLOCK_OK, \
            "the box's quota cut must NOT be persisted onto its device row"
        # ... while the enforcement map (snapshot_state) still cuts it
        snap = await svc.snapshot_state()
        assert snap[GATEWAY_MAC]["blocked"] is True
        await d.close()
    run(scenario())


# ---------------------------------------------------------------------------
# milestone notifications (page-only, per-user)
# ---------------------------------------------------------------------------

def _fixed_user_with_usage(d, svc, name, allowance_gb, used_gb):
    """Create a fixed user + one device, record `used_gb` of usage today.

    ``svc.recompute_allowances()`` after creating the user so their allowance
    is in the snapshot (``open_period`` computed it before they existed).
    Usage must land ON the current period — a hardcoded date would fall
    before ``period_start`` and never count.
    """
    async def _inner():
        today = _dt.datetime.now(TZ).date().isoformat()
        u = await d.create_user(name, _db.QUOTA_FIXED, allowance_gb)
        dev = await d.upsert_device(
            f"de:ad:be:ef:{len(name):02x}:01", name=name, user_id=u.id)
        await d.add_usage(dev.id, today, int(used_gb * GB), 0)
        await svc.recompute_allowances()
        return u, dev
    return _inner


def test_milestone_state_crosses_and_once_only(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        # 40 GB fixed allowance, used 52% of it
        u, _ = await _fixed_user_with_usage(d, svc, "Mom", 40.0, 20.8)()
        ms = await svc.milestone_state()
        assert u.id in ms
        state = ms[u.id]["milestones"]
        assert state[50]["crossed"] is True
        assert state[50]["pending"] is True
        assert state[75]["crossed"] is False
        assert state[100]["crossed"] is False

        # acknowledge 50% -> once-only: not pending anymore, stays notified
        await svc.mark_milestone_notified(u.id, 50)
        ms = await svc.milestone_state()
        assert ms[u.id]["milestones"][50]["notified"] is True
        assert ms[u.id]["milestones"][50]["pending"] is False
        await d.close()
    run(scenario())


def test_milestone_state_75_pending_after_50_notified(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        # 80% of a 50 GB allowance
        u, _ = await _fixed_user_with_usage(d, svc, "Dad", 50.0, 40.0)()
        await svc.mark_milestone_notified(u.id, 50)
        ms = await svc.milestone_state()
        m = ms[u.id]["milestones"]
        assert m[50]["notified"] is True      # stays notified
        assert m[75]["crossed"] is True
        assert m[75]["pending"] is True       # new threshold surfaced
        assert m[100]["pending"] is False
        await d.close()
    run(scenario())


def test_milestone_state_skips_protected_gateway_user(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        gw = next(u for u in await d.list_users()
                  if getattr(u, "protected", False))
        ms = await svc.milestone_state()
        assert gw.id not in ms   # the box's own usage is the admin's concern
        await d.close()
    run(scenario())


def test_milestone_100_crossed_and_pending(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        u, _ = await _fixed_user_with_usage(d, svc, "Kid", 10.0, 12.0)()  # 120%
        ms = await svc.milestone_state()
        m = ms[u.id]["milestones"]
        assert all(m[th]["crossed"] and m[th]["pending"] for th in (50, 75, 100))
        await d.close()
    run(scenario())


def test_milestone_flags_reset_on_period_open(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        u, _ = await _fixed_user_with_usage(d, svc, "Sis", 30.0, 24.0)()  # 80%
        await svc.mark_milestone_notified(u.id, 50)
        await svc.mark_milestone_notified(u.id, 75)
        u = await d.get_user(u.id)
        assert u.notified_50 is True and u.notified_75 is True

        # period roll re-arms every flag
        await svc.open_period()
        u = await d.get_user(u.id)
        assert u.notified_50 is False
        assert u.notified_75 is False
        assert u.notified_100 is False
        await d.close()
    run(scenario())


def test_milestone_mark_invalid_value_raises(database):
    async def scenario():
        d = await database()
        svc = QuotaService(d, timezone="Africa/Cairo")
        await d.set_bundle(_db.Bundle(total_gb=100.0, reset_day=1))
        await svc.open_period()
        u = await d.create_user("Test", _db.QUOTA_FIXED, 10.0)
        try:
            await svc.mark_milestone_notified(u.id, 37)
            raise AssertionError("expected ValueError for milestone 37")
        except ValueError:
            pass
        await d.close()
    run(scenario())
