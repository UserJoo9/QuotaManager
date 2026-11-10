"""Dashboard guards: served, cache-safe, CSP-clean, self-contained, i18n parity.

Since 0.6 the whole dashboard is React + TypeScript (frontend/src), built by Vite into
web/index.html + web/app/* (hashed names). web/js/ keeps only the shared dictionaries
(web/js/i18n/{en,ar}.js) and web/css/ only the design tokens."""

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
SRC = ROOT / "frontend" / "src"


def test_dashboard_uses_themed_choices_and_scrollbars():
    for path in SRC.rglob("*.tsx"):
        if path.name.endswith(".test.tsx"):
            continue
        assert not re.search(r"<(?:select|datalist)\b", path.read_text(encoding="utf-8")), path
    css = (SRC / "styles/app.css").read_text(encoding="utf-8")
    assert "::-webkit-scrollbar-thumb" in css
    assert "::-webkit-scrollbar-button" in css and "scrollbar-color:" in css


def _css():
    return list((UI / "css").rglob("*.css"))


def _js():
    return list((UI / "js").rglob("*.js"))


def _tsx():
    return [p for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx", ".css")]


def _entry() -> str:
    html = (UI / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="module"[^>]*src="(/app/index-[\w-]+\.js)"', html)
    assert m, "web/index.html has no built entry - run: cd frontend && npm run build"
    return m.group(1)


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
    assert r.status_code == 200 and "QuotaManager" in r.text and _entry() in r.text
    assert "no-store" in r.headers["cache-control"]  # the page itself is never cached
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_hashed_app_files_are_immutable_and_precompressed(client):
    """Regression (stale UI after update): only content-hashed files may be cached
    long-term; a new build changes every name and index.html is never cached."""
    entry = _entry()
    r = client.get(entry, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers.get("content-encoding") == "gzip"  # served from the build's .gz twin
    assert "javascript" in r.headers["content-type"]
    plain = client.get(entry, headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers
    assert client.get(entry + ".gz").status_code == 404
    assert client.get("/app/..%2Fapi.py").status_code == 404
    assert client.get("/app/nope-12345.js").status_code == 404
    for f in (UI / "app").iterdir():
        if f.suffix in (".js", ".css") and f.stat().st_size >= 1024:
            assert f.with_name(f.name + ".gz").exists(), f.name


def test_ui_js_and_fonts_served(client):
    r = client.get("/js/i18n/en.js")
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
    for p in _css() + _js() + _tsx() + [UI / "index.html", ROOT / "frontend" / "index.html"]:
        txt = p.read_text(encoding="utf-8")
        for m in re.finditer(r"https?://[^\s\"')]+", txt):
            assert m.group(0).startswith("http://www.w3.org/"), f"{p.name}: {m.group(0)}"


def test_relative_references_resolve():
    for p in _css():
        for m in re.finditer(r"url\(([^)]+)\)", p.read_text(encoding="utf-8")):
            ref = m.group(1).strip("\"'")
            if ref.startswith("#") or ref.startswith("data:"):
                continue  # in-document SVG fragment, not a file
            assert (p.parent / ref.split("?")[0]).resolve().exists(), f"{p.name}: {ref}"
    for p in _js():
        for m in re.finditer(r"from \"(\.[^\"]+)\"", p.read_text(encoding="utf-8")):
            assert (p.parent / m.group(1)).resolve().exists(), f"{p.name}: {m.group(1)}"
    html = (UI / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r"(?:href|src)=\"([^\"#]+)\"", html):
        assert (UI / ref.lstrip("/").split("?")[0]).exists(), ref


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
    for p in _tsx():
        txt = p.read_text(encoding="utf-8")
        used |= set(re.findall(r"\bt\(\"([\w.]+)\"\)", txt)) | set(re.findall(r"\btf\(\"([\w.]+)\"", txt))
    assert not (used - en), f"undefined keys: {used - en}"


def test_vpn_page_only_calls_endpoints_that_exist():
    """The new VPN page must hit the exact routes the gateway exposes (no drift)."""
    app_src = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    routes = {re.sub(r"\{\w+\}", "{}", m) for m in re.findall(r"@app\.\w+\(\"(/api/vpn[^\"]*)\"", app_src)}
    js = (SRC / "lib" / "vpn.ts").read_text(encoding="utf-8")
    used = {re.sub(r"\$\{[^}]+\}", "{}", m).split("?")[0] for m in re.findall(r"[\"`](/api/vpn[^\"`]*)[\"`]", js)}
    assert used, "no VPN endpoints found in frontend/src/lib/vpn.ts"
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


def test_daily_plan_ui_is_wired_and_guarded():
    """The optional daily plan: sheet section, device share, master switch on
    the Quota page, a today line in the people list, and the milestone editor
    that must send the CSRF header."""
    dp = (SRC / "components/domain/DailyPlan.tsx").read_text()
    assert "daily-plan/preview" in dp and "export function DeviceShare" in dp
    # unsaved heavy-day edits must survive live updates of the sheet
    assert "heavyDraft" in dp
    ps = (SRC / "components/domain/PersonSheet.tsx").read_text()
    assert "<DailySection" in ps and "<DeviceShare" in ps
    assert '"/api/daily"' in (SRC / "pages/Quota.tsx").read_text()
    assert "u.daily" in (SRC / "pages/People.tsx").read_text()
    ms = (UI / "milestone.html").read_text()
    assert "/api/milestone/daily-plan" in ms and "'X-QM-CSRF': '1'" in ms
    # the daily editor builds nodes with textContent, never innerHTML of server text
    seg = ms[ms.index("function renderDaily"):ms.index("function ack")]
    assert "innerHTML = '" not in seg.replace("box.innerHTML = '';", "").replace("list.innerHTML = '';", "")


def test_react_never_injects_raw_html():
    """Device names/hostnames are user-controlled: React text only, no raw HTML sinks."""
    for p in _tsx():
        txt = p.read_text(encoding="utf-8")
        assert "dangerouslySetInnerHTML" not in txt and ".innerHTML" not in txt, p.name


def test_live_snapshot_never_overwrites_a_draft():
    """Regression (drawer rebuilt under the cursor): forms keep a local draft and only
    follow the server while untouched; number fields keep the typed STRING."""
    ps = (SRC / "components/domain/PersonSheet.tsx").read_text()
    assert "if (!dirty) { setDraft(server)" in ps
    forms = (SRC / "components/ui/forms.tsx").read_text()
    assert "if (!focused.current)" in forms  # a server value never replaces the box being typed in
    assert "never turns\n * an empty box into 0" in forms or "never turns" in forms


def test_mobile_sheet_keyboard_contract():
    ov = (SRC / "components/ui/overlays.tsx").read_text()
    assert "visualViewport" in ov and "--qm-kb" in ov and "focusin" in ov
    css = (SRC / "styles/app.css").read_text()
    assert "font-size: 16px" in css  # iOS zooms into smaller focused fields
    assert "env(safe-area-inset-bottom)" in css and "prefers-reduced-motion" in css


def test_network_changes_are_never_optimistic():
    """Only appearance/sidebar prefs may update before the server answers; gateway
    changes re-read the dashboard before reporting success."""
    act = (SRC / "lib/actions.ts").read_text()
    assert "await fn();" in act and "await refresh()" in act
    assert act.index("await fn();") < act.index('kind: "success"')
    assert "setQueryData" not in act


def test_transfer_engine_starts_at_sign_in_on_every_page():
    """Regression: the old shell opened the live-transfer socket at boot, so an incoming
    request popped up on ANY page. The React shell mounts the overlay (which starts the
    engine) outside the routed page, and the wire protocol is unchanged."""
    app = (SRC / "shell/App.tsx").read_text()
    assert "<TransferOverlay />" in app and app.index("<TransferOverlay />") > app.index("</Suspense>")
    ov = (SRC / "shell/TransferOverlay.tsx").read_text()
    assert "startLiveShare()" in ov and "acceptIncoming" in ov and "declineIncoming" in ov
    eng = (SRC / "lib/liveTransfer.ts").read_text()
    for needle in ("/api/share/live/ws", "/api/share/live/download/", "transfer-request", "transfer-response",
                   "transfer-done", "transfer-cancel", "0x04034b50", "0x02014b50", "0x06054b50", "http_stream: true"):
        assert needle in eng, needle
    assert not (UI / "js" / "pages").exists() and not (UI / "js" / "core").exists(), "every page is React now"


def test_sidebar_order_uses_stable_ids_and_survives_new_routes():
    rt = (SRC / "shell/routes.ts").read_text()
    assert "export function mergeOrder" in rt and "v: 1" in (SRC / "lib/prefs.ts").read_text()
    sb = (SRC / "shell/Sidebar.tsx").read_text()
    assert '"navOrder", null' in (SRC / "pages/System.tsx").read_text()  # one-click reset
    dnd = (SRC / "shell/SortableNav.tsx").read_text()
    assert "KeyboardSensor" in dnd and "TouchSensor" in dnd  # keyboard + long-press, not pointer-only
    assert "nav.reorder" in sb  # the handle has an accessible name


def test_release_notes_are_rendered_not_dumped_as_raw_markdown():
    """The "What's new" dialog turns **bold**, `code`, ### headings and - bullets
    into nodes (never raw HTML) and lets each line pick its own direction."""
    js = (SRC / "pages/SystemTabs.tsx").read_text(encoding="utf-8")
    assert "export function mdLite" in js and 'dir="auto"' in js
    assert "mdLite(c.body)" in js and "pre-wrap" not in js[js.index("upd.details"):js.index("upd.details") + 900]


def test_read_only_dialogs_have_one_close_button():
    js = (SRC / "pages/SystemTabs.tsx").read_text(encoding="utf-8")
    notes = js[js.index("<Dialog open={notes}"):js.index("</Dialog>", js.index("<Dialog open={notes}"))]
    assert 't("common.close")' in notes and 't("common.cancel")' not in notes


def test_public_pages_have_a_language_switch():
    """/share and /milestone share one tiny AR/EN switch; every Arabic key used
    in the code has a matching dictionary entry."""
    import re
    web = ROOT / "web" if "ROOT" in globals() else __import__("pathlib").Path(__file__).resolve().parent.parent / "web"
    for page, dic in (("share.html", "i18n-share.js"), ("milestone.html", "i18n-milestone.js")):
        html = (web / page).read_text(encoding="utf-8")
        assert "/assets/pub-i18n.js" in html and f"/assets/{dic}" in html and "QMI.mount(" in html
        d = (web / "assets" / dic).read_text(encoding="utf-8")
        for key in re.findall(r"QMI\.t\((['\"])((?:\\.|(?!\1).)*)\1", html):
            k = key[1].replace('\\"', '"').replace("\\'", "'")
            assert f'"{k}"'.replace('"', '"') in d or k in d.replace('\\"', '"'), f"{page}: missing Arabic for {k!r}"
