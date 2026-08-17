"""End-to-end smoke test of the run.py wiring (no admin privileges needed).

Builds a Gateway from config with the packet engine / DHCP subsystems disabled,
then boots uvicorn and exercises the API + WebSocket. Verifies the maintenance
loop ticks and pushes enforcement state.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import config as cfg_mod
from quota import db as _db
from quota.arp_scan import ArpScanner
from quota.engine import GATEWAY_MAC, EngineCounters, EngineSnapshot
from quota.tun2socks import Tun2socksStatus
from quota.vpnshare import VpnShareStatus
from run import Gateway


def _cfg(tmp_path) -> cfg_mod.Config:
    cfg = cfg_mod.Config()
    cfg.db_path = str(tmp_path / "data" / "smoke.db")
    cfg.log_file = str(tmp_path / "logs" / "smoke.log")
    cfg.dhcp.enable = False
    cfg.engine.enabled = False
    # DNS-history tailer is off in hermetic tests unless a test opts in (its
    # temp log file may not exist and the thread would just poll forever).
    cfg.history.enabled = False
    # GitHub self-update checks are off too — the first maintenance tick
    # would otherwise dial out to api.github.com (a 20 s timeout per test).
    cfg.updates.enabled = False
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


def _boot_wan_gateway(tmp_path, monkeypatch, ppp, renew, restarter=None):
    """Boot a Gateway under the WAN DB override with a fake ppp0 link.

    Two-phase (the established pattern): a throwaway Gateway seeds the DB with
    the dashboard-owned topology + the renew schedule, shuts down, and a second
    Gateway re-boots so the override is applied at startup (run.py re-applies
    the DB topology before building the engine). ``ppp`` is the fake
    detect_ppp state, ``renew`` the renew-settings dict, ``restarter`` the fake
    PPPoE dial restarter (default: a silent success). The background
    maintenance loop is cancelled so the caller owns the tick schedule.

    Returns ``(gw, loop)`` — the caller runs ticks on ``loop`` and must finish
    with ``loop.run_until_complete(gw.shutdown()); loop.close()``.
    """
    seed = Gateway(_cfg(tmp_path))
    seed_loop = asyncio.new_event_loop()
    try:
        seed_loop.run_until_complete(seed.startup())
        seed_loop.run_until_complete(
            seed.database.set_setting("topology_source", "dashboard"))
        seed_loop.run_until_complete(
            seed.database.set_setting("topology", "wan"))
        seed_loop.run_until_complete(
            seed.database.set_setting("wan_ip_renew_enabled",
                                      "1" if renew["enabled"] else "0"))
        seed_loop.run_until_complete(
            seed.database.set_setting("wan_ip_renew_minutes",
                                      str(renew["minutes"])))
        seed_loop.run_until_complete(
            seed.database.set_setting("wan_ip_renew_last", renew["last"]))
        seed_loop.run_until_complete(seed.shutdown())
    finally:
        seed_loop.close()

    # run.py does `from quota.topology import detect_ppp` — patch the run.py
    # reference (the module attr looked up at call time).
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": ppp, "local": "1.2.3.4",
                                         "peer": ""})
    gw = Gateway(_cfg(tmp_path))
    if restarter is not None:
        gw._pppoe_restart = restarter
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
    except BaseException:
        loop.run_until_complete(gw.shutdown())
        loop.close()
        raise
    return gw, loop


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


class FakeIpRunner:
    """Injectable ``ip`` runner for the source-interface collector: returns the
    ``ip -j neigh`` JSON for the first call, then the plain-text fallback."""

    def __init__(self, json_out: str = "", text_out: str = "",
                 fail: bool = False) -> None:
        self.json_out = json_out
        self.text_out = text_out
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if self.fail:
            return 1, ""
        if "-j" in argv:
            return 0, self.json_out
        return 0, self.text_out


def test_collect_interfaces_learns_neigh_device(tmp_path):
    """Each leased device's source NIC is learned from `ip -j neigh` and
    persisted to devices.source_interface (drives the WiFi/LAN card tag)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    gw._ip_run = FakeIpRunner(json_out=json.dumps([
        {"dst": "192.168.2.42", "dev": "wlan0", "state": "REACHABLE",
         "lladdr": "aa:bb:cc:dd:ee:42"},
        {"dst": "192.168.2.43", "dev": "eth0", "state": "STALE",
         "lladdr": "aa:bb:cc:dd:ee:43"},
        # filters: IPv6 row + FAILED state + a non-lease IP
        {"dst": "fe80::1", "dev": "wlan0", "state": "REACHABLE"},
        {"dst": "192.168.2.44", "dev": "eth0", "state": "FAILED"},
        {"dst": "192.168.1.99", "dev": "eth0", "state": "REACHABLE"},
    ]))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.database.connect())
        loop.run_until_complete(gw.service.ensure_period())
        loop.run_until_complete(
            gw.database.upsert_device("aa:bb:cc:dd:ee:42", name="Phone"))
        loop.run_until_complete(
            gw.database.upsert_device("aa:bb:cc:dd:ee:43", name="PC"))
        loop.run_until_complete(
            gw.database.upsert_device("aa:bb:cc:dd:ee:44", name="Static"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:42", "192.168.2.42"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:43", "192.168.2.43"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:44", "192.168.2.44"))
        loop.run_until_complete(gw._collect_interfaces())
        dev42 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:42"))
        dev43 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:43"))
        dev44 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:44"))
        assert dev42.source_interface == "wlan0"
        assert dev43.source_interface == "eth0"
        assert dev44.source_interface == "", "FAILED neigh rows are skipped"
        # disconnect keeps the last-known interface (fresh empty row ignored)
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:42", "192.168.2.42"))
        gw._ip_run.json_out = "[]"
        loop.run_until_complete(gw._collect_interfaces())
        dev42 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:42"))
        assert dev42.source_interface == "wlan0"
    finally:
        loop.run_until_complete(gw.database.close())
        loop.close()


def test_collect_interfaces_text_fallback(tmp_path):
    """`ip neigh` plain-text fallback (older iproute2) parses dev names too."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    gw._ip_run = FakeIpRunner(
        json_out="",  # `ip -j neigh` failed/empty -> text fallback
        text_out=("192.168.2.42 dev wlan0 lladdr aa:bb:cc:dd:ee:42 REACHABLE\n"
                  "192.168.2.43 dev eth0 lladdr aa:bb:cc:dd:ee:43 STALE\n"
                  "192.168.2.44 dev eth0 lladdr aa:bb:cc:dd:ee:44 FAILED\n"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.database.connect())
        loop.run_until_complete(gw.service.ensure_period())
        loop.run_until_complete(
            gw.database.upsert_device("aa:bb:cc:dd:ee:42", name="Phone"))
        loop.run_until_complete(
            gw.database.upsert_device("aa:bb:cc:dd:ee:44", name="Static"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:42", "192.168.2.42"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:44", "192.168.2.44"))
        loop.run_until_complete(gw._collect_interfaces())
        dev42 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:42"))
        dev44 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:44"))
        assert dev42.source_interface == "wlan0"
        assert dev44.source_interface == "", "FAILED rows skipped in text too"
        # both ip invocations were attempted (json then text)
        assert gw._ip_run.calls[0][:3] == ["ip", "-j", "neigh"]
        assert gw._ip_run.calls[1][:3] == ["ip", "neigh"]
    finally:
        loop.run_until_complete(gw.database.close())
        loop.close()


def test_prune_events_removes_old_rows(tmp_path):
    """The events table is append-only and unbounded — the hourly prune must
    drop rows older than the retention window and keep the fresh ones."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.database.connect())
        loop.run_until_complete(gw.service.ensure_period())
        loop.run_until_complete(
            gw.database.add_event("new", "info", None))
        # insert an OLD row directly (add_event stamps time.time())
        await_old = gw.database.conn.execute(
            "INSERT INTO events (ts, level, device_id, user_id, message) "
            "VALUES (?, 'info', NULL, NULL, 'old')",
            (time.time() - 40 * 86400,))
        loop.run_until_complete(await_old)
        loop.run_until_complete(gw.database.conn.commit())
        deleted = loop.run_until_complete(
            gw.database.prune_events(time.time() - 30 * 86400))
        assert deleted == 1
        events = loop.run_until_complete(gw.database.list_events(50))
        assert len(events) == 1 and events[0]["message"] == "new"
        # a no-op prune is safe (0 rows)
        assert loop.run_until_complete(
            gw.database.prune_events(time.time() - 30 * 86400)) == 0
    finally:
        loop.run_until_complete(gw.database.close())
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
        cfg.bundle.period_type = "end_of_month"
        gw2 = Gateway(cfg)
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(gw2.startup())
            b2 = loop2.run_until_complete(gw2.database.get_bundle())
            assert b2.total_gb == 250.0, "config.yaml edit must reach the DB"
            assert b2.reset_day == 5
            assert b2.period_type == "end_of_month", \
                "config.yaml period_type must reach the DB"
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


def test_updater_wired_only_when_config_enabled(tmp_path):
    """cfg.updates.enabled gates the whole subsystem (hermetic-tests master
    switch): disabled => ``gw.updater is None`` and a tick stays silent;
    enabled => an updater exists and honors the DB's ``updates_enabled``
    toggle (so a tick with the toggle off never dials GitHub)."""
    gw_off = Gateway(_cfg(tmp_path))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw_off.startup())
        assert gw_off.updater is None
        task = getattr(gw_off, "_maintenance_task", None)
        if task is not None:
            task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        loop.run_until_complete(gw_off._maintenance_tick())  # must not crash
    finally:
        loop.run_until_complete(gw_off.shutdown())
        loop.close()

    cfg = _cfg(tmp_path)
    cfg.updates.enabled = True
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        assert gw.updater is not None
        task = getattr(gw, "_maintenance_task", None)
        if task is not None:
            task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        loop.run_until_complete(
            gw.database.set_setting("updates_enabled", "0"))
        # the tick must not fetch (no GitHub) with the toggle off — if it
        # dialed out, maybe_check would fail fast anyway, but a hermetic
        # suite must never touch the network
        loop.run_until_complete(gw._maintenance_tick())
    finally:
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


def test_new_device_auto_registers_disabled_until_admin_assigns(tmp_path):
    """A brand-new DHCP device mints its user in the DISABLED onboarding lock:
    0 GB, no auto share, quota-blocked — the admin's shared/fixed assignment
    (user/device modal) is the ONLY way it comes online."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:12", "192.168.1.101"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:12"))
        assert dev is not None
        user = asyncio.get_event_loop().run_until_complete(
            gw.database.get_user(dev.user_id))
        assert user.quota_mode == _db.QUOTA_DISABLED, \
            "a fresh device must NOT auto-share the bundle"
        assert (user.fixed_gb or 0.0) == 0.0
        bundle = asyncio.get_event_loop().run_until_complete(
            gw.database.get_bundle())
        assert bundle.allowances.get(user.id, -1) == 0.0
        # the enforcement map cuts it: zero usage, zero allowance
        snap = asyncio.get_event_loop().run_until_complete(
            gw.service.snapshot_state())
        assert snap[dev.mac]["blocked"] is True
        # the admin assigns shared -> the device comes online (usage under share)
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_user(user.id, quota_mode=_db.QUOTA_AUTO))
        asyncio.get_event_loop().run_until_complete(
            gw.service.recompute_allowances())
        snap = asyncio.get_event_loop().run_until_complete(
            gw.service.snapshot_state())
        assert snap[dev.mac]["blocked"] is False
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


def test_guest_mode_off_registers_disabled_device(tmp_path):
    """Without guest mode the same new device mints its user in the DISABLED
    onboarding lock — 0 GB until the admin assigns shared or fixed (the
    legacy auto-share-on-join behavior is retired)."""
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
        assert user.quota_mode == db_mod.QUOTA_DISABLED
        assert (user.fixed_gb or 0.0) == 0.0
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


def test_guest_limit_blocks_new_guest_after_cap(tmp_path):
    """When the guest limit is reached, a NEW device is still registered as a
    guest (visible + counted) but is immediately admin-blocked — a MAC-changer
    can't mint a fresh allowance forever."""
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_mode(True))
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_limit(2))
        # fill the cap with two guests
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:41", "192.168.1.130"))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:42", "192.168.1.131"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.count_guest_users()) == 2

        # third brand-new device beyond the cap -> guest but admin-blocked
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:43", "192.168.1.132"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:43"))
        assert dev is not None
        user = asyncio.get_event_loop().run_until_complete(
            gw.database.get_user(dev.user_id))
        assert user is not None and user.guest
        assert dev.block_state == db_mod.BLOCK_ADMIN, (
            "over-cap guest must be cut immediately")

        # raising the cap lets the NEXT brand-new device join normally
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_limit(4))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:44", "192.168.1.133"))
        dev4 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:44"))
        assert dev4.block_state == db_mod.BLOCK_OK
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_guest_limit_default_is_two(tmp_path):
    """The default guest cap is 2 (documented anti-MAC-spam value)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        assert asyncio.get_event_loop().run_until_complete(
            gw.service.guest_limit()) == 2
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_lowering_guest_limit_cuts_existing_over_cap(tmp_path):
    """Lowering the guest cap admin-blocks the NEWEST guests already over the
    new cap (oldest ``n`` stay online) — "set to 1" actually leaves one guest
    connected even when several joined earlier. Raising the cap never
    un-blocks anyone."""
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(gw.startup())
    try:
        loop.run_until_complete(gw.service.set_guest_mode(True))
        loop.run_until_complete(gw.service.set_guest_limit(3))
        for i, ip in enumerate(("192.168.1.140", "192.168.1.141",
                                "192.168.1.142"), start=1):
            loop.run_until_complete(
                gw._persist_lease("aa:bb:cc:dd:ee:5%d" % i, ip))
        assert loop.run_until_complete(
            gw.database.count_guest_users()) == 3
        for i in range(1, 4):
            dev = loop.run_until_complete(
                gw.database.get_device(mac="aa:bb:cc:dd:ee:5%d" % i))
            assert dev.block_state == db_mod.BLOCK_OK

        # lower the cap to 1 -> exactly the OLDEST guest survives
        loop.run_until_complete(gw.service.set_guest_limit(1))
        guests = sorted((u for u in loop.run_until_complete(
            gw.database.list_users()) if u.guest),
            key=lambda u: u.created_at)
        survivor = guests[0]
        for u in guests:
            devs = loop.run_until_complete(
                gw.database.list_devices(user_id=u.id))
            assert len(devs) == 1
            expected = (db_mod.BLOCK_OK if u.id == survivor.id
                        else db_mod.BLOCK_ADMIN)
            assert devs[0].block_state == expected, (
                "only the oldest guest survives a lowered cap")

        # raising the cap does NOT resurrect the cut guests
        loop.run_until_complete(gw.service.set_guest_limit(3))
        for u in guests[1:]:
            devs = loop.run_until_complete(
                gw.database.list_devices(user_id=u.id))
            assert devs[0].block_state == db_mod.BLOCK_ADMIN
    finally:
        loop.run_until_complete(gw.shutdown())


def test_stop_new_connections_refuses_brand_new_device_at_dhcp(tmp_path):
    """With STOP NEW CONNECTIONS on, a brand-new MAC is refused at the DHCP
    level: no device row (no "unsigned user"), a ``dhcp-host=<mac>,ignore``
    line in the fragment, and a row-less kernel block while its lease
    lingers. An already-registered device keeps joining; turning the gate
    off clears the fragment + the refuse list so the next new device
    registers normally (disabled onboarding)."""
    from quota import db as db_mod
    from pathlib import Path

    cfg = _cfg(tmp_path)
    cfg.dhcp.enable = True  # the DHCP-refusal fragment path
    cfg.dhcp.ignore_file = str(tmp_path / "dnsmasq.d" / "quota-ignore.conf")
    cfg.dhcp.reload_dnsmasq = False
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        # an existing device (joined before the gate) keeps its identity
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:51", "192.168.1.140"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:51"))
        uid = dev.user_id

        asyncio.get_event_loop().run_until_complete(
            gw.service.set_stop_new_connections(True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.service.stop_new_connections()) is True

        # a brand-new MAC is refused: no row, fragment entry, row-less block
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:52", "192.168.1.141"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:52")) is None, (
            "refused device must NOT be registered (no 'unsigned user')")
        refused = asyncio.get_event_loop().run_until_complete(
            gw.service.refused_macs())
        assert "aa:bb:cc:dd:ee:52" in refused
        fragment = Path(cfg.dhcp.ignore_file)
        assert fragment.exists(), "the DHCP-refusal fragment must be written"
        assert "dhcp-host=aa:bb:cc:dd:ee:52,ignore\n" in fragment.read_text(
            encoding="utf-8"), "the refused MAC must be in the fragment"
        snap = asyncio.get_event_loop().run_until_complete(
            gw.service.snapshot_state())
        entry = snap.get("aa:bb:cc:dd:ee:52")
        assert entry is not None and entry["blocked"] is True, (
            "the lingering lease of a refused MAC must be kernel-cut")
        assert entry["block_state"] == _db.BLOCK_ADMIN

        # the pre-existing device reconnects normally (identity preserved)
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_lease("aa:bb:cc:dd:ee:51"))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:51", "192.168.1.142"))
        dev3 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:51"))
        assert dev3.user_id == uid, "existing device must keep its identity"
        assert dev3.block_state != db_mod.BLOCK_ADMIN, (
            "existing device must not be cut by the gate")

        # turning the gate off clears the fragment + refuse list; the next
        # brand-new device registers normally (disabled onboarding)
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_stop_new_connections(False))
        asyncio.get_event_loop().run_until_complete(gw._sync_refuse_fragment())
        assert fragment.read_text(encoding="utf-8") == "", (
            "gate off must empty the DHCP-refusal fragment")
        assert not asyncio.get_event_loop().run_until_complete(
            gw.service.refused_macs())
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:53", "192.168.1.143"))
        dev4 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:53"))
        assert dev4.block_state == db_mod.BLOCK_OK
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_stop_new_falls_back_to_registered_block_when_fragment_unwritable(tmp_path):
    """If the DHCP-refusal fragment can't be written (no root / no dnsmasq
    dir), the STOP-NEW gate falls back to the legacy registered +
    admin-blocked path so the device is still controlled."""
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    cfg.dhcp.enable = True  # the DHCP-refusal fragment path
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # a FILE where a dir is needed
    cfg.dhcp.ignore_file = str(blocker / "quota-ignore.conf")
    cfg.dhcp.reload_dnsmasq = False
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_stop_new_connections(True))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("aa:bb:cc:dd:ee:61", "192.168.1.151"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:61"))
        assert dev is not None, "fallback must register the device"
        assert dev.block_state == db_mod.BLOCK_ADMIN, (
            "fallback device must be admin-blocked")
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_decline_random_macs_refuses_brand_new_randomized_device(tmp_path):
    """With "Decline random MACs" on, a brand-new device whose MAC is
    randomized (locally-administered bit) is refused at the DHCP level — no
    device row (no "unsigned user"), a fragment entry so dnsmasq never hands
    it an IP, and the just-issued lease is kernel-cut row-less. Real-OUI and
    already-registered devices are untouched."""
    from pathlib import Path
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    cfg.dhcp.enable = True  # the DHCP-refusal fragment path
    cfg.dhcp.ignore_file = str(tmp_path / "dnsmasq.d" / "quota-ignore.conf")
    cfg.dhcp.reload_dnsmasq = False
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        # an existing random-MAC device (joined before the gate) keeps identity
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("02:42:ac:11:00:02", "192.168.1.140"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="02:42:ac:11:00:02"))
        uid = dev.user_id

        asyncio.get_event_loop().run_until_complete(
            gw.service.set_decline_random_macs(True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.service.decline_random_macs()) is True

        # a brand-new randomized MAC is refused: no row, fragment entry,
        # row-less kernel cut of the lingering lease
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("02:42:ac:11:00:03", "192.168.1.141"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="02:42:ac:11:00:03")) is None, (
            "declined device must NOT be registered (no 'unsigned user')")
        refused = asyncio.get_event_loop().run_until_complete(
            gw.service.refused_random_macs())
        assert "02:42:ac:11:00:03" in refused
        fragment = Path(cfg.dhcp.ignore_file)
        assert fragment.exists(), "the DHCP-refusal fragment must be written"
        assert "dhcp-host=02:42:ac:11:00:03,ignore\n" in fragment.read_text(
            encoding="utf-8"), "the declined random MAC must be in the fragment"
        snap = asyncio.get_event_loop().run_until_complete(
            gw.service.snapshot_state())
        entry = snap.get("02:42:ac:11:00:03")
        assert entry is not None and entry["blocked"] is True, (
            "the lingering lease of a declined MAC must be kernel-cut")
        assert entry["block_state"] == _db.BLOCK_ADMIN

        # a real-OUI brand-new device joins normally
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("3c:7c:3f:aa:bb:cc", "192.168.1.142"))
        dev3 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="3c:7c:3f:aa:bb:cc"))
        assert dev3.block_state == db_mod.BLOCK_OK, (
            "a real-OUI device is never gated")

        # the pre-existing random-MAC device reconnects normally
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_lease("02:42:ac:11:00:02"))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("02:42:ac:11:00:02", "192.168.1.143"))
        dev4 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="02:42:ac:11:00:02"))
        assert dev4.user_id == uid, "existing device must keep its identity"
        assert dev4.block_state == db_mod.BLOCK_OK, (
            "an already-registered device must not be cut by the gate")

        # turning the gate off clears the fragment + refuse list; the next
        # brand-new random MAC registers normally (disabled onboarding)
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_decline_random_macs(False))
        asyncio.get_event_loop().run_until_complete(gw._sync_refuse_fragment())
        assert fragment.read_text(encoding="utf-8") == "", (
            "gate off must empty the DHCP-refusal fragment")
        assert not asyncio.get_event_loop().run_until_complete(
            gw.service.refused_random_macs())
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("02:42:ac:11:00:04", "192.168.1.144"))
        dev5 = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="02:42:ac:11:00:04"))
        assert dev5.block_state == db_mod.BLOCK_OK
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_decline_random_falls_back_to_registered_block_when_fragment_unwritable(tmp_path):
    """If the DHCP-refusal fragment can't be written (no root / no dnsmasq
    dir), the Decline-random gate falls back to the legacy registered +
    admin-blocked path so the device is still controlled."""
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    cfg.dhcp.enable = True  # the DHCP-refusal fragment path
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # a FILE where a dir is needed
    cfg.dhcp.ignore_file = str(blocker / "quota-ignore.conf")
    cfg.dhcp.reload_dnsmasq = False
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_decline_random_macs(True))
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease("02:42:ac:11:00:07", "192.168.1.155"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac="02:42:ac:11:00:07"))
        assert dev is not None, "fallback must register the device"
        assert dev.block_state == db_mod.BLOCK_ADMIN, (
            "fallback device must be admin-blocked")
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_new_device_quota_blocked_until_admin_assigns(tmp_path):
    """New onboarding contract: a brand-new DHCP device is quota-blocked
    (0 GB, DISABLED user) until the admin assigns shared or fixed — the
    legacy auto-share-on-join behavior is retired. ``_persist_lease`` still
    recomputes allowances so the map stays consistent for the existing users.
    """
    from quota import db as db_mod

    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    try:
        mac = "e6:2a:b3:09:b4:a8"  # the user's phone MAC
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.1.111"))

        # the auto-registered device owns a user with a 0 GB onboarding lock
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        assert dev is not None and dev.user_id is not None, (
            "auto-registered device must own a user")
        bundle = asyncio.get_event_loop().run_until_complete(
            gw.database.get_bundle())
        assert bundle.allowances.get(dev.user_id, -1) == 0.0, (
            "a fresh device must NOT claim an auto share — the admin assigns")

        # the engine cuts it: zero usage, zero allowance, disabled user
        changes = asyncio.get_event_loop().run_until_complete(
            gw.service.evaluate_blocks())
        assert any(ch.get("mac") == mac
                   and ch.get("state") == db_mod.BLOCK_QUOTA
                   for ch in changes), (
            "new device must be quota-blocked until the admin assigns a rule")

        # the admin assigns shared (the modal's action) -> the device goes live
        user = asyncio.get_event_loop().run_until_complete(
            gw.database.get_user(dev.user_id))
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_user(user.id, quota_mode=db_mod.QUOTA_AUTO))
        asyncio.get_event_loop().run_until_complete(
            gw.service.recompute_allowances())
        changes = asyncio.get_event_loop().run_until_complete(
            gw.service.evaluate_blocks())
        for ch in changes:
            assert not (ch.get("mac") == mac
                        and ch.get("state") == db_mod.BLOCK_QUOTA), (
                "a shared-assigned device must not be quota-blocked "
                "before using any data")
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
                             total_up, aqm, lan_rate_mbps=None):
                calls.append((rate_map, enabled, total_down, total_up, aqm,
                              lan_rate_mbps))

        gw.shaper = _FakeShaper()  # type: ignore[assignment]
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())

        assert len(calls) == 1, "one maintenance tick must sync the shaper"
        rate_map, enabled, total_down, total_up, aqm, lan_rate = calls[0]
        assert enabled is True
        assert total_down == 100.0 and total_up == 20.0
        assert aqm is True
        assert lan_rate == 1000.0   # DB default for the LAN pass-through rate
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

        # a LAN pass-through rate edit flows into the shaper immediately
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_shaping(enabled=True, lan_rate_mbps=250))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert calls[-1][-1] == 250.0
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_shaper_applies_default_guest_speed_cap(tmp_path):
    """A default guest speed cap (Mbps) becomes the aggregate ceiling for every
    guest user's rate-map entry: it caps an unlimited guest and tightens an
    explicit guest cap (min wins); non-guest users are untouched; 0 lifts it."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg, internet_probe=lambda: True)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        mac = "aa:bb:cc:dd:ee:77"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.2.120"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))

        # move the device under a guest account and enable shaping
        guest = asyncio.get_event_loop().run_until_complete(
            gw.database.create_user(name="", quota_mode="fixed",
                                    fixed_gb=1.0, guest=True))
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_device(dev.id, user_id=guest.id))
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
                             total_up, aqm, lan_rate_mbps=None):
                calls.append((rate_map, enabled, total_down, total_up, aqm,
                              lan_rate_mbps))

        gw.shaper = _FakeShaper()  # type: ignore[assignment]

        # 1) an unlimited guest is capped at the default guest speed
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_speed_limit(8))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        entry = calls[-1][0][0]
        assert entry["user_down"] == 8.0 and entry["user_up"] == 8.0
        assert entry["down"] == 0.0          # device cap untouched (unlimited)

        # 2) an explicit guest cap below the default wins (min)
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_user(guest.id, limit_down_mbps=4.0,
                                    limit_up_mbps=2.0))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        entry = calls[-1][0][0]
        assert entry["user_down"] == 4.0 and entry["user_up"] == 2.0

        # 3) default 0 = unlimited — no guest cap is applied
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_speed_limit(0))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        entry = calls[-1][0][0]
        assert entry["user_down"] == 4.0 and entry["user_up"] == 2.0

        # 4) a non-guest user is never clamped by the default guest cap
        other_mac = "aa:bb:cc:dd:ee:88"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(other_mac, "192.168.2.121"))
        # lift the explicit guest cap again so the default is the ceiling
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_user(guest.id, limit_down_mbps=0.0,
                                    limit_up_mbps=0.0))
        asyncio.get_event_loop().run_until_complete(
            gw.service.set_guest_speed_limit(8))
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        entries = {e["ip"]: e for e in calls[-1][0]}
        other = entries["192.168.2.121"]
        assert other["user_down"] == 0.0 and other["user_up"] == 0.0
        # the guest entry carries the default cap once its own cap is lifted
        assert entries["192.168.2.120"]["user_down"] == 8.0
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


