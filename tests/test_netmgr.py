"""TopologyManager tests — the dashboard WAN-tab LAN/WAN switch (v19).

Everything external is injected (run_command / spawn_restart / addr_cmd), so
the full apply flow is exercised with fakes, root-free, on any OS. The two
invariants this module exists to guarantee are pinned here:

1. config.yaml and the DB are written TOGETHER in one apply, so the next boot
   cannot pick one and ignore the other (the v18 revert bug).
2. The LAN reality survives a WAN experiment — ``lan_*`` snapshot keys let the
   Revert button restore exactly what was there, never a guess.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import yaml

from core import config as cfg_mod
from quota import db as _db
from quota.netmgr import (
    DEFAULT_LAN_CIDR,
    DEFAULT_LAN_DNS,
    DEFAULT_LAN_ROUTER,
    DEFAULT_UPLINK_IP,
    TopologyManager,
)

#: A realistic LAN box: router 192.168.1.1, box uplink 192.168.1.110/24,
#: clients on their own 192.168.2.0/24 subnet.
CLIENT_NET = "192.168.2.0/24"
UPLINK_NET = "192.168.1.0/24"
UPLINK_IP = "192.168.1.110"
ROUTER = "192.168.1.1"
DNS = ["192.168.1.1", "8.8.8.8"]


def _cfg(tmp_path: Path) -> cfg_mod.Config:
    cfg = cfg_mod.Config()
    cfg.db_path = str(tmp_path / "data" / "netmgr.db")
    cfg.log_file = str(tmp_path / "logs" / "netmgr.log")
    dhcp = cfg.dhcp
    dhcp.enable = True
    dhcp.gateway_ip = "192.168.2.1"
    dhcp.router_ip = ROUTER
    dhcp.dns_servers = list(DNS)
    dhcp.subnet = "255.255.255.0"
    dhcp.pool_start = "192.168.2.100"
    dhcp.pool_end = "192.168.2.200"
    dhcp.lease_hours = 24
    dhcp.lease_file = str(tmp_path / "dnsmasq.leases")
    dhcp.lan_router_ip = ROUTER
    dhcp.lan_dns_servers = list(DNS)
    dhcp.uplink_ip = UPLINK_IP
    dhcp.lan_cidr = 24
    engine = cfg.engine
    engine.client_subnet = CLIENT_NET
    engine.uplink_subnet = UPLINK_NET
    engine.topology = "lan"
    engine.gateway_arp_lock = True
    engine.lan_gateway_arp_lock = True
    shaping = cfg.shaping
    shaping.enabled = True
    shaping.interface = "eth0"
    shaping.client_subnet = CLIENT_NET
    shaping.ifb = "ifb0"
    cfg.web.host = "127.0.0.1"
    cfg.web.port = 0
    return cfg


class _FakeApplier:
    """Records every applier invocation (env) and returns a canned rc/out."""

    def __init__(self, rc: int = 0, out: str = "ok"):
        self.rc = rc
        self.out = out
        self.calls: list[dict[str, str]] = []
        self.scripts: list[str] = []  # cmd[1] per invocation (topology.sh / test_pppoe.sh)

    def __call__(self, cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
        # only the real scripts may be invoked — fake-run, never executed
        assert cmd[:1] == ["bash"]
        assert Path(cmd[1]).name in ("topology.sh", "test_pppoe.sh")
        self.scripts.append(cmd[1])
        self.calls.append(dict(env))
        return self.rc, self.out


def _make_manager(cfg: cfg_mod.Config, tmp_path: Path, rc: int = 0, out: str = "ok",
                  ) -> tuple[TopologyManager, _FakeApplier, list[bool], _db.Database]:
    database = _db.Database(cfg.db_path)
    applier = _FakeApplier(rc=rc, out=out)
    applier.script = Path(tmp_path) / "topology.sh"
    applier.script.write_text("#!/bin/sh\n", encoding="utf-8")
    restarts: list[bool] = []
    manager = TopologyManager(
        cfg, database,
        config_path=tmp_path / "config.yaml",
        script_path=applier.script,
        run_command=applier,
        spawn_restart=lambda: restarts.append(True),
        addr_cmd=lambda: "2: eth0    inet 192.168.1.110/24 brd 192.168.1.255 scope global eth0\n",
    )
    return manager, applier, restarts, database


def _loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _config_yaml(tmp_path: Path) -> dict:
    with open(tmp_path / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ------------------------------------------------------------- lan_values


def test_lan_values_from_snapshot():
    """The ``lan_*`` keys written by the setup script / a prior apply are the
    source of truth for a revert — the exact router, DNS, uplink and ARP state."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        assert lan["router_ip"] == ROUTER
        assert lan["dns_servers"] == DNS
        assert lan["uplink_ip"] == UPLINK_IP
        assert lan["lan_cidr"] == 24
        assert lan["uplink_subnet"] == UPLINK_NET
        assert lan["gateway_arp_lock"] is True


