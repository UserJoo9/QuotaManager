"""Web UI smoke tests: FastAPI must serve the glassmorphism dashboard."""

from __future__ import annotations

import asyncio

import pytest
# import asyncio
_cached_loop = None
def _get_loop():
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop

from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.service import QuotaService


@pytest.fixture
def client(tmp_path):
    database = _db.Database(tmp_path / "ui.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    _get_loop().run_until_complete(database.connect())
    app = create_app(database, service, holder)
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "admin"})
        yield c
    _get_loop().run_until_complete(database.close())


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    for needle in ("Quota Manager", "assets/styles.css", "assets/app.js"):
        assert needle in r.text
    # v10: top-bar tab navigation replaces the stacked sections; the
    # "Usage this period" chart section stays gone (moved to Management).
    assert "Usage this period" not in r.text
    # top-bar nav buttons (v11 adds the Network tab for speed shaping;
    # the v25 merge folded Bundle settings into it; v27 renames it to "Network")
    assert 'data-panel="management"' in r.text
    assert 'data-panel="network"' in r.text
    assert 'data-panel="admin"' in r.text
    assert 'data-panel="logs"' not in r.text
    assert ">Management<" in r.text
    assert ">Network<" in r.text
    assert ">Admin<" in r.text
    assert ">Logs<" not in r.text
    # v26: the Logs tab is gone — the full System Logs console is embedded on
    # the Admin page (2-column top grid + the logs card below).
    assert "System Logs" in r.text
    assert 'id="panel-logs"' not in r.text
    assert "admin-layout" in r.text
    assert "admin-grid" in r.text
    assert "<h3>Security</h3>" in r.text
    assert "System Info &amp; About" in r.text
    assert 'id="log-filters"' in r.text
    assert 'id="log-search"' in r.text
    assert 'id="log-refresh"' in r.text
    assert 'id="log-download"' in r.text
    # Activity tab/panel removed (System logs shows the same info)
    assert "panel-activity" not in r.text
    assert "events-list" not in r.text
    assert "tab-activity" not in r.text
    # v25: the standalone Bundle settings tab is gone — its controls moved
    # into the unified Network & Quota panel (bundle config + guest mode +
    # connection toggles) with a 65/35 grid and a single overview card.
    assert "panel-bundle" not in r.text
    assert ">Bundle settings<" not in r.text
    assert 'id="panel-network"' in r.text
    assert "netquota-layout" in r.text
    assert "Bundle configuration" in r.text
    assert "Connection &amp; security" in r.text
    assert "Live Network &amp; Bundle overview" in r.text
    # guest-mode UI on the Network & Quota panel
    assert "Guest mode" in r.text
    assert 'id="guest-mode-toggle"' in r.text
    assert 'id="guest-quota"' in r.text
    assert 'id="guest-speed-limit"' in r.text
    assert 'id="guest-limit"' in r.text
    assert 'id="stop-new-toggle"' in r.text
    # v27: decline-random-MAC gate (toggle + one-shot existing sweep)
    assert 'id="decline-random-toggle"' in r.text
    assert 'id="decline-random-existing"' in r.text
    # v27: per-user "exempt from quota" (user modal checkbox + device-modal note)
    assert 'id="u-exempt"' in r.text
    assert 'id="d-bypass-exempt-note"' in r.text
    # v27: privacy eye (sidebar quick action) — mask MACs + PPPoE password
    assert 'id="privacy-eye"' in r.text
    # Bundle summary lives INSIDE the Management panel (first tab only), and
    # the Consumption donut section was removed (no usage-chart canvas).
    assert 'id="panel-management"' in r.text
    assert r.text.index('id="panel-management"') < r.text.index("bundle-used")
    assert 'id="usage-chart"' not in r.text
    assert "assets/app.js?v=57" in r.text
    assert "assets/styles.css?v=57" in r.text
    # v24: the sidebar collapse toggle is gone — the sidebar is a fixed rail.
    assert "sidebar-toggle" not in r.text
    assert "sidebar-collapsed" not in r.text
    # the internet reachability pill lives in the sidebar footer, not the WAN
    # status panel (probed every 15 s; dot color = reachability).
    assert 'id="net-status"' in r.text
    assert "net-label" in r.text
    assert "Checking…" in r.text
    assert 'id="wan-internet"' not in r.text
    # v14: first-run welcome overlay + its form fields
    assert 'id="welcome-overlay"' in r.text
    assert 'id="setup-total"' in r.text
    assert 'id="setup-reset-day"' in r.text
    assert 'id="setup-cur-pw"' in r.text
    assert 'id="setup-new-pw"' in r.text
    assert 'id="welcome-skip"' in r.text

    # v11: Network panel (speed limits + latency) with its controls
    assert 'id="panel-network"' in r.text
    assert "Speed limits" in r.text
    assert 'id="shaping-toggle"' in r.text
    assert 'id="set-total-down"' in r.text
    assert 'id="set-total-up"' in r.text
    assert 'id="set-lan-rate"' in r.text
    assert 'id="np-lan"' in r.text
    assert 'id="shaping-save-btn"' in r.text
    # speed-limit inputs in the device + user modals
    assert 'id="d-limit-down"' in r.text
    assert 'id="d-limit-up"' in r.text
    assert 'id="u-limit-down"' in r.text
    assert 'id="u-limit-up"' in r.text