class _FakeVpnManager:
    """Stands in for VpnShareManager in the wiring test: records the
    reconcile args, returns a scripted VpnShareStatus."""

    def __init__(self) -> None:
        self.calls: list[tuple[bool, str]] = []
        self.status = VpnShareStatus()

    def reconcile(self, enabled: bool, pin: str) -> VpnShareStatus:
        self.calls.append((enabled, pin))
        return self.status


class _FakeEngineRelay:
    """Records engine.set_gateway_allowed calls (the real NftablesEngine is
    disabled in hermetic configs; quota/nftables.py's own tests cover the
    gw_allowed program). ``stop`` satisfies shutdown()."""

    def __init__(self) -> None:
        self.allowed_calls: list[list[str]] = []
        self.gateway_allowed: tuple | None = None

    def set_gateway_allowed(self, ips: list[str]) -> None:
        self.allowed_calls.append(list(ips))
        self.gateway_allowed = tuple(ips)

    def stop(self) -> None:
        pass


def test_sync_vpn_share_pins_tunnel_and_allows_vpn_server(tmp_path):
    """The maintenance loop's VPN sync must: reconcile the manager toward the
    DB switch, PIN a detected tunnel so a multi-VPN box stays on the same
    interface, and while the relay is APPLIED feed the engine's gw_allowed
    whitelist from the learned VPN-server peers (so the box's own internet can
    be cut — Gateway OFF — without killing the household's tunnel)."""
    cfg = _cfg(tmp_path)
    cfg.vpn_share.tun2socks = False  # hermetic: the bridge is a real downloader
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        if gw._maintenance_task is not None:
            gw._maintenance_task.cancel()  # the background loop must not race
            try:
                loop.run_until_complete(gw._maintenance_task)
            except asyncio.CancelledError:
                pass
        fake_mgr = _FakeVpnManager()
        fake_engine = _FakeEngineRelay()
        gw.vpn_manager = fake_mgr
        gw.engine = fake_engine  # type: ignore[assignment]
        gw._vpn_learn = lambda _: {"1.2.3.4"}  # fake the `ss` auto-learn probe
        # switch on, no pin yet -> reconcile(enabled=True, pin=""), manager
        # reports utun4 -> the pin is persisted for the NEXT tick
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "1"))
        fake_mgr.status = VpnShareStatus(
            state="on", interface="utun4", candidates=["utun4"])
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_mgr.calls == [(True, "")]
        pin = loop.run_until_complete(
            gw.database.get_setting("vpn_share_interface", ""))
        assert pin == "utun4"
        # the relay IS applied -> the learned VPN server is whitelisted so it
        # stays reachable under a Gateway cut (and stays sticky across ticks)
        assert fake_engine.allowed_calls == [["1.2.3.4"]]
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_engine.allowed_calls[-1] == ["1.2.3.4"]  # sticky: unchanged
        assert gw._last_vpn_status["state"] == "on"
        # switch off -> reconcile(enabled=False, pinned utun4) and the
        # whitelist is cleared (a Gateway cut now blocks the box entirely)
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "0"))
        fake_mgr.status = VpnShareStatus(state="off")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_mgr.calls[2] == (False, "utun4")
        assert fake_engine.allowed_calls[-1] == []
        assert gw._last_vpn_status["state"] == "off"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_sync_vpn_share_keeps_whitelist_when_tunnel_blips(tmp_path):
    """A Gateway-cut box must keep its route to the VPN server even when the
    tunnel momentarily drops (VPN client reconnecting) — if the whitelist was
    cleared on tunnel state, the box could never re-dial the VPN and the
    household tunnel would die permanently. The whitelist is gated on the
    SWITCH (enabled), not the transient tunnel state; only the switch off
    clears it."""
    cfg = _cfg(tmp_path)
    cfg.vpn_share.tun2socks = False  # hermetic: the bridge is a real downloader
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        if gw._maintenance_task is not None:
            gw._maintenance_task.cancel()
            try:
                loop.run_until_complete(gw._maintenance_task)
            except asyncio.CancelledError:
                pass
        fake_mgr = _FakeVpnManager()
        fake_engine = _FakeEngineRelay()
        gw.vpn_manager = fake_mgr
        gw.engine = fake_engine  # type: ignore[assignment]
        gw._vpn_learn = lambda _: {"1.2.3.4"}
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "1"))
        # tunnel up -> whitelist learns the VPN server
        fake_mgr.status = VpnShareStatus(state="on", interface="xray_tun")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_engine.allowed_calls[-1] == ["1.2.3.4"]
        # tunnel blips (no-interface) while the SWITCH is still on: the learned
        # whitelist must survive so the box can re-dial the VPN server
        fake_mgr.status = VpnShareStatus(state="no-interface")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_engine.allowed_calls[-1] == ["1.2.3.4"]  # NOT cleared
        assert gw._last_vpn_status["state"] == "no-interface"
        # switch off -> whitelist cleared (the cut blocks the box entirely)
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "0"))
        fake_mgr.status = VpnShareStatus(state="off")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_engine.allowed_calls[-1] == []
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