def test_lan_values_fallback_to_setup_defaults():
    """A box that was switched to WAN directly by the setup script has no
    ``lan_*`` snapshot — the manager falls back to the setup-script defaults,
    i.e. the LAN it booted with."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.dhcp.lan_router_ip = ""
        cfg.dhcp.lan_dns_servers = []
        cfg.dhcp.uplink_ip = ""
        cfg.engine.lan_gateway_arp_lock = True
        manager = TopologyManager(cfg, _db.Database(cfg.db_path),
                                  addr_cmd=lambda: "")
        lan = manager.lan_values()
        assert lan["router_ip"] == DEFAULT_LAN_ROUTER
        assert lan["dns_servers"] == DEFAULT_LAN_DNS
        assert lan["uplink_ip"] == DEFAULT_UPLINK_IP
        assert lan["lan_cidr"] == DEFAULT_LAN_CIDR
        assert lan["gateway_arp_lock"] is True


# ------------------------------------------------------------ render_config


def test_render_config_lan_restores_active_keys():
    """Rendering LAN puts the snapshot back into the ACTIVE keys — the revert
    restores the exact router/DNS/uplink-subnet/ARP state, not a guess."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        data = yaml.safe_load(manager.render_config("lan", lan))
        dhcp, engine = data["dhcp"], data["engine"]
        assert dhcp["router_ip"] == ROUTER
        assert dhcp["dns_servers"] == DNS
        assert engine["uplink_subnet"] == UPLINK_NET
        assert engine["topology"] == "lan"
        assert engine["gateway_arp_lock"] is True
        # the LAN-reality snapshot is preserved in BOTH topologies
        assert dhcp["lan_router_ip"] == ROUTER
        assert dhcp["lan_dns_servers"] == DNS
        assert dhcp["uplink_ip"] == UPLINK_IP
        assert engine["lan_gateway_arp_lock"] is True


def test_render_config_wan_erases_active_keys():
    """Rendering WAN erases the router/uplink from the ACTIVE keys (the box
    terminates the WAN itself) but keeps the ``lan_*`` snapshot for a revert."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        data = yaml.safe_load(manager.render_config("wan", lan))
        dhcp, engine = data["dhcp"], data["engine"]
        assert dhcp["router_ip"] == ""
        assert dhcp["dns_servers"] == ["8.8.8.8"]  # the public resolver only
        assert engine["uplink_subnet"] == ""
        assert engine["topology"] == "wan"
        assert engine["gateway_arp_lock"] is False
        # snapshot survives the switch
        assert dhcp["lan_router_ip"] == ROUTER
        assert dhcp["lan_dns_servers"] == DNS
        assert engine["lan_gateway_arp_lock"] is True


def test_render_config_preserves_other_values():
    """A bundle edit or a custom DHCP pool must survive an apply — only the
    topology keys flip."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.bundle.total_gb = 77.0
        cfg.bundle.reset_day = 0
        cfg.bundle.period_type = "end_of_month"
        cfg.dhcp.pool_start = "192.168.2.50"
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        data = yaml.safe_load(manager.render_config("lan", lan))
        assert data["bundle"] == {"total_gb": 77.0, "reset_day": 0,
                                  "period_type": "end_of_month"}
        assert data["dhcp"]["pool_start"] == "192.168.2.50"
        assert data["web"]["port"] == 0
        assert data["shaping"]["interface"] == "eth0"