def test_recharge_ui_elements_present(client):
    """The dashboard exposes 'Bundle recharged' and a 0-friendly reset day."""
    r = client.get("/")
    assert "Bundle recharged" in r.text
    assert "set-recharge" in r.text
    assert 'id="recharge-btn"' in r.text
    # reset-day input now accepts 0 (manual)
    assert 'id="set-reset-day" min="0" max="31"' in r.text

    r = client.get("/assets/app.js")
    assert "submitRecharge" in r.text
    assert "add_gb" in r.text
    assert "→ manual" in r.text          # reset_day=0 period rendering
    assert "days_left < 0" in r.text


def test_rogue_section_present(client):
    """v17: the Management panel shows the 'Unmanaged / rogue devices' card
    (active-but-unleased hosts), and the JS renders it from the payload."""
    r = client.get("/")
    assert 'id="rogue-section"' in r.text
    assert 'id="rogue-count"' in r.text
    assert 'id="rogue-list"' in r.text
    assert "Unmanaged / rogue devices" in r.text
    rjs = client.get("/assets/app.js")
    assert "renderRogue" in rjs.text
    assert "data.rogue" in rjs.text


def test_wan_tab_present(client):
    """v19: the WAN tab + panel expose the live Apply/Revert controls
    (creds fields, Apply now / Revert to LAN buttons), the step-by-step
    Egyptian-router guide, and a live status preview from /api/wan."""
    r = client.get("/")
    assert 'data-panel="wan"' in r.text
    assert ">WAN<" in r.text
    assert 'id="panel-wan"' in r.text
    assert 'id="wan-toggle"' in r.text
    assert 'id="wan-restart-banner"' in r.text
    assert 'id="wan-creds"' in r.text
    assert 'id="wan-user"' in r.text
    assert 'id="wan-pass"' in r.text
    assert 'id="wan-if"' in r.text
    assert 'id="wan-apply-btn"' in r.text
    assert 'id="wan-revert-btn"' in r.text
    assert 'id="wan-test-btn"' in r.text
    assert 'id="wan-test-msg"' in r.text
    assert 'id="wan-topology"' in r.text
    assert 'id="wan-source"' in r.text
    assert 'id="wan-ppp0"' in r.text
    assert 'id="wan-ppp-ip"' in r.text
    # v24: WAN public-IP renewal — the Restart button + the auto-renew schedule
    # block (the schedule + button are disabled until ppp0 is actually UP).
    assert 'id="wan-restart-btn"' in r.text
    assert 'id="wan-renew-last"' in r.text
    assert 'id="wan-renew-disabled-note"' in r.text
    assert 'id="wan-renew-toggle"' in r.text
    assert 'id="wan-renew-minutes"' in r.text
    assert 'id="wan-renew-save"' in r.text
    assert 'id="wan-renew-msg"' in r.text
    assert "Restart PPPoE — renew public IP" in r.text
    assert "Auto-renew" in r.text
    # the guide names both physical paths (single-NIC bridge + two-NIC fallback)
    assert "bridge" in r.text or "AP mode" in r.text
    assert "Apply now" in r.text
    assert "Revert to LAN" in r.text
    assert "Test PPPoE connection" in r.text
    # v19.5: the box keeps the router's LAN address as a secondary alias, so the
    # router admin page stays reachable in WAN mode with no extra commands.
    assert "Router admin" in r.text
    assert "remains accessible" in r.text
    # removed

    rjs = client.get("/assets/app.js")
    assert "renderWan" in rjs.text
    assert "refreshWan" in rjs.text
    assert "submitWan" in rjs.text
    assert "revertWan" in rjs.text
    # v19.6: init prefills the saved creds on page load (not only on tab click)
    assert "prefill saved PPPoE creds on load" in rjs.text
    # v27.1: the privacy eye hides BOTH PPPoE creds — the username prefill is
    # gated on privacyHide just like the password, so a masked panel shows no
    # username either. v27.2: the gating is two-way — while masked the fields
    # are actively CLEARED (not merely left un-prefilled), so a value revealed
    # then re-hidden vanishes immediately instead of lingering until a refresh.
    assert "user.value = privacyHide ? \"\" : (w.pppoe_user" in rjs.text
    # Sensitive-data hardening: the stored PPPoE password is NEVER prefilled —
    # the field stays empty (blank = keep the stored value) with a
    # presence-aware placeholder; the server masks it as "********" and ships
    # pppoe_has_password so the panel knows a value exists.
    assert "pppoe_has_password" in rjs.text
    assert "pass.value = \"\"" in rjs.text
    assert "leave blank to keep" in rjs.text
    # v28: the last visited sidebar tab is remembered across page reloads —
    # switchPanel persists it, init restores a saved panel only if it still
    # exists in the nav (falls back to the default Management page otherwise).
    assert "quota_active_panel" in rjs.text
    # v19.7: when WAN is configured but ppp0 is down, the panel auto-runs the
    # throwaway PPPoE test and renders an actionable per-failure verdict.
    assert "auto-testing the PPPoE line to find out why" in rjs.text
    assert "renderPppoeVerdict" in rjs.text
    assert "no-pppoe-server" in rjs.text
    assert "NOT bridged" in rjs.text
    # v29.1: when the saved creds already hold a live ppp0 session, the ISP's
    # concurrency control refuses the throwaway test dial — the verdict says so
    # (a FALSE ALARM), instead of "modem/ISP side, the real dial fails too".
    assert "concurrent-session" in rjs.text
    assert "FALSE ALARM" in rjs.text
    assert "maybeAutoDiagnose" in rjs.text
    assert "testPppoe" in rjs.text
    assert "wanToggleDirty" in rjs.text
    assert "/api/wan/test" in rjs.text
    assert "/api/wan" in rjs.text
    assert "data.wan" in rjs.text
    # v24: WAN renewal JS — the manual Restart + the auto-renew save both call
    # their endpoints; renderWan drives the disabled state off ppp0 being up.
    assert "renewWanIp" in rjs.text
    assert "submitWanRenew" in rjs.text
    assert '"/api/wan/renew"' in rjs.text
    assert '"/api/wan/renew-config"' in rjs.text
    assert "renewEnabled" in rjs.text
    assert "fmtRenewLast" in rjs.text
    # When WAN is already active and online, "Apply now" is dimmed —
    # only Test PPPoE connection and Revert to LAN stay active.
    assert "WAN mode is already active and online — nothing to re-apply." in rjs.text
    assert "applyBtn.disabled" in rjs.text
    # the top-bar internet indicator renders from the payload's top-level
    # `internet` key (the WAN-panel Internet row is gone).
    assert "renderNetStatus" in rjs.text
    assert "data.internet" in rjs.text
    assert '"net-status"' in rjs.text
    assert "Checking…" in rjs.text
    assert "wan-internet" not in rjs.text