class _FakeTun2socks:
    """Stands in for Tun2socksManager in the wiring test: records the
    reconcile args, returns a scripted Tun2socksStatus."""

    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.status = Tun2socksStatus()
        self.interface = "tun0"  # mirrors Tun2socksManager.interface

    def reconcile(self, active: bool) -> Tun2socksStatus:
        self.calls.append(active)
        return self.status if active else Tun2socksStatus()


def test_sync_vpn_share_bridges_userspace_vpn_with_tun2socks(tmp_path):
    """VPN share on a userspace-netstack client (v2rayN — no kernel tun ever
    appears): the routing manager is reconciled FIRST and finds nothing, so
    the tun2socks bridge auto-provisioner is engaged as the FALLBACK and the
    routing is RETRIED with the bridge interface as the pin. While the bridge
    device carries the subnet it is kept (never stopped — the child owns that
    tun). The bridge status rides the cached vpn status; turning the share
    off stops the bridge too."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        if gw._maintenance_task is not None:
            gw._maintenance_task.cancel()  # the background loop must not race
            try:
                loop.run_until_complete(gw._maintenance_task)
            except asyncio.CancelledError:
                pass
        fake_mgr = _FakeVpnManager()
        fake_ts = _FakeTun2socks()
        fake_engine = _FakeEngineRelay()
        gw.vpn_manager = fake_mgr
        gw.tun2socks_manager = fake_ts
        gw.engine = fake_engine  # type: ignore[assignment]
        gw._vpn_learn = lambda _: set()
        # switch on: routing first finds no kernel tunnel, then the bridge is
        # engaged and the routing retried with the bridge device as the pin
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "1"))
        fake_mgr.status = VpnShareStatus(state="no-interface")
        fake_ts.status = Tun2socksStatus(
            state="running", message="sharing the VPN client through tun0",
            proxy="127.0.0.1:10808", interface="tun0")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_ts.calls == [True]  # bridge engaged only as a fallback
        assert fake_mgr.calls == [(True, ""), (True, "tun0")]
        assert gw._last_vpn_status["state"] == "no-interface"
        assert gw._last_vpn_status["tun2socks"]["state"] == "running"
        assert gw._last_vpn_status["tun2socks"]["interface"] == "tun0"
        # bridge up + routing now succeeds -> the bridge's own device keeps the
        # child alive (never stopped) and the pin is persisted
        fake_mgr.status = VpnShareStatus(state="on", interface="tun0")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_ts.calls == [True, True]  # kept, not stopped
        assert fake_mgr.calls[-1] == (True, "")
        pin = loop.run_until_complete(
            gw.database.get_setting("vpn_share_interface", ""))
        assert pin == "tun0"
        assert gw._last_vpn_status["state"] == "on"
        # switch off -> the routing is removed AND the bridge child is stopped
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "0"))
        fake_mgr.status = VpnShareStatus(state="off")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_ts.calls == [True, True, False]
        assert fake_mgr.calls[-1] == (False, "tun0")
        assert gw._last_vpn_status["tun2socks"]["state"] == "off"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_sync_vpn_share_kernel_tunnel_wins_over_bridge(tmp_path):
    """A REAL kernel tunnel (xray/sing-box/WireGuard tun) must win over the
    tun2socks bridge: the routing manager is reconciled FIRST, and when it
    routes into a kernel tunnel the bridge is NOT engaged (and a leftover
    bridge whose device is NOT the routed one is stopped so it can't squat a
    second tun). A kernel-TUN VPN client therefore needs no config edits and
    never downloads the bridge."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        if gw._maintenance_task is not None:
            gw._maintenance_task.cancel()  # the background loop must not race
            try:
                loop.run_until_complete(gw._maintenance_task)
            except asyncio.CancelledError:
                pass
        fake_mgr = _FakeVpnManager()
        fake_ts = _FakeTun2socks()
        fake_engine = _FakeEngineRelay()
        gw.vpn_manager = fake_mgr
        gw.tun2socks_manager = fake_ts
        gw.engine = fake_engine  # type: ignore[assignment]
        gw._vpn_learn = lambda _: {"1.2.3.4"}
        # switch on with a REAL kernel tunnel present -> routing routes into it
        # directly; the bridge is never engaged (no download, no spawn)
        loop.run_until_complete(gw.database.set_setting(
            "vpn_share_enabled", "1"))
        fake_mgr.status = VpnShareStatus(
            state="on", interface="xray_tun", candidates=["xray_tun"])
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_ts.calls == [False]  # idempotent keep-stopped, never spawn
        assert fake_mgr.calls == [(True, "")]
        assert gw._last_vpn_status["state"] == "on"
        assert gw._last_vpn_status["interface"] == "xray_tun"
        assert gw._last_vpn_status["tun2socks"]["state"] == "off"
        # a stale bridge whose device is NOT the real tunnel is stopped, so a
        # junk/second tun can never divert the subnet away from xray_tun
        fake_ts.calls.clear()
        fake_ts.status = Tun2socksStatus(state="running", interface="tun0")
        fake_mgr.status = VpnShareStatus(state="on", interface="xray_tun")
        loop.run_until_complete(gw._sync_vpn_share())
        assert fake_ts.calls == [False]  # stopped as redundant
        assert fake_mgr.calls[-1] == (True, "xray_tun")  # pin persisted
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