def test_render_config_carries_history_block():
    """A WAN/LAN apply is a selective rebuild — the ``history`` block must
    survive it (the v19.1 data-loss bug class: render dropped unknown keys)."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.history.enabled = True
        cfg.history.dnsmasq_log_file = "/var/log/quota-dnsmasq.log"
        cfg.history.retention_days = 7
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        for topology in ("lan", "wan"):
            data = yaml.safe_load(manager.render_config(topology, lan))
            assert data["history"] == {
                "enabled": True,
                "dnsmasq_log_file": "/var/log/quota-dnsmasq.log",
                "retention_days": 7,
            }, topology
        # and a disabled block survives too
        cfg.history.enabled = False
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        data = yaml.safe_load(manager.render_config("lan", lan))
        assert data["history"]["enabled"] is False


# -------------------------------------------------------------- applier_env


def test_applier_env_creds_go_through_env_not_argv():
    """PPPoE credentials travel to the applier via the environment, never argv,
    so an ISP password cannot be read out of ``ps``."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        env = manager.applier_env("wan", lan, pppoe_user="u@isp",
                                  pppoe_password="s3cret", wan_if="eth1")
        assert env["TOPO"] == "wan"
        assert env["PPPOE_USER"] == "u@isp"
        assert env["PPPOE_PASSWORD"] == "s3cret"
        assert env["WAN_IF"] == "eth1"
        assert env["LAN_IF"] == "eth0"  # from the shaping.interface key
        assert env["CLIENT_NET"] == CLIENT_NET
        assert env["WAN_GATEWAY"] == ROUTER
        # the LAN snapshot drives the applier's uplink values
        assert env["LAN_IP"] == UPLINK_IP
        assert env["LAN_CIDR"] == "24"


# ------------------------------------------------------------------- apply


def test_apply_wan_then_lan_roundtrip():
    """The full v19 flow, both directions, with fakes: WAN apply writes
    config.yaml + the DB together, runs the applier with the creds, schedules
    a restart; the LAN revert restores the EXACT router/DNS/ARP/uplink-subnet
    from the snapshot."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager, applier, restarts, database = _make_manager(cfg, Path(td))
        _loop_run(database.connect())
        try:
            # ---- WAN apply ----
            result = _loop_run(manager.apply("wan", pppoe_user="u@isp",
                                             pppoe_password="s3cret"))
            assert result["applied"] == "wan"
            assert result["restart_scheduled"] is True
            assert result["script_rc"] == 0
            assert len(applier.calls) == 1
            assert applier.calls[0]["PPPOE_USER"] == "u@isp"
            assert applier.calls[0]["PPPOE_PASSWORD"] == "s3cret"
            assert restarts == [True]

            data = _config_yaml(Path(td))
            assert data["dhcp"]["router_ip"] == ""
            assert data["dhcp"]["dns_servers"] == ["8.8.8.8"]
            assert data["engine"]["topology"] == "wan"
            assert data["engine"]["gateway_arp_lock"] is False
            # snapshot intact for the revert
            assert data["dhcp"]["lan_router_ip"] == ROUTER

            source = _loop_run(database.get_setting("topology_source", ""))
            topo = _loop_run(database.get_setting("topology", ""))
            assert (source, topo) == ("dashboard", "wan")
            # the entered PPPoE credentials are remembered for the WAN tab prefill
            assert _loop_run(database.get_setting("pppoe_user", "")) == "u@isp"
            assert _loop_run(database.get_setting("pppoe_password", "")) == "s3cret"
            assert _loop_run(database.get_setting("wan_if", "")) == ""

            # ---- LAN revert ----
            result = _loop_run(manager.apply("lan"))
            assert result["applied"] == "lan"
            assert len(applier.calls) == 2
            assert applier.calls[1]["PPPOE_USER"] == ""
            assert restarts == [True, True]

            data = _config_yaml(Path(td))
            assert data["dhcp"]["router_ip"] == ROUTER
            assert data["dhcp"]["dns_servers"] == DNS
            assert data["engine"]["topology"] == "lan"
            assert data["engine"]["uplink_subnet"] == UPLINK_NET
            assert data["engine"]["gateway_arp_lock"] is True

            source = _loop_run(database.get_setting("topology_source", ""))
            topo = _loop_run(database.get_setting("topology", ""))
            assert (source, topo) == ("dashboard", "lan")

            events = _loop_run(database.list_events())
            assert any("WAN topology set to wan" in e["message"] for e in events)
            assert any("WAN topology set to lan" in e["message"] for e in events)
        finally:
            _loop_run(database.close())


def test_apply_revert_preserves_saved_pppoe_creds():
    """REGRESSION (live box report): the WAN tab showed empty creds because
    every panel apply overwrote the DB settings with ``""`` — a "Revert to LAN"
    posts just ``{topology: "lan"}`` (no creds), and apply() unconditionally
    saved ``pppoe_user or ""``, erasing the credentials the prefill reads. A
    revert (or any apply with empty fields) must KEEP the last-known creds;
    only a non-empty value updates them."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager, applier, restarts, database = _make_manager(cfg, Path(td))
        _loop_run(database.connect())
        try:
            # WAN apply WITH creds -> saved
            _loop_run(manager.apply("wan", pppoe_user="u@isp",
                                    pppoe_password="s3cret", wan_if="eth1"))
            assert _loop_run(database.get_setting("pppoe_user", "")) == "u@isp"
            assert _loop_run(database.get_setting("pppoe_password", "")) == "s3cret"
            assert _loop_run(database.get_setting("wan_if", "")) == "eth1"

            # LAN revert with EMPTY creds -> the saved creds must survive
            _loop_run(manager.apply("lan"))
            assert _loop_run(database.get_setting("pppoe_user", "")) == "u@isp"
            assert _loop_run(database.get_setting("pppoe_password", "")) == "s3cret"
            assert _loop_run(database.get_setting("wan_if", "")) == "eth1"

            # an apply WITH a new (non-empty) password updates it
            _loop_run(manager.apply("wan", pppoe_user="u@isp",
                                    pppoe_password="newpass"))
            assert _loop_run(database.get_setting("pppoe_password", "")) == "newpass"
            assert _loop_run(database.get_setting("pppoe_user", "")) == "u@isp"
        finally:
            _loop_run(database.close())


