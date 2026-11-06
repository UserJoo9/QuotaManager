"""Unit tests for the pure daily-quota planner."""
import datetime as dt

import pytest

from quota import daily_plan as dp

D = dt.date


def S(**kw):
    return dp.DailySettings(**kw)


def test_spread_equal_over_remaining_days():
    s = S(mode="spread")
    # 300 GB, 30 days left (today included), nothing used yet
    cap = dp.plan_day(s, allowance=300, used_before=0, used_today=0,
                      today=D(2026, 11, 1), period_end=D(2026, 12, 1))
    assert cap == 10


def test_spread_unused_is_redistributed_next_day():
    s = S(mode="spread", carry="spread")
    # day 1 used 4 of 10 -> 296 left over 29 days
    cap = dp.plan_day(s, allowance=300, used_before=4, used_today=0,
                      today=D(2026, 11, 2), period_end=D(2026, 12, 1))
    assert cap == pytest.approx(296 / 29, abs=1e-3)


def test_next_day_carry_rolls_forward_and_keeps_share_constant():
    s = S(mode="spread", carry="next_day")
    end = D(2026, 12, 1)
    # day 1 used 4 -> bank 6
    c2 = dp.plan_day(s, allowance=300, used_before=4, used_today=0,
                     today=D(2026, 11, 2), period_end=end, bank=6)
    assert c2 == pytest.approx(16, abs=1e-3)
    # day 2 unused -> bank 16, used_before still 4
    c3 = dp.plan_day(s, allowance=300, used_before=4, used_today=0,
                     today=D(2026, 11, 3), period_end=end, bank=16)
    assert c3 == pytest.approx(26, abs=1e-3)


def test_fixed_mode_and_clamped_by_month():
    s = S(mode="fixed", daily_gb=5)
    assert dp.plan_day(s, allowance=100, used_before=0, used_today=0,
                       today=D(2026, 11, 1), period_end=D(2026, 12, 1)) == 5
    # only 3 GB left in the month -> cap 3
    assert dp.plan_day(s, allowance=100, used_before=97, used_today=0,
                       today=D(2026, 11, 20), period_end=D(2026, 12, 1)) == 3


def test_fixed_without_period_end():
    s = S(mode="fixed", daily_gb=2)
    assert dp.plan_day(s, allowance=50, used_before=10, used_today=0,
                       today=D(2026, 11, 5), period_end=None) == 2


def test_enabling_mid_day_nobody_is_cut():
    s = S(mode="spread")
    # 300 allowance, 30 days left, already used 8 today: month left now 292
    cap = dp.plan_day(s, allowance=300, used_before=0, used_today=8,
                      today=D(2026, 11, 1), period_end=D(2026, 12, 1))
    assert cap == pytest.approx(8 + 292 / 30, abs=1e-3)
    assert cap > 8  # today's share is still available


def test_heavy_day_gets_more_rest_split_equally():
    s = S(mode="spread", heavy={"2026-11-10": 40})
    end = D(2026, 12, 1)
    # 30 days, one heavy of 40: normal = (300-40)/29
    normal = dp.plan_day(s, allowance=300, used_before=0, used_today=0,
                         today=D(2026, 11, 1), period_end=end)
    assert normal == pytest.approx(260 / 29, abs=1e-3)
    heavy = dp.plan_day(s, allowance=300, used_before=0, used_today=0,
                        today=D(2026, 11, 10), period_end=end)
    assert heavy == 40


def test_heavy_in_the_past_is_ignored():
    s = S(mode="spread", heavy={"2026-11-02": 99})
    cap = dp.plan_day(s, allowance=300, used_before=0, used_today=0,
                      today=D(2026, 11, 5), period_end=D(2026, 12, 1))
    assert cap == pytest.approx(300 / 26, abs=1e-3)


def test_last_day_gets_everything_left():
    s = S(mode="spread")
    cap = dp.plan_day(s, allowance=100, used_before=88, used_today=0,
                      today=D(2026, 11, 30), period_end=D(2026, 12, 1))
    assert cap == 12


def test_never_above_what_is_left_in_the_month():
    s = S(mode="spread", heavy={"2026-11-30": 500})
    cap = dp.plan_day(s, allowance=100, used_before=90, used_today=0,
                      today=D(2026, 11, 30), period_end=D(2026, 12, 1))
    assert cap == 10


def test_validate_heavy_rejects_overbooking():
    s = S(mode="spread", heavy={"2026-11-10": 80, "2026-11-12": 80})
    err = dp.validate_heavy(s, allowance=100, used_before=0, used_today=0,
                            today=D(2026, 11, 1), period_end=D(2026, 12, 1))
    assert err and "160.00" in err
    s2 = S(mode="spread", heavy={"2026-11-10": 30})
    assert dp.validate_heavy(s2, allowance=100, used_before=0, used_today=0,
                             today=D(2026, 11, 1), period_end=D(2026, 12, 1)) is None


def test_spread_caps_never_exceed_month_over_a_whole_period():
    s = S(mode="spread", carry="spread", heavy={"2026-11-07": 12})
    end = D(2026, 12, 1)
    rows = dp.preview(s, allowance=300, used_before=0, used_today=0,
                      today=D(2026, 11, 1), period_end=end,
                      cap_today=dp.plan_day(s, allowance=300, used_before=0,
                                            used_today=0, today=D(2026, 11, 1),
                                            period_end=end))
    assert len(rows) == 30
    assert sum(r["cap_gb"] for r in rows) <= 300 + 0.05
    assert rows[6]["heavy"] and rows[6]["cap_gb"] == 12


def test_device_caps_open_equal_fixed():
    assert dp.device_caps(10, "open", [1, 2]) == {}
    assert dp.device_caps(10, "equal", [1, 2]) == {1: 5, 2: 5}
    caps = dp.device_caps(10, "fixed", [1, 2, 3], {1: 3, 2: 1})
    assert caps == {1: 7.5, 2: 2.5}   # device 3 has no share -> stays open
    assert dp.device_caps(10, "fixed", [1, 2], {}) == {}


def test_settings_from_user_defaults():
    class U:  # minimal stand-in
        daily_mode = "bogus"
        daily_gb = None
        daily_carry = "x"
        daily_split = "y"
    s = dp.settings_from_user(U())
    assert s.mode == "off" and not s.active
    assert s.carry == "spread" and s.split == "open"


def test_days_left_handles_no_period_end():
    assert dp.days_left(D(2026, 11, 5), None) == 1
    assert dp.days_left(D(2026, 11, 30), D(2026, 12, 1)) == 1
    assert dp.days_left(D(2026, 11, 1), D(2026, 12, 1)) == 30