def test_wan_renew_tick_fires_after_interval(tmp_path, monkeypatch):
    """v24: the WAN auto-renew schedule restarts the PPPoE dial once the
    interval has elapsed since the last renewal (ppp0 up + enabled + WAN)."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    restarts: list[dict] = []
    gw, loop = _boot_wan_gateway(
        tmp_path, monkeypatch,
        ppp="up",
        renew={"enabled": True, "minutes": 30,
               "last": (datetime.now(timezone.utc) - timedelta(minutes=40))
               .isoformat()},
        restarter=lambda: restarts.append({}) or
        {"restarted": True, "state": "active", "detail": "dialed"})
    try:
        loop.run_until_complete(gw._wan_ip_renew_tick())
        assert restarts, "the interval elapsed — the dial must restart"
        # the renewal timestamp is persisted so the countdown restarts now
        last = loop.run_until_complete(
            gw.database.get_setting("wan_ip_renew_last", ""))
        assert last, "mark_wan_renew must persist the new timestamp"
        datetime.fromisoformat(last)
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_renew_tick_skips_when_disabled(tmp_path, monkeypatch):
    """Enabled off -> the schedule never restarts the dial."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    restarts: list[dict] = []
    gw, loop = _boot_wan_gateway(
        tmp_path, monkeypatch, ppp="up",
        renew={"enabled": False, "minutes": 30, "last": ""},
        restarter=lambda: restarts.append({}) or
        {"restarted": True, "state": "active", "detail": ""})
    try:
        loop.run_until_complete(gw._wan_ip_renew_tick())
        assert restarts == [], "disabled schedule must never fire"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_renew_tick_skips_when_ppp0_down(tmp_path, monkeypatch):
    """v24: a down ppp0 gates the schedule — restarting the dial would just
    reconnect to a dead line (and hammer the modem every tick otherwise)."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "down", "local": "",
                                         "peer": ""})
    restarts: list[dict] = []
    gw, loop = _boot_wan_gateway(
        tmp_path, monkeypatch, ppp="down",
        renew={"enabled": True, "minutes": 5, "last": ""},
        restarter=lambda: restarts.append({}) or
        {"restarted": True, "state": "active", "detail": ""})
    try:
        loop.run_until_complete(gw._wan_ip_renew_tick())
        assert restarts == [], "a down line must never be renewed"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_renew_tick_skips_in_lan_mode(tmp_path, monkeypatch):
    """LAN topology has no ppp0 — the schedule is a no-op even when enabled."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    restarts: list[dict] = []
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        loop.run_until_complete(
            gw.database.set_setting("wan_ip_renew_enabled", "1"))
        gw._pppoe_restart = lambda: restarts.append({}) or \
            {"restarted": True, "state": "active", "detail": ""}
        loop.run_until_complete(gw._wan_ip_renew_tick())
        assert restarts == [], "LAN mode has no ppp0 to restart"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_renew_tick_fires_when_never_renewed(tmp_path, monkeypatch):
    """No wan_ip_renew_last yet -> fire immediately (the countdown starts now),
    so a freshly-enabled schedule does not wait for an unknown last time."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    restarts: list[dict] = []
    gw, loop = _boot_wan_gateway(
        tmp_path, monkeypatch, ppp="up",
        renew={"enabled": True, "minutes": 60, "last": ""},
        restarter=lambda: restarts.append({}) or
        {"restarted": True, "state": "active", "detail": ""})
    try:
        loop.run_until_complete(gw._wan_ip_renew_tick())
        assert restarts, "never renewed — the first tick must fire"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_renew_tick_resumes_countdown_after_restart(tmp_path, monkeypatch):
    """The last-renewal timestamp is read from the DB, so a gateway restart
    mid-schedule must NOT re-renew (the countdown continues from the persisted
    timestamp — a just-renewed line stays quiet)."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    restarts: list[dict] = []
    gw, loop = _boot_wan_gateway(
        tmp_path, monkeypatch, ppp="up",
        renew={"enabled": True, "minutes": 60,
               "last": (datetime.now(timezone.utc) - timedelta(minutes=2))
               .isoformat()},
        restarter=lambda: restarts.append({}) or
        {"restarted": True, "state": "active", "detail": ""})
    try:
        loop.run_until_complete(gw._wan_ip_renew_tick())
        assert restarts == [], "2 min elapsed vs a 60 min interval — no fire"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_renew_manual_restart_records_timestamp(tmp_path):
    """The manual Restart button calls _renew_wan_ip directly: it runs the
    restarter, updates the last-renewed state, and persists the timestamp
    (restart-resume)."""
    gw = Gateway(_cfg(tmp_path))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        gw._pppoe_restart = lambda: {"restarted": True, "state": "active",
                                     "detail": "dialed"}
        result = loop.run_until_complete(gw._renew_wan_ip())
        assert result["restarted"] is True
        assert gw._last_wan_renew == result
        last = loop.run_until_complete(
            gw.database.get_setting("wan_ip_renew_last", ""))
        assert last, "a manual renewal must persist its timestamp"
        datetime.fromisoformat(last)
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


