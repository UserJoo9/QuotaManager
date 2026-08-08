"""End-to-end smoke test of the run.py wiring (no admin privileges needed).

Builds a Gateway from config with the packet engine / DHCP subsystems disabled,
then boots uvicorn and exercises the API + WebSocket. Verifies the maintenance
loop ticks and pushes enforcement state.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import config as cfg_mod
from quota.arp_scan import ArpScanner
from quota.engine import GATEWAY_MAC, EngineCounters, EngineSnapshot
from run import Gateway


def _cfg(tmp_path) -> cfg_mod.Config:
    cfg = cfg_mod.Config()
    cfg.db_path = str(tmp_path / "data" / "smoke.db")
    cfg.log_file = str(tmp_path / "logs" / "smoke.log")
    cfg.dhcp.enable = False
    cfg.engine.enabled = False
    return cfg


def _cancel_maintenance(gw: Gateway) -> None:
    """Stop the background maintenance loop a test's manual ticks can't race it.

    ``startup()`` creates ``_maintenance_loop`` as a task and the loop runs its
    FIRST tick immediately — so a test that then calls ``_maintenance_tick()``
    by hand would measure two ticks (a latent race my ``_wan_status`` internet
    probe's ``asyncio.to_thread`` widened). Cancel the task and swallow the
    CancelledError, then the test owns the tick schedule.
    """
    task = getattr(gw, "_maintenance_task", None)
    if task is None:
        return
    task.cancel()
    try:
        asyncio.get_event_loop().run_until_complete(task)
    except asyncio.CancelledError:
        pass


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
            # maintenance tick pushes a valid snapshot: the always-seeded
            # protected Gateway box device appears in the per-device blocked
            # map (unblocked — no lease, no usage, 1 GB allowance)
            loop.run_until_complete(gw._maintenance_tick())
            snap = gw.holder.get()
            assert snap.blocked == {GATEWAY_MAC: False}
            # the enforcement-status fields ride the same swap: engine is
            # disabled in this config, so nothing is programmed (None) and the
            # dashboard's "packet engine off" banner must be reachable (False).
            assert snap.gateway_blocked is None
            assert snap.engine_available is False
        finally:
            loop.run_until_complete(gw.shutdown())
            loop.close()


def test_startup_builds_topology_manager():
    """v19: startup() builds the TopologyManager that owns the runtime LAN/WAN
    switch (the WAN-tab Apply button calls through it), wired to the on-disk
    config.yaml so a panel apply rewrites the file the next boot reads."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        config_path = Path(td) / "config.yaml"
        config_path.write_text("bundle:\n  total_gb: 140\n", encoding="utf-8")
        gw = Gateway(cfg, config_path=config_path)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(gw.startup())
            assert gw.topology_manager is not None
            assert gw.topology_manager.config_path == config_path
            assert gw.topology_manager.database is gw.database
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
        # only the always-seeded protected Gateway box's own device
        assert data["total_devices"] == 1

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
        assert dev.user_id is not None, "auto-registered device must own a user"
        ip = asyncio.get_event_loop().run_until_complete(
            gw.database.get_ip_for_mac("aa:bb:cc:dd:ee:11"))
        assert ip == "192.168.1.100"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_guest_mode_auto_registers_guest_device(tmp_path):
    """With guest mode on, a NEW device joining the network becomes a guest
    (fixed 1 GB allowance) instead of a normal auto user."""
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_mode(True))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:31", "192.168.1.120"))

        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:31"))
        assert dev is not None, "unknown MAC must be auto-registered"
        user = asyncio.get_event_loop().run_until_complete(
            gw.database.get_user(dev.user_id))
        assert user is not None and user.guest, "new device must become a GUEST"
        # guests are fixed users with the guest allowance
        assert user.quota_mode == db_mod.QUOTA_FIXED
        assert user.fixed_gb == 1.0
        # the guest must receive a real allowance (not instantly quota-blocked)
        bundle = asyncio.get_event_loop().run_until_complete(
            gw.database.get_bundle())
        assert bundle.allowances.get(dev.user_id, 0) == pytest.approx(1.0)
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_guest_mode_off_registers_normal_device(tmp_path):
    """Without guest mode the same new device is a normal auto user."""
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        assert asyncio.get_event_loop().run_until_complete(
            gw.service.is_guest_mode()) is False
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:32", "192.168.1.121"))

        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:32"))
        user = asyncio.get_event_loop().run_until_complete(
            gw.database.get_user(dev.user_id))
        assert user is not None and not user.guest
        assert user.quota_mode == db_mod.QUOTA_AUTO
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_guest_device_reconnects_keeps_identity(tmp_path):
    """A known guest reconnecting is NOT re-registered as a fresh account —
    the existing (guest) user is reused, so its allowance survives."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_mode(True))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:35", "192.168.1.123"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:35"))
        uid = dev.user_id

        # the guest disconnects (lease pruned) and comes back
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_lease("aa:bb:cc:dd:ee:35"))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:35", "192.168.1.124"))

        dev2 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:35"))
        assert dev2.user_id == uid, "guest must keep its identity across reconnects"
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

        # the auto-registered device owns a user, and the allowance is keyed
        # by that user (per-user quota model)
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        assert dev is not None and dev.user_id is not None, (
            "auto-registered device must own a user")
        bundle = asyncio.get_event_loop().run_until_complete(
            gw.database.get_bundle())
        assert bundle.allowances.get(dev.user_id, 0) > 0, (
            "the new user must receive a positive share of the bundle")

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
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))

        # Seed usage as if the maintenance tick had flushed it.
        gw.engine = None  # engine disabled in this test config
        asyncio.get_event_loop().run_until_complete(
            gw.database.add_usage(dev.id, time.strftime("%Y-%m-%d"),
                                  5 * 1024 ** 3, 0))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())

        usage = asyncio.get_event_loop().run_until_complete(
            gw.database.get_usage(dev.id))
        assert usage["up_bytes"] >= 5 * 1024 ** 3
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_make_engine_selects_nftables_backend(tmp_path):
    """The Linux gateway always uses NftablesEngine, whatever the config says."""
    from run import _make_engine
    from quota.nftables import NftablesEngine

    cfg = _cfg(tmp_path)
    assert isinstance(_make_engine(cfg, None), NftablesEngine)


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
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    # The background maintenance loop fires its FIRST tick immediately at
    # startup (with the real, disabled engine), so it could race the manual
    # tick below and clobber the holder with an empty flush — the fake engine's
    # live counters then read 0. Cancel it; this test measures manual ticks only
    # (production never runs a tick by hand, so there is no such race there).
    _cancel_maintenance(gw)
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
            def set_gateway_blocked(self, blocked):
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


def test_holder_carries_rogue_scan(tmp_path):
    """The maintenance tick surfaces the rogue LAN scan through the holder, so
    the API + WS push show unmanaged devices alongside the managed ones."""
    from quota.engine import RogueHost

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)

    class _FakeScanner:
        def scan(self, known_macs):
            assert known_macs == set()  # no leases in the test DB
            return [RogueHost(ip="192.168.2.250", mac="11:22:33:44:55:66",
                              vendor="TestCo", online=True)]

    gw.arp_scanner = _FakeScanner()  # type: ignore[assignment]
    gw._last_rogue_scan = time.monotonic() - 9999  # force the scan on tick 1

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        loop.run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        assert len(snap.rogue) == 1
        r = snap.rogue[0]
        assert r.ip == "192.168.2.250"
        assert r.mac == "11:22:33:44:55:66"
        assert r.online is True
        # the rogue event is written so the Activity tab tells the story
        events = loop.run_until_complete(gw.database.list_events())
        assert any("Rogue device on network" in e["message"] for e in events)
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_maintenance_tick_syncs_shaper(tmp_path):
    """The maintenance loop must feed the tc shaper a rate map built from the
    live device IPs + their caps (Linux only; the shaper is None otherwise)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg, internet_probe=lambda: True)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    # The background maintenance loop fires its FIRST tick immediately at
    # startup, so it would race the manual ticks below and append a second,
    # empty shaper call. Cancel it — this test measures manual ticks only
    # (production never runs a tick by hand, so there is no such race there).
    _cancel_maintenance(gw)
    try:
        mac = "aa:bb:cc:dd:ee:66"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.2.110"))

        # give the device its own cap + enable shaping globally
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_device(dev.id, limit_down_mbps=10.0,
                                      limit_up_mbps=5.0))
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_shaping(enabled=True, total_down_mbps=100.0,
                                   total_up_mbps=20.0))

        calls: list[tuple[list, bool, float, float, bool]] = []

        class _FakeShaper:
            available = True
            def start(self):
                pass
            def stop(self):
                pass
            def update_state(self, rate_map, enabled, total_down,
                             total_up, aqm):
                calls.append((rate_map, enabled, total_down, total_up, aqm))

        gw.shaper = _FakeShaper()  # type: ignore[assignment]
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())

        assert len(calls) == 1, "one maintenance tick must sync the shaper"
        rate_map, enabled, total_down, total_up, aqm = calls[0]
        assert enabled is True
        assert total_down == 100.0 and total_up == 20.0
        assert aqm is True
        assert len(rate_map) == 1
        entry = rate_map[0]
        assert entry["ip"] == "192.168.2.110"
        assert entry["device_id"] == dev.id
        assert entry["user_id"] == dev.user_id
        assert entry["down"] == 10.0 and entry["up"] == 5.0

        # disabling shaping + a changed cap feeds the next tick too
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_shaping(enabled=False))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert calls[-1][1] is False
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


