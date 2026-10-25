"""Dashboard (web/) guards: served, cache-safe, CSP-clean, self-contained, i18n parity."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.service import QuotaService

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "web"


def _css():
    return list((UI / "css").rglob("*.css"))


def _js():
    return list((UI / "js").rglob("*.js"))


@pytest.fixture
def client(tmp_path):
    loop = asyncio.new_event_loop()
    database = _db.Database(tmp_path / "ui.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    loop.run_until_complete(database.connect())
    app = create_app(database, service, SnapshotHolder())
    with TestClient(app) as c:
        yield c
    loop.run_until_complete(database.close())
    loop.close()


def test_ui_index_served_with_revalidation(client):
    r = client.get("/")
    assert r.status_code == 200 and "QuotaManager" in r.text and "js/main.js" in r.text
    assert "no-store" in r.headers["cache-control"]  # the page itself is never cached
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_ui_js_and_fonts_served(client):
    r = client.get("/js/main.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"  # revalidate after an update
    f = client.get("/fonts/inter-latin.woff2")
    assert f.status_code == 200 and f.content[:4] == b"wOF2"
    assert client.get("/logo.svg").status_code == 200
    assert client.get("/css/tokens.css").headers["cache-control"] == "no-cache"
    assert client.get("/assets/pub.css").headers["cache-control"] == "no-cache"


def test_old_ui_addresses_redirect_to_the_root(client):
    for old, new in (("/ui", "/"), ("/ui/", "/"), ("/ui/js/main.js", "/js/main.js"), ("/ui/css/tokens.css", "/css/tokens.css")):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == new, old
    assert client.get("/ui/", follow_redirects=True).status_code == 200


def test_classic_dashboard_is_gone():
    for f in ("assets/app.js", "assets/styles.css"):
        assert not (UI / f).exists(), f
    assert not (UI / "ui").exists()


def test_index_has_no_inline_script_or_handlers():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\son\w+\s*=", html)


def test_no_external_urls_in_ui_sources():
    # strict CSP: fonts/scripts/styles must all be self-hosted
    for p in _css() + _js() + [UI / "index.html"]:
        txt = p.read_text(encoding="utf-8")
        for m in re.finditer(r"https?://[^\s\"')]+", txt):
            assert m.group(0).startswith("http://www.w3.org/"), f"{p.name}: {m.group(0)}"


def test_relative_references_resolve():
    for p in _css():
        for m in re.finditer(r"url\(([^)]+)\)", p.read_text(encoding="utf-8")):
            ref = m.group(1).strip("\"'")
            if ref.startswith("#") or ref.startswith("data:"):
                continue  # in-document SVG fragment, not a file
            assert (p.parent / ref).resolve().exists(), f"{p.name}: {ref}"
    for p in _js():
        for m in re.finditer(r"from \"(\.[^\"]+)\"", p.read_text(encoding="utf-8")):
            assert (p.parent / m.group(1)).resolve().exists(), f"{p.name}: {m.group(1)}"
    html = (UI / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r"(?:href|src)=\"([^\"#]+)\"", html):
        assert (UI / ref.split("?")[0]).exists(), ref


def _keys(lang: str) -> set[str]:
    txt = (UI / "js" / "i18n" / f"{lang}.js").read_text(encoding="utf-8")
    # key followed by a quoted value; keys may be bare (app:) or quoted ("nav.main":)
    return set(re.findall(r"(?:^|,)\s*\"?([\w.]+)\"?\s*:\s*\"", txt, flags=re.M))


def test_i18n_arabic_and_english_have_same_keys():
    en, ar = _keys("en"), _keys("ar")
    assert len(en) > 60
    assert en == ar, f"missing in ar: {en - ar}; missing in en: {ar - en}"


def test_every_translation_key_used_exists():
    en = _keys("en")
    used: set[str] = set()
    for p in _js():
        if "i18n" in p.parts:
            continue
        used |= set(re.findall(r"\bt\(\"([\w.]+)\"\)", p.read_text(encoding="utf-8")))
    assert not (used - en), f"undefined keys: {used - en}"


def test_vpn_page_only_calls_endpoints_that_exist():
    """The new VPN page must hit the exact routes the gateway exposes (no drift)."""
    app_src = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    routes = {re.sub(r"\{\w+\}", "{}", m) for m in re.findall(r"@app\.\w+\(\"(/api/vpn[^\"]*)\"", app_src)}
    js = (UI / "js" / "core" / "vpn.js").read_text(encoding="utf-8")
    used = {re.sub(r"\$\{[^}]+\}", "{}", m).split("?")[0] for m in re.findall(r"[\"`](/api/vpn[^\"`]*)[\"`]", js)}
    assert used, "no VPN endpoints found in core/vpn.js"
    assert used <= routes, f"unknown endpoints: {used - routes}"


def test_usage_daily_endpoint(client):
    """Read-only per-day totals feeding the Overview chart."""
    client.post("/api/login", json={"password": "admin"})
    r = client.get("/api/usage/daily?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 7 and isinstance(body["rows"], list)
    assert client.get("/api/usage/daily?days=99999").json()["days"] == 366


def test_usage_daily_requires_login(client):
    assert client.get("/api/usage/daily").status_code == 401