def test_apply_invalid_topology_rejected():
    """Only 'lan' / 'wan' are valid — anything else raises before any write."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager, applier, restarts, database = _make_manager(cfg, Path(td))
        _loop_run(database.connect())
        try:
            try:
                _loop_run(manager.apply("bridge"))
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "lan" in str(exc) and "wan" in str(exc)
            assert applier.calls == []  # nothing ran
            assert restarts == []       # nothing scheduled
            assert not (Path(td) / "config.yaml").exists()  # nothing written
        finally:
            _loop_run(database.close())


def test_apply_applier_failure_raises_without_restart():
    """A failed applier (rc != 0) raises RuntimeError with the stderr, never
    schedules a restart, AND rolls the persisted state back — config.yaml +
    the DB return to the pre-apply LAN. Leaving them at the new topology would
    make the next boot apply a topology its NIC never got (the box would boot
    WAN onto a LAN NIC and cut everyone's internet)."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager, applier, restarts, database = _make_manager(
            cfg, Path(td), rc=7, out="pppd: failed to dial")
        _loop_run(database.connect())
        try:
            # simulate the running LAN config the box booted with
            manager._write_config(manager.render_config("lan", manager.lan_values()))
            try:
                _loop_run(manager.apply("wan", pppoe_user="u@isp",
                                        pppoe_password="s3cret"))
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "exit 7" in str(exc)
                assert "failed to dial" in str(exc)
                assert "restored" in str(exc)
            assert restarts == []  # never restarted into the failure
            # config.yaml rolled back to the pre-apply LAN (never WAN)
            data = _config_yaml(Path(td))
            assert data["engine"]["topology"] == "lan"
            assert data["dhcp"]["router_ip"] == ROUTER
            # the DB settings rolled back too — no stray dashboard override
            assert _loop_run(database.get_setting("topology_source", None)) is None
            assert _loop_run(database.get_setting("topology", None)) is None
            events = _loop_run(database.list_events())
            assert any("FAILED" in e["message"] for e in events)
        finally:
            _loop_run(database.close())


