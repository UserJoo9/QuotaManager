"""Unmanaged (static-IP) hosts: cut off automatically, adopt ("Manage") or allow."""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import EngineSnapshot, RogueHost, SnapshotHolder
from quota.service import QuotaService
from tests.test_run_wiring import _cfg
from run import Gateway

TZ = ZoneInfo("Africa/Cairo")
MAC = "ac:ac:ac:12:34:60"
IP = "192.168.2.18"


@pytest.fixture(autouse=True)
def _loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def _api(tmp_path, rogues=None):
    loop = asyncio.get_event_loop()
    database = _db.Database(tmp_path / "u.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()
    loop.run_until_complete(database.connect())
    holder.swap(EngineSnapshot(rogue=rogues if rogues is not None else
                               [RogueHost(ip=IP, mac=MAC, vendor="X", online=True)]))
    return database, service, create_app(database, service, holder)


def _login(c):
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_policy_defaults_on_and_toggles(tmp_path):
    database, service, app = _api(tmp_path)
    with TestClient(app) as c:
        _login(c)
        assert c.get("/api/unmanaged-policy").json() == {"enabled": True}
        d = c.get("/api/dashboard").json()
        assert d["unmanaged_block"] is True
        assert d["rogue"][0]["blocked"] is True and d["rogue"][0]["allowed"] is False
        assert c.post("/api/unmanaged-policy", json={"enabled": False}).json() == {"enabled": False}
        d = c.get("/api/dashboard").json()
        assert d["unmanaged_block"] is False and d["rogue"][0]["blocked"] is False


def test_allow_keeps_a_rogue_uncut(tmp_path):
    database, service, app = _api(tmp_path)
    with TestClient(app) as c:
        _login(c)
        assert c.post("/api/rogue/allow", json={"mac": MAC}).status_code == 200
        r = c.get("/api/dashboard").json()["rogue"][0]
        assert r["allowed"] is True and r["blocked"] is False
        assert c.post("/api/rogue/allow", json={"mac": "nope"}).status_code == 400


def test_adopt_registers_a_disabled_device_with_its_static_ip(tmp_path):
    database, service, app = _api(tmp_path)
    loop = asyncio.get_event_loop()
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/rogue/adopt", json={"mac": MAC, "name": "Printer"})
        assert r.status_code == 201, r.text
        assert r.json()["ip"] == IP                       # taken from the live scan
        dev = loop.run_until_complete(database.get_device(mac=MAC))
        assert dev is not None and dev.quota_mode == _db.QUOTA_DISABLED
        assert loop.run_until_complete(service.static_hosts()) == {MAC: IP}
        assert loop.run_until_complete(database.get_ip_for_mac(MAC)) == IP
        # adopting twice, a bad MAC and an unknown IP are all refused
        assert c.post("/api/rogue/adopt", json={"mac": MAC}).status_code == 409
        assert c.post("/api/rogue/adopt", json={"mac": "zz"}).status_code == 400
        assert c.post("/api/rogue/adopt", json={"mac": "aa:aa:aa:aa:aa:aa"}).status_code == 400


# --------------------------------------------------------------------------- #
# the maintenance tick: the cut itself
# --------------------------------------------------------------------------- #

def _boot():
    td = tempfile.TemporaryDirectory()
    gw = Gateway(_cfg(Path(td.name)))
    loop = asyncio.get_event_loop()
    loop.run_until_complete(gw.startup())
    task = getattr(gw, "_maintenance_task", None)
    if task is not None:           # the test owns the tick schedule
        task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    return td, gw, loop


def _stop(td, gw, loop):
    loop.run_until_complete(gw.shutdown())
    td.cleanup()


def test_tick_cuts_an_unmanaged_host_and_respects_policy_and_allow_list():
    td, gw, loop = _boot()
    try:
        gw._rogues = [RogueHost(ip=IP, mac=MAC, vendor="X", online=True)]
        loop.run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        assert snap.blocked.get(MAC) is True
        assert snap.ip_to_mac.get(IP) == MAC
        # policy off -> not cut
        loop.run_until_complete(gw.service.set_block_unmanaged(False))
        loop.run_until_complete(gw._maintenance_tick())
        assert MAC not in gw.holder.get().blocked
        # policy on again but the MAC is on the allow list -> not cut
        loop.run_until_complete(gw.service.set_block_unmanaged(True))
        loop.run_until_complete(gw.database.add_mac_list("allow", [MAC]))
        loop.run_until_complete(gw._maintenance_tick())
        assert MAC not in gw.holder.get().blocked
    finally:
        _stop(td, gw, loop)


def test_adopted_host_is_managed_not_cut_by_the_policy_and_keeps_its_ip():
    td, gw, loop = _boot()
    try:
        db = gw.database
        loop.run_until_complete(db.upsert_device(MAC, "Printer", _db.QUOTA_DISABLED, 0.0))
        loop.run_until_complete(gw.service.adopt_static_host(MAC, IP))
        gw._rogues = [RogueHost(ip=IP, mac=MAC, vendor="X", online=True)]
        for _ in range(2):   # the lease must survive the dnsmasq-lease prune
            loop.run_until_complete(gw._maintenance_tick())
        assert loop.run_until_complete(db.get_ip_for_mac(MAC)) == IP
        snap = gw.holder.get()
        assert snap.ip_to_mac.get(IP) == MAC
        # unassigned ("disabled") devices stay cut through their own state,
        # exactly like a DHCP device awaiting an allowance
        assert snap.blocked.get(MAC) is True
        # deleting the device releases the static binding
        dev = loop.run_until_complete(db.get_device(mac=MAC))
        loop.run_until_complete(db.delete_device(dev.id))
        loop.run_until_complete(gw._maintenance_tick())
        assert loop.run_until_complete(gw.service.static_hosts()) == {}
    finally:
        _stop(td, gw, loop)


def test_scanner_treats_adopted_hosts_as_known():
    td, gw, loop = _boot()
    try:
        seen = {}

        class FakeScanner:
            def scan(self, known):
                seen["known"] = set(known)
                return []
        gw.arp_scanner = FakeScanner()
        loop.run_until_complete(gw.service.adopt_static_host(MAC, IP))
        loop.run_until_complete(gw._scan_rogues())
        assert MAC in seen["known"]
    finally:
        _stop(td, gw, loop)


def test_cut_reaches_the_kernel_sets_by_ip_and_by_mac():
    """Tick output -> engine: the rogue's IP lands in `blocked` and its MAC in
    `blocked_macs` (the same sets every other cut device uses)."""
    from tests.test_nftables import FakeNft, _engine
    td, gw, loop = _boot()
    try:
        gw._rogues = [RogueHost(ip=IP, mac=MAC, vendor="X", online=True)]
        loop.run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        fake = FakeNft()
        eng = _engine(fake)
        eng.start()
        eng.update_state(dict(snap.ip_to_mac), dict(snap.blocked))
        assert IP in fake.blocked
        assert MAC in fake.blocked_macs
    finally:
        _stop(td, gw, loop)
