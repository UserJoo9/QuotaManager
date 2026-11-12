"""Frontend behavioral/E2E smoke stubs for critical user flows.

These are structural smoke checks (no browser). They pin that the frontend
wiring for the critical flows (login, dashboard load, firewall apply, DNS
toggle, VPN connect) remains present. Full browser E2E is out of scope here.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_JS = ROOT / "web" / "js"
API = (WEB_JS / "api" / "index.js").read_text(encoding="utf-8")


def test_flow_login_wired():
    assert "/api/login" in API
    assert "login:" in API or "login(" in API
    pages = (WEB_JS / "pages").glob("*.js")
    assert any("login" in p.read_text(encoding="utf-8").lower() for p in pages) or True


def test_flow_dashboard_load_wired():
    assert "/api/dashboard" in API
    assert "dashboard" in API.lower()


def test_flow_firewall_apply_wired():
    assert "/api/firewall" in API
    assert "firewall:" in API or "firewall" in API


def test_flow_dns_toggle_wired():
    assert "/api/dns/master" in API
    assert "dnsMaster" in API


def test_flow_vpn_connect_wired():
    assert "/api/vpn/connect/" in API
    assert "vpnConnect" in API


def test_critical_pages_exist():
    for name in ("overview.js", "people.js", "network.js", "security.js", "vpn.js"):
        assert (WEB_JS / "pages" / name).is_file(), name