def test_assets_served(client):
    r = client.get("/assets/styles.css")
    assert r.status_code == 200
    # v22+: obsidian glass theme — backdrop blur + gradient glows, sidebar shell
    assert "backdrop-filter" in r.text
    assert ".sidebar" in r.text
    # v22: user cards reflow as a 2-column masonry (CSS columns) so an expanded
    # accordion card lengthens its column instead of leaving a grid hole.
    # (normalize CRLF so the assertion is line-ending agnostic)
    css = r.text.replace("\r\n", "\n")
    assert "columns: 2;" in css
    # v21: the per-device [user] badge pill on aggregate history recent rows
    assert ".hist-device-badge" in css

    r = client.get("/assets/app.js")
    assert r.status_code == 200
    assert "WebSocket" in r.text
    # v11 speed-shaping JS: Network-tab fetch/save + modal speed prefills
    assert "refreshNetwork" in r.text
    assert "submitNetwork" in r.text
    assert "/api/network" in r.text
    assert "d-limit-down" in r.text
    assert "u-limit-up" in r.text
    assert "speed-tag" in r.text
    # v12 regression: the per-user / per-device speed sections must actually be
    # UNHIDDEN in the modal-open code (u-speed-wrap was stuck `hidden` in v11 —
    # the fields existed in HTML but no JS ever removed the class).
    assert '$("u-speed-wrap").classList.remove("hidden")' in r.text
    assert '$("d-speed-wrap").classList.remove("hidden")' in r.text
    # v14: welcome panel JS — shown on a fresh install, hides on submit/skip
    assert "showWelcomeIfNeeded" in r.text
    assert "/api/setup" in r.text
    assert "setup_complete" in r.text
    assert '$("welcome-overlay").classList.remove("hidden")' in r.text
    # gateway enforcement-status JS: the protected Gateway card renders whether
    # the box's own cut is REALLY in the kernel (gw_blocked set), not just what
    # the block toggle resolves to — so "Blocked in the UI but not cut" shows.
    assert "gatewayEnforceHtml" in r.text
    assert "engine_available" in r.text
    assert "blocked_programmed" in r.text
    assert "gw-enforce" in r.text