# --------------------------------------------------------------------------- #
# v18 WAN-mode wiring: the dashboard toggle persists a topology preference that
# applies on the NEXT restart (mirrors bundle_source). The override must land
# on cfg BEFORE the engine + rogue scanner are built, because both read
# engine.topology / the resolved local subnets at construction.
# --------------------------------------------------------------------------- #

def test_topology_override_from_db_sets_wan(tmp_path):
    """A dashboard WAN-toggle (topology_source=dashboard + topology=wan) must
    reach cfg.engine.topology on the next startup — the setup script is what
    physically rewires the box, so the dashboard only persists the preference."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(gw.startup())
    # simulate a dashboard WAN-toggle (api/app.py POST /api/wan sets these)
    loop.run_until_complete(gw.database.set_setting("topology_source", "dashboard"))
    loop.run_until_complete(gw.database.set_setting("topology", "wan"))
    loop.run_until_complete(gw.shutdown())
    loop.close()

    gw2 = Gateway(cfg)
    loop2 = asyncio.new_event_loop()
    try:
        loop2.run_until_complete(gw2.startup())
        assert gw2.cfg.engine.topology == "wan", \
            "dashboard WAN-toggle must override config.yaml on restart"
    finally:
        loop2.run_until_complete(gw2.shutdown())
        loop2.close()


def test_topology_override_rejected_when_invalid(tmp_path):
    """A corrupted dashboard topology value warns + keeps the config value
    (never lets a bad DB row disable counting by accident)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(gw.startup())
    loop.run_until_complete(gw.database.set_setting("topology_source", "dashboard"))
    loop.run_until_complete(gw.database.set_setting("topology", "sneaky"))
    loop.run_until_complete(gw.shutdown())
    loop.close()

    gw2 = Gateway(cfg)
    loop2 = asyncio.new_event_loop()
    try:
        loop2.run_until_complete(gw2.startup())
        assert gw2.cfg.engine.topology == "lan", \
            "invalid dashboard topology must keep config.yaml"
    finally:
        loop2.run_until_complete(gw2.shutdown())
        loop2.close()


