"""End-to-end smoke test of the run.py wiring (no admin privileges needed).

Builds a Gateway from config with the packet engine / DHCP / ARP subsystems
disabled (they need Administrator), then boots uvicorn and exercises the API +
WebSocket. Verifies the maintenance loop ticks and pushes enforcement state.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from core import config as cfg_mod
from run import Gateway


def _cfg(tmp_path) -> cfg_mod.Config:
    cfg = cfg_mod.Config()
    cfg.db_path = str(tmp_path / "data" / "smoke.db")
    cfg.log_file = str(tmp_path / "logs" / "smoke.log")
    cfg.dhcp.enable = False
    cfg.engine.enabled = False
    cfg.arp.enabled = False
    return cfg


def test_gateway_startup_shutdown():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        gw = Gateway(cfg)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(gw.startup())
            # DB connected + period opened
            bundle = loop.run_until_complete(gw.database.get_bundle())
            assert bundle.period_start, "period should be opened at startup"
            assert gw.holder is not None
            # maintenance tick pushes an empty-but-valid snapshot
            loop.run_until_complete(gw._maintenance_tick())
            snap = gw.holder.get()
            assert snap.blocked == {}
        finally:
            loop.run_until_complete(gw.shutdown())
            loop.close()


def test_config_yaml_seeds_bundle_on_first_boot():
    """config.yaml bundle values must reach the DB on a fresh install, so the
    UI and quota math show them instead of the hardcoded 140 GB / day 1."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.bundle.total_gb = 200.0
        cfg.bundle.reset_day = 0
        gw = Gateway(cfg)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(gw.startup())
            bundle = loop.run_until_complete(gw.database.get_bundle())
            assert bundle.total_gb == 200.0, "fresh DB must pick up config.yaml total_gb"
            assert bundle.reset_day == 0, "fresh DB must pick up config.yaml reset_day"
            # reset_day=0 -> period opened once, no automatic end
            assert bundle.period_start, "period should open on first boot"
            assert bundle.period_end == ""
        finally:
            loop.run_until_complete(gw.shutdown())
            loop.close()


def test_config_yaml_edit_reaches_db_on_reboot():
    """The bug: config.yaml only seeded the DB on the very first boot, so a
    later YAML edit never reached the dashboard. Now config.yaml is the
    default source and is re-applied on every boot."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.bundle.total_gb = 200.0
        cfg.bundle.reset_day = 0
        gw = Gateway(cfg)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(gw.startup())
        loop.run_until_complete(gw.shutdown())
        loop.close()

        # admin edits config.yaml...
        cfg.bundle.total_gb = 250.0
        cfg.bundle.reset_day = 5
        gw2 = Gateway(cfg)
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(gw2.startup())
            b2 = loop2.run_until_complete(gw2.database.get_bundle())
            assert b2.total_gb == 250.0, "config.yaml edit must reach the DB"
            assert b2.reset_day == 5
        finally:
            loop2.run_until_complete(gw2.shutdown())
            loop2.close()


def test_dashboard_edit_takes_ownership_of_bundle():
    """After the admin edits/recharges via the dashboard (bundle_source=
    dashboard), a restart must NOT re-apply config.yaml (which would wipe the
    dashboard value)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.bundle.total_gb = 200.0
        gw = Gateway(cfg)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(gw.startup())
        # simulate a dashboard edit (api/app.py /api/bundle sets this)
        loop.run_until_complete(gw.database.set_setting("bundle_source", "dashboard"))
        loop.run_until_complete(gw.shutdown())
        loop.close()

        cfg.bundle.total_gb = 250.0  # config.yaml changed, but dashboard owns it
        gw2 = Gateway(cfg)
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(gw2.startup())
            b2 = loop2.run_until_complete(gw2.database.get_bundle())
            assert b2.total_gb == 200.0, "dashboard value must survive a restart"
        finally:
            loop2.run_until_complete(gw2.shutdown())
            loop2.close()