def test_apply_applier_failure_restores_previous_state():
    """After a SUCCESSFUL LAN apply, a failing WAN apply must restore the exact
    previous config.yaml text + DB settings — not just the defaults. The next
    boot then stays on the working LAN."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager, applier, restarts, database = _make_manager(cfg, Path(td))
        _loop_run(database.connect())
        try:
            # successful LAN apply first -> the box is on the dashboard LAN
            _loop_run(manager.apply("lan"))
            lan_text = (Path(td) / "config.yaml").read_text(encoding="utf-8")
            assert _loop_run(database.get_setting("topology", "")) == "lan"

            # now a WAN apply that fails
            applier.rc = 7
            applier.out = "pppd: timeout waiting for PADO"
            try:
                _loop_run(manager.apply("wan", pppoe_user="u@isp",
                                        pppoe_password="s3cret"))
                assert False, "expected RuntimeError"
            except RuntimeError:
                pass
            assert restarts == [True]  # only the first apply restarted
            # config.yaml is byte-identical to the pre-failure LAN text
            assert (Path(td) / "config.yaml").read_text(encoding="utf-8") == lan_text
            # DB settings restored to the previous (dashboard, lan)
            assert _loop_run(database.get_setting("topology_source", "")) == "dashboard"
            assert _loop_run(database.get_setting("topology", "")) == "lan"
        finally:
            _loop_run(database.close())


# -------------------------------------------------------------- discovery


def test_uplink_ip_resolution_order():
    """The uplink is taken from the config key when present, then a live NIC
    address that is NOT the client alias / client subnet, then the default."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        # 1. config key wins
        manager = TopologyManager(cfg, _db.Database(cfg.db_path),
                                  addr_cmd=lambda: "")
        ip, cidr = manager.uplink_ip()
        assert (ip, cidr) == (UPLINK_IP, 24)

        # 2. no config key -> a live NIC address outside the client subnet
        cfg.dhcp.uplink_ip = ""
        manager = TopologyManager(
            cfg, _db.Database(cfg.db_path),
            addr_cmd=lambda: "2: eth0    inet 192.168.2.1/24 scope global eth0\n"
                             "3: eth1    inet 192.168.9.5/24 scope global eth1\n")
        ip, cidr = manager.uplink_ip()
        assert (ip, cidr) == ("192.168.9.5", 24)  # skips the client-subnet alias

        # 3. nothing resolvable -> the setup default
        manager = TopologyManager(cfg, _db.Database(cfg.db_path),
                                  addr_cmd=lambda: "")
        ip, cidr = manager.uplink_ip()
        assert (ip, cidr) == (DEFAULT_UPLINK_IP, DEFAULT_LAN_CIDR)


def test_upstream_dns_last_nonempty():
    """The public resolver is the LAST non-empty entry (LAN dns = [router,
    public]; WAN = [public]). An empty trailing entry falls back to 8.8.8.8."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.dhcp.dns_servers = ["192.168.1.1", "1.1.1.1"]
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        assert manager.upstream_dns() == "1.1.1.1"

        cfg.dhcp.dns_servers = ["192.168.1.1", ""]
        assert manager.upstream_dns() == "8.8.8.8"

        cfg.dhcp.dns_servers = []
        assert manager.upstream_dns() == "8.8.8.8"


def test_upstream_dns_skips_router():
    """A one-entry [router] list (or one whose last entry IS the router) must
    never forward to the router in WAN mode — the router does not exist on the
    WAN segment, so every upstream query would go nowhere (Bug C)."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.dhcp.dns_servers = ["192.168.1.1"]
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        assert manager.upstream_dns() == "8.8.8.8"

        # the last entry IS the router -> still skip it
        cfg.dhcp.dns_servers = ["8.8.8.8", "192.168.1.1"]
        assert manager.upstream_dns() == "8.8.8.8"


