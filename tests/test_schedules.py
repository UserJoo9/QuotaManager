"""Internet schedules: time math, validation, kernel programming, dnsmasq
fragment, service storage and the REST API."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from quota import schedules as sch
from quota.dns_rules import DnsRuleManager, render_schedule_sites
from quota.schedule_nft import ScheduleFirewall

MON = dt.date(2026, 11, 23)  # a Monday


def _s(windows, **kw):
    raw = {"name": "Bedtime", "users": [1], "windows": windows}
    raw.update(kw)
    return sch.normalize(raw, 1)


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm))


# -- validation ----------------------------------------------------------------

def test_parse_hhmm():
    assert sch.parse_hhmm("07:30") == 450
    assert sch.parse_hhmm("24:00") == 1440
    for bad in ("7", "25:00", "24:01", "ab:cd", "07:60", ""):
        with pytest.raises(ValueError):
            sch.parse_hhmm(bad)


@pytest.mark.parametrize("raw, msg", [
    ({"name": "", "users": [1], "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]}, "name"),
    ({"name": "x", "users": [], "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]}, "person"),
    ({"name": "x", "users": [1], "windows": []}, "window"),
    ({"name": "x", "users": [1], "windows": [{"days": [], "start": "1:00", "end": "2:00"}]}, "day"),
    ({"name": "x", "users": [1], "windows": [{"days": [7], "start": "1:00", "end": "2:00"}]}, "range"),
    ({"name": "x", "users": [1], "windows": [{"days": [0], "start": "3:00", "end": "3:00"}]}, "same"),
    ({"name": "x", "users": [1], "windows": [{"days": [0], "start": "24:00", "end": "3:00"}]}, "24:00"),
    ({"name": "x", "users": [True], "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]}, "whole"),
    ({"name": "x", "users": [1], "skip_dates": ["2026-02-30"],
      "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]}, "real date"),
])
def test_normalize_rejects(raw, msg):
    with pytest.raises(ValueError, match=msg):
        sch.normalize(raw, 1)


def test_normalize_end_midnight_and_dedupe():
    s = _s([{"days": [3, 1, 1], "start": "22:00", "end": "00:00"}])
    assert s.windows[0].days == (1, 3)
    assert s.windows[0].end == 1440  # "until 00:00" == until midnight
    assert s.to_dict()["windows"][0]["end"] == "24:00"


def test_old_skip_dates_are_dropped():
    s = sch.normalize({"name": "x", "users": [1],
                       "windows": [{"days": [0], "start": "1:00", "end": "2:00"}],
                       "skip_dates": ["2026-11-20", "2026-11-22", "2026-12-01"]},
                      1, today=MON)
    # yesterday stays (a window that began yesterday may still run today)
    assert s.skip_dates == ("2026-11-22", "2026-12-01")


def test_load_skips_broken_rows():
    good = _s([{"days": [0], "start": "1:00", "end": "2:00"}]).to_dict()
    assert [x.id for x in sch.load([good, {"id": 2, "name": ""}, "junk", None])] == [1]
    assert sch.load("not a list") == []


# -- time math ------------------------------------------------------------------

def test_simple_window():
    s = _s([{"days": [0], "start": "14:00", "end": "16:00"}])
    assert not sch.state(s, at(MON, 13, 59)).active
    st = sch.state(s, at(MON, 14, 0))
    assert st.active and st.until == at(MON, 16)
    assert not sch.state(s, at(MON, 16, 0)).active  # end is exclusive


def test_window_crossing_midnight_belongs_to_start_day():
    # Sunday 23:00 -> Monday 07:00 only (the window starts on Sunday)
    s = _s([{"days": [6], "start": "23:00", "end": "07:00"}])
    sun = MON - dt.timedelta(days=1)
    assert sch.state(s, at(sun, 23, 30)).active
    st = sch.state(s, at(MON, 6, 59))
    assert st.active and st.until == at(MON, 7)
    # Monday night is NOT part of it
    assert not sch.state(s, at(MON, 23, 30)).active


def test_touching_windows_merge_into_one_cut():
    s = _s([{"days": [0], "start": "22:00", "end": "24:00"},
            {"days": [1], "start": "00:00", "end": "07:00"}])
    st = sch.state(s, at(MON, 22, 30))
    assert st.active and st.until == at(MON + dt.timedelta(days=1), 7)


def test_skip_date_and_disabled():
    s = _s([{"days": [0], "start": "14:00", "end": "16:00"}],
           skip_dates=[MON.isoformat()])
    st = sch.state(s, at(MON, 15))
    assert not st.active and st.skipped_today
    # next start is next Monday
    assert st.next_start == at(MON + dt.timedelta(days=7), 14)
    off = _s([{"days": [0], "start": "14:00", "end": "16:00"}], enabled=False)
    assert not sch.state(off, at(MON, 15)).active


def test_bonus_time_pauses_the_cut():
    s = _s([{"days": [0], "start": "14:00", "end": "16:00"}])
    now = at(MON, 15)
    paused = sch.Schedule(**{**s.__dict__, "paused_until": now.timestamp() + 600})
    st = sch.state(paused, now)
    assert not st.active and st.paused
    later = sch.state(paused, now + dt.timedelta(minutes=11))
    assert later.active


def test_next_start_reported():
    s = _s([{"days": [2], "start": "08:00", "end": "09:00"}])
    st = sch.state(s, at(MON, 12))
    assert st.next_start == at(MON + dt.timedelta(days=2), 8)
    assert st.next_end == at(MON + dt.timedelta(days=2), 9)


def test_timezone_aware_now():
    tz = dt.timezone(dt.timedelta(hours=2))
    s = _s([{"days": [0], "start": "14:00", "end": "16:00"}])
    st = sch.state(s, at(MON, 15).replace(tzinfo=tz))
    assert st.active and st.until == at(MON, 16).replace(tzinfo=tz)


def test_resolve_targets_users_and_devices():
    devices = [SimpleNamespace(id=1, user_id=10, mac="AA:00:00:00:00:01"),
               SimpleNamespace(id=2, user_id=10, mac="aa:00:00:00:00:02"),
               SimpleNamespace(id=3, user_id=11, mac="aa:00:00:00:00:03"),
               SimpleNamespace(id=4, user_id=12, mac="00:00:00:00:00:00")]
    a = sch.normalize({"name": "A", "users": [10], "devices": [4],
                       "windows": [{"days": [0], "start": "14:00", "end": "16:00"}]}, 1)
    b = sch.normalize({"name": "B", "devices": [2],
                       "windows": [{"days": [0], "start": "15:00", "end": "18:00"}]}, 2)
    cuts = sch.resolve([a, b], devices, at(MON, 15, 30),
                       skip_macs={"00:00:00:00:00:00"})
    assert set(cuts) == {"aa:00:00:00:00:01", "aa:00:00:00:00:02"}
    assert cuts["aa:00:00:00:00:01"].until == at(MON, 16)
    # overlapping schedules: the later end wins
    assert cuts["aa:00:00:00:00:02"].until == at(MON, 18)
    assert [x["name"] for x in cuts["aa:00:00:00:00:02"].schedules] == ["A", "B"]


# -- kernel ---------------------------------------------------------------------

class FakeNft:
    def __init__(self, ok=True):
        self.scripts: list[str] = []
        self.ok = ok

    def __call__(self, script: str) -> bool:
        self.scripts.append(script)
        return self.ok


def test_base_table_rules_v4_v6_and_lan_excluded():
    nft = FakeNft()
    fw = ScheduleFirewall(["192.168.2.0/24", "bad"], ["fd00::/8", "fe80::/10"], nft)
    assert fw.ensure()
    base = nft.scripts[0]
    assert "add table inet quota_schedule" in base
    assert "devs4 { type ipv4_addr; flags timeout; }" in base
    assert "devs6 { type ipv6_addr; flags timeout; }" in base
    assert "priority -5" in base
    assert ("ip saddr @devs4 ip daddr != @allow4 ip daddr != 192.168.2.0/24 drop"
            in base)
    assert "ip6 saddr @devs6 ip6 daddr != @allow6 ip6 daddr != fd00::/8" in base
    assert "ether saddr @macs ip daddr != @allow4" in base
    # the table itself and its sets are never flushed (they must survive a
    # service restart); only the chain is rewritten
    assert "flush table" not in base and "flush set" not in base
    assert "bad" not in base
    fw.ensure()
    assert len(nft.scripts) == 1  # once per process


def test_sync_is_atomic_and_cache_gated():
    nft = FakeNft()
    fw = ScheduleFirewall(["192.168.2.0/24"], [], nft)
    now = 1_000_000.0
    cuts = {"AA:BB:CC:DD:EE:01": now + 3600, "00:00:00:00:00:00": now + 99,
            "not-a-mac": now + 99}
    ip_map = {"192.168.2.50": "aa:bb:cc:dd:ee:01", "fd00::50": "aa:bb:cc:dd:ee:01",
              "192.168.2.60": "aa:bb:cc:dd:ee:99", "junk": "aa:bb:cc:dd:ee:01"}
    assert fw.sync(cuts, ip_map, now)
    batch = nft.scripts[-1]
    # flush + re-add in ONE batch: no unblock window
    assert batch.index("flush set inet quota_schedule devs4") < batch.index("add element")
    assert "devs4 { 192.168.2.50 timeout 3605s }" in batch
    assert "devs6 { fd00::50 timeout 3605s }" in batch
    assert "macs { aa:bb:cc:dd:ee:01 timeout 3605s }" in batch
    assert "192.168.2.60" not in batch and "00:00:00:00:00:00" not in batch
    n = len(nft.scripts)
    assert fw.sync(cuts, ip_map, now + 15)  # unchanged -> no nft call
    assert len(nft.scripts) == n
    assert fw.sync({}, ip_map, now + 30)  # schedule over -> sets emptied
    assert "add element" not in nft.scripts[-1]
    assert "flush set inet quota_schedule macs" in nft.scripts[-1]


def test_sync_failure_retries_next_time():
    nft = FakeNft(ok=False)
    fw = ScheduleFirewall([], [], nft)
    assert not fw.sync({"aa:bb:cc:dd:ee:01": 2e9}, {}, 1e9)
    nft.ok = True
    assert fw.sync({"aa:bb:cc:dd:ee:01": 2e9}, {}, 1e9)
    assert any("add element" in s for s in nft.scripts)


def test_reset_allowed():
    nft = FakeNft()
    fw = ScheduleFirewall([], [], nft)
    assert fw.reset_allowed()
    assert "flush set inet quota_schedule allow4" in nft.scripts[-1]
    assert "flush set inet quota_schedule allow6" in nft.scripts[-1]


# -- dnsmasq ----------------------------------------------------------------------

def test_render_schedule_sites():
    text = render_schedule_sites(["*.Classroom.Google.com", "bad site!", "x.org", "x.org"])
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert lines == ["nftset=/classroom.google.com/x.org/"
                     "4#inet#quota_schedule#allow4,6#inet#quota_schedule#allow6"]
    many = render_schedule_sites([f"s{i}.example.com" for i in range(45)])
    assert sum(ln.startswith("nftset=") for ln in many.splitlines()) == 3
    assert "nftset" not in render_schedule_sites([])


def test_dns_manager_writes_schedule_file(tmp_path):
    m = DnsRuleManager(conf_dir=str(tmp_path), reload_dnsmasq=False)
    m.apply([], [], [], {}, None, ["school.edu"])
    assert "school.edu" in (tmp_path / "quota-schedule.conf").read_text()
    # None leaves it untouched (callers that know nothing about schedules)
    m.apply([], [], [], {}, None)
    assert "school.edu" in (tmp_path / "quota-schedule.conf").read_text()


# -- service + API ------------------------------------------------------------------

def _service_run(tmp_path, body):
    """Run ``body(db, service, now)`` against a fresh database."""
    import asyncio

    from quota.db import Database
    from quota.service import QuotaService

    async def main():
        db = Database(tmp_path / "q.db")
        await db.connect()
        now = {"t": dt.datetime(2026, 11, 23, 15, 0).astimezone()}
        try:
            await body(db, QuotaService(db, clock=lambda: now["t"]), now)
        finally:
            await db.close()
    asyncio.run(main())


async def _person(db, name, mac):
    return await db.upsert_device(mac, name=name)


def test_service_crud_and_cuts(tmp_path):
    async def body(db, service, now):
        dev = await _person(db, "Kid phone", "aa:bb:cc:00:00:01")
        other = await _person(db, "Dad", "aa:bb:cc:00:00:02")
        s = await service.save_schedule({
            "name": "Bedtime", "users": [dev.user_id],
            "windows": [{"days": [0], "start": "14:00", "end": "16:00"}]})
        assert s.id == 1
        cuts = await service.schedule_cuts()
        assert set(cuts) == {"aa:bb:cc:00:00:01"}
        assert other.mac not in cuts

        # bonus time: nobody cut, and an edit keeps the bonus
        await service.pause_schedule(1, 30)
        assert await service.schedule_cuts() == {}
        await service.save_schedule({**s.to_dict(), "name": "Night"}, 1)
        assert await service.schedule_cuts() == {}
        await service.pause_schedule(1, 0)
        assert set(await service.schedule_cuts()) == {"aa:bb:cc:00:00:01"}

        over = (await service.schedule_overview())[0]
        assert over["name"] == "Night" and over["state"]["active"]
        assert over["device_count"] == 1

        with pytest.raises(KeyError):
            await service.pause_schedule(99, 5)
        with pytest.raises(ValueError):
            await service.pause_schedule(1, 5000)
        assert await service.delete_schedule(1)
        assert not await service.delete_schedule(1)
        assert await service.schedule_cuts() == {}
    _service_run(tmp_path, body)


def test_service_refuses_gateway_and_cleans_missing(tmp_path):
    async def body(db, service, now):
        gw = next((u for u in await db.list_users() if u.protected), None)
        if gw is not None:
            with pytest.raises(ValueError, match="gateway"):
                await service.save_schedule({
                    "name": "x", "users": [gw.id],
                    "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]})
        dev = await _person(db, "Tab", "aa:bb:cc:00:00:03")
        s = await service.save_schedule({
            "name": "x", "users": [dev.user_id, 999], "devices": [998],
            "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]})
        assert s.users == (dev.user_id,) and s.devices == ()
        with pytest.raises(ValueError, match="person or device"):
            await service.save_schedule({
                "name": "y", "users": [999],
                "windows": [{"days": [0], "start": "1:00", "end": "2:00"}]})
    _service_run(tmp_path, body)


def test_service_sites(tmp_path):
    async def body(db, service, now):
        assert await service.set_schedule_sites(
            ["*.school.edu", "", "school.edu", "x.org"]) == ["school.edu", "x.org"]
        assert await service.schedule_sites() == ["school.edu", "x.org"]
        with pytest.raises(ValueError):
            await service.set_schedule_sites(["not a domain"])
    _service_run(tmp_path, body)


def test_corrupt_setting_is_harmless(tmp_path):
    async def body(db, service, now):
        await db.set_setting("schedules", "{oops")
        assert await service.list_schedules() == []
        assert await service.schedule_cuts() == {}
        await db.set_setting("schedules", json.dumps([{"id": 1, "name": ""}]))
        assert await service.list_schedules() == []
    _service_run(tmp_path, body)