def test_full_server_boot(tmp_path):
    """Boot the real uvicorn server via Gateway + create_app and hit it."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(gw.startup())

    from api.app import create_app
    app = create_app(gw.database, gw.service, gw.holder)

    with TestClient(app) as c:
        # run the maintenance loop manually (TestClient has no event loop task
        # for it; we already test _maintenance_tick separately)
        loop.run_until_complete(gw._maintenance_tick())

        c.post("/api/login", json={"password": "admin"})
        r = c.get("/api/dashboard")
        assert r.status_code == 200
        data = r.json()
        assert "bundle" in data and "devices" in data
        assert data["bundle"]["total_gb"] == 140.0
        assert data["total_devices"] == 0

        # UI served
        assert c.get("/").status_code == 200
        assert c.get("/assets/app.js").status_code == 200

    loop.run_until_complete(gw.shutdown())
    loop.close()


def test_lease_persists_and_device_auto_registered(tmp_path):
    """Simulate a DHCP lease: device should be auto-registered."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:11", "192.168.1.100"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:11"))
        assert dev is not None, "unknown MAC should be auto-registered"
        ip = asyncio.get_event_loop().run_until_complete(
            gw.database.get_ip_for_mac("aa:bb:cc:dd:ee:11"))
        assert ip == "192.168.1.100"
        assert "192.168.1.100" in gw._device_ips()
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_auto_registered_device_not_instantly_quota_blocked(tmp_path):
    """Regression: a brand-new DHCP device must receive its allowance BEFORE
    the next block evaluation.

    Prior bug: ``_persist_lease`` auto-registered the MAC but never recomputed
    allowances, so ``bundle.allowances[mac]`` stayed 0.0 and the very next
    ``evaluate_blocks`` run marked the device "quota exceeded" (used 0 >= 0).
    The engine then dropped every packet for that device — the phone showed
    "connected without internet" seconds after connecting.
    """
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        mac = "e6:2a:b3:09:b4:a8"  # the user's phone MAC
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.1.111"))

        bundle = asyncio.get_event_loop().run_until_complete(
            gw.database.get_bundle())
        assert mac in bundle.allowances, (
            "auto-registered MAC must appear in bundle.allowances")
        assert bundle.allowances[mac] > 0, (
            "new device must receive a positive share of the bundle")

        changes = asyncio.get_event_loop().run_until_complete(
            gw.service.evaluate_blocks())
        # the device must not be quota-blocked before using any data
        for ch in changes:
            assert not (ch.get("mac") == mac
                        and ch.get("state") == db_mod.BLOCK_QUOTA), (
                "new device must not be quota-blocked before using any data")
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_flush_counts_usage(tmp_path):
    """The maintenance tick drains engine counters into usage_daily."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        mac = "aa:bb:cc:dd:ee:22"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.1.101"))

        # Seed usage as if the maintenance tick had flushed it.
        gw.engine = None  # engine disabled in this test config
        asyncio.get_event_loop().run_until_complete(
            gw.database.add_usage(1, time.strftime("%Y-%m-%d"), 5 * 1024 ** 3, 0))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())

        usage = asyncio.get_event_loop().run_until_complete(
            gw.database.get_usage(1))
        assert usage["up_bytes"] >= 5 * 1024 ** 3
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_make_engine_selects_nftables_backend(tmp_path):
    """engine.backend=nftables must pick NftablesEngine even on Windows (and
    the default 'auto' picks WinDivert here)."""
    from run import _make_engine
    from quota.engine import PacketEngine
    from quota.nftables import NftablesEngine

    cfg = _cfg(tmp_path)
    cfg.engine.backend = "nftables"
    assert isinstance(_make_engine(cfg, None), NftablesEngine)

    cfg2 = _cfg(tmp_path)
    cfg2.engine.backend = "windivert"
    assert isinstance(_make_engine(cfg2, None), PacketEngine)


def test_sync_dnsmasq_leases_registers_devices(tmp_path):
    """Linux: reading dnsmasq's lease file auto-registers devices."""
    cfg = _cfg(tmp_path)
    lease_file = tmp_path / "dnsmasq.leases"
    lease_file.write_text(
        "1730000000 aa:bb:cc:dd:ee:33 192.168.1.111 phone1 01:aa:bb:cc:dd:ee:33\n"
        "1730000000 aa:bb:cc:dd:ee:44 192.168.1.112 laptop2 01:aa:bb:cc:dd:ee:44\n",
        encoding="utf-8")
    cfg.dhcp.lease_file = str(lease_file)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
        dev33 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:33"))
        assert dev33 is not None, "lease MAC must be auto-registered"
        ip44 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_ip_for_mac("aa:bb:cc:dd:ee:44"))
        assert ip44 == "192.168.1.112"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_sync_dnsmasq_leases_missing_file_is_safe(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.dhcp.lease_file = str(tmp_path / "does-not-exist.leases")
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_live_counters_flow_into_holder(tmp_path):
    """Regression: the holder's by_ip was hardcoded to {} so the dashboard's
    live up/down were always zero. The flushed engine delta must reach it."""
    from quota.engine import EngineCounters, EngineSnapshot

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        mac = "aa:bb:cc:dd:ee:55"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.1.113"))

        # Fake engine that returns a real delta on flush().
        class _FakeEngine:
            def flush(self) -> EngineSnapshot:
                return EngineSnapshot(
                    by_ip={"192.168.1.113": EngineCounters(up=1000, down=2000)},
                    ip_to_mac={"192.168.1.113": mac}, blocked={})
            def update_state(self, ip_to_mac, blocked):
                pass
            def stop(self):
                pass
        gw.engine = _FakeEngine()  # type: ignore[assignment]

        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        live = snap.counters_for(mac)
        assert live.up == 1000 and live.down == 2000, \
            "flushed engine deltas must reach the holder for the live UI"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())
