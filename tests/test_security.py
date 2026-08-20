"""Security-hardening tests (2026-08-19) — authentication core, OWASP fixes,
embedded WAF, and the extra hardening. Kept separate from test_api.py so the
huge legacy file stays stable; the fixtures mirror its pattern."""

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api import waf as _waf
from core import passwords as _passwords
from core.config import WafConfig
from quota import totp as _totp
from quota.db import Database
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.service import QuotaService

WEAK = "Str0ng!Passw0rd42"  # compliant password used across the suite


def _valid_totp(secret: str) -> str:
    """The 6-digit code currently valid for ``secret`` (RFC 6238, 30 s step)."""
    return _totp._totp(secret, time.time())


@pytest.fixture
def client(tmp_path):
    """A TestClient wired to a temp database (same shape as test_api.py's
    fixture, plus the holder so tests can flip the topology for WAF mode)."""
    database = Database(tmp_path / "sec.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()

    async def _init():
        await database.connect()
        return database, service

    asyncio.get_event_loop().run_until_complete(_init())

    def _make(waf_config=None, web_config=None):
        return create_app(database, service, holder,
                          waf_config=waf_config, web_config=web_config)

    app = _make()
    with TestClient(app) as c:
        yield c, database, service, holder, _make
    asyncio.get_event_loop().run_until_complete(database.close())


def _login(c: TestClient) -> None:
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


# -- password policy (core/passwords.py) --------------------------------------


def test_policy_violations_units():
    assert _passwords.policy_violations("short")  # length + classes
    assert _passwords.is_compliant(WEAK)
    # common-password list catches case-flipped variants
    assert _passwords.policy_violations("ADMIN12345")
    assert _passwords.policy_violations(" Password123 ")


def test_password_change_rejects_weak(client):
    c, _, _, _, _ = client
    _login(c)
    # long enough to pass the schema (min 12) but on the common-password list
    r = c.post("/api/password", json={"current": "admin", "new": "password1234"})
    assert r.status_code == 400
    assert "weak password" in r.json()["detail"]
    # old password still works — nothing changed
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_setup_complete_rejects_weak(client):
    c, _, _, _, _ = client
    _login(c)
    r = c.post("/api/setup/complete", json={
        "current_password": "admin", "new_password": "password1234"})
    assert r.status_code == 400
    assert "weak password" in r.json()["detail"]


# -- default-password WAN gate -------------------------------------------------


def test_default_password_blocks_wan(client):
    c, _, _, _, _ = client
    _login(c)  # still on the factory default
    r = c.post("/api/wan", json={"topology": "wan"})
    assert r.status_code == 400
    assert "default admin password" in r.json()["detail"]
    # no preference leaked through
    data = c.get("/api/wan").json()
    assert data.get("topology", "lan") == "lan"
    # changing the password clears the gate
    assert c.post("/api/password", json={
        "current": "admin", "new": WEAK}).status_code == 200
    r = c.post("/api/login", json={"password": WEAK})
    assert r.status_code == 200
    r = c.post("/api/wan", json={"topology": "wan"})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] == "wan"


def test_setup_change_clears_default_flag(client):
    c, database, _, _, _ = client
    _login(c)
    assert c.post("/api/setup/complete", json={
        "current_password": "admin", "new_password": WEAK}).status_code == 200
    flag = asyncio.get_event_loop().run_until_complete(
        database.get_setting("admin_password_default", "1"))
    assert flag == "0"


# -- login: generic message, no enumeration -----------------------------------


def test_unknown_account_and_wrong_password_identical(client):
    c, database, _, _, _ = client
    # no account yet (or a DB wiped of the hash) -> same generic 401 as a
    # wrong password: the response never reveals which part failed
    r_none = c.post("/api/login", json={"password": "AdminWrong"})
    assert r_none.status_code == 401
    asyncio.get_event_loop().run_until_complete(
        database.set_setting("admin_password", _hash("s3cret")))
    r_wrong = c.post("/api/login", json={"password": "AdminWrong"})
    assert r_wrong.status_code == 401
    assert r_none.json() == r_wrong.json() == {"detail": "invalid credentials"}


def _hash(password: str) -> str:
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"{salt.hex()}$600000${dk.hex()}"


# -- session invalidation ------------------------------------------------------


def test_logout_rotates_session(client):
    c, _, _, _, _ = client
    _login(c)
    assert c.get("/api/me").json()["authenticated"] is True
    token_before = c.cookies.get("qmsession")
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").json()["authenticated"] is False
    assert c.cookies.get("qmsession") != token_before
    # the old cookie can never be replayed
    c.cookies.set("qmsession", token_before)
    assert c.get("/api/me").json()["authenticated"] is False