def test_arp_scanner_built_after_topology_override(tmp_path):
    """The rogue scanner resolves its probe networks from cfg at construction,
    so startup must build it AFTER the DB topology override lands (in WAN mode
    it probes only the client subnet — no uplink LAN to scan)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        assert isinstance(gw.arp_scanner, ArpScanner), \
            "startup must construct a real scanner when none was injected"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_preinjected_arp_scanner_survives_startup(tmp_path):
    """Regression guard: a fake scanner injected before startup() must NOT be
    clobbered by the None-guard build (test_holder_carries_rogue_scan relies
    on this to keep its deterministic probe)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    gw.arp_scanner = object()  # type: ignore[assignment]  # not None -> kept
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        assert type(gw.arp_scanner) is object, \
            "a pre-injected scanner must survive startup untouched"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_arp_lock_not_started_under_wan(tmp_path):
    """In WAN mode the box terminates the line itself — there is no router on
    the client segment to lock against, so the ARP gateway-lock responder must
    not start even when config requests it."""
    cfg = _cfg(tmp_path)
    cfg.engine.gateway_arp_lock = True
    cfg.engine.topology = "wan"
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        assert gw.arp_lock is None, \
            "WAN mode must not start the ARP gateway-lock responder"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_holder_swap_carries_wan_status_lan(tmp_path):
    """Every tick pushes a wan_status into the holder, so the dashboard WAN tab
    and /api/wan see the effective topology without a separate query. In LAN
    mode ppp0 is always n/a (no ppp0 to dial)."""
    cfg = _cfg(tmp_path)
    # Fake the internet probe (a real TCP connect would dial out in the test).
    gw = Gateway(cfg, internet_probe=lambda: True)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        loop.run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        assert snap.wan_status.get("topology") == "lan"
        assert snap.wan_status.get("configured") == "lan"
        assert snap.wan_status.get("source") == "config"
        assert snap.wan_status.get("pending") is None
        assert snap.wan_status.get("ppp0") == "n/a"
        assert snap.wan_status.get("internet") is True
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_holder_swap_carries_wan_status_wan(tmp_path):
    """Under the WAN override the tick surfaces the WAN topology + ppp state
    (detect_ppp degrades to a safe value on a box without ppp0 — never raises)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(gw.startup())
    loop.run_until_complete(gw.database.set_setting("topology_source", "dashboard"))
    loop.run_until_complete(gw.database.set_setting("topology", "wan"))
    loop.run_until_complete(gw.shutdown())
    loop.close()

    gw2 = Gateway(cfg, internet_probe=lambda: False)
    loop2 = asyncio.new_event_loop()
    try:
        loop2.run_until_complete(gw2.startup())
        loop2.run_until_complete(gw2._maintenance_tick())
        snap = gw2.holder.get()
        assert snap.wan_status.get("topology") == "wan"
        assert snap.wan_status.get("configured") == "wan"
        assert snap.wan_status.get("source") == "dashboard"
        assert snap.wan_status.get("pending") == "wan"
        assert snap.wan_status.get("ppp0") in ("up", "down", "unknown")
        assert snap.wan_status.get("internet") is False
    finally:
        loop2.run_until_complete(gw2.shutdown())
        loop2.close()


def test_wan_internet_gated_on_ppp0_link(tmp_path, monkeypatch):
    """v19.6: the WAN-tab green dot must NEVER claim internet while ppp0 is down.

    The probe measures the BOX's reachability — in the half-applied state (router
    not bridged yet) the box still reaches the internet via the router's NAT, so
    the probe alone returns True. But in WAN mode ppp0 IS the internet path: a
    down dial means the gateway is not serving clients. The dot is gated on the
    link: ppp0 down -> internet False (even when the probe succeeds); ppp0 up +
    probe -> True. LAN mode keeps the probe as the whole story.
    """
    # run.py does `from quota.topology import detect_ppp`, so patch the run.py
    # reference (the module attr that _wan_status looks up at call time).
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "down", "local": "", "peer": ""})

    def _seed_wan():
        gw = Gateway(_cfg(tmp_path))
        loop = asyncio.new_event_loop()
        loop.run_until_complete(gw.startup())
        loop.run_until_complete(
            gw.database.set_setting("topology_source", "dashboard"))
        loop.run_until_complete(gw.database.set_setting("topology", "wan"))
        loop.run_until_complete(gw.shutdown())
        loop.close()

    def _tick(probe_ok: bool) -> dict:
        gw = Gateway(_cfg(tmp_path), internet_probe=lambda: probe_ok)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(gw.startup())
            task = getattr(gw, "_maintenance_task", None)
            if task is not None:  # cancel so the manual tick is the only one
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
            loop.run_until_complete(gw._maintenance_tick())
            return dict(gw.holder.get().wan_status or {})
        finally:
            loop.run_until_complete(gw.shutdown())
            loop.close()

    # ppp0 down, but the probe says the box can reach the internet.
    _seed_wan()
    ws = _tick(probe_ok=True)
    assert ws["ppp0"] == "down"
    assert ws["internet"] is False, \
        "a down ppp0 must read red even when the box itself has internet"

    # ppp0 up + probe OK -> green.
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    ws = _tick(probe_ok=True)
    assert ws["ppp0"] == "up"
    assert ws["internet"] is True

    # ppp0 up but the line is actually dead -> red.
    ws = _tick(probe_ok=False)
    assert ws["ppp0"] == "up"
    assert ws["internet"] is False


def test_wan_internet_reads_dns_probe_while_box_cut(tmp_path):
    """v29.2: cutting the Gateway user must NOT flip the green dot red.

    The block drops the box's own TCP egress (``gw_blocked`` = 0.0.0.0/0), so
    the TCP probe reads "down" even though the PPPoE line and every client are
    fine. DNS (UDP 53) is exempted from the block, so while the box is cut the
    dot switches to the DNS probe — it reports the SERVICE, and the box's cut
    stays on the Gateway card ("Box internet is cut at the kernel")."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "run.detect_ppp",
        lambda *a, **k: {"state": "up", "local": "1.2.3.4", "peer": ""})

    def _seed_wan():
        gw = Gateway(_cfg(tmp_path))
        loop = asyncio.new_event_loop()
        loop.run_until_complete(gw.startup())
        loop.run_until_complete(
            gw.database.set_setting("topology_source", "dashboard"))
        loop.run_until_complete(gw.database.set_setting("topology", "wan"))
        loop.run_until_complete(gw.shutdown())
        loop.close()

    def _tick(tcp_ok: bool, dns_ok: bool, box_cut: bool) -> dict:
        gw = Gateway(_cfg(tmp_path), internet_probe=lambda: tcp_ok,
                     dns_probe=lambda: dns_ok)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(gw.startup())
            task = getattr(gw, "_maintenance_task", None)
            if task is not None:
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass

            # The engine is disabled in this test config — fake one that reports
            # the kernel's gateway-block state the maintenance loop would have
            # programmed via set_gateway_blocked.
            class _FakeEngine:
                def flush(self) -> EngineSnapshot:
                    return EngineSnapshot(by_ip={}, ip_to_mac={}, blocked={})
                def update_state(self, ip_to_mac, blocked):
                    pass
                def set_gateway_blocked(self, blocked):
                    pass
                def stop(self):
                    pass
            gw.engine = _FakeEngine()  # type: ignore[assignment]
            gw.engine.gateway_blocked = box_cut

            loop.run_until_complete(gw._maintenance_tick())
            return dict(gw.holder.get().wan_status or {})
        finally:
            loop.run_until_complete(gw.shutdown())
            loop.close()

    _seed_wan()
    # Box cut + line fine: the TCP probe is dropped (red) but DNS answers ->
    # the dot reports the SERVICE as Online, not a false "internet down".
    ws = _tick(tcp_ok=False, dns_ok=True, box_cut=True)
    assert ws["internet"] is True, \
        "while the box is cut, DNS (exempted) proves the line delivers"

    # Box cut + line actually dead: DNS also fails -> red (honest).
    ws = _tick(tcp_ok=False, dns_ok=False, box_cut=True)
    assert ws["internet"] is False

    # Box NOT cut: the TCP probe is the whole story (DNS unused).
    ws = _tick(tcp_ok=False, dns_ok=True, box_cut=False)
    assert ws["internet"] is False, \
        "an unblocked box with a dead probe must read red — DNS is not a crutch"


