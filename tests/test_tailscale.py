"""Built-in Tailscale remote access: status parsing, the manager's states, the
API routes and the firewall rule that lets the tailnet reach the dashboard."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.firewall import render_commands
from quota.service import QuotaService
from quota.tailscale import TailscaleManager, parse_status, state_from

from tests.test_api import _get_loop

RUNNING = json.dumps({
    "BackendState": "Running", "Version": "1.80.0",
    "Self": {"HostName": "quota-gateway", "DNSName": "quota-gateway.tail1234.ts.net.",
             "TailscaleIPs": ["100.101.102.103", "fd7a:115c::1"], "Online": True},
    "Peer": {"a": {}, "b": {}}})
NEEDS_LOGIN = json.dumps({"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/abc123"})


def _runner(outputs):
    """Fake ``run_command``: ``outputs`` maps a keyword in argv -> (code, text)."""
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        for key, val in outputs.items():
            if key in argv:
                return val
        return 0, ""
    run.calls = calls
    return run


def test_parse_status_and_state_mapping():
    info = parse_status(RUNNING)
    assert info["ip"] == "100.101.102.103"          # IPv4 preferred
    assert info["dns_name"] == "quota-gateway.tail1234.ts.net"   # trailing dot dropped
    assert info["peers"] == 2 and info["online"] is True
    assert parse_status("not json")["backend"] == ""
    assert parse_status("[]")["ip"] == ""
    assert state_from("Running", True) == "connected"
    assert state_from("Running", False) == "starting"
    assert state_from("NeedsLogin", False) == "needs_login"
    assert state_from("Stopped", False) == "stopped"
    assert state_from("", False) == "daemon_down"


def test_status_states():
    m = TailscaleManager(run_command=_runner({}), which=lambda _: None)
    assert m.status()["state"] == "not_installed"
    assert m.status()["installed"] is False

    m = TailscaleManager(run_command=_runner({"status": (0, RUNNING)}), which=lambda _: "/usr/bin/tailscale")
    s = m.status()
    assert s["state"] == "connected" and s["ip"] == "100.101.102.103" and s["peers"] == 2

    m = TailscaleManager(run_command=_runner({"status": (0, NEEDS_LOGIN)}), which=lambda _: "/usr/bin/tailscale")
    s = m.status()
    assert s["state"] == "needs_login" and s["auth_url"].startswith("https://login.tailscale.com/")

    m = TailscaleManager(run_command=_runner({"status": (1, "failed to connect to local tailscaled")}),
                         which=lambda _: "/usr/bin/tailscale")
    assert m.status()["state"] == "daemon_down"


def test_connect_returns_sign_in_link_and_disconnect_calls_cli():
    started: list[list[str]] = []

    class Proc:
        def poll(self):
            return None

        def terminate(self):
            started.append(["terminated"])

    def popen(argv, **kw):
        started.append(list(argv))
        return Proc()

    run = _runner({"status": (0, NEEDS_LOGIN)})
    m = TailscaleManager(run_command=run, which=lambda _: "/usr/bin/tailscale", popen=popen)
    snap = m.connect(wait=2.0)
    assert snap["state"] == "needs_login" and snap["auth_url"].endswith("abc123")
    up = started[0]
    assert up[:2] == ["/usr/bin/tailscale", "up"] and "--accept-dns=false" in up and "--reset" in up
    # a second connect replaces the waiting ``up`` process instead of piling up
    m.connect(wait=0.5)
    assert ["terminated"] in started

    m.disconnect()
    assert ["/usr/bin/tailscale", "down"] in run.calls
    m.disconnect(logout=True)
    assert ["/usr/bin/tailscale", "logout"] in run.calls


def test_connect_when_already_connected_does_not_spawn():
    spawned = []
    m = TailscaleManager(run_command=_runner({"status": (0, RUNNING)}), which=lambda _: "/usr/bin/tailscale",
                         popen=lambda *a, **k: spawned.append(a))
    assert m.connect()["state"] == "connected"
    assert spawned == []


def test_install_runs_in_background_and_logs():
    installed = {"yes": False}

    class Proc:
        stdout = iter(["Installing tailscale", "Done"])

        def wait(self):
            installed["yes"] = True
            return 0

    m = TailscaleManager(run_command=_runner({}), which=lambda _: "/usr/bin/tailscale" if installed["yes"] else None,
                         popen=lambda *a, **k: Proc())
    assert m.install()["state"] == "installing"
    for _ in range(50):
        if not m._installing:
            break
        time.sleep(0.05)
    s = m.status()
    assert s["state"] != "installing"
    assert "Done" in m._install_log
    # nothing to do when already installed
    assert m.install()["state"] != "installing"


def test_not_installed_error_is_reported():
    class Proc:
        stdout = iter([])

        def wait(self):
            return 7

    m = TailscaleManager(run_command=_runner({}), which=lambda _: None, popen=lambda *a, **k: Proc())
    m.install()
    for _ in range(50):
        if not m._installing:
            break
        time.sleep(0.05)
    s = m.status()
    assert s["state"] == "not_installed" and "exit 7" in s["message"]


def _app(tmp_path, manager):
    database = _db.Database(tmp_path / "ts.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    _get_loop().run_until_complete(database.connect())
    return database, create_app(database, service, SnapshotHolder(), tailscale_manager=manager)


def test_api_requires_login_and_returns_address(tmp_path):
    m = TailscaleManager(run_command=_runner({"status": (0, RUNNING)}), which=lambda _: "/usr/bin/tailscale")
    database, app = _app(tmp_path, m)
    with TestClient(app) as c:
        assert c.get("/api/tailscale").status_code == 401
        assert c.post("/api/tailscale/connect").status_code == 401
        assert c.post("/api/tailscale/disconnect").status_code == 401
        assert c.post("/api/tailscale/install").status_code == 401
        c.post("/api/login", json={"password": "admin"})
        r = c.get("/api/tailscale")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "connected"
        assert body["url"] == "http://100.101.102.103:8080"
        assert body["url_name"] == "http://quota-gateway.tail1234.ts.net:8080"
        assert body["wan_exposed"] is False
        assert c.post("/api/tailscale/disconnect", json={"logout": False}).status_code == 200
        assert c.post("/api/tailscale/disconnect").status_code == 200
    _get_loop().run_until_complete(database.close())


def test_firewall_accepts_tailnet_in_both_topologies():
    rule = '"iifname \\"tailscale0\\" accept"'.strip('"').replace('\\"', '"')
    for topology in ("lan", "wan"):
        cmds = render_commands({"enabled": True, "wan_confirmed": False}, topology,
                               ["192.168.2.0/24"], ["192.168.2.1"])
        rules = [c[-1] for c in cmds if c[:1] == ["add"] and c[1] == "rule" and "fw_input" in c]
        assert rule in rules, topology
        # it sits BEFORE every drop so the remote-access switch can never lock it out
        first_drop = next(i for i, r in enumerate(rules) if " drop" in r)
        assert rules.index(rule) < first_drop, topology


# -- friendly install progress, peers, health, post-install heal --------------

def test_parse_status_peers_and_health():
    info = parse_status(json.dumps({
        "BackendState": "Running", "Health": ["cannot reach control", 5],
        "Self": {"TailscaleIPs": ["100.1.1.1"], "Online": True},
        "Peer": {"k1": {"HostName": "iphone", "OS": "iOS", "Online": True, "CurAddr": "1.2.3.4:5"},
                 "k2": {"HostName": "laptop", "OS": "windows", "Online": False},
                 "k3": {"HostName": "tab", "Online": True, "Relay": "fra"}}}))
    assert [p["name"] for p in info["peer_list"]] == ["iphone", "tab", "laptop"]   # online first
    assert info["peer_list"][0]["path"] == "direct" and info["peer_list"][1]["path"] == "relay"
    assert info["health"] == ["cannot reach control"]


def test_install_progress_hides_raw_log_and_notifies():
    from quota.tailscale import install_step_from
    lines = ["+ curl -fsSL https://pkgs.tailscale.com/stable/debian/bullseye.noarmor.gpg",
             "+ tee /usr/share/keyrings/tailscale-archive-keyring.gpg",
             "+ apt-get update", "Unpacking tailscale (1.104.2) ...", "Setting up tailscale (1.104.2) ..."]
    assert install_step_from(lines[:2]) == 1 and install_step_from(lines[:3]) == 2
    assert install_step_from(lines[:4]) == 3 and install_step_from(lines) == 4
    assert install_step_from([]) == 0

    seen: list[str] = []
    installed = {"yes": False}

    class Proc:
        stdout = iter(lines)

        def wait(self):
            installed["yes"] = True
            return 0

    m = TailscaleManager(run_command=_runner({}), which=lambda _: "/usr/bin/tailscale" if installed["yes"] else None,
                         popen=lambda *a, **k: Proc(), on_system_change=seen.append)
    m.install()
    for _ in range(50):
        if not m._installing:
            break
        time.sleep(0.05)
    assert seen == ["installed"]
    assert m._snapshot("installing")["install_log"] == []          # no raw output for the user
    assert m._snapshot("installing")["install_step"] == 4


def test_failed_install_keeps_log_for_troubleshooting():
    class Proc:
        stdout = iter(["E: Unable to locate package"])

        def wait(self):
            return 100

    m = TailscaleManager(run_command=_runner({}), which=lambda _: None, popen=lambda *a, **k: Proc())
    m.install()
    for _ in range(50):
        if not m._installing:
            break
        time.sleep(0.05)
    s = m.status()
    assert s["state"] == "not_installed" and s["message"] and s["install_log"]


def test_hook_failure_never_breaks_connect():
    def boom(_reason):
        raise RuntimeError("x")

    m = TailscaleManager(run_command=_runner({"status": (0, RUNNING)}), which=lambda _: "/usr/bin/tailscale",
                         on_system_change=boom)
    assert m.connect()["state"] == "connected"


def test_gateway_network_heal_restores_missing_pieces(monkeypatch):
    import types
    import run as run_mod
    from quota import startup_health as sh

    monkeypatch.setattr(run_mod.shutil, "which", lambda _n: "/usr/sbin/nft")
    state = {"nat": False, "engine": False, "healed": 0, "engine_started": 0}
    monkeypatch.setattr(sh, "_table_exists", lambda run: state["nat"])
    monkeypatch.setattr(sh, "_has_masquerade", lambda run: state["nat"])
    monkeypatch.setattr(sh, "_default_run", lambda argv: (0 if state["engine"] else 1, ""))

    class FakePath:
        def read_text(self, encoding="utf-8"):
            return "1\n"
    monkeypatch.setattr(sh, "_PROC_IPFORWARD", FakePath())

    eng = types.SimpleNamespace(available=True, table="quota_gateway",
                                start=lambda: state.__setitem__("engine_started", state["engine_started"] + 1))
    gw = types.SimpleNamespace(engine=eng, _net_watch=True)
    gw._net_problems = lambda: run_mod.Gateway._net_problems(gw)
    gw._startup_self_heal = lambda: state.__setitem__("healed", state["healed"] + 1)

    assert run_mod.Gateway._network_heal(gw, "installed") == ["nat", "engine"]
    assert state["healed"] == 1 and state["engine_started"] == 1
    state["nat"] = state["engine"] = True
    assert run_mod.Gateway._network_heal(gw, "installed") == []         # healthy: nothing to do
    # without an available engine nothing is touched (tests / non-Linux)
    gw.engine = None
    assert run_mod.Gateway._network_heal(gw, "tick") == []