def test_render_config_preserves_full_config():
    """An apply must never drop config that survives it: log_level, the DHCP
    interface and the engine count direction all flow through render_config
    (they were silently dropped before the v19.1 audit fix)."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.log_level = "DEBUG"
        cfg.dhcp.interface = "eth0"
        cfg.engine.count_direction = "both"
        manager = TopologyManager(cfg, _db.Database(cfg.db_path))
        lan = manager.lan_values()
        data = yaml.safe_load(manager.render_config("wan", lan))
        assert data["log_level"] == "DEBUG"
        assert data["dhcp"]["interface"] == "eth0"
        assert data["engine"]["count_direction"] == "both"


def test_lan_interface_parses_name_not_index():
    """The fallback NIC discovery must return the interface NAME (``eth0``) —
    the old code split on ``:`` and returned the INDEX (``2``), which is
    meaningless to the applier. ``shaping.interface`` still wins when set."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        cfg.shaping.interface = ""  # force the addr_cmd fallback
        manager = TopologyManager(
            cfg, _db.Database(cfg.db_path),
            addr_cmd=lambda: "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 "
                             "scope global eth0\n"
                             "3: eth1    inet 192.168.9.5/24 scope global eth1\n")
        assert manager.lan_interface() == "eth0"
        # no match -> empty, not a bogus index
        cfg.dhcp.gateway_ip = "10.0.0.1"
        assert manager.lan_interface() == ""


# ------------------------------------------------------------ pppoe test


def test_test_pppoe_success():
    """The throwaway dial succeeds: RESULT=success with the negotiated
    local/peer IPs and a reachable internet, env carries the NIC + creds, and
    the test script (not topology.sh) is what gets invoked."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        out = ("RESULT=success\nLOCAL=100.64.0.2\nPEER=100.64.0.1\n"
               "INTERNET=yes\nDETAIL=PPPoE link is up\n")
        manager, applier, restarts, database = _make_manager(
            cfg, Path(td), rc=0, out=out)
        # the test helper lives next to topology.sh
        (Path(td) / "test_pppoe.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        _loop_run(database.connect())
        try:
            result = _loop_run(manager.test_pppoe(
                pppoe_user="u@isp", pppoe_password="s3cret"))
            assert result["status"] == "success"
            assert result["ok"] is True
            assert result["local_ip"] == "100.64.0.2"
            assert result["peer_ip"] == "100.64.0.1"
            assert result["internet"] is True
            # the TEST script ran (not the topology applier) with the right env
            assert applier.scripts == [str(Path(td) / "test_pppoe.sh")]
            assert applier.calls[0]["PPPOE_USER"] == "u@isp"
            assert applier.calls[0]["PPPOE_PASSWORD"] == "s3cret"
            assert applier.calls[0]["PPP_IF"] == "eth0"  # from shaping.interface
        finally:
            _loop_run(database.close())


def test_test_pppoe_auth_failed():
    """The ISP rejected the credentials — the parse reports auth-failed."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        out = ("RESULT=auth-failed\nDETAIL=the ISP rejected the PPPoE "
               "user/password\n")
        manager, applier, restarts, database = _make_manager(
            cfg, Path(td), rc=0, out=out)
        (Path(td) / "test_pppoe.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        _loop_run(database.connect())
        try:
            result = _loop_run(manager.test_pppoe(
                pppoe_user="bad", pppoe_password="wrong"))
            assert result["status"] == "auth-failed"
            assert result["ok"] is False
            assert "rejected" in result["detail"]
        finally:
            _loop_run(database.close())


def test_test_pppoe_missing_script_raises():
    """No test_pppoe.sh next to the applier -> RuntimeError, nothing dialed."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(Path(td))
        manager, applier, restarts, database = _make_manager(cfg, Path(td))
        _loop_run(database.connect())
        try:
            try:
                _loop_run(manager.test_pppoe())
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "test_pppoe.sh" in str(exc)
            assert applier.calls == []  # nothing ran
        finally:
            _loop_run(database.close())


def test_parse_pppoe_test_error_on_rc():
    """A non-zero exit without a RESULT= line parses as 'error'."""
    result = TopologyManager._parse_pppoe_test(3, "bash: pppd: command not found\n")
    assert result["status"] == "error"
    assert result["ok"] is False
    assert "command not found" in result["script_output"]