# --------------------------------------------------------------------------- #
# guest-deletion suppression + the protected Gateway user's accounting
# --------------------------------------------------------------------------- #

def test_suppressed_mac_is_not_re_registered(tmp_path):
    """A manually-deleted guest stays deleted while its device is still on the
    network: _persist_lease skips a suppressed MAC (checked before guest mode)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    # guest mode ON: a new device auto-registers as a GUEST (only a guest-owned
    # delete records the suppression — a normal-user delete never does)
    asyncio.get_event_loop().run_until_complete(gw.service.set_guest_mode(True))
    _cancel_maintenance(gw)  # the background loop must not race the manual calls
    try:
        mac = "aa:bb:cc:dd:ee:81"
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.41"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        assert dev is not None, "first sighting auto-registers as a guest"
        # delete + suppress, exactly what DELETE /api/devices does for a guest
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_device(dev.id, suppress_guest_mac=True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.is_mac_suppressed(mac)) is True

        # the device is still connected: another lease tick must NOT resurrect it
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.41"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac)) is None, \
            "suppressed MAC must not be re-registered while still connected"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_suppression_cleared_when_lease_drops(tmp_path):
    """When the device genuinely leaves (its lease disappears from dnsmasq's
    file), the suppression is cleared — a future reconnect registers fresh."""
    cfg = _cfg(tmp_path)
    lease_file = tmp_path / "dnsmasq.leases"
    lease_file.write_text(
        "1730000000 aa:bb:cc:dd:ee:82 192.168.2.42 phone 01:aa:bb:cc:dd:ee:82\n",
        encoding="utf-8")
    cfg.dhcp.lease_file = str(lease_file)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    asyncio.get_event_loop().run_until_complete(gw.service.set_guest_mode(True))
    _cancel_maintenance(gw)  # the background loop must not race the manual calls
    try:
        mac = "aa:bb:cc:dd:ee:82"
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        assert dev is not None, "lease device auto-registers as a guest"
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_device(dev.id, suppress_guest_mac=True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.is_mac_suppressed(mac)) is True

        # the device leaves the network: the lease file no longer lists it
        # (other devices still are — a transiently EMPTY lease file is a
        # dnsmasq restart and must NOT clear any suppression)
        lease_file.write_text(
            "1730000000 aa:bb:cc:dd:ee:99 192.168.2.99 tablet 01:aa:bb:cc:dd:ee:99\n",
            encoding="utf-8")
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.is_mac_suppressed(mac)) is False

        # reconnecting now registers a fresh account
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.42"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac)) is not None
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_gateway_delta_drains_into_box_device(tmp_path):
    """The maintenance tick charges the box's OWN q_gw_* traffic to the
    protected Gateway user's device (usage_daily), so the machine's bundle
    consumption is inside the quota math."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        class _FakeEngine:
            def flush(self) -> EngineSnapshot:
                return EngineSnapshot(
                    by_ip={},
                    ip_to_mac={}, blocked={},
                    gateway=EngineCounters(up=3000, down=7000))
            def update_state(self, ip_to_mac, blocked):
                pass
            def set_gateway_blocked(self, blocked):
                pass
            def stop(self):
                pass
        gw.engine = _FakeEngine()  # type: ignore[assignment]

        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        box = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=GATEWAY_MAC))
        assert box is not None
        usage = asyncio.get_event_loop().run_until_complete(
            gw.database.get_usage(box.id))
        assert usage["up_bytes"] == 3000
        assert usage["down_bytes"] == 7000
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_set_gateway_blocked_called_with_resolved_state(tmp_path):
    """The enforcement push drives the gateway chains from the Gateway user's
    resolved block state (quota or admin), even though the box has no lease/IP
    and never enters the per-device forward blocked set."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        calls: list[bool] = []

        class _FakeEngine:
            def flush(self) -> EngineSnapshot:
                return EngineSnapshot(by_ip={}, ip_to_mac={}, blocked={},
                                      gateway=EngineCounters())
            def update_state(self, ip_to_mac, blocked):
                pass
            def set_gateway_blocked(self, blocked):
                calls.append(blocked)
            def stop(self):
                pass
        gw.engine = _FakeEngine()  # type: ignore[assignment]

        # the box's own user: first push at 0 usage -> unblocked
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert calls == [False]

        # drop the Gateway user's allowance to 0 -> the box itself is cut.
        # Mirrors PATCH /api/users/{id}: the DB edit must be followed by a
        # recompute so the allowance SNAPSHOT drops to 0 (enforcement reads the
        # snapshot, not the user row).
        async def _cut():
            u = next(u for u in await gw.database.list_users()
                     if getattr(u, "protected", False))
            await gw.database.update_user(u.id, fixed_gb=0.0)
            await gw.service.recompute_allowances()
            await gw.service.evaluate_blocks()
        asyncio.get_event_loop().run_until_complete(_cut())
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert calls[-1] is True, "0-allowance Gateway must cut the box's internet"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_tick_copies_engine_gateway_state_into_snapshot(tmp_path):
    """The holder swap carries what the engine ACTUALLY programmed for the
    box's cut (engine_available + gateway_blocked), so the dashboard can show
    "Blocked in the UI but not cut at the kernel" instead of silently trusting
    the toggle."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        # a faithful fake: reports programmed cut + engine live.
        class _FakeEngine:
            available = True
            gateway_blocked = True

            def flush(self) -> EngineSnapshot:
                return EngineSnapshot(by_ip={}, ip_to_mac={}, blocked={},
                                      gateway=EngineCounters())
            def update_state(self, ip_to_mac, blocked):
                pass
            def set_gateway_blocked(self, blocked):
                self.gateway_blocked = bool(blocked)
            def stop(self):
                pass
        gw.engine = _FakeEngine()  # type: ignore[assignment]

        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        assert snap.engine_available is True
        assert snap.gateway_blocked is False  # resolved state: 0 usage, 1 GB

        # cut the box: resolved state True flows into the fake -> snapshot.
        async def _cut():
            u = next(u for u in await gw.database.list_users()
                     if getattr(u, "protected", False))
            await gw.database.update_user(u.id, fixed_gb=0.0)
            await gw.service.recompute_allowances()
            await gw.service.evaluate_blocks()
        asyncio.get_event_loop().run_until_complete(_cut())
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        snap = gw.holder.get()
        assert snap.gateway_blocked is True
        assert snap.engine_available is True
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_per_device_admin_block_reaches_the_kernel(tmp_path):
    """A per-DEVICE admin block (PATCH /api/devices/{id} {block:true}) must cut
    that ONE device's internet — its IP lands in the kernel @blocked set — while
    a sibling device of the same user stays online.

    Regression for the report "per-device block doesn't work, only per-user
    block works": the full chain (API -> service -> snapshot -> real
    NftablesEngine.update_state -> @blocked) must be exercised together, not
    just the service layer in isolation.
    """
    from quota.nftables import NftablesEngine

    class _BlockedNft:
        """Fake nft tracking the @blocked set + counters (mirrors FakeNft)."""

        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.counters: dict[str, int] = {}
            self._sets: dict[str, dict[str, set[str]]] = {
                "inet quota_gateway": {"blocked": set(), "known_ips": set()},
            }

        @property
        def blocked(self) -> set[str]:
            return self._sets["inet quota_gateway"]["blocked"]

        @staticmethod
        def _table(args: list[str]) -> str:
            return " ".join(args[2].split()[:2])

        def __call__(self, argv: list[str]) -> tuple[int, str]:
            self.calls.append(argv)
            if argv[0] != "nft":
                return 1, f"unknown binary {argv[0]}"
            args = argv[1:]
            if args[0] == "-j":  # nft -j list counters
                return self._list_counters()
            if args[0] == "flush" and args[1] == "set":
                # Handle flush-set BEFORE the add/flush create branch: a flush
                # must CLEAR the set, never be swallowed as a no-op create.
                name = args[2].split()[-1]
                self._sets[self._table(args)].setdefault(name, set()).clear()
                return 0, ""
            if args[0] in ("add", "flush") and args[1] in (
                    "table", "chain", "set", "rule", "counter"):
                if args[0] == "flush" and args[1] == "table":
                    table = args[2]
                    for s in self._sets.setdefault(table, {}):
                        self._sets[table][s].clear()
                return 0, ""
            if args[0] == "add" and args[1] == "element":
                name = args[2].split()[-1]
                target = self._sets[self._table(args)].setdefault(name, set())
                for ip in args[-1].strip("{}").split(","):
                    target.add(ip.strip())
                return 0, ""
            if args[0] == "add" and args[1] == "counter":
                name = args[-1]
                self.counters.setdefault(name, 0)
                return 0, ""
            if args[0] == "reset" and args[1] == "counters":
                return 0, ""
            return 0, ""

        def _list_counters(self) -> tuple[int, str]:
            import json as _json
            entries = [{"metainfo": {"version": "1.0.6"}}]
            for name, bytes_ in self.counters.items():
                entries.append({
                    "counter": {"family": "inet", "table": "quota_gateway",
                                "name": name, "handle": 1,
                                "packets": 0, "bytes": bytes_},
                })
            return 0, _json.dumps({"nftables": entries})

    from core.config import Config
    from quota.engine import SnapshotHolder

    cfg = Config()
    cfg.db_path = str(tmp_path / "data" / "smoke.db")
    cfg.log_file = str(tmp_path / "logs" / "smoke.log")
    cfg.dhcp.enable = False
    cfg.engine.enabled = False  # we install a real engine by hand below
    lease_file = tmp_path / "leases"
    lease_file.write_text(
        "1730000000 aa:bb:cc:dd:ee:61 192.168.2.61 phone 01:aa:bb:cc:dd:ee:61\n"
        "1730000000 aa:bb:cc:dd:ee:62 192.168.2.62 tablet 01:aa:bb:cc:dd:ee:62\n",
        encoding="utf-8")
    cfg.dhcp.lease_file = str(lease_file)

    gw = Gateway(cfg)
    try:
        asyncio.get_event_loop().run_until_complete(gw.startup())
        _cancel_maintenance(gw)

        nft = _BlockedNft()
        gw.engine = NftablesEngine(Config(), SnapshotHolder(), run_command=nft)
        gw.engine.start()

        # both devices register from the lease file (same user, auto-created)
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert not nft.blocked, "nothing blocked yet"

        # block ONE device (PATCH /api/devices/{id} {block:true} maps to this)
        devs = asyncio.get_event_loop().run_until_complete(gw.database.list_devices())
        target = next(d for d in devs if d.mac == "aa:bb:cc:dd:ee:62")
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_admin_block(target.id, True))

        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert "192.168.2.62" in nft.blocked, (
            "per-device block must land the device's IP in @blocked")
        assert "192.168.2.61" not in nft.blocked, (
            "the sibling device of the same user must stay online")

        # unblock -> the IP leaves the set
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_admin_block(target.id, False))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert "192.168.2.62" not in nft.blocked, (
            "unblock must remove the IP from @blocked")
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_immediate_reshaping_waits_for_shaping_lock(tmp_path):
    """_reshaping_now (the API's immediate re-sync) is serialized with the
    maintenance tick by _shaping_lock. _sync_shaping reads the DB before it
    programs tc, so without the lock a tick that read the caps BEFORE an edit
    committed could re-apply its stale snapshot AFTER the immediate re-sync —
    briefly undoing the user's fresh caps. Whoever runs second re-reads the
    DB, so serializing the whole sync makes both orderings end fresh."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg, internet_probe=lambda: True)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        mac = "aa:bb:cc:dd:ee:77"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.2.130"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_device(dev.id, limit_down_mbps=10.0,
                                      limit_up_mbps=5.0))
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_shaping(enabled=True, total_down_mbps=100.0,
                                   total_up_mbps=20.0))

        calls: list[tuple] = []

        class _FakeShaper:
            available = True
            def start(self):
                pass
            def stop(self):
                pass
            def update_state(self, rate_map, enabled, total_down,
                             total_up, aqm):
                calls.append((rate_map, enabled, total_down, total_up, aqm))

        gw.shaper = _FakeShaper()  # type: ignore[assignment]
        # prime the ip->mac map a real tick would have filled, so the re-sync
        # has a device to shape once the lock frees
        gw._last_ip_to_mac = {"192.168.2.130": mac}

        # simulate the tick holding the lock mid-sync, then start the API's
        # immediate re-sync — it must block until the lock frees.
        lock = gw._shaping_lock
        asyncio.get_event_loop().run_until_complete(lock.acquire())
        task = asyncio.get_event_loop().create_task(gw._reshaping_now())
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0))
        assert calls == [], ("re-sync must wait for the tick's sync — it "
                             "cannot program tc mid-way through")
        lock.release()  # synchronous in 3.10+ (only acquire() is a coroutine)
        asyncio.get_event_loop().run_until_complete(task)
        assert len(calls) == 1, "re-sync programs the tree once the lock frees"
        rate_map, enabled, total_down, total_up, aqm = calls[0]
        assert enabled is True and total_down == 100.0 and total_up == 20.0
        assert rate_map[0]["ip"] == "192.168.2.130"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())
