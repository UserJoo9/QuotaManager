"""UPnP / NAT-PMP (``quota.upnp``): pure renderers, the supervisor with a fake
host (no kernel, no miniupnpd), the firewall hook and the REST surface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.firewall import render_commands
from quota.service import QuotaService
from quota.upnp import (ALWAYS_BLOCKED, UpnpContext, UpnpManager,
                        UpnpSettings, dnat_guard, is_public_ipv4,
                        normalize_settings, parse_external_ip, parse_leases,
                        render_conf, render_nft)

LAN = UpnpContext(topology="lan", gateway_ip="192.168.2.1", lan_prefix=24,
                  uplink_ip="192.168.1.50", uplink_iface="eth0")
WAN = UpnpContext(topology="wan", gateway_ip="192.168.2.1", lan_prefix=24)


# -- settings ----------------------------------------------------------------

def test_defaults_are_off_and_safe():
    s = normalize_settings({})
    assert s.enabled is False and s.secure_mode is True
    assert (s.port_min, s.port_max) == (1024, 65535)


def test_normalize_clamps_and_cleans_everything():
    s = normalize_settings({
        "enabled": 1, "port_min": 9000, "port_max": 100,        # swapped
        "max_lifetime": 5, "blocked_ports": [25, 25, 22, "x", 0, 70000, 445],
        "ext_ip": "10.0.0.1"})                                    # private: dropped
    assert (s.port_min, s.port_max) == (100, 9000)
    assert s.max_lifetime == 120
    assert s.blocked_ports == [25, 445]       # dup / always-blocked / junk removed
    assert s.ext_ip == ""
    assert normalize_settings({"ext_ip": "93.184.216.34"}).ext_ip == "93.184.216.34"
    assert normalize_settings("garbage").enabled is False


# -- pure renderers ------------------------------------------------------------

def test_conf_listens_on_lan_only_and_denies_by_default(tmp_path):
    conf = render_conf(UpnpSettings(enabled=True, blocked_ports=[25]), LAN,
                       ext_iface="eth0", ext_ip="93.184.216.34",
                       lease_file=tmp_path / "l")
    assert "listening_ip=192.168.2.1/24" in conf     # never the NIC name
    assert "ext_ip=93.184.216.34" in conf and "ipv6_disable=yes" in conf
    assert "secure_mode=yes" in conf and "upnp_table_name=quota_upnp" in conf
    lines = conf.splitlines()
    assert lines[-1] == "deny 0-65535 0.0.0.0/0 0-65535"
    first_allow = next(i for i, x in enumerate(lines) if x.startswith("allow "))
    for port in (*ALWAYS_BLOCKED, 25):
        idx = lines.index(f"deny {port} 192.168.2.0/24 0-65535")
        assert idx < first_allow, "deny rules must precede the allow rule"


def test_conf_rejects_unusable_input(tmp_path):
    kw = dict(ext_iface="eth0", ext_ip="93.184.216.34", lease_file=tmp_path / "l")
    with pytest.raises(ValueError):
        render_conf(UpnpSettings(), LAN, **{**kw, "ext_iface": "eth0; rm"})
    with pytest.raises(ValueError):
        render_conf(UpnpSettings(), LAN, **{**kw, "ext_ip": "192.168.1.1"})
    with pytest.raises(ValueError):
        render_conf(UpnpSettings(), UpnpContext(gateway_ip="nope"), **kw)


def test_dnat_guard_never_lets_lan_traffic_into_mappings():
    assert dnat_guard("lan", "192.168.1.50") == ["ip", "daddr", "192.168.1.50"]
    assert dnat_guard("wan", "") == ["iifname", '"ppp0"']
    with pytest.raises(ValueError):
        dnat_guard("lan", "not-an-ip")
    table = render_nft(dnat_guard("lan", "192.168.1.50"))
    assert "ip daddr 192.168.1.50 jump prerouting_miniupnpd" in table
    assert "policy drop" not in table, "must never drop forwarded traffic"
    assert "postrouting_miniupnpd" in table and "hook postrouting" not in table


def test_public_address_check():
    assert is_public_ipv4("93.184.216.34")
    for bad in ("192.168.1.1", "10.0.0.1", "100.64.1.2", "169.254.1.1", "::1", "x"):
        assert not is_public_ipv4(bad)


def test_parsers():
    leases = parse_leases("TCP:5555:192.168.2.77:5555:1795748365:my game\n"
                          "junk\nUDP:99999:192.168.2.7:1:1:x\nTCP:80:bad:80:1:x\n"
                          "udp:6000:192.168.2.8:6000:5:\n")
    assert [(m.proto, m.ext_port, m.ip, m.desc) for m in leases] == [
        ("TCP", 5555, "192.168.2.77", "my game"), ("UDP", 6000, "192.168.2.8", "")]
    assert parse_external_ip("ExternalIPAddress = 93.184.216.34\n") == "93.184.216.34"
    assert parse_external_ip("nothing") == ""


# -- supervisor with a fake host ---------------------------------------------------

class Host:
    """Fake kernel + daemon + router; records every command."""

    def __init__(self, tmp: Path, router_ip: str = "93.184.216.34",
                 ppp_ip: str = "93.184.216.50", active_unit: bool = False):
        self.tmp, self.router_ip, self.ppp_ip = tmp, router_ip, ppp_ip
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.table = False
        self.guard = ""
        self.cmds: list[list[str]] = []
        self.router: dict[tuple[str, int], str] = {}
        self.active_unit = active_unit
        self.start_fails = False
        self.now = 1000.0
        self.pid = 4242

    def run(self, argv):
        self.cmds.append(list(argv))
        a = argv
        if a[:4] == ["nft", "list", "table", "inet"]:
            return (0, "") if self.table else (1, "no such table")
        if a[:3] == ["nft", "-f", str(self.tmp / "state" / "upnp.nft")]:
            text = (self.tmp / "state" / "upnp.nft").read_text()
            self.table = True
            self.guard = text.split("policy accept; ")[2].split(" jump ")[0]
            return 0, ""
        if a[:5] == ["nft", "list", "chain", "inet", "quota_upnp"]:
            return 0, f"{self.guard} jump prerouting_miniupnpd\n"
        if a[:2] == ["nft", "flush"]:
            self.guard = ""
            return 0, ""
        if a[:3] == ["nft", "add", "rule"]:
            self.guard = " ".join(a[6:a.index("jump")])
            return 0, ""
        if a[:3] == ["nft", "delete", "table"]:
            self.table = False
            return 0, ""
        if a[:3] == ["systemctl", "is-active", "--quiet"]:
            return (0, "") if self.active_unit else (3, "")
        if a[:2] == ["systemctl", "disable"]:
            self.active_unit = False
            return 0, ""
        if a[0] == "/usr/sbin/miniupnpd":
            if self.start_fails:
                return 1, "boom"
            (self.proc / str(self.pid)).mkdir(exist_ok=True)
            (self.proc / str(self.pid) / "comm").write_text("miniupnpd\n")
            Path(a[a.index("-P") + 1]).write_text(f"{self.pid}\n")
            return 0, ""
        if a[0] == "ip" and "ppp0" in a:
            if not self.ppp_ip:
                return 1, "Device does not exist"
            return 0, f"5: ppp0    inet {self.ppp_ip}/32 scope global ppp0\n"
        if a[0] == "upnpc":
            return self._upnpc(a)
        return 1, "unexpected"

    def _upnpc(self, a):
        if "-s" in a:
            return (0, f"ExternalIPAddress = {self.router_ip}\n") if self.router_ip else (1, "")
        if "-a" in a:
            i = a.index("-a")
            self.router[(a[i + 4], int(a[i + 2]))] = a[i + 1]
            return 0, f"external x:{a[i + 2]} {a[i + 4]} is redirected to internal {a[i + 1]}\n"
        if "-d" in a:
            i = a.index("-d")
            self.router.pop((a[i + 2], int(a[i + 1])), None)
            return 0, "UPNP_DeletePortMapping() returned : 0\n"
        return 1, ""

    def kill_daemon(self):
        (self.proc / str(self.pid) / "comm").unlink()

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    mgr = UpnpManager(tmp_path / "state", run=h.run, proc_root=h.proc,
                      which=lambda n: f"/usr/sbin/{n}" if n in ("miniupnpd", "upnpc") else None,
                      monotonic=h.clock, wall=h.clock, sleep=h.sleep,
                      nft_build=lambda path: True)
    return h, mgr


def test_off_by_default_does_nothing_and_is_cheap(host):
    h, mgr = host
    assert mgr.sync(UpnpSettings(), LAN)["state"] == "off"
    n = len(h.cmds)
    for _ in range(5):
        mgr.sync(UpnpSettings(), LAN)
    assert len(h.cmds) == n, "an idle tick must not spawn subprocesses"
    assert not any(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds)


def test_lan_mode_starts_daemon_with_router_address_and_guard(host):
    h, mgr = host
    r = mgr.sync(UpnpSettings(enabled=True), LAN)
    assert r["state"] == "running" and r["ext_ip"] == "93.184.216.34"
    assert h.guard == "ip daddr 192.168.1.50"
    conf = mgr.conf_path.read_text()
    assert "ext_ip=93.184.216.34" in conf and "ext_ifname=eth0" in conf


def test_second_sync_is_idempotent(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    n = len(h.cmds)
    h.now += 5
    mgr.sync(UpnpSettings(enabled=True), LAN)
    assert not any(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds[n:]), "no restart"
    assert not any(c[:2] == ["nft", "flush"] for c in h.cmds), "never re-flush"


def test_settings_change_restarts_and_disable_cleans_up(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    starts = sum(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds)
    mgr.sync(UpnpSettings(enabled=True, natpmp=False), LAN)
    assert sum(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds) == starts + 1
    r = mgr.sync(UpnpSettings(enabled=False), LAN)
    assert r["state"] == "off" and not r["running"] and h.table is False


def test_vanished_table_restarts_the_daemon_to_reload_mappings(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    starts = sum(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds)
    h.table = False                      # e.g. `systemctl restart nftables`
    h.now += 120                          # past the re-check interval
    mgr.sync(UpnpSettings(enabled=True), LAN)
    assert h.table and sum(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds) == starts + 1


def test_guard_change_only_swaps_the_guard(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    moved = UpnpContext(topology="lan", gateway_ip="192.168.2.1", lan_prefix=24,
                        uplink_ip="192.168.1.77", uplink_iface="eth0")
    mgr.sync(UpnpSettings(enabled=True), moved)
    assert h.guard == "ip daddr 192.168.1.77"
    assert any(c[:6] == ["nft", "flush", "chain", "inet", "quota_upnp", "prerouting"] for c in h.cmds)
    assert not any(c[:3] == ["nft", "delete", "table"] for c in h.cmds)


def test_stock_distro_unit_is_stopped_once(host):
    h, mgr = host
    h.active_unit = True
    mgr.sync(UpnpSettings(enabled=True), LAN)
    assert ["systemctl", "disable", "--now", "miniupnpd"] in h.cmds


@pytest.mark.parametrize("router_ip,state", [
    ("", "no_router_upnp"),                       # router has no UPnP
    ("100.64.7.7", "cgnat"),                      # carrier-grade NAT
])
def test_lan_mode_refuses_when_no_public_address(host, router_ip, state):
    h, mgr = host
    h.router_ip = router_ip
    r = mgr.sync(UpnpSettings(enabled=True), LAN)
    assert r["state"] == state and not r["running"]
    assert not any(c[0] == "/usr/sbin/miniupnpd" for c in h.cmds)


def test_manual_ext_ip_is_used_without_asking_the_router(host):
    h, mgr = host
    h.router_ip = ""
    r = mgr.sync(UpnpSettings(enabled=True, relay=False, ext_ip="93.184.216.99"), LAN)
    assert r["state"] == "running" and r["ext_ip"] == "93.184.216.99"
    assert not any(c[0] == "upnpc" for c in h.cmds)


def test_wan_mode_uses_ppp_address_and_iif_guard(host):
    h, mgr = host
    r = mgr.sync(UpnpSettings(enabled=True), WAN)
    assert r["state"] == "running" and r["ext_ip"] == "93.184.216.50"
    assert h.guard == 'iifname "ppp0"'
    assert not any(c[0] == "upnpc" for c in h.cmds), "no relay on a public WAN"
    h.ppp_ip = "100.64.0.9"
    h.now += 120
    mgr.sync(UpnpSettings(enabled=True), WAN)      # same cached conf is fine…
    mgr2 = UpnpManager(mgr._dir, run=h.run, proc_root=h.proc, which=mgr._which,
                       monotonic=h.clock, wall=h.clock, sleep=h.sleep, nft_build=lambda p: True)
    assert mgr2.sync(UpnpSettings(enabled=True), WAN)["state"] == "cgnat"


def test_wan_down_waits(host):
    h, mgr = host
    h.ppp_ip = ""
    assert mgr.sync(UpnpSettings(enabled=True), WAN)["state"] == "waiting"


def test_missing_binary_and_wrong_backend(tmp_path):
    h = Host(tmp_path)
    mk = lambda which, nft: UpnpManager(tmp_path / "s", run=h.run, proc_root=h.proc,
                                        which=which, monotonic=h.clock, sleep=h.sleep,
                                        nft_build=nft)
    r = mk(lambda n: None, lambda p: True).sync(UpnpSettings(enabled=True), LAN)
    assert r["state"] == "not_installed" and r["installed"] is False
    r = mk(lambda n: f"/usr/sbin/{n}", lambda p: False).sync(UpnpSettings(enabled=True), LAN)
    assert r["state"] == "wrong_backend"
    assert not any(c[0].endswith("miniupnpd") for c in h.cmds)


def test_start_failure_is_reported_not_raised(host):
    h, mgr = host
    h.start_fails = True
    r = mgr.sync(UpnpSettings(enabled=True), LAN)
    assert r["state"] == "error" and "failed to start" in r["reason"]


def test_a_broken_host_never_raises(tmp_path):
    def boom(argv):
        raise OSError("nope")
    mgr = UpnpManager(tmp_path / "s", run=boom, which=lambda n: "/x", nft_build=lambda p: True)
    assert mgr.sync(UpnpSettings(enabled=True), LAN)["state"] == "error"


# -- relay -----------------------------------------------------------------------------

def _lease(mgr, h, *rows):
    mgr.lease_path.parent.mkdir(parents=True, exist_ok=True)
    mgr.lease_path.write_text("".join(
        f"{p}:{e}:{ip}:{e}:{int(h.now) + 600}:{d}\n" for p, e, ip, d in rows))


def test_relay_mirrors_and_removes_mappings_on_the_router(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    _lease(mgr, h, ("TCP", 5555, "192.168.2.77", "game"))
    r = mgr.sync(UpnpSettings(enabled=True), LAN)
    assert h.router == {("TCP", 5555): "192.168.1.50"}
    assert r["mappings"][0]["relayed"] is True
    mgr.lease_path.write_text("")
    mgr.sync(UpnpSettings(enabled=True), LAN)
    assert h.router == {}


def test_relay_does_not_spam_the_router(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    _lease(mgr, h, ("UDP", 6000, "192.168.2.8", "x"))
    mgr.sync(UpnpSettings(enabled=True), LAN)
    adds = sum("-a" in c for c in h.cmds if c[0] == "upnpc")
    for _ in range(4):
        mgr.sync(UpnpSettings(enabled=True), LAN)
    assert sum("-a" in c for c in h.cmds if c[0] == "upnpc") == adds


def test_relay_off_leaves_the_router_alone(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True, relay=False, ext_ip="93.184.216.34"), LAN)
    _lease(mgr, h, ("TCP", 5555, "192.168.2.77", "game"))
    mgr.sync(UpnpSettings(enabled=True, relay=False, ext_ip="93.184.216.34"), LAN)
    assert h.router == {}


def test_disable_removes_router_mappings(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    _lease(mgr, h, ("TCP", 5555, "192.168.2.77", "game"))
    mgr.sync(UpnpSettings(enabled=True), LAN)
    mgr.sync(UpnpSettings(enabled=False), LAN)
    assert h.router == {}


def test_remove_one_mapping_edits_the_lease_file(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    _lease(mgr, h, ("TCP", 5555, "192.168.2.77", "a"), ("UDP", 6000, "192.168.2.8", "b"))
    assert mgr.remove(LAN, "tcp", 5555) is True
    assert [m.ext_port for m in parse_leases(mgr.lease_path.read_text())] == [6000]
    assert mgr.remove(LAN, "TCP", 5555) is False
    assert mgr.remove(LAN, "ICMP", 5555) is False and mgr.remove(LAN, "TCP", 0) is False


def test_clear_forgets_everything(host):
    h, mgr = host
    mgr.sync(UpnpSettings(enabled=True), LAN)
    _lease(mgr, h, ("TCP", 5555, "192.168.2.77", "game"))
    mgr.sync(UpnpSettings(enabled=True), LAN)
    r = mgr.clear(UpnpSettings(enabled=True), LAN)
    assert r["mappings"] == [] and h.router == {}


def test_install_uses_apt_only_and_reports_other_distros(tmp_path):
    h = Host(tmp_path)
    mgr = UpnpManager(tmp_path / "s", run=h.run, which=lambda n: None)
    assert "apt" in mgr.install()["reason"]
    mgr2 = UpnpManager(tmp_path / "s", run=h.run, which=lambda n: "/usr/bin/apt-get"
                       if n == "apt-get" else None, install_script=["fake-install"])
    h.run = lambda a: (h.cmds.append(a) or (0, ""))
    mgr2._run = h.run
    mgr2.install()
    for _ in range(50):
        if ["fake-install"] in h.cmds:
            break
        import time as _t
        _t.sleep(0.05)
    assert ["fake-install"] in h.cmds


# -- firewall hook ----------------------------------------------------------------------

def _fw_cfg(**over):
    cfg = {"enabled": True, "watchdog_seconds": 45, "services": [], "port_forwards": [],
           "rules": [], "dmz": "", "allow_cidrs": [], "deny_cidrs": [],
           "syn_flood": {"rate": 10, "burst": 20},
           "brute_force": {"threshold": 10, "ban_seconds": 1800},
           "scan_detect": {"enabled": True, "syn_threshold": 200, "ban_seconds": 3600},
           "geo_block": False, "wan_confirmed": False}
    cfg.update(over)
    return cfg


def _forward_rules(cfg, topology):
    return [c[-1] for c in render_commands(cfg, topology, ["192.168.2.0/24"], ["192.168.2.1"])
            if c[:2] == ["add", "rule"] and "fw_forward" in c]


def test_firewall_lets_mapped_flows_through_the_wan_default_deny():
    on = _forward_rules(_fw_cfg(_upnp=True), "wan")
    accept = 'iifname "ppp0" ct status dnat ct state new accept'
    drop = next(i for i, r in enumerate(on) if r.startswith('iifname "ppp0" ct state new counter'))
    assert accept in on and on.index(accept) < drop
    assert accept not in _forward_rules(_fw_cfg(), "wan"), "off unless UPnP is live"
    assert accept not in _forward_rules(_fw_cfg(_upnp=True), "lan")


# -- REST --------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    loop = asyncio.new_event_loop()
    database = _db.Database(tmp_path / "u.db")
    loop.run_until_complete(database.connect())
    service = QuotaService(database, timezone="Africa/Cairo")
    saved: dict = {}
    calls: list = []

    async def get():
        return {"settings": normalize_settings(saved).to_dict(), "status": {"state": "off", "mappings": []}}

    async def save(data):
        saved.clear(); saved.update(data)
        return await get()

    async def action(name, **kw):
        calls.append((name, kw))
        return await get()

    app = create_app(database, service, SnapshotHolder(), upnp_get=get, upnp_save=save,
                     upnp_action=action)
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        yield c, calls
    loop.run_until_complete(database.close())
    loop.close()


def test_api_roundtrip_and_validation(api):
    c, calls = api
    assert c.get("/api/upnp").json()["settings"]["enabled"] is False
    ok = c.put("/api/upnp", json={"enabled": True, "port_min": 2000, "blocked_ports": [25]})
    assert ok.status_code == 200 and ok.json()["settings"]["enabled"] is True
    assert c.get("/api/upnp").json()["settings"]["port_min"] == 2000
    for bad in ({"port_min": 0}, {"blocked_ports": [70000]}, {"ext_ip": "192.168.1.1"},
                {"ext_ip": "nope"}, {"max_lifetime": 5}):
        assert c.put("/api/upnp", json=bad).status_code == 422, bad
    assert c.post("/api/upnp/install").status_code == 200
    assert c.post("/api/upnp/clear").status_code == 200
    assert c.post("/api/upnp/remove", json={"proto": "tcp", "port": 5555}).status_code == 200
    assert c.post("/api/upnp/remove", json={"proto": "icmp", "port": 5555}).status_code == 422
    assert [n for n, _ in calls] == ["install", "clear", "remove"]
    assert calls[-1][1] == {"proto": "TCP", "port": 5555}


def test_api_requires_login(api):
    c, _ = api
    c.post("/api/logout")
    assert c.get("/api/upnp").status_code == 401
    assert c.put("/api/upnp", json={}).status_code == 401
    assert c.post("/api/upnp/clear").status_code == 401


def test_api_unavailable_without_a_manager(tmp_path):
    loop = asyncio.new_event_loop()
    database = _db.Database(tmp_path / "n.db")
    loop.run_until_complete(database.connect())
    app = create_app(database, QuotaService(database, timezone="Africa/Cairo"), SnapshotHolder())
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        assert c.get("/api/upnp").status_code == 503
    loop.run_until_complete(database.close())
    loop.close()