def test_history_tab_present(client):
    """v20: the History tab + panel expose the per-device DNS history viewer
    (device picker, look-back window, top-domains/activity/recent panels)."""
    r = client.get("/")
    assert 'data-panel="history"' in r.text
    assert ">History<" in r.text
    assert 'id="panel-history"' in r.text
    assert 'id="hist-device"' in r.text
    assert 'id="hist-window"' in r.text
    assert 'id="hist-refresh"' in r.text
    assert 'id="hist-summary"' in r.text
    assert 'id="hist-top"' in r.text
    assert 'id="hist-activity"' in r.text
    assert 'id="hist-recent"' in r.text
    # v21: household "All devices" aggregate is the default dropdown selection
    assert 'value="all"' in r.text
    assert "All devices" in r.text
    # per-user retention override field in the user modal
    assert 'id="u-history-days"' in r.text
    assert "History retention" in r.text

    rjs = client.get("/assets/app.js")
    assert "refreshHistory" in rjs.text
    assert "renderHistory" in rjs.text
    assert "/api/history/" in rjs.text
    assert "histDevices" in rjs.text
    assert "syncHistoryDeviceSelect" in rjs.text
    assert "bucket_minute" in rjs.text
    # v21: per-device badge on aggregate recent rows + name resolver
    assert "histDeviceName" in rjs.text
    assert "hist-device-badge" in rjs.text


def test_history_assets_bumped(client):
    """Cache-busting tags track the newest UI change. History's "All devices"
    tab took styles 34->37, app.js 31->32; the DNS-filtering tab (rules,
    presets, import, history status badges/quick-actions) took them to
    38/33; the v25 Network & Quota merge took them to 46/45; the v26 Admin
    page (System Logs console embedded) took them to 47/46; the v27 batch
    (privacy eye, decline-random gate, exempt-from-quota UI) took them to
    48/47; the v27.1 PPPoE-username privacy fix took app.js to 48 — this
    always checks the CURRENT baseline, not the original bump."""
    r = client.get("/")
    assert "assets/styles.css?v=57" in r.text
    assert "assets/app.js?v=57" in r.text