def test_password_change_rotates_session(client):
    c, _, _, _, _ = client
    _login(c)
    assert c.get("/api/me").json()["authenticated"] is True
    assert c.post("/api/password", json={
        "current": "admin", "new": WEAK}).status_code == 200
    # the (now-rotated) old cookie is dead
    assert c.get("/api/me").json()["authenticated"] is False
    assert c.post("/api/password", json={
        "current": WEAK, "new": WEAK + "!"}).status_code == 401
    # a fresh login with the new password works
    assert c.post("/api/login", json={"password": WEAK}).status_code == 200
    assert c.get("/api/me").json()["authenticated"] is True


# -- TOTP (quota/totp.py + /api/totp*) ----------------------------------------


def test_totp_full_flow(client):
    c, _, _, _, _ = client
    _login(c)
    assert c.get("/api/totp").json() == {"enabled": False, "pending": False}
    r = c.post("/api/totp/enroll")
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert _totp.is_valid_secret(secret)
    # wrong code -> not enabled
    r = c.post("/api/totp/enable", json={"code": "000000"})
    assert r.status_code == 400
    assert c.get("/api/totp").json()["enabled"] is False
    # right code -> enabled
    r = c.post("/api/totp/enable", json={"code": _valid_totp(secret)})
    assert r.status_code == 200, r.text
    assert c.get("/api/totp").json() == {"enabled": True}
    # logout + login: password alone is NOT enough anymore
    assert c.post("/api/logout").status_code == 200
    r = c.post("/api/login", json={"password": "admin"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "totp": True}  # stage 1: prompt for code
    # code alone never suffices — wrong password + valid code -> 401
    r = c.post("/api/login", json={
        "password": "wrong-pass", "code": _valid_totp(secret)})
    assert r.status_code == 401
    # correct password + valid code -> session
    r = c.post("/api/login", json={
        "password": "admin", "code": _valid_totp(secret)})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert c.get("/api/me").json()["authenticated"] is True
    # wrong code -> 401 + failed-login audit
    r = c.post("/api/login", json={"password": "admin", "code": "111111"})
    assert r.status_code == 401
    # disable restores the password-only flow
    r = c.post("/api/totp/disable")
    assert r.status_code == 200
    assert c.get("/api/totp").json()["enabled"] is False
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200


def test_totp_requires_session(client):
    c, _, _, _, _ = client
    assert c.get("/api/totp").status_code == 401
    assert c.post("/api/totp/enroll").status_code == 401


def test_totp_enroll_conflict_when_enabled(client):
    c, _, _, _, _ = client
    _login(c)
    r = c.post("/api/totp/enroll")
    secret = r.json()["secret"]
    assert c.post("/api/totp/enable",
                  json={"code": _valid_totp(secret)}).status_code == 200
    assert c.post("/api/totp/enroll").status_code == 409


# -- CSRF custom-header guard --------------------------------------------------


def test_csrf_blocks_missing_header(client):
    c, _, _, _, _ = client
    _login(c)
    # a browser-context (Origin present) mutation without the custom header
    r = c.post("/api/bundle", json={"total_gb": 50},
               headers={"Origin": "http://testserver"})
    assert r.status_code == 403
    assert r.json()["detail"] == "missing CSRF token"
    # the SAME request with the custom header passes
    r = c.post("/api/bundle", json={"total_gb": 50},
               headers={"Origin": "http://testserver", "X-QM-CSRF": "1"})
    assert r.status_code == 200, r.text
    assert c.get("/api/bundle").json()["total_gb"] == 50.0
    # a raw (non-browser) client is NOT broken — no Origin/Referer, no gate
    r = c.post("/api/bundle", json={"total_gb": 60})
    assert r.status_code == 200, r.text


# -- security headers ----------------------------------------------------------


def test_security_headers_present(client):
    c, _, _, _, _ = client
    _login(c)
    r = c.get("/api/dashboard")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_report_page_gets_looser_csp(client):
    c, _, _, _, _ = client
    r = c.get("/report")  # inline scripts allowed, still sandboxed otherwise
    assert "script-src 'self' 'unsafe-inline'" in r.headers["content-security-policy"]


# -- API surface hygiene (no public schema / noindex) --------------------------


def test_docs_disabled_by_default(client):
    c, _, _, _, make = client
    # the FastAPI auto-docs + the full OpenAPI schema are OFF by default — an
    # attacker reaching the port gets NO structured endpoint map to mine
    assert c.get("/api/docs").status_code == 404
    assert c.get("/api/openapi.json").status_code == 404
    # an explicit dev opt-in (web.docs_enabled) restores them
    from core.config import WebConfig
    with TestClient(make(web_config=WebConfig(docs_enabled=True))) as c2:
        assert c2.get("/api/docs").status_code == 200
        assert c2.get("/api/openapi.json").status_code == 200


def test_noindex_headers_and_robots(client):
    c, _, _, _, _ = client
    assert c.get("/api/dashboard").headers["x-robots-tag"] == "noindex, nofollow"
    r = c.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /" in r.text


def test_sensitive_responses_never_cached(client):
    """Sensitive-data protection: every DATA response (API + HTML pages) is
    no-store so the browser never persists MACs/usage/history/logs to cache
    or disk on a shared machine; static assets stay cacheable."""
    c, _, _, _, _ = client
    _login(c)
    for path in ("/api/dashboard", "/api/wan", "/api/logs",
                 "/report", "/milestone", "/"):
        r = c.get(path)
        assert "no-store" in r.headers.get("cache-control", ""), path
        assert "no-cache" in r.headers.get("cache-control", ""), path
    # static assets are public and secret-free — caching them is fine
    for path in ("/assets/app.js", "/assets/styles.css"):
        r = c.get(path)
        assert "no-store" not in r.headers.get("cache-control", ""), path


def test_dashboard_payload_minimized(client):
    """Data minimization: the broadcast dashboard payload no longer carries the
    raw events list or the gateway log tail (the most sensitive plaintext) —
    both were shipped every 5 s with no UI consumer. The on-demand /api/logs
    endpoint still serves the admin Logs panel (legacy path intact)."""
    c, _, _, _, _ = client
    _login(c)
    data = c.get("/api/dashboard").json()
    assert "events" not in data
    assert "logs" not in data
    assert "security" in data  # aggregated counts still drive the banner
    logs = c.get("/api/logs?limit=50")
    assert logs.status_code == 200
    assert "lines" in logs.json() or "tail" in logs.json() or "log" in logs.json()


# -- dashboard security block ---------------------------------------------------


def test_dashboard_security_block(client):
    c, database, _, _, _ = client
    _login(c)
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    sec = c.get("/api/dashboard").json()["security"]
    assert sec["failed_logins_1h"] >= 1
    assert sec["waf_blocks_1h"] == 0
    assert sec["default_password"] is True
    assert sec["totp_enabled"] is False
    assert sec["wan_http"] is False


# -- WAF classification (api/waf.py) -------------------------------------------


def test_waf_classify_signatures():
    h = {"host": "box"}
    assert _waf.classify("GET", "/", "", "id=1 union select * from users", h) \
        == ("sqli-1", "sql-injection")
    assert _waf.classify("GET", "/api/x?q=", "<script>alert(1)</script>", "", h) \
        == ("xss-1", "xss")
    assert _waf.classify("POST", "/api/x", "", "whoami && cat /etc/passwd", h) \
        in (("cmdi-1", "command-injection"), ("cmdi-3", "command-injection"))
    assert _waf.classify("GET", "/../../../etc/passwd", "", "", h) \
        == ("path-1", "path-traversal")
    assert _waf.classify("TRACE", "/", "", "", h) == ("method", "method-not-allowed")
    assert _waf.classify("GET", "/", "", "", {}) == ("host-1", "missing-host")
    assert _waf.classify("GET", "/api/dashboard", "", "", h) is None
    assert _waf.classify_ua("sqlmap/1.7.4 testing") is True
    assert _waf.classify_ua("Mozilla/5.0 (Windows NT 10.0) Firefox/128") is False


def test_waf_mode_resolution():
    assert _waf.resolve_mode("auto", "wan") == "strict"
    assert _waf.resolve_mode("auto", "lan") == "log"
    assert _waf.resolve_mode("strict", "lan") == "strict"
    assert _waf.resolve_mode("off", "wan") == "off"
    assert _waf.resolve_mode("garbage", "lan") == "log"


def test_waf_rate_state_window():
    state = _waf.WafRateState()
    limits = {"/api/login": [2, 60]}
    t = 1000.0
    assert state.rate_limited("1.2.3.4", "/api/login", limits, t) is False
    assert state.rate_limited("1.2.3.4", "/api/login", limits, t) is False
    assert state.rate_limited("1.2.3.4", "/api/login", limits, t) is True
    # window expires
    assert state.rate_limited("1.2.3.4", "/api/login", limits, t + 61) is False
    # other paths / IPs unaffected
    assert state.rate_limited("1.2.3.4", "/api/dashboard", limits, t) is False
    assert state.rate_limited("5.6.7.8", "/api/login", limits, t) is False


# -- WAF middleware behaviour ---------------------------------------------------


def test_waf_strict_blocks_on_wan(client):
    c, database, _, holder, _ = client
    holder.swap(EngineSnapshot(wan_status={"topology": "wan"}))
    # SQLi payload to a mutating route -> blocked before the handler runs
    r = c.post("/api/bundle", json={"total_gb": 1},
               data="total_gb=1' or 1=1 --", headers={
                   "content-type": "text/plain"})
    assert r.status_code == 403
    assert "WAF" in r.json()["detail"]
    events = asyncio.get_event_loop().run_until_complete(database.list_events())
    assert any("WAF" in e["message"] for e in events)


def test_waf_log_only_on_lan(client):
    c, database, _, holder, _ = client
    holder.swap(EngineSnapshot(wan_status={"topology": "lan"}))
    # same payload on LAN: recorded, NOT blocked (the LAN dashboard stays up)
    r = c.post("/api/bundle", json={"total_gb": 1},
               data="total_gb=<script>alert(1)</script>", headers={
                   "content-type": "text/plain"})
    assert r.status_code == 401  # passed through WAF, hit auth
    events = asyncio.get_event_loop().run_until_complete(database.list_events())
    assert any("WAF xss" in e["message"] for e in events)


def test_waf_scanner_ua_blocks_on_wan(client):
    c, _, _, holder, _ = client
    holder.swap(EngineSnapshot(wan_status={"topology": "wan"}))
    r = c.get("/api/dashboard", headers={"User-Agent": "sqlmap/1.7.4"})
    assert r.status_code == 403


def test_waf_oversized_body_blocks(client):
    c, _, _, holder, make = client
    holder.swap(EngineSnapshot(wan_status={"topology": "wan"}))
    app = make(WafConfig(max_body_bytes=64))
    with TestClient(app) as c2:
        c2.post("/api/login", json={"password": "admin"})
        r = c2.post("/api/bundle",
                    json={"total_gb": 1, "padding": "x" * 100})
        assert r.status_code == 403
        assert "oversized-body" in str(r.json())


def test_waf_endpoint_rate_limit_strict(client):
    c, _, _, holder, make = client
    holder.swap(EngineSnapshot(wan_status={"topology": "wan"}))
    app = make(WafConfig(endpoint_limits={"/api/dashboard": [1, 60]}))
    with TestClient(app) as c2:
        c2.post("/api/login", json={"password": "admin"})
        assert c2.get("/api/dashboard").status_code == 200
        assert c2.get("/api/dashboard").status_code == 429


def test_waf_off_when_disabled(client):
    c, _, _, holder, make = client
    holder.swap(EngineSnapshot(wan_status={"topology": "wan"}))
    app = make(WafConfig(enabled=False))
    with TestClient(app) as c2:
        r = c2.post("/api/bundle", json={"total_gb": 1},
                    data="x' or 1=1 --", headers={
                        "content-type": "text/plain"})
        assert r.status_code == 401  # WAF off -> passed through


# -- SSRF guards (updater / dns_rules) -----------------------------------------


def test_updater_ssrf_url_guard():
    from quota import updater
    updater._assert_safe_github_url(
        "https://raw.githubusercontent.com/UserJoo9/QuotaManager/main/x")
    for evil in ("http://169.254.169.254/latest/meta-data/",
                 "https://evil.example.com/QuotaManager/releases",
                 "file:///etc/passwd",
                 "https://github.com.evil.com/repo"):
        with pytest.raises(ValueError):
            updater._assert_safe_github_url(evil)


def test_dns_rules_ssrf_url_guard():
    from quota import dns_rules
    dns_rules._assert_safe_preset_url(
        "https://raw.githubusercontent.com/foo/bar/main/hosts")
    for evil in ("https://evil.example.com/hosts",
                 "http://127.0.0.1/presets",
                 "https://raw.githubusercontent.com@evil.com/x"):
        with pytest.raises(ValueError):
            dns_rules._assert_safe_preset_url(evil)


# -- login limiter unit tests ---------------------------------------------------


def test_login_limiter_escalation():
    from api.app import _LoginLimiter
    lim = _LoginLimiter()
    key = "ip:10.0.0.9"
    now = 1000.0
    assert lim.check(key, now) == 0.0
    for i in range(3):
        assert lim.check(key, now + i) == 0.0
        lim.fail(key, now + i)
    # crossing the free tier arms the first step (1s)
    assert lim.check(key, now + 3.0) == 0.0
    lim.fail(key, now + 3.0)
    retry1 = lim.check(key, now + 3.5)
    assert retry1 > 0.0
    lim.fail(key, now + 3.5)  # rejection escalates
    retry2 = lim.check(key, now + 3.6)
    assert retry2 > retry1
    lim.success(key)
    assert lim.check(key, now + 3.6) == 0.0