def test_wan_status_carries_renew_schedule(tmp_path, monkeypatch):
    """The WS snapshot / GET /api/wan carry the auto-renew config so the WAN
    tab renders the toggle + last-renewed line without a separate query."""
    monkeypatch.setattr("run.detect_ppp",
                        lambda *a, **k: {"state": "up", "local": "1.2.3.4",
                                         "peer": ""})
    last_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    gw, loop = _boot_wan_gateway(
        tmp_path, monkeypatch, ppp="up",
        renew={"enabled": True, "minutes": 60, "last": last_iso},
        restarter=lambda: {"restarted": True, "state": "active", "detail": ""})
    try:
        loop.run_until_complete(gw._maintenance_tick())
        ws = dict(gw.holder.get().wan_status or {})
        assert ws["renew_enabled"] is True
        assert ws["renew_minutes"] == 60
        assert ws["renew_last"] == last_iso  # rides the snapshot, never reset
        assert ws["ppp0"] == "up"
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()


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
# delete -> MAC blacklist (deny list) + the protected Gateway user's accounting
# --------------------------------------------------------------------------- #

def test_blacklisted_mac_is_not_re_registered(tmp_path):
    """A manually-deleted device stays deleted while its device is still on the
    network: _persist_lease skips a blacklisted (deny-listed) MAC (checked
    before guest mode), for ANY owner — guest or normal."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    # guest mode ON: a new device auto-registers as a GUEST (a delete records
    # the deny list for every owner, guest or normal)
    asyncio.get_event_loop().run_until_complete(gw.service.set_guest_mode(True))
    _cancel_maintenance(gw)  # the background loop must not race the manual calls
    try:
        mac = "aa:bb:cc:dd:ee:81"
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.41"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        assert dev is not None, "first sighting auto-registers as a guest"
        # delete + blacklist, exactly what DELETE /api/devices does
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_device(dev.id, deny_list_mac=True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_mac_list("deny")) == [mac]

        # the device is still connected: another lease tick must NOT resurrect it
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.41"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac)) is None, \
            "blacklisted MAC must not be re-registered while still connected"
        # ...and its row-less entry is kernel-blocked through snapshot_state
        snap = asyncio.get_event_loop().run_until_complete(gw.service.snapshot_state())
        assert snap[mac]["blocked"] is True
        assert snap[mac]["ip"] == "192.168.2.41"
        # a NORMAL (non-guest) owner's delete blacklists too
        n = asyncio.get_event_loop().run_until_complete(
            gw.database.create_user(name="Dad", quota_mode=_db.QUOTA_FIXED,
                                    fixed_gb=20.0))
        ndev = asyncio.get_event_loop().run_until_complete(
            gw.database.upsert_device("aa:bb:cc:dd:ee:83", user_id=n.id))
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_device(ndev.id, deny_list_mac=True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_mac_list("deny")) == [mac, "aa:bb:cc:dd:ee:83"]
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_blacklist_survives_lease_drop(tmp_path):
    """The deny list is PERMANENT: when the device genuinely leaves (its lease
    disappears from dnsmasq's file), the blacklist is NOT cleared — a future
    reconnect stays blocked until the admin removes the MAC in the Network
    tab."""
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
            gw.database.delete_device(dev.id, deny_list_mac=True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_mac_list("deny")) == [mac]

        # the device leaves the network: the lease file no longer lists it
        # (other devices still are — a transiently EMPTY lease file is a
        # dnsmasq restart and must NOT clear any deny row either)
        lease_file.write_text(
            "1730000000 aa:bb:cc:dd:ee:99 192.168.2.99 tablet 01:aa:bb:cc:dd:ee:99\n",
            encoding="utf-8")
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_mac_list("deny")) == [mac]

        # reconnecting now does NOT register a fresh account — still blacklisted
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.42"))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac)) is None, \
            "a blacklisted MAC stays blocked after reconnect"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_unblacklist_re_registers_device(tmp_path):
    """Removing a MAC from the deny list (Network tab) unblocks + re-registers:
    the next lease tick mints a fresh account (the ONLY way back in)."""
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    asyncio.get_event_loop().run_until_complete(gw.service.set_guest_mode(True))
    _cancel_maintenance(gw)
    try:
        mac = "aa:bb:cc:dd:ee:84"
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.44"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        asyncio.get_event_loop().run_until_complete(
            gw.database.delete_device(dev.id, deny_list_mac=True))
        assert asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac)) is None

        # un-blacklist (the Network-tab save replaces the whole deny list)
        asyncio.get_event_loop().run_until_complete(
            gw.database.set_mac_list("deny", []))
        asyncio.get_event_loop().run_until_complete(gw._persist_lease(mac, "192.168.2.44"))
        redev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        assert redev is not None, "un-blacklisting must re-register the device"
        assert asyncio.get_event_loop().run_until_complete(
            gw.service.snapshot_state())[mac]["blocked"] is False
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

        # both devices register from the lease file (same user, auto-created
        # in the DISABLED onboarding lock — the admin must assign shared or
        # fixed before anything goes online)
        asyncio.get_event_loop().run_until_complete(gw._sync_dnsmasq_leases())
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert nft.blocked == {"192.168.2.61", "192.168.2.62"}, (
            "fresh devices are kernel-cut until the admin assigns a quota rule")

        # the admin assigns shared to the auto-created users -> both go live
        devs = asyncio.get_event_loop().run_until_complete(gw.database.list_devices())
        for uid in {d.user_id for d in devs if d.user_id is not None}:
            asyncio.get_event_loop().run_until_complete(
                gw.database.update_user(uid, quota_mode=_db.QUOTA_AUTO))
        asyncio.get_event_loop().run_until_complete(
            gw.service.recompute_allowances())
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        assert not nft.blocked, "shared-assigned devices are online"

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
                             total_up, aqm, lan_rate_mbps=None):
                calls.append((rate_map, enabled, total_down, total_up, aqm,
                              lan_rate_mbps))

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
        rate_map, enabled, total_down, total_up, aqm, lan_rate = calls[0]
        assert enabled is True and total_down == 100.0 and total_up == 20.0
        assert lan_rate == 1000.0
        assert rate_map[0]["ip"] == "192.168.2.130"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


# ---------------------------------------------------------------------------
# DNS browsing history (quota/dnslog.py tailer drained by the maintenance tick)
# ---------------------------------------------------------------------------

def _history_cfg(tmp_path, dnslog: str) -> cfg_mod.Config:
    cfg = _cfg(tmp_path)
    cfg.history.enabled = True
    cfg.history.dnsmasq_log_file = dnslog
    cfg.history.retention_days = 7
    return cfg


def _wait_for_events(tailer, count: int, timeout: float = 3.0) -> None:
    """Wait until the tailer has queued ``count`` events (peeks ``qsize()`` —
    the tick under test must drain them itself, so this never consumes)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and tailer.q.qsize() < count:
        time.sleep(0.1)
    assert tailer.q.qsize() >= count, (
        f"tailer queued only {tailer.q.qsize()}/{count} events in "
        f"{timeout}s — is the log file being appended?")


def test_dns_history_tick_drains_and_upserts(tmp_path):
    """A dnsmasq query log line becomes a dns_history row for the device that
    owns the requestor IP, bucketed by minute/domain."""
    dnslog = str(tmp_path / "dnslog.log")
    Path(dnslog).write_bytes(b"")
    cfg = _history_cfg(tmp_path, dnslog)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        assert gw.dnslog is not None
        mac = "aa:bb:cc:dd:ee:55"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.2.155"))
        with open(dnslog, "a", encoding="utf-8") as fh:
            fh.write("query[A] example.com from 192.168.2.155\n"
                     "query[A] example.com from 192.168.2.155\n"
                     "query[AAAA] example.com from 192.168.2.155\n"
                     "query[A] other.net from 192.168.2.155\n")
        _wait_for_events(gw.dnslog, 4)
        asyncio.get_event_loop().run_until_complete(gw._dns_history_tick())
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        hist = asyncio.get_event_loop().run_until_complete(
            gw.database.get_dns_history(dev.id, "2020-01-01 00:00"))
        assert hist["total"] == 4
        top = {t["domain"]: t["hits"] for t in hist["top_domains"]}
        assert top["example.com"] == 3
        assert top["other.net"] == 1
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_dns_history_tick_persists_offset_state(tmp_path):
    """Each drain persists the tailer's read cursor so a restart resumes — the
    pre-feature lines never re-attributed."""
    dnslog = str(tmp_path / "dnslog.log")
    Path(dnslog).write_bytes(b"")
    cfg = _history_cfg(tmp_path, dnslog)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        with open(dnslog, "a", encoding="utf-8") as fh:
            fh.write("query[A] one.com from 192.168.2.155\n")
        _wait_for_events(gw.dnslog, 1)
        asyncio.get_event_loop().run_until_complete(gw._dns_history_tick())
        state = asyncio.get_event_loop().run_until_complete(
            gw.database.get_setting("dnslog_state", "{}"))
        import json as _json
        saved = _json.loads(state)
        assert saved["inode"] == os.stat(dnslog).st_ino
        assert saved["offset"] == os.stat(dnslog).st_size
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_dns_history_prune_runs_on_hourly_gate(tmp_path):
    """Past the hourly gate, each user's rows older than THEIR retention are
    deleted (per-user cutoff, not a global one)."""
    dnslog = str(tmp_path / "dnslog.log")
    Path(dnslog).write_bytes(b"")
    cfg = _history_cfg(tmp_path, dnslog)
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        # a user with a shorter per-user retention (1 day) — set via
        # update_user (create_user has no history_days kwarg)
        uid = asyncio.get_event_loop().run_until_complete(
            gw.database.create_user("short")).id
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_user(uid, history_days=1))
        mac = "aa:bb:cc:dd:ee:56"
        asyncio.get_event_loop().run_until_complete(
            gw._persist_lease(mac, "192.168.2.156"))
        dev = asyncio.get_event_loop().run_until_complete(
            gw.database.get_device(mac=mac))
        asyncio.get_event_loop().run_until_complete(
            gw.database.update_device(dev.id, user_id=uid))
        # a row far older than the 1-day retention + one at "now"
        now_minute = time.strftime("%Y-%m-%d %H:%M")
        asyncio.get_event_loop().run_until_complete(
            gw.database.batch_add_dns_history(
                [(dev.id, "2026-07-31 10:00", "old.com", 1),
                 (dev.id, now_minute, "new.com", 1)]))
        # feed one live event so the tick passes the empty-queue early-return
        # and reaches the hourly prune gate (which we force open)
        with open(dnslog, "a", encoding="utf-8") as fh:
            fh.write("query[A] live.com from 192.168.2.156\n")
        _wait_for_events(gw.dnslog, 1)
        gw._last_dns_prune = time.monotonic() - 3601.0
        asyncio.get_event_loop().run_until_complete(gw._dns_history_tick())
        hist = asyncio.get_event_loop().run_until_complete(
            gw.database.get_dns_history(dev.id, "2026-01-01 00:00"))
        domains = {t["domain"] for t in hist["top_domains"]}
        assert "old.com" not in domains, "the stale row is pruned"
        assert "new.com" in domains and "live.com" in domains
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())


def test_dns_history_disabled_does_nothing(tmp_path):
    """cfg.history.enabled: false => no tailer is built and the maintenance
    tick's guard skips the history drain (no crash, no persisted cursor)."""
    cfg = _cfg(tmp_path)
    assert cfg.history.enabled is False
    gw = Gateway(cfg)
    asyncio.get_event_loop().run_until_complete(gw.startup())
    _cancel_maintenance(gw)
    try:
        assert gw.dnslog is None, "no tailer is built when history is disabled"
        asyncio.get_event_loop().run_until_complete(gw._maintenance_tick())
        state = asyncio.get_event_loop().run_until_complete(
            gw.database.get_setting("dnslog_state", "{}"))
        assert state == "{}", "no read cursor is persisted when disabled"
    finally:
        asyncio.get_event_loop().run_until_complete(gw.shutdown())
