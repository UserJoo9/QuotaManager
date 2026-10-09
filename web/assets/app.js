/* ============================================================
   Quota Manager — dashboard client (vanilla JS)
   Consumes the FastAPI REST API + /ws WebSocket.
   ============================================================ */

"use strict";

/* ---------------- helpers ---------------- */

const $ = (id) => document.getElementById(id);
const fmt = (gb) => `${(+gb).toFixed(2)} GB`;
const fmtBytes = (b) => {
  const n = +b || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/* ---------------- clean SVG icons (replacing emojis) ---------------- */
const ICON_EDIT = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
const ICON_KICK = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>`;
const ICON_BAN = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
const ICON_ZAP = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>`;


/* ---------------- privacy eye ---------------- */
/* Hides on-screen sensitive details — MAC addresses (device rows, rogue rows,
   device modal) and the saved PPPoE credentials prefill (username + password) —
   so the dashboard can be shown without giving away device identities or the
   WAN credentials. The pref lives in localStorage (default: hidden) and
   re-renders in place; only the display is masked, the edit fields keep their
   real values. */

let privacyHide = localStorage.getItem("quota_privacy_hide") !== "0";

function macText(mac) {
  if (!privacyHide) return mac || "";
  const m = String(mac || "");
  const parts = m.split(/[:\-]/);
  if (parts.length !== 6) return m;
  return `${parts[0]}:${parts[1]}:${parts[2]}:••:••:••`;
}

function setPrivacyButton() {
  const btn = $("privacy-eye");
  if (!btn) return;
  btn.classList.toggle("off", !privacyHide);
  btn.setAttribute("aria-label",
    privacyHide ? "Show sensitive details (MACs, PPPoE credentials)" : "Hide sensitive details");
  btn.title = privacyHide
    ? "Showing masked MACs + hidden PPPoE credentials — click to reveal"
    : "Hide MAC addresses + PPPoE credentials";
}

function togglePrivacy() {
  privacyHide = !privacyHide;
  localStorage.setItem("quota_privacy_hide", privacyHide ? "1" : "0");
  setPrivacyButton();
  // re-render in place: device rows + rogue rows pick up the mask via macText()
  renderUsers(dashboard.users, dashboard.devices, dashboard.gateway);
  renderRogue(dashboard.rogue);
  // the device modal's MAC line — refresh it if it is open
  if (!$("modal").classList.contains("hidden") && editDeviceId != null) {
    openDeviceModal(editDeviceId);
  }
  // the WAN panel prefill (credentials) — re-prefetch so the user/pass fields
  // clear/set when masking flips
  if (!$("panel-wan") || !$("panel-wan").classList.contains("hidden")) refreshWan();
}

/* ---------------- API client ---------------- */

const API = {
  async req(method, path, body) {
    const opts = {
      method,
      // X-QM-CSRF: every state-changing request must carry the custom header
      // (CSRF defense-in-depth — a cross-site form can't set one).
      headers: { "Content-Type": "application/json", "X-QM-CSRF": "1" },
      credentials: "same-origin",
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (res.status === 401 && path !== "/api/login") {
      showLogin();
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const msg = (data && data.detail) || `HTTP ${res.status}`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  },
  get: (p) => API.req("GET", p),
  post: (p, b) => API.req("POST", p, b),
  patch: (p, b) => API.req("PATCH", p, b),
  del: (p) => API.req("DELETE", p),
  delete: (p) => API.req("DELETE", p),
};

/* ---------------- state ---------------- */

let dashboard = null;      // latest snapshot payload
let wanStatus = null;      // latest /api/wan payload (WAN tab status)
let wanToggleDirty = false; // toggle flipped but not yet applied — freeze WS renders
let pppoeAutoRan = false;  // v19.7: the auto PPPoE diagnosis already ran (once per page load)
let editDeviceId = null;   // device being edited (null = add mode)
let settingsDirty = false; // admin typed in the bundle form — freeze its sync
let expandedUsers = new Set(); // user ids whose device accordion is open
let networkConfig = null;  // latest /api/network payload (Network preview)
let shapingLive = null;    // latest snapshot's shaping engine state {available, applied}
let macListsDirty = false; // admin typed in the MAC-lists textareas — freeze their sync
let logLines = [];         // raw lines from /api/logs
let logMeta = null;        // {total, truncated} from the last /api/logs call
let logFilter = "ALL";     // active log level filter
let logSearch = "";        // current log search string
let firewallState = null;  // latest /api/firewall payload (Firewall tab)

/* ---------------- screens ---------------- */

function showLogin() {
  $("app").classList.add("hidden");
  $("login-screen").classList.remove("hidden");
  wsClose();
}

function showApp() {
  $("login-screen").classList.add("hidden");
  $("app").classList.remove("hidden");
}

/* ---------------- rendering ---------------- */

function render(data) {
  dashboard = data;
  shapingLive = data.shaping || null;
  renderBundle(data.bundle, data.devices, data.users);
  renderUsers(data.users, data.devices, data.gateway);
  renderRogue(data.rogue);
  renderWan(data.wan); // null-safe — {} before the first Gateway tick
  renderNetStatus(data.internet);
  renderNetworkPreview(networkConfig); // null-safe — refreshed by refreshNetwork()
  renderVpnShare(data); // live status rides the WS snapshot so "applying…" advances
  if (data.vpn) renderVpnStatus(data.vpn);
  renderUpdate(data.update); // null-safe — no updater wired (tests / degraded boot)
  renderSecurity(data.security); // auth-hardening alerting banner
  const v = $("app-version");
  if (v) v.textContent = data.version ? `Quota Manager ${data.version}` : "—";
}

/* ---------------- security alerting banner ---------------- */

function renderSecurity(sec) {
  /* ---- notification center ---- */
  _pushNotifs(sec);

  const el = $("security-banner");
  if (!el || !sec) return;
  const msgs = [];
  if (sec.default_password) {
    msgs.push("Default admin password still active — change it in Settings (Strong WAN mode is blocked until you do).");
  }
  if (sec.wan_http) {
    msgs.push("WAN mode is running over plain HTTP — credentials travel unencrypted. Configure web.tls_* for HTTPS (or set secure_cookies).");
  }
  if (sec.failed_logins_1h > 0) {
    msgs.push(`${sec.failed_logins_1h} failed login attempt${sec.failed_logins_1h === 1 ? "" : "s"} blocked in the last hour.`);
  }
  if (sec.waf_blocks_1h > 0) {
    msgs.push(`${sec.waf_blocks_1h} request${sec.waf_blocks_1h === 1 ? "" : "s"} blocked by the WAF in the last hour.`);
  }
  if (msgs.length) {
    const text = "⚠ " + msgs.join("  ");
    // Dismissed until the warning text changes (a new condition or count).
    if (localStorage.getItem("quota_sec_banner_dismissed") === text) {
      el.classList.add("hidden");
      return;
    }
    $("security-banner-text").textContent = text;
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

/* ---------------- notification center ---------------- */

const _notifSeenKey = "quota_notifs_seen";
const _notifBuf = [];

function _upsertNotif(id, type, msg) {
  const existing = _notifBuf.find((n) => n.id === id);
  if (existing) {
    if (existing.msg !== msg) {
      existing.msg = msg;
      existing.time = Date.now();
      try {
        const seen = JSON.parse(localStorage.getItem(_notifSeenKey) || "[]");
        const idx = seen.indexOf(id);
        if (idx !== -1) {
          seen.splice(idx, 1);
          localStorage.setItem(_notifSeenKey, JSON.stringify(seen));
        }
      } catch (e) {}
    }
  } else {
    _notifBuf.push({ id, type, time: Date.now(), msg });
  }
}

function _removeNotif(id) {
  const idx = _notifBuf.findIndex((n) => n.id === id);
  if (idx !== -1) {
    _notifBuf.splice(idx, 1);
  }
  try {
    const seen = JSON.parse(localStorage.getItem(_notifSeenKey) || "[]");
    const sIdx = seen.indexOf(id);
    if (sIdx !== -1) {
      seen.splice(sIdx, 1);
      localStorage.setItem(_notifSeenKey, JSON.stringify(seen));
    }
  } catch (e) {}
}

function _pushNotifs(sec) {
  if (!sec) return;

  if (sec.failed_logins_1h > 0) {
    _upsertNotif("logins", "danger",
      `${sec.failed_logins_1h} failed login attempt${sec.failed_logins_1h === 1 ? "" : "s"} in the last hour.`);
  } else {
    _removeNotif("logins");
  }

  if (sec.waf_blocks_1h > 0) {
    _upsertNotif("waf", "warn",
      `${sec.waf_blocks_1h} request${sec.waf_blocks_1h === 1 ? "" : "s"} blocked by the WAF in the last hour.`);
  } else {
    _removeNotif("waf");
  }

  if (sec.default_password) {
    _upsertNotif("default_pw", "danger",
      "Default admin password is still active.");
  } else {
    _removeNotif("default_pw");
  }

  if (sec.wan_http) {
    _upsertNotif("wan_http", "warn",
      "WAN mode running over plain HTTP — credentials unencrypted.");
  } else {
    _removeNotif("wan_http");
  }

  _renderNotifDropdown();
}

function _renderNotifDropdown() {
  const badge = $("notif-badge");
  const list = $("notif-list");
  if (!badge || !list) return;

  let seen = [];
  try {
    seen = JSON.parse(localStorage.getItem(_notifSeenKey) || "[]");
  } catch (e) {
    seen = [];
  }

  const activeNotifs = _notifBuf.filter((n) => !seen.includes(n.id));

  if (activeNotifs.length) {
    badge.textContent = activeNotifs.length > 9 ? "9+" : String(activeNotifs.length);
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }

  if (!activeNotifs.length) {
    list.innerHTML = `<p class="muted small notif-empty">No notifications.</p>`;
    return;
  }

  list.innerHTML = activeNotifs.map((n) => {
    const t = new Date(n.time);
    const ts = t.getHours().toString().padStart(2, "0") + ":" + t.getMinutes().toString().padStart(2, "0");
    return `<div class="notif-item ${esc(n.type)}">
      <span class="notif-time">${ts}</span>
      <span class="notif-msg">${esc(n.msg)}</span>
    </div>`;
  }).join("");
}

function _notifDismissAll() {
  const seen = _notifBuf.map((n) => n.id);
  localStorage.setItem(_notifSeenKey, JSON.stringify(seen));
  const badge = $("notif-badge");
  if (badge) badge.classList.add("hidden");
  const dropdown = $("notif-dropdown");
  if (dropdown) dropdown.classList.add("hidden");
  _renderNotifDropdown();
}

/* ---------------- software updates (Admin tab + notification) ---------------- */

//: the update version the notification banner has already announced (or the
//: admin dismissed). Persisted per version so a reload doesn't re-announce
//: the same release, but a NEWER one still notifies.
function updateBannerDismissedFor() {
  return localStorage.getItem("quota_update_banner") || "";
}

function renderUpdate(u) {
  const card = $("upd-current");
  if (!card) return;  // update card absent — nothing to render
  const checking = u && u.checking;
  const off = !u || !u.enabled;
  $("upd-current").textContent = u && u.current_version ? `v${u.current_version}` : "—";
  // while the toggle is OFF, hide the STALE last-check/latest results — they
  // describe an old run and only confuse; the status line tells the admin to
  // enable checks instead
  $("upd-latest").textContent =
    off ? "—" : (u && u.latest_version ? `v${u.latest_version}` : (checking ? "checking…" : "—"));
  $("upd-checked").textContent =
    off ? "—" : (u && u.checked_at ? new Date(u.checked_at).toLocaleString() : "never");
  const statusEl = $("upd-status");
  let status = "—";
  if (off) status = "Checks are OFF — toggle ON to check for updates";
  else if (checking) status = "Checking…";
  else if (u && u.error) {
    // the raw exception (e.g. "<urlopen error timed out>") is kept in the
    // muted detail line below + the hover title — the status row stays
    // human and points at the cause
    const timeout = /timeout|timed ?out/i.test(u.error);
    status = timeout
      ? "Couldn't reach GitHub (timed out) — check the box's internet; retries automatically"
      : "Update check failed — retrying automatically";
    statusEl.title = u.error;
  } else if (u && u.available) status = "Update available";
  else if (u && u.latest_version) status = "Up to date";
  statusEl.textContent = status;
  const errEl = $("upd-error");
  if (errEl) {
    errEl.textContent = u && u.error ? u.error : "";
    errEl.classList.toggle("hidden", !(u && u.error) || off);
  }
  $("upd-check").disabled = !!checking || off;
  $("upd-install").classList.toggle("hidden", !(u && u.available) || off);
  $("upd-install").textContent = u && u.available ? `Install v${u.latest_version}` : "Install";
  $("upd-details").classList.toggle("hidden", !(u && u.available && (u.changelog || []).length) || off);
  // the toggles are set without firing the change handlers (those POST on change)
  $("upd-enabled").checked = u ? !!u.enabled : true;
  $("upd-auto").checked = u ? !!u.auto_install : false;

  // "update available" notification: show once per version (or until dismissed)
  const banner = $("update-banner");
  if (banner) {
    if (u && u.available && u.latest_version !== updateBannerDismissedFor()) {
      $("update-banner-sub").textContent = `v${u.current_version} → v${u.latest_version}`;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }
}

async function refreshUpdates() {
  let u;
  try {
    u = await API.get("/api/updates");
  } catch (_) {
    return;  // updater not wired (tests) — card stays blank
  }
  renderUpdate(u);
}

function openChangelog() {
  const u = dashboard && dashboard.update;
  const body = $("changelog-body");
  if (body && u) {
    const list = (u.changelog || []).map((c) =>
      `<h4>${esc(c.title)}</h4><p>${esc(c.body)}</p>`).join("");
    body.innerHTML = list ||
      `<p class="muted">No changelog available for the new version.</p>`;
    $("changelog-sub").textContent =
      `New versions since v${u.current_version} (${(u.changelog || []).length})`;
  }
  $("changelog-modal").classList.remove("hidden");
}

function updateResetDayAvailability() {
  // end-of-month bills still take a day (many ISPs close the month on the
  // 25th/28th); only the 0-hint differs from renew-day mode.
  const eom = $("set-period-type").value === "end_of_month";
  const hint = $("reset-day-hint");
  if (hint) hint.textContent = eom ? "(0 = calendar end)" : "(0 = manual)";
  const setupHint = $("setup-reset-day-hint");
  if (setupHint) setupHint.textContent =
    $("setup-period-type").value === "end_of_month" ? "(0 = calendar end)" : "(0 = never)";
}

let lastBundleState = null;

function renderBundle(b, devices, users) {
  lastBundleState = { b, devices, users };
  const usedPct = b.total_gb > 0 ? Math.min(100, (b.used_gb / b.total_gb) * 100) : 0;
  const ringEl = $("bundle-ring");
  if (ringEl) {
    ringEl.style.setProperty("--p", usedPct.toFixed(1));

    // Half-circle arc: radius=64, arc length = π×64 ≈ 201.1
    // dashoffset=201.1 → empty; dashoffset=0 → full
    const totalArcLen = 201.1;
    const offset = Math.max(0, totalArcLen - (usedPct / 100) * totalArcLen);
    const fillPath = $("gauge-svg-fill");
    if (fillPath) {
      fillPath.style.strokeDashoffset = offset.toFixed(1);
      if (usedPct >= 80) {
        fillPath.setAttribute("stroke", "url(#gauge-redline-grad)");
        fillPath.style.filter = "drop-shadow(0 0 8px rgba(244,63,94,0.85))";
      } else {
        fillPath.setAttribute("stroke", "url(#gauge-fill-grad)");
        fillPath.style.filter = "drop-shadow(0 0 5px rgba(56,189,248,0.7))";
      }
    }

    // Needle: pivot at (100,100); -90° = 0%, +90° = 100%
    const needleDeg = -90 + (usedPct * 1.8);
    const needleGroup = $("gauge-svg-needle-group");
    if (needleGroup) {
      needleGroup.style.transform = `rotate(${needleDeg.toFixed(1)}deg)`;
    }

    ringEl.classList.toggle("redline", usedPct >= 80);
  }
  $("bundle-used").textContent = fmt(b.used_gb);
  $("bundle-total").textContent = b.total_gb;
  $("bundle-remaining").textContent = fmt(b.remaining_gb);
  // reset_day=0 => period never rolls: show "→ manual" and "—" for days left.
  $("bundle-period").textContent =
    b.period_end ? `${b.period_start || "…"} → ${b.period_end}` : `${b.period_start || "…"} → manual`;
  $("bundle-days").textContent = b.days_left < 0 ? "—" : b.days_left;
  $("bundle-users").textContent = (users || []).length;
  $("bundle-devices").textContent = devices.length;
  $("bundle-blocked").textContent = devices.filter((d) => d.blocked).length;

  renderRecharges(b.recharges, users, devices);
  updateRechargeTargetOptions();

  // keep settings form in sync — but NEVER clobber an input the admin is
  // editing. A WS snapshot arrives every 5 s; the old per-field focus guard
  // only protected the ONE field that had focus, so typing the bundle size and
  // then moving to the reset-day field let the next snapshot revert it. Freeze
  // the whole settings section once the admin touches either field, and only
  // unfreeze after a successful save.
  if (!settingsDirty) {
    $("set-total").value = b.total_gb;
    $("set-period-type").value = b.period_type || "renew_day";
    $("set-reset-day").value = b.reset_day;
  }
  // end-of-month bills still take a day (many ISPs close the month on the
  // 25th/28th); only the 0-hint differs from renew-day mode.
  updateResetDayAvailability();

  // bundle ownership banner: config.yaml owns the bundle until the admin
  // edits it once in the dashboard (then the dashboard owns it and edits
  // survive restarts). Make that state visible so an edit isn't lost.
  const banner = $("bundle-source-banner");
  if (banner) {
    if (b.bundle_source === "config") {
      banner.textContent = "Bundle is set from config.yaml — edit it here once to take over (your change then survives restarts).";
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }

  renderBundlePreview(b, usedPct);
}

function renderBundlePreview(b, usedPct) {
  if (!$("bundle-preview-remaining")) return;
  $("bundle-preview-remaining").textContent = fmt(b.remaining_gb);
  $("bundle-preview-used").textContent = fmt(b.used_gb);
  $("bundle-preview-period").textContent =
    b.period_end ? `${b.period_start || "…"} → ${b.period_end}` : `${b.period_start || "…"} → manual`;
  $("bundle-preview-days").textContent = b.days_left < 0 ? "—" : b.days_left;
  $("bundle-preview-used-num").textContent = `${fmt(b.used_gb)} used`;
  $("bundle-preview-pct").textContent = `${usedPct.toFixed(1)}%`;
  $("bundle-preview-fill").style.width = `${usedPct.toFixed(1)}%`;
}

/* status dot replaces the old "ACTIVE" text badge: green online, gray offline,
   amber quota-exceeded, red admin-blocked. Blocked states keep a small text tag
   in addition to the dot (see statusTag). */
function statusDot(state, connected) {
  let cls = "dot ok", label = "Online";
  if (state === "quota") { cls = "dot quota"; label = "Quota exceeded"; }
  else if (state === "admin_off") { cls = "dot admin_off"; label = "Blocked by admin"; }
  else if (!connected) { cls = "dot off"; label = "Offline"; }
  return `<span class="${cls}" title="${label}" aria-label="${label}"></span>`;
}

function statusTag(state) {
  if (state === "quota") return `<span class="status-tag quota">Quota</span>`;
  if (state === "admin_off") return `<span class="status-tag admin_off">Blocked</span>`;
  return "";
}

function renderUsers(users, devices, gw) {
  const list = $("devices-list");
  if ((!users || !users.length) && (!devices || !devices.length)) {
    list.innerHTML = `<div class="empty">No users yet. Add a user or a device — a
      device that asks the router for an IP will appear here automatically.</div>`;
    return;
  }
  const byUser = new Map();
  for (const d of devices || []) {
    const k = d.user_id;
    if (!byUser.has(k)) byUser.set(k, []);
    byUser.get(k).push(d);
  }

  // Ordering: 1. Gateway (protected) always first -> 2. Registered users -> 3. Guests always last
  const sortedUsers = [...(users || [])].sort((a, b) => {
    const isGwA = !!(a.protected || a.name === "Gateway");
    const isGwB = !!(b.protected || b.name === "Gateway");
    if (isGwA && !isGwB) return -1;
    if (!isGwA && isGwB) return 1;

    const isGuestA = !!a.guest;
    const isGuestB = !!b.guest;
    if (!isGuestA && isGuestB) return -1;
    if (isGuestA && !isGuestB) return 1;

    // Stable sort within group: preserve existing order or by id / name
    return (a.id || 0) - (b.id || 0);
  });

  const parts = [];
  for (const u of sortedUsers) {
    parts.push(userCard(u, byUser.get(u.id) || [], gw));
  }
  // orphan devices (no user) — should not happen post-migration, but render
  // them so they stay controllable if the DB is mid-migration.
  const orphan = byUser.get(null) || byUser.get(undefined) || [];
  if (orphan.length) {
    parts.push(userCard(
      { id: null, name: "Unassigned devices", quota_mode: "auto",
        allowance_gb: 0, used_gb: 0, percent: 0, blocked: false,
        block_state: "ok" },
      orphan, gw, true));
  }
  list.innerHTML = parts.join("");
}

/* Unmanaged / rogue devices: active hosts that are NOT leased by the quota
   DHCP. A static-IP device with the router as its gateway bypasses the box
   entirely (never counted, never blocked), so seeing it here is the first
   step to shutting it down — with the ARP gateway-lock on, its internet is
   cut automatically. */
function renderRogue(rogue) {
  const section = $("rogue-section");
  const list = $("rogue-list");
  if (!section || !list) return;
  rogue = rogue || [];
  if (!rogue.length) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");
  $("rogue-count").textContent = `${rogue.length} found`;
  list.innerHTML = rogue.map((r) => {
    const vendorTag = r.vendor && r.vendor !== "Unknown" ? esc(r.vendor) : "";
    return `<div class="rogue-row">
      <span class="${r.online ? "dot ok" : "dot off"}"
            title="${r.online ? "Online now" : "Not seen in the last scan"}"
            aria-label="${r.online ? "Online" : "Offline"}"></span>
      <div class="rogue-meta">
        <div class="rogue-ip">${esc(r.ip)}<span class="muted small"> · not in DHCP</span></div>
        <div class="rogue-mac">${esc(macText(r.mac))}${vendorTag ? ` · <span class="device-vendor">${vendorTag}</span>` : ""}</div>
      </div>
    </div>`;
  }).join("");
}

/* The box's own enforcement: the Gateway card shows whether the UI toggle and
   the kernel agree. "Blocked" in the dashboard is the resolved DB state; the
   actual cut lives in the engine's gw_blocked set (blocked_programmed). When
   they diverge — a stale engine, a failed set program, or the engine off —
   the box would still reach the internet while the card says Blocked. Make
   that visible instead of silent. */
function gatewayEnforceHtml(gw) {
  if (!gw) return "";
  if (!gw.engine_available) {
    return `<div class="gw-enforce warn" title="The packet engine is not running (config engine.enabled=false, or it failed to start) — no device is counted or blocked right now.">⚠ Packet engine is off — nothing is being counted or blocked</div>`;
  }
  if (gw.blocked_desired && !gw.blocked_programmed) {
    return `<div class="gw-enforce warn" title="The dashboard says Blocked, but the kernel's gw_blocked set does not contain 0.0.0.0/0 — the box can still reach the internet. Check the gateway journal / nft list set inet quota_gateway gw_blocked.">⚠ Blocked in the UI but NOT cut at the kernel — the box can still reach the internet</div>`;
  }
  if (gw.blocked_desired && gw.blocked_programmed) {
    return `<div class="gw-enforce ok" title="The kernel's gw_blocked set holds 0.0.0.0/0 — the box's own internet is dropped at the input/output hooks.">✓ Box internet is cut at the kernel</div>`;
  }
  return "";
}

function userCard(u, udevs, gw, ghost) {
  ghost = ghost || u.id == null;
  const key = u.id == null ? "orphan" : String(u.id);
  const open = expandedUsers.has(key);
  const connected = (udevs || []).some((d) => d.connected);
  // The protected Gateway user is permanent: the admin cuts the box's own
  // internet with the block toggle + edit, but it can never be deleted.
  const delBtn = u.protected ? "" : `
      <button class="icon-btn danger" data-ua="delete" data-uid="${u.id}" title="Kick / disconnect user (5s timeout, no ban)">${ICON_KICK}</button>
      <button class="icon-btn danger" data-ua="block-blacklist" data-uid="${u.id}" title="Block & blacklist user">${ICON_BAN}</button>`;
  const guestActions = u.guest ? (
    u.blocked ? `
      <button class="btn ok small" data-ua="guest-accept" data-uid="${u.id}" title="Accept guest — unblock and grant quota">Accept</button>
      <button class="btn danger small" data-ua="guest-reject" data-uid="${u.id}" title="Reject guest — keep blocked">Reject</button>` : `
      <button class="btn danger small" data-ua="guest-reject" data-uid="${u.id}" title="Cut / block guest access">Reject</button>`
  ) : "";
  const actions = ghost ? "" : `
      ${guestActions}
      <label class="switch" title="Cut / restore all of this user's devices">
        <input type="checkbox" class="toggle-user" data-uid="${u.id}" ${u.blocked ? "" : "checked"}>
        <span class="slider"></span>
      </label>
      <button class="icon-btn" data-ua="edit" data-uid="${u.id}" title="Edit user">${ICON_EDIT}</button>
      ${delBtn}`;
  const devHtml = udevs.map(deviceRow).join("");
  const guestTag = u.guest
    ? (u.blocked
        ? ` <span class="guest-tag" title="Guest account — pending approval">guest (pending)</span>`
        : ` <span class="status-tag ok" title="Guest account — approved">guest (approved)</span>`)
    : "";
  // the box itself: its own internet consumption is charged here, and it is
  // permanent (edit + block work, delete does not)
  const gatewayTag = u.protected
    ? ` <span class="gateway-tag" title="The gateway box itself — its own internet is charged to this user. Cannot be deleted.">gateway</span>` : "";
  // per-user aggregate speed caps (Mbps; shown only when one is set)
  const speedTag = (u.limit_down_mbps || u.limit_up_mbps)
    ? ` <span class="speed-tag" title="Total speed for all this user's devices">↓${u.limit_down_mbps || "∞"} ↑${u.limit_up_mbps || "∞"}</span>` : "";
  // quota-exempt users are never quota-blocked (manual admin cuts still apply)
  const exemptTag = u.exempt_quota
    ? ` <span class="bypass-tag" title="Exempt from quota — never quota-blocked, however much they use">unlimited</span>` : "";
  return `
  <div class="glass card user-card ${u.blocked ? "blocked" : ""}">
    <div class="user-head">
      <button class="accordion-toggle ${open ? "open" : ""}" data-acc="${key}"
              aria-expanded="${open}" title="Show/hide this user's devices">
        <svg class="chevron" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <div class="user-head-info">
        <div class="user-name">${statusDot(u.block_state, connected)}${esc(u.name || (u.guest ? "Guest" : "Unnamed user"))}${guestTag}${gatewayTag}${exemptTag}${speedTag}${statusTag(u.block_state)}</div>
        <div class="user-sub">${udevs.length} device${udevs.length === 1 ? "" : "s"} ·
          ${u.quota_mode === "fixed" ? `Fixed ${u.fixed_gb ?? u.allowance_gb} GB`
            : u.quota_mode === "disabled" ? "Disabled — assign quota"
            : "Auto (share of remainder)"}</div>
      </div>
    </div>
    <div class="device-bar">
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, u.percent || 0)}%"></div></div>
      <div class="device-numbers">
        <span>${fmt(u.used_gb)} of <b>${fmt(u.allowance_gb)}</b></span>
        <span>${(u.percent || 0).toFixed(1)}%</span>
      </div>
    </div>
    ${u.protected ? gatewayEnforceHtml(gw) : ""}
    <div class="device-actions actions">${actions}</div>
    <div class="user-devices ${open ? "" : "hidden"}">
      ${devHtml || `<div class="empty small">No devices yet.</div>`}
    </div>
  </div>`;
}

function deviceRow(d) {
  const bypassTag = d.bypass
    ? `<span class="bypass-tag" title="Exempt from this user's quota block">bypass</span>` : "";
  const vpnBypassTag = "";
  // per-device internet speed caps (Mbps; shown only when one is set)
  const speedTag = (d.limit_down_mbps || d.limit_up_mbps)
    ? ` <span class="speed-tag" title="This device's speed limit">↓${d.limit_down_mbps || "∞"} ↑${d.limit_up_mbps || "∞"}</span>` : "";
  // guests are auto-created period-scoped accounts (deleted on month reset)
  const guestTag = d.guest
    ? (d.blocked
        ? `<span class="guest-tag" title="Guest device — pending approval">guest (pending)</span>`
        : `<span class="status-tag ok" title="Guest device — approved">guest (approved)</span>`)
    : "";
  // the box's own device — controlled from its user card (no per-device block
  // toggle, no delete; edit/top-up stays)
  const gatewayTag = d.gateway
    ? `<span class="gateway-tag" title="The gateway box itself — controlled from its user card">gateway</span>` : "";
  // vendor: fallback title for unnamed devices, small tag next to the MAC otherwise
  const vendorTag = (d.name && d.vendor)
    ? ` · <span class="device-vendor">${esc(d.vendor)}</span>` : "";
  // per-device consumption monitor: THIS device's share of the user's allowance
  // (device_used_gb is the device's own period usage — the user card bar above
  // shows the aggregate). device_percent is capped at 100 so the bar fills.
  const devPct = Math.min(100, d.device_percent || 0);
  const devBar = `
    <div class="device-bar" title="This device's consumption this period — share of the user's ${fmt(d.allowance_gb)} allowance">
      <div class="bar-track"><div class="bar-fill" style="width:${devPct}%"></div></div>
      <div class="device-numbers">
        <span>↓ ${fmt(d.device_down_gb)} · ↑ ${fmt(d.device_up_gb)}</span>
        <span>${fmt(d.device_used_gb)} of <b>${fmt(d.allowance_gb)}</b> · ${(d.device_percent || 0).toFixed(1)}%</span>
      </div>
    </div>`;
  // The box's own device is controlled via its user card: no per-device block
  // toggle (that would double up with the Gateway user's toggle) and no delete.
  // Edit stays — rename/top-up/caps are still allowed.
  const blockSwitch = d.gateway ? "" : `
      <label class="switch" title="Toggle internet access">
        <input type="checkbox" class="toggle-block" data-id="${d.id}" ${d.blocked ? "" : "checked"}>
        <span class="slider"></span>
      </label>`;
  const guestDevActions = d.guest ? (
    d.blocked ? `
      <button class="btn ok small" data-act="guest-accept" data-id="${d.id}" title="Accept guest — unblock and grant quota">Accept</button>
      <button class="btn danger small" data-act="guest-reject" data-id="${d.id}" title="Reject guest — keep blocked">Reject</button>` : `
      <button class="btn danger small" data-act="guest-reject" data-id="${d.id}" title="Cut / block guest access">Reject</button>`
  ) : "";
  const deleteBtn = d.gateway ? "" : `
      <button class="icon-btn danger" data-act="delete" data-id="${d.id}" title="Kick / disconnect device (5s timeout, no ban)">${ICON_KICK}</button>
      <button class="icon-btn danger" data-act="block-blacklist" data-id="${d.id}" title="Block & blacklist device">${ICON_BAN}</button>`;
  return `
  <div class="device-row ${d.blocked ? "blocked" : ""}" data-id="${d.id}">
    <div class="device-head">
      <div>
        <div class="device-name">${esc(d.name || (d.guest ? "Guest" : d.vendor) || "Unnamed device")}${speedTag}</div>
        <div class="device-mac">${esc(macText(d.mac))}${statusDot(d.block_state, d.connected)}${d.ip ? ` · <span class="device-ip">${esc(d.ip)}</span>` : ""}${vendorTag}${guestTag}${gatewayTag}${bypassTag}${vpnBypassTag}${statusTag(d.block_state)}</div>
      </div>
    </div>
    ${devBar}
    <div class="device-live">
      <span class="live-down">Download <b>${fmtBytes(d.live_down)}</b></span>
      <span class="live-up">Upload <b>${fmtBytes(d.live_up)}</b></span>
    </div>
    <div class="device-actions actions">
      ${guestDevActions}
      ${blockSwitch}
      <button class="icon-btn" data-act="edit" data-id="${d.id}" title="Edit / top up">${ICON_EDIT}</button>
      ${deleteBtn}
    </div>
  </div>`;
}

/* ---------------- sidebar panels (management / network / wan / admin / logs) ---------------- */

const PANEL_LOADING_INFO = {
  management: { title: "Loading Management...", sub: "Synchronizing devices and quota status..." },
  network: { title: "Loading Network...", sub: "Inspecting interfaces, leases, and client rules..." },
  wan: { title: "Loading WAN...", sub: "Checking uplink, PPPoE status, and traffic..." },
  admin: { title: "Loading Admin...", sub: "Fetching system logs and router settings..." },
  history: { title: "Loading History...", sub: "Loading consumption timelines and records..." },
  dns: { title: "Loading DNS...", sub: "Fetching blocklists, custom records, and query rules..." },
  firewall: { title: "Loading Firewall...", sub: "Inspecting firewall rules and port forwardings..." },
  vpn: { title: "Loading VPN...", sub: "Verifying tunnel state and proxy endpoints..." },
};

let activePanelLoadSeq = 0;
let panelLoadHideTimeout = null;

function showPanelPopup(name) {
  const popup = $("panel-loading-popup");
  if (!popup) return;
  const titleEl = $("panel-loading-title");
  const subEl = $("panel-loading-sub");
  const info = PANEL_LOADING_INFO[name] || {
    title: `Loading ${name ? name.charAt(0).toUpperCase() + name.slice(1) : "Section"}...`,
    sub: "Fetching latest system state...",
  };
  if (titleEl) titleEl.textContent = info.title;
  if (subEl) subEl.textContent = info.sub;
  if (panelLoadHideTimeout) {
    clearTimeout(panelLoadHideTimeout);
    panelLoadHideTimeout = null;
  }
  popup.classList.remove("hidden");
  void popup.offsetWidth;
  popup.classList.add("active");
}

function hidePanelPopup() {
  const popup = $("panel-loading-popup");
  if (!popup) return;
  popup.classList.remove("active");
  if (panelLoadHideTimeout) clearTimeout(panelLoadHideTimeout);
  panelLoadHideTimeout = setTimeout(() => {
    popup.classList.add("hidden");
  }, 220);
}

function updateNavIndicator(panelName) {
  const indicator = $("nav-indicator");
  if (!indicator) return;
  const activeTab = document.querySelector(`.nav-tab[data-panel="${panelName}"]`) || document.querySelector(".nav-tab.active");
  if (!activeTab) {
    indicator.style.opacity = "0";
    return;
  }
  const top = activeTab.offsetTop;
  const height = activeTab.offsetHeight;
  indicator.style.transform = `translateY(${top}px)`;
  indicator.style.height = `${height}px`;
  indicator.style.opacity = "1";
}

const loadedPanels = new Set();

function isPanelContentLoaded(name) {
  if (loadedPanels.has(name)) return true;
  if (name === "management") {
    return !!(dashboard && dashboard.bundle);
  }
  if (name === "network") {
    return !!(dashboard && dashboard.network);
  }
  if (name === "wan") {
    return !!wanStatus;
  }
  if (name === "firewall") {
    return !!fwStatus;
  }
  if (name === "history") {
    return !!historyCache;
  }
  if (name === "dns") {
    return Array.isArray(dnsPresetsCache) && dnsPresetsCache.length > 0;
  }
  if (name === "vpn") {
    return !!vpnStatusCache;
  }
  if (name === "admin") {
    return Array.isArray(logLines) && logLines.length > 0;
  }
  return false;
}

async function switchPanel(name) {
  try { localStorage.setItem("quota_active_panel", name); } catch (_) { /* ignore */ }
  document.querySelectorAll(".nav-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.panel === name));
  updateNavIndicator(name);
  document.querySelectorAll(".nav-panel").forEach((p) =>
    p.classList.toggle("hidden", p.id !== `panel-${name}`));

  const isLoaded = isPanelContentLoaded(name);
  const seq = ++activePanelLoadSeq;
  let startTime = 0;

  // Only display the loading popup if the panel has not loaded its content yet
  if (!isLoaded) {
    showPanelPopup(name);
    startTime = Date.now();
  }

  try {
    if (name === "admin") {
      await refreshLogs();
    } else if (name === "network") {
      await Promise.allSettled([refreshNetwork(), refreshGuest()]);
    } else if (name === "wan") {
      await refreshWan();
    } else if (name === "firewall") {
      await refreshFirewall();
    } else if (name === "history") {
      await refreshHistory();
    } else if (name === "dns") {
      await refreshDns();
    } else if (name === "vpn") {
      await refreshVpn(!isLoaded);
      startVpnLogPolling();
    } else if (name === "management") {
      await refreshAll();
    }
    loadedPanels.add(name);
  } catch (err) {
    console.warn("Panel refresh notice:", err);
  } finally {
    if (name !== "vpn") {
      stopVpnLogPolling();
    }
    if (!isLoaded) {
      const elapsed = Date.now() - startTime;
      const remaining = Math.max(0, 240 - elapsed);
      setTimeout(() => {
        if (seq === activePanelLoadSeq) {
          hidePanelPopup();
        }
      }, remaining);
    } else {
      hidePanelPopup();
    }
  }
}

async function refreshLogs() {
  try {
    const data = await API.get("/api/logs?limit=300");
    logLines = data.lines;
    logMeta = data;
    renderLogs();
  } catch (_) { /* not critical — activity still works */ }
}

/* ---------------- firewall ---------------- */

function fwRuleStr(r) {
  const parts = [];
  if (r.src && r.src !== "0.0.0.0/0") parts.push("src " + r.src);
  if (r.dst && r.dst !== "0.0.0.0/0") parts.push("dst " + r.dst);
  if (r.protocol) parts.push(r.protocol);
  if (r.src_port) parts.push("sport " + r.src_port);
  if (r.dst_port) parts.push("dport " + r.dst_port);
  return parts.join(" ") || "any";
}

function fwRuleRow(r, i) {
  const cls = r.action === "allow" ? "fw-rule allow" : "fw-rule deny";
  const badge = r.action === "allow" ? "ALLOW" : "DENY";
  return `
    <div class="${cls}">
      <div class="fw-rule-main">
        <span class="fw-rule-badge ${r.action}">${badge}</span>
        <span class="fw-rule-text">
          <strong>${esc(r.name || "rule " + (i + 1))}</strong>
          <span class="muted small">${esc(r.chain)} · ${esc(fwRuleStr(r))}</span>
        </span>
      </div>
      <div class="fw-rule-actions">
        <button type="button" class="btn ghost tiny" data-fw-act="edit-rule" data-idx="${i}">Edit</button>
        <button type="button" class="btn ghost tiny danger" data-fw-act="del-rule" data-idx="${i}">×</button>
      </div>
    </div>`;
}

function fwServiceRow(s, i) {
  return `
    <div class="fw-rule allow">
      <div class="fw-rule-main">
        <span class="fw-rule-badge allow">OPEN</span>
        <span class="fw-rule-text">
          <strong>${esc(s.name || "service " + (i + 1))}</strong>
          <span class="muted small">${esc(s.protocol)}/${s.port}${s.source && s.source !== "0.0.0.0/0" ? " · from " + esc(s.source) : ""}</span>
        </span>
      </div>
      <div class="fw-rule-actions">
        <button type="button" class="btn ghost tiny danger" data-fw-act="del-service" data-idx="${i}">×</button>
      </div>
    </div>`;
}

function fwForwardRow(f, i) {
  return `
    <div class="fw-rule allow">
      <div class="fw-rule-main">
        <span class="fw-rule-badge allow">FWD</span>
        <span class="fw-rule-text">
          <strong>${esc(f.name || "forward " + (i + 1))}</strong>
          <span class="muted small">${esc(f.protocol)}/${f.source_port} → ${esc(f.target_ip)}:${f.target_port}${f.source && f.source !== "0.0.0.0/0" ? " from " + esc(f.source) : ""}</span>
        </span>
      </div>
      <div class="fw-rule-actions">
        <button type="button" class="btn ghost tiny" data-fw-act="edit-fwd" data-idx="${i}">Edit</button>
        <button type="button" class="btn ghost tiny danger" data-fw-act="del-fwd" data-idx="${i}">×</button>
      </div>
    </div>`;
}

function fwLoadFromState() {
  const c = (firewallState && firewallState.config) || {};
  $("fw-enabled").checked = c.enabled !== false;
  $("fw-syn-rate").value = (c.syn_flood && c.syn_flood.rate) ?? 10;
  $("fw-syn-burst").value = (c.syn_flood && c.syn_flood.burst) ?? 20;
  $("fw-bf-threshold").value = (c.brute_force && c.brute_force.threshold) ?? 10;
  $("fw-bf-seconds").value = (c.brute_force && c.brute_force.ban_seconds) ?? 1800;
  $("fw-scan-threshold").value = (c.scan_detect && c.scan_detect.syn_threshold) ?? 200;
  $("fw-scan-seconds").value = (c.scan_detect && c.scan_detect.ban_seconds) ?? 3600;
  $("fw-scan-enabled").checked = !c.scan_detect || c.scan_detect.enabled !== false;
  $("fw-geo").checked = c.geo_block === true;
  $("fw-wan-confirmed").checked = c.wan_confirmed === true;
  $("fw-dmz").value = c.dmz || "";
  $("fw-allow-cidrs").value = (c.allow_cidrs || []).join("\n");
  $("fw-deny-cidrs").value = (c.deny_cidrs || []).join("\n");
  $("fw-rules").innerHTML = (c.rules || []).length
    ? (c.rules || []).map(fwRuleRow).join("")
    : `<p class="muted small">No custom rules — the posture defaults apply.</p>`;
  $("fw-services").innerHTML = (c.services || []).length
    ? (c.services || []).map(fwServiceRow).join("")
    : `<p class="muted small">No services exposed (LAN-only box management).</p>`;
  $("fw-forwards").innerHTML = (c.port_forwards || []).length
    ? (c.port_forwards || []).map(fwForwardRow).join("")
    : `<p class="muted small">No port forwards (WAN mode only).</p>`;
}

function fwRenderStatus() {
  const st = (firewallState && firewallState.status) || {};
  const mode = firewallState && firewallState.mode;
  const badge = $("fw-mode-badge");
  badge.textContent = mode === "wan" ? "WAN mode · default-deny inbound on ppp0"
    : mode === "lan" ? "LAN mode · permissive-out" : "—";
  badge.className = "badge " + (mode === "wan" ? "badge-wan" : "badge-lan");
  $("fw-apply-state").textContent =
    st.apply_ok === false ? "⚠ last apply failed — " + (st.last_error || "nft error")
    : st.apply_ok ? "applied ✓"
    : "not applied yet";
  const unavail = $("fw-unavailable");
  if (firewallState && firewallState.available === false) {
    unavail.classList.remove("hidden");
    unavail.textContent = "Firewall unavailable (no nft/root) — the table is not programmed. " +
      (firewallState.reason || "");
  } else {
    unavail.classList.add("hidden");
  }
  // bans
  const bans = (st.bans || []);
  $("fw-bans").innerHTML = bans.length
    ? bans.map((b) => `
        <div class="fw-ban">
          <span><strong>${esc(b.ip)}</strong> <span class="muted small">${esc(b.reason)}</span></span>
          <span class="muted small">${Math.ceil(b.remaining / 60)} min left</span>
          <button type="button" class="btn ghost tiny danger" data-fw-unban="${esc(b.ip)}">×</button>
        </div>`).join("")
    : `<p class="muted small">No active bans.</p>`;
  // log
  const log = (firewallState && firewallState.log) || [];
  $("fw-log").innerHTML = log.length
    ? log.map((e) => `
        <div class="fw-log-entry ${e.level}">
          <span class="muted small">${new Date(e.ts * 1000).toLocaleString()}</span>
          <span class="fw-log-msg">${esc(e.message)}</span>
        </div>`).join("")
    : `<p class="muted small">No firewall events yet.</p>`;
}

async function refreshFirewall() {
  try {
    firewallState = await API.get("/api/firewall");
    if (!firewallState || firewallState.available === false) {
      fwRenderStatus();
      $("fw-rules").innerHTML = `<p class="muted small">Firewall disabled in config.</p>`;
      return;
    }
    fwLoadFromState();
    fwRenderStatus();
    fwCheckTls();
  } catch (e) {
    if (e.message !== "unauthorized") {
      $("fw-rules").innerHTML = `<p class="muted small">Firewall unavailable: ${esc(e.message)}</p>`;
    }
  }
}

function fwCollect() {
  return {
    enabled: $("fw-enabled").checked,
    services: (firewallState && firewallState.config && firewallState.config.services) || [],
    port_forwards: (firewallState && firewallState.config && firewallState.config.port_forwards) || [],
    rules: (firewallState && firewallState.config && firewallState.config.rules) || [],
    allow_cidrs: $("fw-allow-cidrs").value.split("\n").map((s) => s.trim()).filter(Boolean),
    deny_cidrs: $("fw-deny-cidrs").value.split("\n").map((s) => s.trim()).filter(Boolean),
    dmz: $("fw-dmz").value.trim(),
    syn_flood: { rate: +$("fw-syn-rate").value || 10, burst: +$("fw-syn-burst").value || 20 },
    brute_force: { threshold: +$("fw-bf-threshold").value || 10, ban_seconds: +$("fw-bf-seconds").value || 1800 },
    scan_detect: { enabled: $("fw-scan-enabled").checked, syn_threshold: +$("fw-scan-threshold").value || 200, ban_seconds: +$("fw-scan-seconds").value || 3600 },
    geo_block: $("fw-geo").checked,
    wan_confirmed: $("fw-wan-confirmed").checked,
  };
}

async function fwApply() {
  const btn = $("fw-apply");
  btn.disabled = true;
  btn.textContent = "Applying…";
  const msg = $("fw-msg");
  msg.textContent = "";
  try {
    const res = await API.post("/api/firewall", fwCollect());
    msg.textContent = "Applied ✓" + (res.watchdog_seconds ? ` (watchdog armed: auto-reverts in ${res.watchdog_seconds}s only if the box is locked out)` : "");
    if (res.warnings && res.warnings.length) {
      $("fw-warnings").innerHTML =
        `<p class="muted small" style="color:var(--warn)">Ignored (would lock you out / invalid):</p>` +
        res.warnings.map((w) => `<p class="muted small">• ${esc(w)}</p>`).join("");
    } else {
      $("fw-warnings").innerHTML = "";
    }
    await refreshFirewall();
  } catch (e) {
    msg.textContent = "Apply failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Apply firewall";
  }
}

async function fwRevert() {
  try {
    const res = await API.post("/api/firewall/revert");
    $("fw-msg").textContent = res.applied ? "Reverted to last-good ✓" : "Revert failed";
    await refreshFirewall();
  } catch (e) {
    $("fw-msg").textContent = "Revert failed: " + e.message;
  }
}

async function fwCheckTls() {
  try {
    const st = await API.get("/api/security/tls");
    const card = $("fw-https-card");
    const btn = $("fw-enforce-https");
    const rmBtn = $("fw-remove-https");
    const status = $("fw-https-status");
    if (st.enforced) {
      btn.textContent = "HTTPS active";
      btn.disabled = true;
      btn.classList.remove("primary");
      btn.classList.add("ghost");
      rmBtn.classList.remove("hidden");
      status.textContent = "TLS certificate and secure cookies are enabled. "
        + "The dashboard is accessible at https://<your-box-ip>:8080";
      status.classList.remove("hidden");
      card.classList.add("fw-https-done");
    } else {
      btn.textContent = "Enable HTTPS";
      btn.disabled = false;
      btn.classList.remove("ghost");
      btn.classList.add("primary");
      rmBtn.classList.add("hidden");
      status.classList.add("hidden");
      card.classList.remove("fw-https-done");
    }
  } catch (e) {
    // TLS check failed — non-critical, just show the button
  }
}

async function fwEnforceHttps() {
  const btn = $("fw-enforce-https");
  const status = $("fw-https-status");
  btn.disabled = true;
  btn.textContent = "Generating certificate…";
  status.classList.remove("hidden");
  status.textContent = "Creating self-signed TLS certificate and updating config…";
  try {
    const res = await API.post("/api/security/enforce-https");
    btn.textContent = "Restarting…";
    status.textContent = res.message || "HTTPS enforced — the dashboard is restarting over HTTPS. "
      + "Accept the self-signed certificate warning in your browser once, then "
      + "the connection is encrypted. If the page does not reload, open "
      + "https://<your-box-ip>:8080 manually.";
    status.classList.add("fw-https-success");
    btn.classList.remove("primary");
    btn.classList.add("ghost");
    btn.textContent = "HTTPS active";
    $("fw-remove-https").classList.remove("hidden");
  } catch (e) {
    status.textContent = "Failed: " + e.message;
    status.classList.add("fw-https-error");
    btn.disabled = false;
    btn.textContent = "Enable HTTPS";
  }
}

async function fwRemoveHttps() {
  $("fw-https-modal").classList.add("hidden");
  const btn = $("fw-enforce-https");
  const rmBtn = $("fw-remove-https");
  const status = $("fw-https-status");
  rmBtn.disabled = true;
  rmBtn.textContent = "Removing…";
  status.classList.remove("hidden", "fw-https-success", "fw-https-error");
  status.textContent = "Removing TLS certificate and reverting to plain HTTP…";
  try {
    const res = await API.post("/api/security/remove-https");
    status.textContent = res.message || "HTTPS removed — the dashboard is restarting over plain HTTP.";
    status.classList.add("fw-https-success");
    rmBtn.classList.add("hidden");
    btn.textContent = "Enable HTTPS";
    btn.disabled = false;
    btn.classList.remove("ghost");
    btn.classList.add("primary");
    $("fw-https-card").classList.remove("fw-https-done");
  } catch (e) {
    status.textContent = "Failed: " + e.message;
    status.classList.add("fw-https-error");
    rmBtn.disabled = false;
    rmBtn.textContent = "Remove HTTPS";
  }
}

async function fwBan() {
  const ip = $("fw-ban-ip").value.trim();
  if (!ip) return;
  try {
    await API.post("/api/firewall/ban", { ip, seconds: 1800, reason: "manual" });
    $("fw-ban-ip").value = "";
    await refreshFirewall();
  } catch (e) {
    alert("Ban failed: " + e.message);
  }
}

async function fwUnban(ip) {
  try {
    await API.post("/api/firewall/unban", { ip });
    await refreshFirewall();
  } catch (e) {
    alert("Unban failed: " + e.message);
  }
}

function fwEditRule(i) {
  const rules = (firewallState && firewallState.config && firewallState.config.rules) || [];
  const r = rules[i];
  if (!r) return;
  _openRuleModal(i, r);
}

function fwDeleteRule(i) {
  (firewallState.config.rules || []).splice(i, 1);
  fwLoadFromState();
}

function fwDeleteService(i) {
  (firewallState.config.services || []).splice(i, 1);
  fwLoadFromState();
}

function fwDeleteForward(i) {
  (firewallState.config.port_forwards || []).splice(i, 1);
  fwLoadFromState();
}

function fwAddRule() {
  _openRuleModal(-1, {
    name: "", chain: "forward", action: "deny", src: "", dst: "",
    protocol: "tcp", src_port: 0, dst_port: 0, log: true,
  });
}

function fwAddForward() {
  _openFwdModal(-1, {
    name: "", protocol: "tcp", source_port: 8080,
    target_ip: "", target_port: 8080, source: "",
  });
}

/* ---- rule modal ---- */
let _fwRuleIdx = -1;
function _openRuleModal(idx, r) {
  _fwRuleIdx = idx;
  $("fw-rule-modal-title").textContent = idx < 0 ? "Add rule" : "Edit rule";
  $("fw-rm-name").value = r.name || "";
  $("fw-rm-action").value = r.action || "deny";
  $("fw-rm-chain").value = r.chain || "forward";
  $("fw-rm-src").value = (r.src && r.src !== "0.0.0.0/0") ? r.src : "";
  $("fw-rm-dst").value = (r.dst && r.dst !== "0.0.0.0/0") ? r.dst : "";
  $("fw-rm-proto").value = r.protocol || "";
  $("fw-rm-sport").value = r.src_port || 0;
  $("fw-rm-dport").value = r.dst_port || 0;
  $("fw-rule-modal").classList.remove("hidden");
}
function _saveRuleModal() {
  const r = {
    name: $("fw-rm-name").value.trim(),
    action: $("fw-rm-action").value,
    chain: $("fw-rm-chain").value,
    src: $("fw-rm-src").value.trim() || "0.0.0.0/0",
    dst: $("fw-rm-dst").value.trim() || "0.0.0.0/0",
    protocol: $("fw-rm-proto").value,
    src_port: +$("fw-rm-sport").value || 0,
    dst_port: +$("fw-rm-dport").value || 0,
    log: true,
  };
  const rules = (firewallState.config.rules = firewallState.config.rules || []);
  if (_fwRuleIdx < 0) {
    rules.push(r);
  } else {
    rules[_fwRuleIdx] = r;
  }
  $("fw-rule-modal").classList.add("hidden");
  fwLoadFromState();
}

/* ---- forward modal ---- */
let _fwFwdIdx = -1;
function fwEditForward(i) {
  const fwds = (firewallState && firewallState.config && firewallState.config.port_forwards) || [];
  const f = fwds[i];
  if (!f) return;
  _openFwdModal(i, f);
}
function _openFwdModal(idx, f) {
  _fwFwdIdx = idx;
  $("fw-fwd-modal-title").textContent = idx < 0 ? "Add port forward" : "Edit port forward";
  $("fw-fm-name").value = f.name || "";
  $("fw-fm-proto").value = f.protocol || "tcp";
  $("fw-fm-wan-port").value = f.source_port || 8080;
  $("fw-fm-lan-port").value = f.target_port || 8080;
  $("fw-fm-from").value = (f.source && f.source !== "0.0.0.0/0") ? f.source : "";
  $("fw-fm-to").value = f.target_ip || "";
  $("fw-fwd-modal").classList.remove("hidden");
}
function _saveFwdModal() {
  const f = {
    name: $("fw-fm-name").value.trim(),
    protocol: $("fw-fm-proto").value,
    source_port: +$("fw-fm-wan-port").value || 8080,
    target_port: +$("fw-fm-lan-port").value || 8080,
    source: $("fw-fm-from").value.trim() || "0.0.0.0/0",
    target_ip: $("fw-fm-to").value.trim(),
  };
  const fwds = (firewallState.config.port_forwards = firewallState.config.port_forwards || []);
  if (_fwFwdIdx < 0) {
    fwds.push(f);
  } else {
    fwds[_fwFwdIdx] = f;
  }
  $("fw-fwd-modal").classList.add("hidden");
  fwLoadFromState();
}

/* ---------------- browsing history ---------------- */

let historyCache = null;   // last /api/history response (refetched on demand)

function histDevices() {
  const out = [];
  (dashboard.users || []).forEach((u) =>
    (u.devices || []).forEach((d) => out.push({ id: d.id, label: `${d.name || d.vendor || d.mac} (${u.name})` })));
  return out;
}

// Keep the device picker in sync with the live payload (devices appear/disappear
// as leases change), preserving the admin's selection when it still exists.
// "all" is the default household overview.
function syncHistoryDeviceSelect() {
  const sel = $("hist-device");
  const prev = sel.value;
  const devices = histDevices();
  const had = prev === "all" || devices.some((d) => String(d.id) === prev);
  sel.innerHTML = `<option value="all">All devices</option>` +
    devices.map((d) => `<option value="${d.id}">${esc(d.label)}</option>`).join("");
  sel.value = had ? prev : "all";
}

// Resolve a device_id to a [name] badge for aggregate rows (user -> device -> mac).
function histDeviceName(deviceId) {
  const dev = (dashboard.users || [])
    .flatMap((u) => u.devices || [])
    .find((x) => x.id === deviceId);
  if (!dev) return "#" + deviceId;
  return dev.user_name || dev.name || dev.mac;
}

// DNS-filter status badge (colored dot + label), and — for the "top
// domains" table only — quick block/allow buttons that call
// POST /api/dns/rules/quick. Buttons offer "this device" only when viewing
// a specific device (aggregate/"all" has no single device to scope to).
function dnsStatusBadge(status) {
  const map = {
    blocked: ["dns-badge blocked", "● Blocked"],
    allowed: ["dns-badge allowed", "● Allowed"],
    redirected: ["dns-badge redirected", "● Redirected"],
    none: ["dns-badge none", "○ No rule"],
  };
  const [cls, label] = map[status] || map.none;
  return `<span class="${cls}">${label}</span>`;
}

async function quickDnsRule(domain, action, scope, deviceId) {
  try {
    await API.post("/api/dns/rules/quick", {
      domain, action, scope, device_id: deviceId ?? null,
    });
    await refreshHistory();
  } catch (e) {
    alert("Could not update the DNS rule: " + e.message);
  }
}

function dnsQuickActions(domain, deviceId) {
  const domEsc = esc(domain);
  const devBtns = deviceId
    ? `<button type="button" class="btn ghost tiny" data-dns-quick="block" data-dns-scope="device" data-dns-domain="${domEsc}" data-dns-devid="${deviceId}">Block device</button>`
    : "";
  return `
    <div class="dns-quick-actions">
      ${devBtns}
      <button type="button" class="btn ghost tiny" data-dns-quick="block" data-dns-scope="global" data-dns-domain="${domEsc}">Block everyone</button>
      <button type="button" class="btn ghost tiny" data-dns-quick="allow" data-dns-scope="global" data-dns-domain="${domEsc}">Allow</button>
    </div>`;
}

let historyAnalyticsCache = null;
let currentHistoryView = "apps";

async function refreshHistory(showLoading = false) {
  const sel = $("hist-device");
  syncHistoryDeviceSelect();
  const id = sel.value;   // "all" (household) or a device id string
  if (id === "") {
    historyCache = null;
    historyAnalyticsCache = null;
    renderHistory(null, null);
    return;
  }
  const windowHours = Number($("hist-window").value) || 24;
  const loading = $("hist-loading");
  const empty = $("hist-empty");
  const statCards = $("hist-stat-cards");
  const isFirstLoad = !historyCache;

  if ((showLoading || isFirstLoad) && loading) {
    loading.classList.remove("hidden");
    if (empty) empty.classList.add("hidden");
    document.querySelectorAll(".hist-view").forEach((v) => v.classList.add("hidden"));
    if (statCards) statCards.classList.add("loading-dim");
  }

  try {
    const raw = await API.get(`/api/history/${id}?window=${windowHours}&limit=200`);
    historyCache = raw;
    if (raw && raw.analytics) {
      historyAnalyticsCache = raw.analytics;
    } else {
      historyAnalyticsCache = await API.get(`/api/history/${id}/analytics?window=${windowHours}`);
    }
  } catch (_) {
    historyCache = null;
    historyAnalyticsCache = null;
  } finally {
    if (loading) loading.classList.add("hidden");
    if (statCards) statCards.classList.remove("loading-dim");
  }
  renderHistory(historyCache, historyAnalyticsCache);
}

function renderHistory(d, analytics) {
  const empty = $("hist-empty"), summary = $("hist-summary"), loading = $("hist-loading");
  if (loading) loading.classList.add("hidden");
  if (!d || !d.top_domains || !d.top_domains.length) {
    empty.classList.remove("hidden");
    if ($("hist-stat-cards")) $("hist-stat-cards").classList.add("hidden");
    document.querySelectorAll(".hist-view").forEach((v) => v.classList.add("hidden"));
    if (summary) summary.classList.add("hidden");
    empty.textContent = d && d.device_id === "all"
      ? "No browsing history recorded for the household in this window yet."
      : "No browsing history recorded for this device in this window yet.";
    return;
  }
  empty.classList.add("hidden");
  if ($("hist-stat-cards")) $("hist-stat-cards").classList.remove("hidden");

  const total = d.total_queries || 0;
  if (summary) {
    summary.classList.remove("hidden");
    if (d.device_id === "all") {
      summary.textContent = `All devices — ${total.toLocaleString()} queries in the last ${d.window_hours} h.`;
    } else {
      const device = histDevices().find((x) => String(x.id) === String(d.device_id));
      summary.textContent = `Device: ${device ? device.label : "#" + d.device_id} — ${total.toLocaleString()} queries in the last ${d.window_hours} h.`;
    }
  }

  // Summary Tiles
  if ($("hist-total-queries")) $("hist-total-queries").textContent = total.toLocaleString();
  const topApp = (analytics && analytics.top_apps && analytics.top_apps[0]) ? analytics.top_apps[0] : null;
  if ($("hist-top-app")) $("hist-top-app").textContent = topApp ? `${topApp.name} (${topApp.percentage}%)` : "—";
  const topWeb = (analytics && analytics.top_websites && analytics.top_websites[0]) ? analytics.top_websites[0] : null;
  if ($("hist-top-web")) $("hist-top-web").textContent = topWeb ? `${topWeb.domain} (${topWeb.percentage}%)` : "—";
  if ($("hist-domains-count")) $("hist-domains-count").textContent = (d.top_domains || []).length.toLocaleString();

  // Render Apps View
  const appsList = $("hist-apps-list");
  if (appsList && analytics && analytics.top_apps) {
    if (!analytics.top_apps.length) {
      appsList.innerHTML = `<p class="muted small" style="text-align: center; padding: 16px;">No recognized application traffic in this window.</p>`;
    } else {
      appsList.innerHTML = analytics.top_apps.map((app) => `
        <div class="hist-bar-item">
          <div class="hist-bar-header">
            <div class="hist-bar-left">
              <span class="hist-bar-indicator" style="background: ${app.color || '#38BDF8'}"></span>
              <span class="hist-bar-name">${esc(app.name)}</span>
              <span class="hist-bar-category">${esc(app.category)}</span>
            </div>
            <div class="hist-bar-right">
              <span class="hist-bar-queries">${app.queries.toLocaleString()} queries</span>
              <span class="hist-bar-pct">${app.percentage}%</span>
            </div>
          </div>
          <div class="hist-progress-bg">
            <div class="hist-progress-fill" style="width: ${app.percentage}%; background: ${app.color || '#38BDF8'}"></div>
          </div>
        </div>
      `).join("");
    }
  }

  // Render Websites View
  const webList = $("hist-websites-list");
  if (webList && analytics && analytics.top_websites) {
    if (!analytics.top_websites.length) {
      webList.innerHTML = `<p class="muted small" style="text-align: center; padding: 16px;">No websites recorded in this window.</p>`;
    } else {
      webList.innerHTML = analytics.top_websites.map((web) => `
        <div class="hist-bar-item">
          <div class="hist-bar-header">
            <div class="hist-bar-left">
              <span class="hist-bar-indicator" style="background: #38BDF8"></span>
              <span class="hist-bar-name">${esc(web.domain)}</span>
            </div>
            <div class="hist-bar-right">
              <span class="hist-bar-queries">${web.queries.toLocaleString()} queries</span>
              <span class="hist-bar-pct">${web.percentage}%</span>
            </div>
          </div>
          <div class="hist-progress-bg">
            <div class="hist-progress-fill" style="width: ${web.percentage}%; background: #38BDF8"></div>
          </div>
        </div>
      `).join("");
    }
  }

  // Render Timeline View
  const timelineChart = $("hist-timeline-chart");
  if (timelineChart && analytics && analytics.timeline) {
    const maxQ = Math.max(1, ...analytics.timeline.map((t) => t.queries));
    timelineChart.innerHTML = analytics.timeline.map((item) => `
      <div class="hist-timeline-col" title="${item.slot}: ${item.queries} queries">
        <div class="hist-timeline-bar" style="height: ${Math.max(4, Math.round((item.queries / maxQ) * 100))}%"></div>
        <span class="hist-timeline-label">${item.label}</span>
      </div>
    `).join("");
  }

  // Render Raw Table View
  const curDeviceId = d.device_id === "all" ? null : d.device_id;
  if ($("hist-top")) {
    $("hist-top").innerHTML = d.top_domains.map((t) => `
      <tr>
        <td class="domain">${esc(t.domain)}</td>
        <td class="num">${t.hits.toLocaleString()}</td>
        <td class="num">${total ? ((t.hits / total) * 100).toFixed(1) : "0.0"}%</td>
        <td>${dnsStatusBadge(t.status)} ${dnsQuickActions(t.domain, curDeviceId)}</td>
      </tr>`).join("");
  }

  const hours = new Map();
  (d.activity || []).forEach((a) => {
    const h = a.bucket_minute.slice(0, 13) + "00";
    hours.set(h, (hours.get(h) || 0) + a.count);
  });
  const maxHits = Math.max(1, ...hours.values());
  if ($("hist-activity")) {
    $("hist-activity").innerHTML = [...hours.entries()].map(([h, c]) => `
      <li>
        <span>${esc(h)}</span>
        <span class="num">${c.toLocaleString()} <b style="opacity:.35">${"█".repeat(Math.round((c / maxHits) * 20))}</b></span>
      </li>`).join("");
  }

  if ($("hist-recent")) {
    $("hist-recent").innerHTML = (d.recent || []).map((r) => {
      const badge = r.device_id
        ? `<b class="hist-device-badge">[${esc(histDeviceName(r.device_id))}]</b> `
        : "";
      return `
      <li>
        <span class="domain">${badge}${esc(r.domain)} ${dnsStatusBadge(r.status)}</span>
        <span class="num">${esc(r.bucket_minute)} × ${r.count}</span>
      </li>`;
    }).join("");
  }

  switchHistoryView(currentHistoryView);
}

function switchHistoryView(view) {
  currentHistoryView = view;
  document.querySelectorAll(".hist-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  document.querySelectorAll(".hist-view").forEach((el) => {
    el.classList.add("hidden");
  });
  const target = $(`hist-view-${view}`);
  if (target) target.classList.remove("hidden");
}

/* level filter + search are applied client-side to the raw /api/logs lines;
   the level is the 3rd whitespace token ("2026-08-06 12:00:00,123 INFO name: …") */
/* ---------------- DNS tab: rules, presets, import ---------------- */

let dnsPresetsCache = [];
let dnsRulesCache = [];

// scope-target selects (rule form + import form) share the same options:
// every user, then every device, each tagged so submit knows which id/scope.
function populateDnsTargetSelect(sel, scopeSel) {
  const scope = scopeSel.value;
  sel.classList.toggle("hidden", scope === "global");
  if (scope === "global") return;
  if (scope === "user") {
    sel.innerHTML = (dashboard.users || [])
      .map((u) => {
        const note = u.exempt_quota ? " — unlimited" : "";
        return `<option value="${u.id}">${esc(u.name || `User #${u.id}`)}${note}</option>`;
      }).join("");
  } else {
    sel.innerHTML = (dashboard.devices || [])
      .map((d) => `<option value="${d.id}">${esc(d.name || d.vendor || d.mac)} (${esc(d.user_name || `User #${d.user_id}`)})</option>`).join("");
  }
}

function scopeLabel(rule) {
  if (rule.scope === "global") return "Global";
  if (rule.scope === "user") {
    const u = (dashboard.users || []).find((x) => x.id === rule.scope_id);
    return `User: ${esc(u ? (u.name || `#${rule.scope_id}`) : `#${rule.scope_id}`)}`;
  }
  const dev = (dashboard.devices || []).find((x) => x.id === rule.scope_id);
  return `Device: ${esc(dev ? `${dev.name || dev.vendor || dev.mac} (${dev.user_name || `User #${dev.user_id}`})` : `#${rule.scope_id}`)}`;
}

function renderDnsRules() {
  const list = $("dns-rules-list"), empty = $("dns-rules-empty");
  if (!dnsRulesCache.length) {
    list.innerHTML = "";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  list.innerHTML = dnsRulesCache.map((r) => {
    const actionLabel = r.action === "redirect" ? `Redirect → ${esc(r.target_ip || "")}` :
      r.action === "allow" ? "Allow" : "Block";
    return `
    <tr>
      <td>${scopeLabel(r)}</td>
      <td class="domain">${esc(r.domain)}</td>
      <td>${actionLabel}</td>
      <td class="muted small">${esc(r.source)}</td>
      <td><button type="button" class="btn ghost tiny" data-del-rule="${r.id}">Delete</button></td>
    </tr>`;
  }).join("");
}

function renderDnsPresets() {
  const listEl = $("dns-presets-list");
  if (!listEl) return;
  const contentPresets = dnsPresetsCache.filter((p) => p.id !== "adblock-ultra" && p.id !== "ads-tracking");
  if (!contentPresets.length) {
    listEl.innerHTML = '<div class="muted small" style="padding: 10px 0;">No additional category filters available.</div>';
    return;
  }
  listEl.innerHTML = contentPresets.map((p) => `
    <div class="dns-preset-row">
      <div>
        <b>${esc(p.name)}</b>
        <p class="muted small">${esc(p.description)}</p>
        ${p.enabled ? `<span class="muted small">${p.domain_count.toLocaleString()} domains · ${scopeLabel({ scope: p.scope, scope_id: p.scope_id })}</span>` : ""}
      </div>
      <label class="switch" title="${p.enabled ? 'Disable' : 'Enable'} ${esc(p.name)}">
        <input type="checkbox" data-preset-toggle="${p.id}" ${p.enabled ? "checked" : ""}>
        <span class="slider"></span>
      </label>
    </div>`).join("");
}

function renderAdblockHero() {
  const masterSwitch = $("adblock-master-switch");
  const statusBadge = $("adblock-status-badge");
  const shieldIcon = $("adblock-shield-icon");
  const toggleLabel = $("adblock-toggle-label");
  const domainsCount = $("adblock-domains-count");
  const modeVal = $("adblock-mode-val");

  if (!masterSwitch) return;

  const ultraPreset = dnsPresetsCache.find((p) => p.id === "adblock-ultra");
  const stdPreset = dnsPresetsCache.find((p) => p.id === "ads-tracking");

  const ultraActive = ultraPreset && ultraPreset.enabled;
  const stdActive = stdPreset && stdPreset.enabled;
  const isEnabled = ultraActive || stdActive;

  // Selected tier
  let activeTier = "adblock-ultra";
  if (stdActive && !ultraActive) {
    activeTier = "ads-tracking";
  }

  // Update radios
  const radio = document.querySelector(`input[name="adblock-tier"][value="${activeTier}"]`);
  if (radio && (ultraActive || stdActive)) {
    radio.checked = true;
  }
  document.querySelectorAll(".adblock-tier-card").forEach((card) => {
    const r = card.querySelector('input[type="radio"]');
    card.classList.toggle("active", r && r.checked);
  });

  masterSwitch.checked = !!isEnabled;

  if (toggleLabel) {
    toggleLabel.textContent = isEnabled ? "Active" : "Disabled";
  }

  if (statusBadge) {
    if (isEnabled) {
      statusBadge.className = "badge ok";
      statusBadge.textContent = "● Shield Active";
    } else {
      statusBadge.className = "badge off";
      statusBadge.textContent = "Protection Off";
    }
  }

  if (shieldIcon) {
    shieldIcon.classList.toggle("active", !!isEnabled);
  }

  if (domainsCount) {
    const totalBlocked = (ultraActive ? (ultraPreset.domain_count || 0) : 0) + (stdActive ? (stdPreset.domain_count || 0) : 0);
    domainsCount.textContent = isEnabled ? `${totalBlocked.toLocaleString()} domains` : "0 (Disabled)";
  }

  if (modeVal) {
    modeVal.textContent = ultraActive ? "🔥 Ultra PRO" : (stdActive ? "⚡ Standard" : "—");
  }
}

async function toggleAdblockMaster(enable) {
  const selectedRadio = document.querySelector('input[name="adblock-tier"]:checked');
  const tier = (selectedRadio && selectedRadio.value) || "adblock-ultra";
  const otherTier = tier === "adblock-ultra" ? "ads-tracking" : "adblock-ultra";

  const sw = $("adblock-master-switch");
  if (sw) sw.disabled = true;

  try {
    if (enable) {
      const otherPreset = dnsPresetsCache.find((p) => p.id === otherTier);
      if (otherPreset && otherPreset.enabled) {
        await API.post(`/api/dns/presets/${otherTier}/disable`, { scope: "global" }).catch(() => {});
      }
      await API.post(`/api/dns/presets/${tier}/enable`, { scope: "global" });
    } else {
      await API.post(`/api/dns/presets/adblock-ultra/disable`, { scope: "global" }).catch(() => {});
      await API.post(`/api/dns/presets/ads-tracking/disable`, { scope: "global" }).catch(() => {});
    }
  } catch (err) {
    alert("Ad-blocker update failed: " + err.message);
  } finally {
    if (sw) sw.disabled = false;
    await refreshDns();
  }
}

async function updateAdblockFeeds() {
  const btn = $("adblock-update-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Updating…";
  }
  try {
    const selectedRadio = document.querySelector('input[name="adblock-tier"]:checked');
    const tier = (selectedRadio && selectedRadio.value) || "adblock-ultra";
    await API.post(`/api/dns/presets/${tier}/enable`, { scope: "global" });
    await refreshDns();
  } catch (err) {
    alert("Failed to update ad feeds: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⟳ Update Feeds";
    }
  }
}

function openAdblockWhitelist() {
  const ruleForm = $("dns-rule-form");
  const actionSel = $("dns-rule-action");
  const domainInp = $("dns-rule-domain");
  if (actionSel) actionSel.value = "allow";
  if (domainInp) {
    domainInp.focus();
    domainInp.placeholder = "e.g. ads.google.com";
  }
  if (ruleForm) {
    ruleForm.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

async function refreshDns() {
  try {
    [dnsPresetsCache, dnsRulesCache] = await Promise.all([
      API.get("/api/dns/presets"),
      API.get("/api/dns/rules"),
    ]);
  } catch (_) { dnsPresetsCache = []; dnsRulesCache = []; }
  renderDnsPresets();
  renderAdblockHero();
  renderDnsRules();
  populateDnsTargetSelect($("dns-rule-target"), $("dns-rule-scope"));
  populateDnsTargetSelect($("dns-import-target"), $("dns-import-scope"));
}

async function togglePreset(presetId, enable, inputEl) {
  const err = $("dns-presets-error") || $("dns-rule-error");
  if (err) err.classList.add("hidden");
  if (inputEl) inputEl.disabled = true;
  try {
    if (enable) {
      await API.post(`/api/dns/presets/${presetId}/enable`, { scope: "global" });
    } else {
      await API.post(`/api/dns/presets/${presetId}/disable`, { scope: "global" });
    }
  } catch (e) {
    if (err) {
      err.textContent = "Preset update failed: " + e.message;
      err.classList.remove("hidden");
    }
  } finally {
    if (inputEl) inputEl.disabled = false;
  }
  await refreshDns();
}

async function submitDnsRule(ev) {
  ev.preventDefault();
  const err = $("dns-rule-error");
  err.classList.add("hidden");
  const scope = $("dns-rule-scope").value;
  const scopeId = scope === "global" ? null : Number($("dns-rule-target").value) || null;
  const action = $("dns-rule-action").value;
  const domain = $("dns-rule-domain").value.trim();
  const targetIp = $("dns-rule-target-ip").value.trim();
  try {
    const body = { scope, scope_id: scopeId, action, domain, enabled: true };
    if (action === "redirect") body.target_ip = targetIp;
    await API.post("/api/dns/rules", body);
    $("dns-rule-domain").value = "";
    $("dns-rule-target-ip").value = "";
    await refreshDns();
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove("hidden");
  }
}

async function submitDnsImport(ev) {
  ev.preventDefault();
  const result = $("dns-import-result");
  const scope = $("dns-import-scope").value;
  const scopeId = scope === "global" ? null : Number($("dns-import-target").value) || null;
  try {
    const res = await API.post("/api/dns/import", {
      text: $("dns-import-text").value,
      format: $("dns-import-format").value,
      scope, scope_id: scopeId,
      action: $("dns-import-action").value,
    });
    result.textContent = `Imported ${res.created} rule(s), skipped ${res.skipped} unenforceable line(s).`;
    result.classList.remove("hidden");
    $("dns-import-text").value = "";
    await refreshDns();
  } catch (e) {
    result.textContent = "Import failed: " + e.message;
    result.classList.remove("hidden");
  }
}

function filterLogs() {
  let lines = logLines;
  if (logFilter !== "ALL") {
    const re = new RegExp(`^\\S+ \\S+ ${logFilter}\\b`);
    lines = lines.filter((l) => re.test(l));
  }
  if (logSearch) {
    const q = logSearch.toLowerCase();
    lines = lines.filter((l) => l.toLowerCase().includes(q));
  }
  return lines;
}

function renderLogs() {
  const pre = $("logs-view");
  if (!logLines.length) {
    pre.textContent = "(no log file yet — the gateway writes logs/quota.log as it runs)";
    return;
  }
  const filtered = filterLogs();
  if (!filtered.length) {
    pre.textContent = "(no lines match the current filter)";
    return;
  }
  const html = filtered.map((l) => {
    const m = l.match(/^(\S+ \S+ )(DEBUG|INFO|WARNING|ERROR)(.*)$/);
    if (!m) return esc(l);
    return `${esc(m[1])}<span class="log-level ${m[2].toLowerCase()}">${m[2]}</span>${esc(m[3])}`;
  }).join("\n");
  let out = html;
  if (logMeta && logMeta.truncated) {
    out += `\n\n… ${logMeta.total} lines total, showing the last ${logMeta.lines.length}.`;
  }
  pre.innerHTML = out;
}

function downloadLogs() {
  const lines = filterLogs();
  const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "quota.log";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/* ---------------- websocket ---------------- */

let ws = null;
let wsRetry = 0;
let wsTimer = null;
let pollTimer = null;
let pollInFlight = false;

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (pollInFlight) return;  // don't stack requests on a slow box
    pollInFlight = true;
    try {
      const data = await API.get("/api/dashboard");
      if (data && typeof data === "object" && !data.error) render(data);
    } catch (_) { /* WS will resume pushing when it reconnects */ }
    finally { pollInFlight = false; }
  }, 10000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function wsConnect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    wsRetry = 0;
    stopPolling();  // live push is back — kill the polling fallback
  };
  ws.onmessage = async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") render(msg.data);
    } catch (_) { /* ignore malformed */ }
  };
  ws.onclose = () => {
    ws = null;
    // fall back to polling while disconnected, then keep retrying the socket
    startPolling();
    scheduleWsRetry();
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

function wsClose() {
  if (ws) { try { ws.close(); } catch (_) {} }
  ws = null;
  clearTimeout(wsTimer);
  stopPolling();
}

function scheduleWsRetry() {
  clearTimeout(wsTimer);
  wsTimer = setTimeout(wsConnect, Math.min(1000 * 2 ** wsRetry++, 15000));
}

/* ---------------- actions ---------------- */

async function doAction(act, id) {
  try {
    if (act === "toggle") {
      // switch reflects current state; we want the NEW value
      const checkbox = document.querySelector(`.toggle-block[data-id="${id}"]`);
      const blocked = !checkbox.checked;
      await API.patch(`/api/devices/${id}`, { block: blocked });
    } else if (act === "delete") {
      const dev = (dashboard.devices || []).find((d) => d.id === id);
      if (dev && dev.gateway) return;  // the box cannot be deleted (API 400s too)
      const name = dev && dev.name ? `“${dev.name}”` : "this device";
      if (!confirm(`Kick & disconnect ${name} from the network?\n\nClient will be disconnected for a 5-second timeout and can reconnect freely without a MAC ban.`)) return;
      await API.del(`/api/devices/${id}?blacklist=false`);
    } else if (act === "block-blacklist") {
      const dev = (dashboard.devices || []).find((d) => d.id === id);
      if (dev && dev.gateway) return;
      const name = dev && dev.name ? `“${dev.name}”` : "this device";
      const mac = dev && dev.mac ? ` (${dev.mac})` : "";
      if (!confirm(`Permanently block & blacklist ${name}${mac}?\n\nTheir MAC address will be added to the blacklist and network access will be completely cut.`)) return;
      await API.del(`/api/devices/${id}?blacklist=true`);
    } else if (act === "edit") {
      openDeviceModal(id);
      return;
    } else if (act === "guest-accept") {
      await API.post(`/api/guest/${id}/approve`);
    } else if (act === "guest-reject") {
      await API.post(`/api/guest/${id}/reject`);
    }
  } catch (err) {
    alert("Action failed: " + (err.message || String(err)));
  }
  await refreshAll();
}

async function doUserAction(act, uid) {
  try {
    if (act === "toggle") {
      const checkbox = document.querySelector(`.toggle-user[data-uid="${uid}"]`);
      const blocked = !checkbox.checked;
      await API.patch(`/api/users/${uid}`, { block: blocked });
    } else if (act === "delete") {
      const user = (dashboard.users || []).find((x) => x.id === uid);
      if (user && user.protected) return;  // the Gateway user is permanent (API 400s too)
      const names = ((user && user.devices) || [])
        .map((d) => `“${d.name || d.mac}”`).join(", ");
      const msg = `Kick & disconnect ${user && user.name ? `“${user.name}”` : `user #${uid}`}` +
        `${names ? ` and device(s): ${names}` : ""} from the network?\n\nDevices will be disconnected for a 5-second timeout and can reconnect freely without a MAC ban.`;
      if (!confirm(msg)) return;
      await API.del(`/api/users/${uid}?blacklist=false`);
    } else if (act === "block-blacklist") {
      const user = (dashboard.users || []).find((x) => x.id === uid);
      if (user && user.protected) return;
      const names = ((user && user.devices) || [])
        .map((d) => `“${d.name || d.mac}”`).join(", ");
      const msg = `Permanently block & blacklist ${user && user.name ? `“${user.name}”` : `user #${uid}`}` +
        `${names ? ` and all device(s): ${names}` : ""}?\n\nTheir MAC addresses will be added to the blacklist and network access will be completely blocked.`;
      if (!confirm(msg)) return;
      await API.del(`/api/users/${uid}?blacklist=true`);
    } else if (act === "edit") {
      openUserModal(uid);
      return;
    } else if (act === "guest-accept") {
      await API.post(`/api/guest/user/${uid}/approve`);
    } else if (act === "guest-reject") {
      await API.post(`/api/guest/user/${uid}/reject`);
    }
  } catch (err) {
    alert("Action failed: " + (err.message || String(err)));
  }
  await refreshAll();
}

async function refreshAll() {
  const data = await API.get("/api/dashboard");
  render(data);
  refreshGuest();
  refreshNetwork();
  if (!$("panel-admin").classList.contains("hidden")) refreshLogs();
}

/* ---------------- device modal ---------------- */

let originalUserId = null;  // the device's user when the modal opened
let editUserId = null;      // user being edited (null = add mode)

function selectedUserId() {
  const v = $("d-user").value;
  if (v && v.startsWith("u_")) return parseInt(v.slice(2), 10);
  return null;  // "__new__" => a new user is created on save
}

function populateUserSelect(selectedId, allowNew) {
  const sel = $("d-user");
  let html = "";
  if (allowNew) html += `<option value="__new__">New user…</option>`;
  for (const u of dashboard.users || []) {
    const note = u.exempt_quota ? " — unlimited" : "";
    html += `<option value="u_${u.id}">${esc(u.name || `User #${u.id}`)}${note}</option>`;
  }
  sel.innerHTML = html;
  sel.value = selectedId != null ? `u_${selectedId}` : "__new__";
  sel.value = sel.value || (allowNew ? "__new__" : "");
}

function openDeviceModal(id) {
  editDeviceId = id;
  const dev = id != null ? (dashboard.devices || []).find((d) => d.id === id) : null;
  originalUserId = dev ? dev.user_id : null;

  $("modal-title").textContent = dev ? "Edit device" : "Add device";
  $("modal-sub").textContent = dev
    ? `${macText(dev.mac)} — quota lives on the user; reassign or exempt here.`
    : "New devices from DHCP appear automatically. Add one by MAC.";

  $("d-mac-wrap").classList.toggle("hidden", !!dev);
  $("d-mac").required = !dev;

  // add mode offers "New user…"; edit mode only existing users
  populateUserSelect(originalUserId, /* allowNew */ !dev);
  refreshDeviceModalFields();

  $("d-name").value = dev ? dev.name : "";
  $("d-mode").value = dev ? dev.quota_mode : "auto";
  // For AUTO devices the Fixed-GB input is hidden — never leave a stale value
  // in a hidden field: an invalid, non-focusable control makes Firefox block
  // the whole form submit ("invalid form control is not focusable").
  $("d-fixed").value = dev && dev.quota_mode === "fixed"
    ? (dev.fixed_gb ?? dev.allowance_gb ?? 10) : "";
  $("d-bypass").checked = dev ? !!dev.bypass : false;
  const dVpn = $("d-vpn-bypass");
  if (dVpn) dVpn.checked = dev ? !!dev.vpn_bypass : false;
  $("d-topup").value = "";
  // per-device speed caps (Mbps, 0 = unlimited)
  $("d-limit-down").value = dev ? (dev.limit_down_mbps || 0) : 0;
  $("d-limit-up").value = dev ? (dev.limit_up_mbps || 0) : 0;
  $("d-dns-server").value = dev ? (dev.dns_server || "") : "";
  $("d-fixed-wrap").classList.toggle("hidden", $("d-mode").value !== "fixed");
  $("modal-submit").textContent = dev ? "Save" : "Add";
  $("modal").classList.remove("hidden");
  if (!dev) $("d-mac").focus();
}

function refreshDeviceModalFields() {
  const isNew = $("d-user").value === "__new__";
  const sameUser = editDeviceId != null && $("d-user").value === `u_${originalUserId}`;
  // quota fields apply only to a brand-new user, or to the SAME user in edit
  // (editing an existing user's quota from a device card forwards to the user).
  $("d-newuser-wrap").classList.toggle("hidden", !isNew);
  $("d-quota-wrap").classList.toggle("hidden", !(isNew || sameUser));
  $("d-bypass-wrap").classList.toggle("hidden", editDeviceId == null);
  $("d-topup-wrap").classList.toggle("hidden", editDeviceId == null);
  // speed caps are always per-device — shown for new devices AND on every edit
  // (a device keeps its own limit even when its user has none).
  $("d-speed-wrap").classList.remove("hidden");
  // A quota-exempt user is never quota-blocked, so the per-device bypass is
  // redundant for its devices — disable it instead of silently ignoring it.
  const uid = selectedUserId();
  const user = uid != null ? (dashboard.users || []).find((x) => x.id === uid) : null;
  const userExempt = !!user && !!user.exempt_quota;
  $("d-bypass").disabled = userExempt;
  $("d-bypass-exempt-note").classList.toggle("hidden", !userExempt);
}

function closeModal() {
  $("modal").classList.add("hidden");
  editDeviceId = null;
}

function normalizeMac(raw) {
  let hex = String(raw || "").toLowerCase().replace(/[^0-9a-f]/g, "");
  if (hex.length !== 12) return null;
  return hex.match(/.{1,2}/g).join(":");
}

async function submitDevice(ev) {
  ev.preventDefault();
  const name = $("d-name").value.trim();
  const userId = selectedUserId();
  const mode = $("d-mode").value;
  const fixed = mode === "fixed" ? Math.max(0.1, parseFloat($("d-fixed").value) || 0.1) : null;
  // per-device speed caps (Mbps, 0 = unlimited) — always sent, device-scoped
  const limitDown = Math.max(0, parseFloat($("d-limit-down").value) || 0);
  const limitUp = Math.max(0, parseFloat($("d-limit-up").value) || 0);
  const dnsServer = $("d-dns-server").value.trim();

  let targetDeviceId = editDeviceId;
  if (editDeviceId == null) {
    const mac = normalizeMac($("d-mac").value);
    if (!mac) { alert("Invalid MAC address."); return; }
    const body = { mac, name, limit_down_mbps: limitDown, limit_up_mbps: limitUp };
    if (userId != null) {
      body.user_id = userId;      // attach to an existing user
    } else {
      const uname = $("d-user-name").value.trim();
      if (uname) body.user_name = uname;  // name the auto-created user
      body.quota_mode = mode;
      body.fixed_gb = fixed;
    }
    const created = await API.post("/api/devices", body);
    targetDeviceId = created.id;
  } else {
    const patch = { name, limit_down_mbps: limitDown, limit_up_mbps: limitUp };
    if (userId != null && userId !== originalUserId) patch.user_id = userId;
    patch.bypass = $("d-bypass").checked;
    const dVpn = $("d-vpn-bypass");
    if (dVpn) patch.vpn_bypass = dVpn.checked;
    const topupRaw = parseFloat($("d-topup").value);
    if (!Number.isNaN(topupRaw) && topupRaw > 0) {
      await API.post(`/api/devices/${editDeviceId}/topup`, { extra_gb: topupRaw });
    }
    if (userId === originalUserId) {
      // quota fields edit the owning user — only safe when not reassigning
      patch.quota_mode = mode;
      patch.fixed_gb = fixed;
    }
    await API.patch(`/api/devices/${editDeviceId}`, patch);
  }
  // dns_server has its own PATCH endpoint (validated as a bare IP there,
  // see api/schemas.py's DnsServerUpdate) — a separate call either way,
  // now that we have a real device id for a brand-new device too.
  if (targetDeviceId != null) {
    try { await API.patch(`/api/devices/${targetDeviceId}/dns`, { dns_server: dnsServer }); }
    catch (e) { alert("Device saved, but the DNS server value was rejected: " + e.message); }
  }
  closeModal();
  await refreshAll();
}

/* ---------------- user modal ---------------- */

function openUserModal(id) {
  editUserId = id;
  const u = id != null ? (dashboard.users || []).find((x) => x.id === id) : null;
  $("user-modal-title").textContent = u ? "Edit user" : "Add user";
  $("user-modal-sub").textContent = u
    ? `${esc(u.name || "user")} — ${u.devices.length} device(s).`
    : "A user's devices share one allowance.";
  $("u-name").value = u ? u.name : "";
  $("u-mode").value = u ? u.quota_mode : "auto";
  $("u-fixed").value = u && u.quota_mode === "fixed"
    ? (u.fixed_gb ?? u.allowance_gb ?? 10) : "";
  // per-user aggregate speed caps (Mbps, 0 = unlimited)
  $("u-limit-down").value = u ? (u.limit_down_mbps || 0) : 0;
  $("u-limit-up").value = u ? (u.limit_up_mbps || 0) : 0;
  $("u-speed-wrap").classList.remove("hidden");  // shown for new + existing users
  // per-user DNS-history retention (days); blank = global default
  $("u-history-days").value = u ? (u.history_days ?? "") : "";
  $("u-dns-server").value = u ? (u.dns_server || "") : "";
  $("u-exempt").checked = u ? !!u.exempt_quota : false;
  const uVpn = $("u-vpn-bypass");
  if (uVpn) uVpn.checked = u ? !!u.vpn_bypass : false;
  $("u-fixed-wrap").classList.toggle("hidden", $("u-mode").value !== "fixed");
  $("user-modal-submit").textContent = u ? "Save" : "Add";
  $("user-modal").classList.remove("hidden");
  if (!u) $("u-name").focus();
}

function closeUserModal() {
  $("user-modal").classList.add("hidden");
  editUserId = null;
}

async function submitUser(ev) {
  ev.preventDefault();
  const name = $("u-name").value.trim();
  const mode = $("u-mode").value;
  const fixed = mode === "fixed" ? Math.max(0.1, parseFloat($("u-fixed").value) || 0.1) : null;
  // per-user aggregate speed caps (Mbps, 0 = unlimited)
  const limitDown = Math.max(0, parseFloat($("u-limit-down").value) || 0);
  const limitUp = Math.max(0, parseFloat($("u-limit-up").value) || 0);
  // per-user DNS-history retention (days); blank/null = global default, 0 = off
  const historyDaysField = $("u-history-days").value;
  const historyDays = historyDaysField === "" ? null
    : Math.max(0, Math.min(365, parseInt(historyDaysField, 10) || 0));
  const dnsServer = $("u-dns-server").value.trim();
  const exemptQuota = $("u-exempt").checked;
  const uVpn = $("u-vpn-bypass");
  const vpnBypass = uVpn ? uVpn.checked : false;
  let targetUserId = editUserId;
  if (editUserId == null) {
    const created = await API.post("/api/users", { name, quota_mode: mode, fixed_gb: fixed,
      limit_down_mbps: limitDown, limit_up_mbps: limitUp, exempt_quota: exemptQuota,
      vpn_bypass: vpnBypass });
    targetUserId = created.id;
  } else {
    await API.patch(`/api/users/${editUserId}`, { name, quota_mode: mode, fixed_gb: fixed,
      limit_down_mbps: limitDown, limit_up_mbps: limitUp, history_days: historyDays,
      exempt_quota: exemptQuota, vpn_bypass: vpnBypass });
  }
  if (targetUserId != null) {
    try { await API.patch(`/api/users/${targetUserId}/dns`, { dns_server: dnsServer }); }
    catch (e) { alert("User saved, but the DNS server value was rejected: " + e.message); }
  }
  closeUserModal();
  await refreshAll();
}

/* ---------------- login / settings ---------------- */

// One-time welcome panel: shown only on a genuinely fresh install (see
// /api/setup). Confirms the bundle + optionally changes the password, then
// hides forever. "Skip" is session-only — an unconfigured box keeps nudging
// on the next login.
async function showWelcomeIfNeeded() {
  if (window.__welcomeSkipped) return;
  let state;
  try {
    state = await API.get("/api/setup");
  } catch (_) { return; } // auth/network hiccup — never block the dashboard
  if (state.setup_complete) return;
  $("setup-total").value = state.total_gb;
  $("setup-period-type").value = state.period_type || "renew_day";
  $("setup-reset-day").value = state.reset_day;
  updateResetDayAvailability();
  $("welcome-overlay").classList.remove("hidden");
}

async function submitWelcome(ev) {
  ev.preventDefault();
  const errEl = $("welcome-error");
  errEl.classList.add("hidden");
  const periodType = $("setup-period-type").value;
  const body = {
    total_gb: parseFloat($("setup-total").value),
    reset_day: parseInt($("setup-reset-day").value, 10),
    period_type: periodType,
    current_password: $("setup-cur-pw").value,
    new_password: $("setup-new-pw").value || null,
  };
  if (!(body.total_gb > 0)) { errEl.textContent = "Bundle size must be positive."; errEl.classList.remove("hidden"); return; }
  if (!(body.reset_day >= 0 && body.reset_day <= 31)) {
    errEl.textContent = "Reset day must be 0–31 (0 = never auto-reset).";
    errEl.classList.remove("hidden"); return;
  }
  try {
    await API.post("/api/setup/complete", body);
    $("welcome-overlay").classList.add("hidden");
    await refreshAll(); // bundle card reflects any changes
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("hidden");
  }
}

async function submitLogin(ev) {
  ev.preventDefault();
  $("login-error").classList.add("hidden");
  const code = $("login-totp").value.trim();
  try {
    // Two-stage TOTP: stage 1 (password only) returns {ok:false, totp:true}
    // when 2FA is enabled — then prompt for the authenticator code and
    // re-submit WITH it. A code is never accepted without the password.
    const res = await API.post("/api/login", {
      password: $("login-password").value,
      code: code || undefined,
    });
    if (res && res.ok === false && res.totp) {
      $("login-totp").classList.remove("hidden");
      $("login-totp-label").classList.remove("hidden");
      $("login-submit").textContent = "Verify code & unlock";
      $("login-totp").focus();
      return;
    }
    $("login-password").value = "";
    $("login-totp").value = "";
    $("login-totp").classList.add("hidden");
    $("login-totp-label").classList.add("hidden");
    $("login-submit").textContent = "Unlock dashboard";
    showApp();
    await refreshAll();
    wsConnect();
    await showWelcomeIfNeeded();
  } catch (err) {
    $("login-error").textContent = err.message === "unauthorized" ? "Wrong password. Try again." : err.message;
    $("login-error").classList.remove("hidden");
  }
}

async function submitSettings(ev) {
  ev.preventDefault();
  const total = parseFloat($("set-total").value);
  const periodType = $("set-period-type").value;
  const resetDay = parseInt($("set-reset-day").value, 10);
  if (!(total > 0)) { alert("Bundle size must be positive."); return; }
  if (!(resetDay >= 0 && resetDay <= 31)) {
    alert("Reset day must be 0–31 (0 = never auto-reset; you recharge manually).");
    return;
  }
  await API.post("/api/bundle", { total_gb: total, reset_day: resetDay, period_type: periodType });
  settingsDirty = false;
  await refreshAll();
}

function updateRechargeTargetOptions() {
  const typeEl = $("recharge-target-type");
  const fieldEl = $("recharge-target-id-field");
  const labelEl = $("recharge-target-id-label");
  const selectEl = $("recharge-target-id");
  if (!typeEl || !fieldEl || !selectEl) return;

  const mode = typeEl.value;
  if (mode === "all") {
    fieldEl.classList.add("hidden");
    return;
  }
  fieldEl.classList.remove("hidden");
  const curVal = selectEl.value;
  const users = (dashboard && dashboard.users) || [];
  const devices = (dashboard && dashboard.devices) || [];

  if (mode === "user") {
    if (labelEl) labelEl.textContent = "Select User";
    selectEl.innerHTML = users.map(u => `<option value="${u.id}">${esc(u.name || "User #" + u.id)}</option>`).join("");
  } else if (mode === "device") {
    if (labelEl) labelEl.textContent = "Select Device";
    selectEl.innerHTML = devices.map(d => `<option value="${d.id}">${esc(d.name || d.mac)} (${esc(d.mac)})</option>`).join("");
  }
  if (curVal) selectEl.value = curVal;
}

function renderRecharges(packs, users, devices) {
  const tbody = $("recharges-queue-tbody");
  if (!tbody) return;
  const userMap = {};
  for (const u of (users || [])) userMap[u.id] = u.name || `User #${u.id}`;
  const devMap = {};
  for (const d of (devices || [])) devMap[d.id] = d.name || d.mac;

  const activePacks = (packs || []).filter(p => p.active);
  if (!activePacks.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted small" style="text-align: center; padding: 10px;">No add-on packs in queue.</td></tr>`;
    return;
  }

  const now = Date.now() / 1000;
  tbody.innerHTML = activePacks.map(p => {
    let targetLabel = "All (General)";
    if (p.target_type === "user") targetLabel = `User: ${esc(userMap[p.target_id] || "#" + p.target_id)}`;
    else if (p.target_type === "device") targetLabel = `Device: ${esc(devMap[p.target_id] || "#" + p.target_id)}`;

    const expDate = new Date(p.expires_at * 1000);
    const daysLeft = Math.ceil((p.expires_at - now) / 86400);
    const expText = daysLeft <= 0 ? "Expired" : `${expDate.toLocaleDateString()} (${daysLeft}d left)`;

    const typeBadge = p.recurring 
      ? `<span class="status-tag ok" style="font-size: 10px; padding: 2px 6px;">Recurring</span>`
      : `<span class="status-tag" style="font-size: 10px; padding: 2px 6px;">One-time</span>`;

    const note = p.comment ? `<br><span class="muted tiny">${esc(p.comment)}</span>` : "";

    return `<tr>
      <td><strong>${targetLabel}</strong>${note}</td>
      <td><b>${fmt(p.remaining_gb)}</b> / ${fmt(p.gb)} GB</td>
      <td>${typeBadge}</td>
      <td class="small">${expText}</td>
      <td class="num">
        <button type="button" class="btn warning tiny" data-pack-del="${p.id}">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

async function submitRecharge(ev) {
  if (ev) ev.preventDefault();
  const addGb = parseFloat($("set-recharge").value);
  if (!(addGb > 0)) { alert("Enter how many GB to add to the bundle."); return; }

  const targetType = $("recharge-target-type") ? $("recharge-target-type").value : "all";
  let targetId = null;
  if (targetType === "user" || targetType === "device") {
    const val = $("recharge-target-id") ? $("recharge-target-id").value : "";
    targetId = val ? parseInt(val, 10) : null;
    if (!targetId) {
      alert(`Please select a ${targetType} to assign this pack to.`);
      return;
    }
  }

  const expDateVal = $("recharge-expiry") ? $("recharge-expiry").value : "";
  let expiresAt = null;
  if (expDateVal) {
    const d = new Date(expDateVal + "T23:59:59");
    expiresAt = d.getTime() / 1000;
  }

  const recurring = $("recharge-recurring") ? $("recharge-recurring").value === "1" : false;
  const comment = $("recharge-comment") ? $("recharge-comment").value.trim() : "";

  const payload = {
    gb: addGb,
    target_type: targetType,
    target_id: targetId,
    expires_at: expiresAt,
    recurring: recurring,
    comment: comment,
  };

  await API.post("/api/bundle/recharges", payload);
  $("set-recharge").value = "";
  if ($("recharge-expiry")) $("recharge-expiry").value = "";
  if ($("recharge-comment")) $("recharge-comment").value = "";
  if ($("recharge-modal")) $("recharge-modal").classList.add("hidden");
  await refreshAll();
}

async function deleteRechargePack(packId) {
  if (!confirm(`Delete add-on pack #${packId}?`)) return;
  await API.delete(`/api/bundle/recharges/${packId}`);
  await refreshAll();
}

async function doResetMonth() {
  if (!confirm("Start a new quota period now? All counters restart from today, and all guest accounts are deleted.")) return;
  await API.post("/api/reset-month");
  await refreshAll();
}

/* ---------------- guest mode ---------------- */

async function refreshGuest() {
  try {
    const g = await API.get("/api/guest");
    $("guest-mode-toggle").checked = g.enabled;
    $("guest-quota").value = g.quota_gb;
    $("guest-speed-limit").value = g.speed_limit_mbps;
    if ($("guest-limit")) $("guest-limit").value = g.limit;
  } catch (_) { /* guest panel is not critical */ }
}

async function toggleGuestMode(ev) {
  await API.post("/api/guest", { enabled: ev.target.checked });
  await refreshAll();
}

async function submitGuestQuota() {
  const gb = parseFloat($("guest-quota").value);
  if (!(gb > 0)) { alert("Guest quota must be positive."); return; }
  if (!confirm(`Set every guest's allowance to ${gb} GB? Existing guests are updated too.`)) {
    refreshGuest();
    return;
  }
  await API.post("/api/guest", { quota_gb: gb });
  await refreshAll();
}

async function submitGuestLimit() {
  const n = parseInt($("guest-limit").value, 10);
  if (!(n >= 1)) { alert("Guest limit must be at least 1."); return; }
  await API.post("/api/guest", { limit: n });
  await refreshGuest();
}

async function submitGuestSpeed() {
  const mbps = parseFloat($("guest-speed-limit").value);
  if (!(mbps >= 0)) { alert("Guest speed limit must be 0 or positive (0 = unlimited)."); return; }
  await API.post("/api/guest", { speed_limit_mbps: mbps });
  await refreshGuest();
}

async function toggleStopNew(ev) {
  await API.post("/api/guest", { stop_new: ev.target.checked });
  await refreshGuest();
}

/* Decline random MACs: a randomized (locally-administered) MAC carries no
   vendor OUI, so the box can't identify/budget the device. The toggle blocks
   new ones on first sight; the one-shot checkbox also cuts devices that
   ALREADY joined with a random MAC (the server sweeps them, then resets the
   flag). */
async function toggleDeclineRandom(ev) {
  const msg = $("decline-random-msg");
  try {
    await API.post("/api/network", { decline_random_macs: ev.target.checked });
    if (msg) { msg.textContent = ""; msg.classList.add("hidden"); }
  } catch (e) {
    if (msg) {
      msg.textContent = `Could not save: ${e.message}`;
      msg.classList.remove("hidden");
    }
    ev.target.checked = !ev.target.checked;
  }
  await refreshNetwork();
}

async function cutExistingRandomMacs(ev) {
  if (!ev.target.checked) return;
  const msg = $("decline-random-msg");
  if (!confirm("Cut every device that already joined with a randomized MAC? "
      + "Blocked ones stay cut until an admin lifts them.")) {
    ev.target.checked = false;
    return;
  }
  try {
    await API.post("/api/network",
      { decline_random_macs: true, decline_random_macs_existing: true });
    if (msg) {
      msg.textContent = "Existing randomized-MAC devices were cut.";
      msg.classList.remove("hidden");
    }
  } catch (e) {
    if (msg) {
      msg.textContent = `Sweep failed: ${e.message}`;
      msg.classList.remove("hidden");
    }
  }
  await refreshNetwork();
}

/* MAC whitelist / blacklist: clean rules table + add rule modal with conflict validation */
let cachedMacLists = { allow: [], deny: [] };

function renderMacRulesTable() {
  const tbody = $("mac-rules-tbody");
  if (!tbody) return;
  const allow = cachedMacLists.allow || [];
  const deny = cachedMacLists.deny || [];
  if (!allow.length && !deny.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted small" style="text-align: center; padding: 12px;">No MAC rules configured yet.</td></tr>`;
    return;
  }

  const devMap = {};
  const devs = (dashboard && dashboard.devices) || [];
  for (const d of devs) {
    if (d.mac) devMap[d.mac.toLowerCase()] = d.name || d.vendor || "";
  }

  const rows = [];
  allow.forEach(mac => {
    const name = devMap[mac.toLowerCase()] || "—";
    rows.push(`<tr>
      <td><code class="font-mono">${esc(macText(mac))}</code></td>
      <td><strong>${esc(name)}</strong></td>
      <td><span class="mac-rule-badge allow">Whitelist (Allow)</span></td>
      <td class="num">
        <button type="button" class="btn ghost danger tiny" data-mac-rule-del="${esc(mac)}" data-mac-rule-type="allow" title="Remove rule">${ICON_TRASH}</button>
      </td>
    </tr>`);
  });

  deny.forEach(mac => {
    const name = devMap[mac.toLowerCase()] || "—";
    rows.push(`<tr>
      <td><code class="font-mono">${esc(macText(mac))}</code></td>
      <td><strong>${esc(name)}</strong></td>
      <td><span class="mac-rule-badge deny">Blacklist (Deny)</span></td>
      <td class="num">
        <button type="button" class="btn ghost danger tiny" data-mac-rule-del="${esc(mac)}" data-mac-rule-type="deny" title="Remove rule">${ICON_TRASH}</button>
      </td>
    </tr>`);
  });

  tbody.innerHTML = rows.join("");
}

async function refreshMacLists() {
  try {
    const lists = await API.get("/api/mac-lists");
    cachedMacLists = {
      allow: (lists.allow || []).map(m => m.trim().toLowerCase()).filter(Boolean),
      deny: (lists.deny || []).map(m => m.trim().toLowerCase()).filter(Boolean),
    };
    if ($("mac-allow-list")) $("mac-allow-list").value = cachedMacLists.allow.join("\n");
    if ($("mac-deny-list")) $("mac-deny-list").value = cachedMacLists.deny.join("\n");
    renderMacRulesTable();
  } catch (_) { /* MAC-lists panel is not critical */ }
}

async function removeMacRule(mac, type) {
  const norm = mac.trim().toLowerCase();
  cachedMacLists[type] = (cachedMacLists[type] || []).filter(m => m !== norm);
  try {
    await API.post("/api/mac-lists", {
      allow: cachedMacLists.allow,
      deny: cachedMacLists.deny,
    });
    if ($("mac-allow-list")) $("mac-allow-list").value = cachedMacLists.allow.join("\n");
    if ($("mac-deny-list")) $("mac-deny-list").value = cachedMacLists.deny.join("\n");
    renderMacRulesTable();
  } catch (e) {
    alert("Could not remove MAC rule: " + e.message);
  }
}

function openMacRuleModal() {
  const modal = $("mac-rule-modal");
  if (!modal) return;
  $("mac-rule-macs").value = "";
  $("mac-rule-type").value = "allow";
  const warn = $("mac-rule-conflict-warn");
  if (warn) {
    warn.textContent = "";
    warn.classList.add("hidden");
  }

  const devSelect = $("mac-rule-dev-select");
  if (devSelect) {
    let html = `<option value="">Choose a known device…</option>`;
    const devs = (dashboard && dashboard.devices) || [];
    for (const d of devs) {
      if (d.mac) {
        const label = d.name ? `${d.name} (${d.mac})` : d.mac;
        html += `<option value="${esc(d.mac)}">${esc(label)}</option>`;
      }
    }
    devSelect.innerHTML = html;
  }

  modal.classList.remove("hidden");
}

function closeMacRuleModal() {
  const modal = $("mac-rule-modal");
  if (modal) modal.classList.add("hidden");
}

async function submitNewMacRule(ev) {
  if (ev) ev.preventDefault();
  const type = $("mac-rule-type").value;
  const raw = $("mac-rule-macs").value || "";
  const macs = raw.split(/[\n,]+/).map(s => s.trim().toLowerCase()).filter(Boolean);
  const warn = $("mac-rule-conflict-warn");

  if (!macs.length) {
    if (warn) {
      warn.textContent = "Please enter at least one valid MAC address.";
      warn.classList.remove("hidden");
    }
    return;
  }

  // Check conflicts with the opposite list
  const oppositeType = type === "allow" ? "deny" : "allow";
  const oppositeLabel = type === "allow" ? "Blacklist (Deny)" : "Whitelist (Allow)";
  const currentLabel = type === "allow" ? "Whitelist (Allow)" : "Blacklist (Deny)";
  const oppositeList = cachedMacLists[oppositeType] || [];

  for (const m of macs) {
    if (oppositeList.includes(m)) {
      if (warn) {
        warn.textContent = `Conflict detected: MAC ${m} is currently assigned to the ${oppositeLabel} list. You must remove it from the ${oppositeLabel} list before adding it to ${currentLabel}.`;
        warn.classList.remove("hidden");
      }
      return;
    }
  }

  const targetList = [...(cachedMacLists[type] || [])];
  for (const m of macs) {
    if (!targetList.includes(m)) targetList.push(m);
  }
  cachedMacLists[type] = targetList;

  try {
    await API.post("/api/mac-lists", {
      allow: cachedMacLists.allow,
      deny: cachedMacLists.deny,
    });
    if ($("mac-allow-list")) $("mac-allow-list").value = cachedMacLists.allow.join("\n");
    if ($("mac-deny-list")) $("mac-deny-list").value = cachedMacLists.deny.join("\n");
    closeMacRuleModal();
    renderMacRulesTable();
  } catch (e) {
    if (warn) {
      warn.textContent = `Could not save MAC rule: ${e.message}`;
      warn.classList.remove("hidden");
    }
  }
}

async function submitMacLists() {
  const msg = $("mac-lists-msg");
  const split = (el) => (el ? el.value || "" : "")
    .split(/[\n,]+/).map((s) => s.trim().toLowerCase()).filter(Boolean);
  try {
    const allow = split($("mac-allow-list"));
    const deny = split($("mac-deny-list"));
    await API.post("/api/mac-lists", { allow, deny });
    cachedMacLists = { allow, deny };
    macListsDirty = false;
    if (msg) {
      msg.textContent = "MAC lists saved — applied on the next tick.";
      msg.classList.remove("hidden");
    }
    renderMacRulesTable();
  } catch (e) {
    if (msg) {
      msg.textContent = `Could not save: ${e.message}`;
      msg.classList.remove("hidden");
    }
  }
}

/* ---------------- static IP reservations (Network tab) ---------------- */

let staticLeasesList = [];

function populateStaticLeaseDeviceSelect() {
  const sel = $("sl-device-select");
  if (!sel) return;
  const curVal = sel.value;
  let html = `<option value="">Custom MAC address…</option>`;
  const devs = dashboard && dashboard.devices ? dashboard.devices : [];
  for (const d of devs) {
    const label = d.name ? `${d.name} (${d.mac})` : d.mac;
    html += `<option value="${esc(d.mac)}">${esc(label)}</option>`;
  }
  sel.innerHTML = html;
  sel.value = curVal || "";
}

function renderStaticLeases() {
  const tbody = $("static-leases-tbody");
  if (!tbody) return;
  if (!staticLeasesList.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted small" style="text-align: center; padding: 12px;">No static reservations configured yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = staticLeasesList.map((item) => {
    const devLabel = item.device_name || item.hostname || "—";
    const sub = (item.device_name && item.hostname && item.device_name !== item.hostname)
      ? `<br><span class="muted small">${esc(item.hostname)}</span>` : "";
    return `<tr>
      <td><strong>${esc(devLabel)}</strong>${sub}</td>
      <td><code>${esc(macText(item.mac))}</code></td>
      <td><code>${esc(item.ip)}</code></td>
      <td class="num">
        <button type="button" class="btn warning tiny" data-lease-del="${esc(item.mac)}">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

async function refreshStaticLeases() {
  try {
    populateStaticLeaseDeviceSelect();
    const data = await API.get("/api/network/static-leases");
    staticLeasesList = Array.isArray(data) ? data : [];
    renderStaticLeases();
  } catch (_) { /* non-critical */ }
}

async function submitStaticLease(ev) {
  ev.preventDefault();
  const msg = $("sl-form-msg");
  if (msg) { msg.classList.add("hidden"); msg.textContent = ""; }

  const macRaw = $("sl-mac").value.trim();
  const mac = normalizeMac(macRaw);
  if (!mac) {
    if (msg) { msg.textContent = "Invalid MAC address format."; msg.classList.remove("hidden"); }
    return;
  }
  const ip = $("sl-ip").value.trim();
  if (!ip) {
    if (msg) { msg.textContent = "Static IP is required."; msg.classList.remove("hidden"); }
    return;
  }
  const hostname = $("sl-hostname").value.trim();

  try {
    await API.post("/api/network/static-leases", { mac, ip, hostname });
    if (msg) {
      msg.textContent = "Reservation saved!";
      msg.classList.remove("hidden");
      setTimeout(() => { if (msg) msg.classList.add("hidden"); }, 3000);
    }
    $("sl-mac").value = "";
    $("sl-ip").value = "";
    $("sl-hostname").value = "";
    $("sl-device-select").value = "";
    if ($("static-lease-modal")) $("static-lease-modal").classList.add("hidden");
    await refreshStaticLeases();
  } catch (e) {
    if (msg) {
      msg.textContent = `Error: ${e.message}`;
      msg.classList.remove("hidden");
    }
  }
}

async function deleteStaticLease(mac) {
  if (!confirm(`Delete static reservation for ${mac}?`)) return;
  try {
    await API.del(`/api/network/static-leases/${encodeURIComponent(mac)}`);
    await refreshStaticLeases();
  } catch (e) {
    alert(`Could not delete reservation: ${e.message}`);
  }
}

/* ---------------- speed shaping (Network tab) ---------------- */

async function refreshNetwork() {
  try {
    const n = await API.get("/api/network");
    networkConfig = n;
    $("shaping-toggle").checked = n.enabled;
    $("set-total-down").value = n.total_down_mbps || "";
    $("set-total-up").value = n.total_up_mbps || "";
    $("set-lan-rate").value = n.lan_rate_mbps || "";
    // random-MAC gate: toggle + the one-shot "cut existing" checkbox (the
    // latter only makes sense while the gate is on).
    $("decline-random-toggle").checked = !!n.decline_random_macs;
    $("decline-random-existing").disabled = !n.decline_random_macs;
    renderVpnShare(n);
    renderNetworkPreview(n);
    refreshMacLists(); // prefill the allow/deny textareas (non-critical)
    refreshStaticLeases(); // load DHCP static IP reservations
  } catch (_) { /* network panel is not critical */ }
}

/* VPN share: the Network-tab switch routes the whole client subnet through
   the box's VPN tunnel (policy routing, quota/vpnshare.py). The persisted
   switch rides /api/network; the `status` sub-key is the kernel-side state
   the maintenance tick cached. */
function renderVpnShare(n) {
  const toggle = $("vpn-toggle"), statusEl = $("vpn-status");
  if (!n || !toggle || !statusEl) return;
  const vs = n.vpn_share || {};
  const st = vs.status || {};
  toggle.checked = !!vs.enabled;
  const iface = st.interface || vs.interface || "";
  let text, cls = "";
  if (st.state === "on") {
    text = `Sharing through ${iface || "the VPN tunnel"} — every device's internet exits via the VPN.`;
    cls = "ok";
  } else if (st.state === "no-interface") {
    text = "No VPN tunnel detected — starting the automatic tun2socks bridge (one-time download)…";
  } else if (st.state === "error") {
    text = `Error: ${st.message || "could not program the routing."}`;
  } else if (vs.enabled) {
    text = "Waiting for the gateway to apply…";
  } else {
    text = "Off — devices use the direct uplink.";
  }
  statusEl.textContent = text;
  statusEl.className = `vpn-status muted small ${cls}`.trim();
  // Auto-provisioned tun2socks bridge status (userspace VPN clients like
  // v2rayN have no kernel tun; quota/tun2socks.py downloads + runs the
  // bridge). Progress/failure messages are honest — a Gateway-OFF box that
  // can't download yet must say so, not silently retry forever.
  const ts = st.tun2socks;
  const tsEl = $("vpn-ts-hint");
  if (tsEl) {
    if (ts && ts.state !== "running" && ts.state !== "off" && ts.message) {
      tsEl.textContent = ts.message;
      tsEl.classList.remove("hidden");
    } else {
      tsEl.textContent = "";
      tsEl.classList.add("hidden");
    }
  }
  // When the Gateway's own internet is cut (Gateway OFF) the relay must keep
  // the box's VPN-server connection alive — it does, automatically (the
  // engine's gw_allowed whitelist). Say so plainly, so a cut Gateway doesn't
  // read as a broken tunnel.
  const gw = (dashboard && dashboard.gateway) || {};
  const hintEl = $("vpn-gw-hint");
  if (hintEl) {
    if (gw.blocked_programmed && st.state === "on") {
      hintEl.textContent = "Gateway OFF: the box's own internet is cut, but its VPN-server connection stays open automatically — devices still reach the internet through the tunnel.";
      hintEl.classList.remove("hidden");
    } else if (gw.blocked_programmed && vs.enabled) {
      hintEl.textContent = "Gateway OFF: the box's own internet is cut — devices route through the VPN tunnel once the relay applies.";
      hintEl.classList.remove("hidden");
    } else {
      hintEl.textContent = "";
      hintEl.classList.add("hidden");
    }
  }
}

function renderNetworkPreview(n) {
  if (!n || !$("np-status")) return;
  // "applying…" while a saved change is queued: the shaper's kernel tree is
  // rebuilt off the event loop after each save. applied=false (with tc
  // available) means the tree doesn't match the last save yet — show it
  // instead of silently stale numbers. Degrades to plain On/Off when the
  // snapshot carries no shaping state (pre-first-tick / no shaper wired).
  const rebuilding = shapingLive && shapingLive.available && !shapingLive.applied;
  $("np-status").textContent = rebuilding ? "Applying…" : (n.enabled ? "On" : "Off");
  $("np-status").className = `stat-value ${rebuilding ? "warning" : n.enabled ? "ok" : "off"}`;
  $("np-down").textContent = n.total_down_mbps ? `${n.total_down_mbps} Mbps` : "—";
  $("np-up").textContent = n.total_up_mbps ? `${n.total_up_mbps} Mbps` : "—";
  $("np-lan").textContent = n.lan_rate_mbps ? `${n.lan_rate_mbps} Mbps` : "1000 Mbps";
  const capped = (dashboard && dashboard.devices
    ? dashboard.devices : []).filter((d) => d.limit_down_mbps || d.limit_up_mbps);
  $("np-capped").textContent = capped.length;
  $("np-devices").innerHTML = capped.length
    ? capped.slice(0, 20).map((d) =>
        `<li><span>${esc(d.name || d.mac)}</span>` +
        `<span class="muted">↓${d.limit_down_mbps || "∞"} ↑${d.limit_up_mbps || "∞"}</span></li>`).join("")
    : `<li class="muted">No device caps set.</li>`;
  const vpn = $("np-vpn");
  if (vpn) {
    const vs = (n.vpn_share || {}).status || {};
    const on = vs.state === "on";
    vpn.textContent = on
      ? `On · ${vs.interface || (n.vpn_share || {}).interface || "tunnel"}`
      : "Off";
    vpn.className = `stat-value ${on ? "ok" : "off"}`;
  }
}

async function submitNetwork() {
  const body = {
    enabled: $("shaping-toggle").checked,
    total_down_mbps: parseFloat($("set-total-down").value) || 0,
    total_up_mbps: parseFloat($("set-total-up").value) || 0,
    lan_rate_mbps: parseFloat($("set-lan-rate").value) || 0,
  };
  const vpnToggle = $("vpn-toggle");
  if (vpnToggle) body.vpn_share = vpnToggle.checked;
  await API.post("/api/network", body);
  await refreshAll();
}

/* The VPN-share switch saves IMMEDIATELY on change (like the guest-mode
   toggle) — a partial POST with only vpn_share so it never clobbers the
   shaping totals. Without this, flipping the switch and refreshing the page
   silently reverted to OFF, because nothing persisted until the panel's
   separate Save button was clicked. */
async function toggleVpnShare(ev) {
  await API.post("/api/network", { vpn_share: ev.target.checked });
  await refreshAll();
}

/* ---------------- WAN mode (strong: the box dials PPPoE) ---------------- */

// Top-bar internet reachability indicator. `internet` is the box's live probe
// (every 15 s tick): true = green Online, false = red Offline, undefined (not
// probed yet, pre-first-tick) = gray Checking….
function renderNetStatus(internet) {
  const el = $("net-status");
  if (!el) return;
  const dot = el.querySelector(".dot");
  const label = el.querySelector(".net-label");
  if (!dot || !label) return;
  if (internet === true) {
    dot.className = "dot ok";
    label.textContent = "Online";
    el.title = "Internet connection is up.";
  } else if (internet === false) {
    dot.className = "dot red";
    label.textContent = "Offline";
    el.title = "Internet connection is down.";
  } else {
    dot.className = "dot off";
    label.textContent = "Checking…";
    el.title = "Checking internet connection…";
  }
}

function renderWan(wan) {
  if (!wan || (typeof wan.topology === "undefined" && typeof wan.configured === "undefined"))
    return; // not populated yet
  // The toggle reflects the CONFIGURED (desired) topology — what the box will
  // boot into — NOT the live one. Right after an Apply the live topology has
  // not flipped yet (it changes when the gateway restarts), so keying the
  // switch on the live value made it snap back off on every render. `configured`
  // carries the target; `topology` is what the engine is actually running.
  const desired = wan.configured || wan.topology;
  const wanOn = desired === "wan";
  // The PPPoE link state is judged by the negotiated address (carrier-less ppp
  // can report a non-up operstate while dialed up), matching the backend.
  const linkUp = (wan.ppp0 || "") === "up";
  const t = $("wan-toggle");
  // While a toggle flip is un-applied, the 5 s WS push must not clobber the
  // draft (flip-then-Apply within the window broke the toggle before).
  if (t && !wanToggleDirty) t.checked = wanOn;
  const tp = $("wan-topology");
  if (tp) {
    tp.textContent = wanOn ? "wan" : "lan";
    tp.className = `stat-value ${wanOn ? "warning" : "ok"}`;
  }
  const src = $("wan-source");
  if (src) src.textContent = wan.source === "dashboard"
    ? "dashboard"
    : "config.yaml";
  const p = $("wan-ppp0");
  if (p) {
    const state = wan.ppp0 || "n/a";
    p.textContent = state === "up" ? "up" : state;
    p.className = `stat-value ${state === "up" ? "ok" : state === "n/a" ? "off" : "warning"}`;
  }
  const ip = $("wan-ppp-ip");
  if (ip) ip.textContent = wan.ppp_ip || "—";
  const creds = $("wan-creds");
  if (creds && !wanToggleDirty) creds.classList.toggle("hidden", !wanOn);
  const banner = $("wan-restart-banner");
  if (banner) {
    if (wanToggleDirty) {
      // A pending (un-applied) flip — keep the toggle where the user put it.
      banner.textContent = "Mode change pending — press “Apply now” to rewire + " +
        "restart, or “Revert to LAN” to cancel.";
      banner.classList.remove("hidden");
      return;
    }
    const pending = wan.pending && wan.pending !== wan.topology;
    if (wan.restart_scheduled) {
      // The apply just succeeded and the detached restart is about to fire.
      banner.textContent = wanOn
        ? "WAN (strong) mode applied — the gateway is restarting now…"
        : "LAN mode applied — the gateway is restarting now…";
      banner.classList.remove("hidden");
    } else if (pending) {
      // The saved preference has not been booted into yet (restart pending /
      // a restart that failed). The toggle stays ON so the state is visible.
      banner.textContent = "Configured mode is " + wan.pending + " but the gateway is " +
        "still running " + wan.topology + " — it takes effect on the next restart.";
      banner.classList.remove("hidden");
    } else if (wanOn) {
      // Honest ACTIVE banner: only claim WAN is carrying traffic when the ppp0
      // link is actually up. A configured-but-down dial (or the box booted into
      // wan without ppp0) must NOT read as "active" — it means traffic is not
      // going through the box and the router admin page is unreachable.
      banner.textContent = linkUp
        ? "WAN (strong) mode is ACTIVE — the gateway dials the PPPoE line itself. " +
          "Keep the router bridged/AP (guide at left); press “Revert to LAN” to switch back."
        : "WAN (strong) mode is configured but the PPPoE link is DOWN — the box is " +
          "dialing the line but nothing answers (ppp0 down, no public IP), so internet " +
          "is not going through it. The #1 cause: the router is NOT in bridge/modem " +
          "mode yet. Run the test below for the exact reason, or press “Revert to LAN” " +
          "to restore the router uplink now.";
      banner.classList.toggle("wan-down", !linkUp);
      banner.classList.toggle("wan-active", linkUp);
      banner.classList.remove("hidden");
    } else {
      banner.classList.remove("wan-active", "wan-down");
      banner.classList.add("hidden");
    }
  }
  // When WAN mode is already running AND the link is up AND internet is
  // reachable, "Apply now" has nothing to do — it would just re-apply the same
  // topology and restart the gateway for no reason. Dim it; only Test PPPoE
  // connection and Revert to LAN stay active. A pending toggle flip (dirty) or
  // a broken link keeps Apply enabled (there IS something to change or fix).
  const applyBtn = $("wan-apply-btn");
  if (applyBtn) {
    const dim = !wanToggleDirty && wanOn && linkUp && wan.internet === true;
    applyBtn.disabled = dim;
    if (dim) {
      applyBtn.title = "WAN mode is already active and online — nothing to re-apply.";
    } else {
      applyBtn.removeAttribute("title");
    }
  }
  // -- WAN public-IP renewal: the Restart button + the auto-renew schedule are
  //    disabled unless WAN mode is active AND ppp0 is actually UP. A down dial
  //    means internet isn't working — restarting the PPPoE session would just
  //    reconnect to a dead line. The disabled note only appears in WAN mode
  //    with a down link (LAN has no ppp0 to speak of).
  const renewEnabled = wanOn && linkUp;
  const restartBtn = $("wan-restart-btn");
  if (restartBtn) restartBtn.disabled = !renewEnabled;
  const renewNote = $("wan-renew-disabled-note");
  if (renewNote) renewNote.classList.toggle("hidden", renewEnabled || !wanOn);
  const renewToggle = $("wan-renew-toggle");
  if (renewToggle) {
    renewToggle.disabled = !renewEnabled;
    renewToggle.checked = !!wan.renew_enabled;
  }
  const renewMinutes = $("wan-renew-minutes");
  if (renewMinutes) {
    renewMinutes.disabled = !renewEnabled;
    renewMinutes.value = wan.renew_minutes || 15;
  }
  const renewSave = $("wan-renew-save");
  if (renewSave) renewSave.disabled = !renewEnabled;
  const renewLast = $("wan-renew-last");
  if (renewLast) renewLast.textContent = fmtRenewLast(wan.renew_last);
}

// WAN public-IP renewal: the "last renewed" timestamp rendered as a friendly
// relative time ("just now" / "x min ago"), or "—" when never renewed.
function fmtRenewLast(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  if (diff < 0) return "just now";
  if (diff < 60000) return "just now";
  if (diff < 3600000) return Math.floor(diff / 60000) + " min ago";
  if (diff < 86400000) return Math.floor(diff / 3600000) + " h ago";
  return d.toLocaleString();
}

async function refreshWan() {
  try {
    const w = await API.get("/api/wan");
    wanStatus = w;
    // A failed apply reverted the server state — drop the pending draft so the
    // toggle snaps back to reality.
    wanToggleDirty = false;
    // Prefill the saved PPPoE credentials (GET /api/wan serves them from the
    // DB; the WS snapshot does NOT carry them). Only when the user is not
    // mid-editing a draft — a dirty toggle keeps its typed values. Both the
    // username AND the password are gated on the privacy eye — and the gating
    // is a two-way street: while details are masked the fields are actively
    // CLEARED (not just left un-prefilled), so a value revealed earlier and
    // then re-hidden disappears from the screen immediately instead of
    // lingering in the DOM until a refresh.
    const user = $("wan-user"), pass = $("wan-pass"), wanif = $("wan-if");
    if (user && !wanToggleDirty) user.value = privacyHide ? "" : (w.pppoe_user || "");
    if (pass && !wanToggleDirty) {
      // Sensitive-data hardening: the server never ships the stored PPPoE
      // password (masked "********" + pppoe_has_password). The field stays
      // EMPTY — leaving it blank preserves the stored value on apply — with a
      // placeholder that says so. A non-empty value is always a NEW password.
      pass.value = "";
      pass.placeholder = w.pppoe_has_password
        ? "•••••••• (stored — leave blank to keep)"
        : "Enter PPPoE password";
    }
    if (wanif && !wanToggleDirty) wanif.value = w.wan_if || "";
    renderWan(w);
    await maybeAutoDiagnose(w);
    await refreshWanTelegram();
  } catch (_) { /* wan panel is not critical */ }
}

async function testPppoe(ev) {
  ev.preventDefault();
  const btn = ev.currentTarget;
  const msg = $("wan-test-msg");
  btn.disabled = true;
  msg.className = "test-msg loading";
  msg.textContent = "Dialing a test PPPoE link… (up to ~15 s)";
  try {
    const r = await API.post("/api/wan/test", {
      pppoe_user: $("wan-user").value.trim(),
      pppoe_password: $("wan-pass").value,
      wan_if: $("wan-if").value.trim(),
    });
    renderPppoeVerdict(msg, r);
  } catch (err) {
    msg.className = "test-msg fail";
    msg.textContent = err.message === "unauthorized"
      ? "Session expired — please log in again."
      : `PPPoE test error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

// v19.7: turn the /api/wan/test verdict into an ACTIONABLE message — the panel
// must say WHY the line is down, not just "failed". Per-failure-mode fix.
function renderPppoeVerdict(msg, r) {
  const ok = r && r.ok;
  msg.className = `test-msg ${ok ? "ok" : "fail"}`;
  if (ok) {
    msg.textContent = `✓ PPPoE link is UP — the credentials work.` +
      (r.local_ip ? `  local ${r.local_ip} ↔ peer ${r.peer_ip}` : "") +
      `  Internet reachable: ${r.internet ? "yes ✓" : "no ✗"}` +
      (r.internet ? "" : `\n${r.detail}`);
    return;
  }
  const st = (r && r.status) || "error";
  const detail = (r && r.detail) || "the line could not be dialed.";
  const fix = {
    "no-pppoe-server":
      "\n→ Your router is NOT bridged (or the DSL/FTTH line is not synced). " +
      "Log into the router admin (192.168.1.1) and set its WAN to Bridge/Modem " +
      "mode (NAT + DHCP off), then press Apply now again — or use the two-NIC " +
      "layout in the guide. The box keeps reaching the router page either way.",
    "auth-failed":
      "\n→ The ISP rejected the username/password. Re-check them on your ISP card " +
      "or the router's WAN status page, fix the fields above, and Test again.",
    "concurrent-session":
      "\n→ This is usually a FALSE ALARM: the same PPPoE username already holds a " +
      "live session — almost always the box's own ppp0, which is up and working. " +
      "ETIS/We allow only ONE session per username, so the throwaway test dial is " +
      "refused. Check the ppp0 status above: if it's up and devices have internet, " +
      "the line + credentials are fine — nothing to fix. (If ppp0 is NOT up, the " +
      "router may still be dialing PPPoE itself — it must be bridged, not routed/NAT.)",
    "link-down":
      "\n→ A PPPoE server was found but the session stalled — usually the modem/ISP " +
      "side. Wait a minute and Test again, or check the quota-wan-ppp service (the " +
      "real dial fails the same way).",
    "error":
      "\n→ The test could not run (missing pppd / wrong interface). Check the " +
      "quota-wan-ppp service and that the WAN interface above is the NIC that " +
      "reaches the ONT/modem.",
  }[st] || "";
  const lead = st === "concurrent-session"
    ? "ℹ PPPoE test: the ISP refused a SECOND dial (same username already online)"
    : `✗ PPPoE test failed — ${detail}`;
  msg.textContent = `${lead}${fix}`;
  if (r && r.script_output) msg.textContent += `\n${r.script_output}`;
}

// v19.7: when WAN is configured but ppp0 is down, auto-run the throwaway test
// ONCE (per page load) so the panel says WHY — not just "DOWN". Only fires when
// the WAN tab is actually open (init's refreshWan skips it), never while a
// toggle draft is pending, and never against an up link.
async function maybeAutoDiagnose(w) {
  if (pppoeAutoRan || wanToggleDirty) return;
  const panel = $("panel-wan");
  if (!panel || panel.classList.contains("hidden")) return;
  const desired = (w.configured || w.topology || "");
  if (desired !== "wan") return;
  const state = (w.ppp0 || "");
  if (!state || state === "up") return;
  pppoeAutoRan = true;
  const btn = $("wan-test-btn");
  const msg = $("wan-test-msg");
  btn.disabled = true;
  msg.className = "test-msg loading";
  msg.textContent = "ppp0 is down — auto-testing the PPPoE line to find out why… (up to ~15 s)";
  try {
    const r = await API.post("/api/wan/test", {
      pppoe_user: $("wan-user").value.trim(),
      pppoe_password: $("wan-pass").value,
      wan_if: $("wan-if").value.trim(),
    });
    renderPppoeVerdict(msg, r);
  } catch (err) {
    msg.className = "test-msg fail";
    msg.textContent = err.message === "unauthorized"
      ? "Session expired — please log in again."
      : `PPPoE auto-test error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function submitWan(ev) {
  ev.preventDefault();
  const wan = $("wan-toggle").checked ? "wan" : "lan";
  const label = wan === "wan"
    ? "Apply WAN (strong) mode now? The gateway will rewire itself (NIC, DHCP/DNS, " +
      "PPPoE dial) and RESTART automatically. The router must already be in " +
      "bridge/AP mode (see the guide)."
    : "Apply LAN mode now? The gateway restores the router uplink and RESTARTS " +
      "automatically. Put the router back in routed mode first.";
  if (!confirm(label)) return;
  const body = { topology: wan };
  if (wan === "wan") {
    body.pppoe_user = $("wan-user").value.trim();
    body.pppoe_password = $("wan-pass").value;
    body.wan_if = $("wan-if").value.trim();
  }
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.textContent = "Applying… (gateway restarts)";
  try {
    const r = await API.post("/api/wan", body);
    // The response is the live status; show the applier's tail for 6s so the
    // admin can see what changed, then let the auto-reconnect take over.
    $("wan-apply-msg").textContent =
      (r && r.script_output) ? "Applied — restarting. Script output:" : "Applied — restarting.";
    wanStatus = r || null;
    wanToggleDirty = false; // the draft is now the applied state
    renderWan(r || {});
    setTimeout(() => { $("wan-apply-msg").textContent = ""; }, 6000);
  } catch (err) {
    alert(err.message === "unauthorized" ? "Session expired — please log in again."
      : `Apply failed: ${err.message}`);
    await refreshWan(); // refreshWan clears the dirty flag (reverts to reality)
  } finally {
    btn.disabled = false;
    btn.textContent = "Apply now";
  }
}

async function revertWan(ev) {
  ev.preventDefault();
  if (!confirm("Revert to LAN mode now? The gateway restores the router uplink " +
               "(from the saved LAN snapshot) and RESTARTS automatically. The " +
               "router must be back in routed mode first.")) return;
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.textContent = "Reverting… (gateway restarts)";
  try {
    const r = await API.post("/api/wan", { topology: "lan" });
    $("wan-apply-msg").textContent = "LAN restored — restarting.";
    wanStatus = r || null;
    wanToggleDirty = false; // the draft is now the applied state
    renderWan(r || {});
    setTimeout(() => { $("wan-apply-msg").textContent = ""; }, 6000);
  } catch (err) {
    alert(err.message === "unauthorized" ? "Session expired — please log in again."
      : `Revert failed: ${err.message}`);
    await refreshWan(); // refreshWan clears the dirty flag (reverts to reality)
  } finally {
    btn.disabled = false;
    btn.textContent = "Revert to LAN";
  }
}

async function renewWanIp(ev) {
  ev.preventDefault();
  if (!confirm("Restart the PPPoE dial now to renew the public IP?\n\n" +
               "Internet will drop for a few seconds while ppp0 re-dials. On " +
               "most Egyptian lines the ISP hands the new session a fresh public IP.")) return;
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.textContent = "Restarting…";
  try {
    const r = await API.post("/api/wan/renew");
    $("wan-apply-msg").textContent = r.restarted
      ? "PPPoE dial restarted — ppp0 re-dialing (a new public IP if the ISP " +
        "rotates it per session)."
      : `PPPoE restart did not confirm: ${r.detail || "the service is not active"}`;
    // Refetch the live status once the dial has had a moment to settle, so the
    // Public IP + last-renewed lines update without waiting for the next 15 s tick.
    setTimeout(async () => { await refreshWan(); }, 3000);
  } catch (err) {
    alert(err.message === "unauthorized" ? "Session expired — please log in again."
      : `Renew failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Restart PPPoE — renew public IP";
  }
}

async function submitWanRenew(ev) {
  ev.preventDefault();
  const enabled = $("wan-renew-toggle").checked;
  const minutes = parseInt($("wan-renew-minutes").value, 10) || 15;
  const btn = ev.currentTarget;
  const msg = $("wan-renew-msg");
  btn.disabled = true;
  msg.className = "test-msg loading";
  msg.textContent = "Saving…";
  try {
    const r = await API.post("/api/wan/renew-config", { enabled, minutes });
    // The response carries the clamped config — mirror it into the live status
    // so the WAN panel reflects the saved schedule immediately.
    if (wanStatus) {
      wanStatus.renew_enabled = r.enabled;
      wanStatus.renew_minutes = r.minutes;
      wanStatus.renew_last = r.last;
    }
    renderWan(wanStatus || {});
    msg.className = "test-msg ok";
    msg.textContent = enabled
      ? `Auto-renew ${r.minutes} min — the PPPoE dial restarts on schedule ` +
        "(internet drops briefly each time)."
      : "Auto-renew disabled.";
  } catch (err) {
    msg.className = "test-msg fail";
    msg.textContent = err.message === "unauthorized"
      ? "Session expired — please log in again."
      : `Save failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- Telegram WAN IP Trigger ---------------- */

let wanTelegramConfig = null;

async function refreshWanTelegram() {
  try {
    const data = await API.get("/api/wan/telegram");
    wanTelegramConfig = data;
    renderWanTelegram(data);
  } catch (_) {}
}

function renderWanTelegram(data) {
  if (!data) return;
  const toggle = $("wan-tg-toggle");
  const tokenInp = $("wan-tg-token");
  const chatInp = $("wan-tg-chat");
  const lastIp = $("wan-tg-last-ip");
  const lastSent = $("wan-tg-last-sent");
  const fwBanner = $("wan-tg-firewall-banner");
  const fwIcon = $("wan-tg-fw-icon");
  const fwText = $("wan-tg-fw-text");

  if (toggle) toggle.checked = !!data.enabled;
  if (tokenInp && data.has_token && !tokenInp.value) {
    tokenInp.placeholder = data.bot_token || "Token saved (type to replace)";
  }
  if (chatInp && !chatInp.value) {
    chatInp.value = data.chat_id || "";
  }
  if (lastIp) {
    lastIp.textContent = data.last_ip || "—";
  }
  if (lastSent) {
    if (data.last_sent) {
      try {
        const d = new Date(data.last_sent);
        lastSent.textContent = d.toLocaleString();
      } catch (_) {
        lastSent.textContent = data.last_sent;
      }
    } else {
      lastSent.textContent = "—";
    }
  }

  // Firewall status banner
  if (fwBanner && fwText && fwIcon) {
    if (data.wan_exposed) {
      fwBanner.className = "banner ok";
      fwIcon.textContent = "✅";
      fwText.innerHTML = `<strong>WAN Remote Web Access is OPEN</strong> on port ${data.web_port}. You can access the dashboard remotely from outside home.`;
    } else {
      fwBanner.className = "banner warn";
      fwIcon.textContent = "⚠️";
      fwText.innerHTML = `<strong>WAN Remote Web Access is BLOCKED</strong> in Firewall. Telegram IP notifications will work, but remote dashboard access from outside home will be rejected until enabled in Firewall.`;
    }
  }
}

async function saveWanTelegram() {
  const toggle = $("wan-tg-toggle");
  const tokenInp = $("wan-tg-token");
  const chatInp = $("wan-tg-chat");
  const msg = $("wan-tg-msg");
  const saveBtn = $("wan-tg-save-btn");

  const enabled = toggle ? toggle.checked : false;
  const bot_token = tokenInp ? tokenInp.value.trim() : "";
  const chat_id = chatInp ? chatInp.value.trim() : "";

  if (saveBtn) saveBtn.disabled = true;
  if (msg) {
    msg.className = "test-msg loading";
    msg.textContent = "Saving…";
  }

  try {
    const payload = { enabled, chat_id };
    if (bot_token) payload.bot_token = bot_token;
    const res = await API.post("/api/wan/telegram", payload);
    wanTelegramConfig = res;
    renderWanTelegram(res);
    if (tokenInp && bot_token) tokenInp.value = "";
    if (msg) {
      msg.className = "test-msg ok";
      msg.textContent = enabled ? "Telegram trigger saved and active!" : "Telegram trigger disabled.";
      setTimeout(() => { if (msg) msg.textContent = ""; }, 4000);
    }
  } catch (err) {
    if (msg) {
      msg.className = "test-msg fail";
      msg.textContent = `Save failed: ${err.message}`;
    }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function testWanTelegram() {
  const tokenInp = $("wan-tg-token");
  const chatInp = $("wan-tg-chat");
  const msg = $("wan-tg-msg");
  const testBtn = $("wan-tg-test-btn");

  const bot_token = tokenInp ? tokenInp.value.trim() : "";
  const chat_id = chatInp ? chatInp.value.trim() : "";

  if (testBtn) testBtn.disabled = true;
  if (msg) {
    msg.className = "test-msg loading";
    msg.textContent = "Sending test message to Telegram…";
  }

  try {
    const payload = {};
    if (bot_token) payload.bot_token = bot_token;
    if (chat_id) payload.chat_id = chat_id;
    const res = await API.post("/api/wan/telegram/test", payload);
    if (msg) {
      msg.className = "test-msg ok";
      msg.textContent = res.message || "Test message sent! Check your Telegram.";
    }
  } catch (err) {
    if (msg) {
      msg.className = "test-msg fail";
      msg.textContent = `Test failed: ${err.message}`;
    }
  } finally {
    if (testBtn) testBtn.disabled = false;
  }
}

async function submitPassword(ev) {
  ev.preventDefault();
  const cur = $("p-cur").value;
  const next = $("p-new").value;
  if (next.length < 12) { alert("New password must be at least 12 characters."); return; }
  try {
    await API.post("/api/password", { current: cur, new: next });
    $("pwd-modal").classList.add("hidden");
    $("pwd-form").reset();
    // Password change invalidates the session — back to the login screen.
    showLogin();
    alert("Password updated — sign in again with the new password.");
  } catch (err) {
    // A 401 already showed the login screen (session expired) — don't double-alert.
    if (err.message === "unauthorized") return;
    alert(err.message === "current password incorrect" ? "Current password is wrong." : err.message);
  }
}

/* ---------------- TOTP 2FA (opt-in) ---------------- */

async function refreshTotp() {
  try {
    const st = await API.get("/api/totp");
    $("totp-state").textContent = st.enabled ? "· enabled" : "";
  } catch (_) { /* offline / degraded */ }
}

async function openTotp() {
  try {
    const st = await API.get("/api/totp");
    let body;
    if (st.enabled) {
        body = `<p class="muted small">Two-factor is <strong>enabled</strong> — every login now needs a 6-digit code 
from your authenticator app.</p>
          <div class="modal-actions">
          <button type="button" id="totp-close" class="btn ghost">Close</button>
          <button type="button" id="totp-disable" class="btn danger">Disable 2FA</button>
          </div>`;
        $("totp-body").innerHTML = body;
      } else {
        let secret, uri;
        if (st.pending) {
            secret = st.secret;
            uri = st.otpauth_uri;
        } else {
            const enr = await API.post("/api/totp/enroll");
            $("totp-state").textContent = "A pending";
            secret = enr.secret;
            uri = enr.otpauth_uri;
        }
        body = `<p class="muted small">Scan this QR Code with your authenticator app:</p>
        <div id="totp-qr" style="margin: 16px auto; width: 200px; height: 200px; background: #fff; padding: 10px; border-radius: 8px; display: flex; align-items: center; justify-content: center;"></div>
        <p class="muted small">Or type the secret manually:</p>
        <div class="totp-secret" style="margin-bottom: 14px; word-break: break-all;">${secret}</div>
        <input type="text" id="totp-code" inputmode="numeric" placeholder="000 000" required>
        <p id="totp-err" class="error hidden"></p>
        <div class="modal-actions">
          <button type="button" id="totp-close" class="btn ghost">Cancel</button>
          <button type="button" id="totp-enable" class="btn primary">Enable</button>
        </div>`;
        
        $("totp-body").innerHTML = body;
        
        const qrEl = document.getElementById("totp-qr");
        if (qrEl && typeof QRCode !== "undefined") {
            new QRCode(qrEl, {
                text: uri,
                width: 180,
                height: 180,
                colorDark: "#000000",
                colorLight: "#ffffff",
                correctLevel: QRCode.CorrectLevel.M
            });
        }
      }
      $("totp-modal").classList.remove("hidden");
    $("totp-close").addEventListener("click", () => $("totp-modal").classList.add("hidden"));
    const enableBtn = $("totp-enable");
    if (enableBtn) enableBtn.addEventListener("click", enableTotp);
    const disableBtn = $("totp-disable");
    if (disableBtn) disableBtn.addEventListener("click", async () => {
      await API.post("/api/totp/disable").catch(() => {});
      $("totp-modal").classList.add("hidden");
      await refreshTotp();
      alert("Two-factor disabled.");
    });
  } catch (err) {
    if (err.message !== "unauthorized") alert(err.message);
  }
}

async function enableTotp() {
  const code = $("totp-code").value.trim();
  try {
    await API.post("/api/totp/enable", { code });
    $("totp-modal").classList.add("hidden");
    await refreshTotp();
    alert("Two-factor enabled — the next login needs your authenticator code.");
  } catch (err) {
    if (err.message === "unauthorized") return;
    $("totp-err").textContent = err.message;
    $("totp-err").classList.remove("hidden");
  }
}

async function logout() {
  await API.post("/api/logout").catch(() => {});
  showLogin();
}

/* ==========================================================================
   VPN SUBSYSTEM LOGIC (sing-box + Clash API)
   ========================================================================== */

let vpnStatusCache = null;
let vpnNodesCache = [];
let vpnRoutingCache = { users: [], devices: [] };
let editingVpnNodeId = null;

async function refreshVpn(showLoading = true) {
  const loading = $("vpn-loading");
  const content = $("vpn-content");
  const isFirstLoad = !vpnStatusCache;

  if (showLoading && loading) {
    loading.classList.remove("hidden");
    if (content) {
      if (isFirstLoad) {
        content.classList.add("hidden");
      } else {
        content.classList.add("loading-dim");
      }
    }
  }

  try {
    await Promise.allSettled([
      loadVpnStatus(),
      loadVpnNodes(),
      loadVpnRouting(),
      loadVpnSettings(),
      loadVpnLogs(),
    ]);
  } finally {
    if (loading) loading.classList.add("hidden");
    if (content) {
      content.classList.remove("hidden");
      content.classList.remove("loading-dim");
    }
  }
}

async function loadVpnSettings() {
  try {
    const s = await API.get("/api/vpn/settings");
    const sw = $("vpn-allow-insecure-switch");
    if (sw) sw.checked = !!s.allow_insecure;
  } catch (_) {}
}

async function loadVpnStatus() {
  try {
    vpnStatusCache = await API.get("/api/vpn/status");
    renderVpnStatus(vpnStatusCache);
  } catch (e) {
    console.debug("Failed to load VPN status:", e);
  }
}

function renderVpnStatus(st) {
  if (!st) return;
  vpnStatusCache = st;
  const dot = $("vpn-status-dot");
  const txt = $("vpn-status-text");
  const sub = $("vpn-node-subtext");
  const btn = $("vpn-toggle-btn");
  const err = $("vpn-error-banner");

  if (err) {
    if (st.last_error) {
      err.textContent = st.last_error;
      err.classList.remove("hidden");
    } else {
      err.classList.add("hidden");
    }
  }

  if (dot) {
    dot.className = "vpn-badge-dot " + (st.state === "connected" ? "on" : (st.state === "connecting" ? "connecting" : (st.state === "error" ? "error" : "off")));
  }
  if (txt) {
    txt.textContent = st.state === "connected" ? "Connected" : (st.state === "connecting" ? "Connecting…" : (st.state === "error" ? "Error" : "Disconnected"));
  }
  if (sub) {
    sub.textContent = st.active_node_name ? `${st.active_node_name} (${(st.protocol || "vless").toUpperCase()}) · ${st.interface}` : "No active VPN node selected";
  }

  if (btn) {
    btn.textContent = st.state === "connected" ? "Disconnect VPN" : (st.state === "connecting" ? "Cancel" : "Connect VPN");
    btn.className = st.state === "connected" ? "btn ghost danger" : "btn primary";
  }

  // Speed meters
  if ($("vpn-speed-down")) $("vpn-speed-down").textContent = fmtBytes(st.speed_down_bps / 8) + "/s";
  if ($("vpn-speed-up")) $("vpn-speed-up").textContent = fmtBytes(st.speed_up_bps / 8) + "/s";
  if ($("vpn-total-down")) $("vpn-total-down").textContent = fmt(st.total_down_bytes / (1024 * 1024 * 1024)) + " GB";
  if ($("vpn-total-up")) $("vpn-total-up").textContent = fmt(st.total_up_bytes / (1024 * 1024 * 1024)) + " GB";
  if ($("vpn-conns-count")) $("vpn-conns-count").textContent = (st.active_connections || 0).toLocaleString();

  // Uptime format
  if ($("vpn-uptime")) {
    if (st.state === "connected" && st.uptime_seconds) {
      const s = st.uptime_seconds;
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      $("vpn-uptime").textContent = `${h}h ${m}m`;
    } else {
      $("vpn-uptime").textContent = "—";
    }
  }

  // Always update the nodes table so active node badge and connect/disconnect buttons reflect current state
  if (vpnNodesCache && vpnNodesCache.length) {
    renderVpnNodes(vpnNodesCache);
  }
}

async function loadVpnNodes() {
  try {
    vpnNodesCache = await API.get("/api/vpn/nodes");
    renderVpnNodes(vpnNodesCache);
  } catch (e) {
    console.debug("Failed to load VPN nodes:", e);
  }
}

function renderVpnNodes(nodes) {
  const tbody = $("vpn-nodes-tbody");
  if (!tbody) return;
  if (!nodes || !nodes.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted small" style="text-align: center; padding: 16px;">No VPN nodes added yet. Click "Add Node" to import a link.</td></tr>`;
    return;
  }
  const activeId = (vpnStatusCache && vpnStatusCache.active_node_id != null) ? Number(vpnStatusCache.active_node_id) : null;
  const isConnected = vpnStatusCache && vpnStatusCache.state === "connected";

  tbody.innerHTML = nodes.map((n) => {
    const isActive = activeId !== null && activeId === Number(n.id);
    let pingText = "—";
    let pingCls = "";
    if (n.ping_ms != null && n.ping_ms > 0) {
      pingText = `${n.ping_ms} ms`;
      pingCls = n.ping_ms < 150 ? "text-accent" : "";
    } else if (n.ping_ms != null && n.ping_ms < 0) {
      pingText = "Timeout";
      pingCls = "text-danger";
    }
    return `
      <tr class="${isActive ? "active-node-row" : ""}">
        <td>
          ${isActive ? '<span class="status-tag ok">Active</span>' : '<span class="status-tag off">Idle</span>'}
        </td>
        <td><strong>${esc(n.name)}</strong></td>
        <td><span class="vpn-node-proto-tag">${esc((n.protocol || "vless").toUpperCase())}</span></td>
        <td>
          <span class="${pingCls}" id="vpn-ping-val-${n.id}">${pingText}</span>
          <button type="button" id="vpn-ping-btn-${n.id}" class="btn ghost tiny" data-vpn-act="ping" data-vpn-id="${n.id}" title="Test latency">${ICON_ZAP}</button>
        </td>
        <td class="num">
          ${isActive && isConnected
            ? `<button type="button" class="btn ghost danger tiny" data-vpn-act="disconnect">Disconnect</button>`
            : `<button type="button" class="btn primary tiny" data-vpn-act="connect" data-vpn-id="${n.id}">Connect</button>`}
          <button type="button" class="btn ghost tiny" data-vpn-act="edit" data-vpn-id="${n.id}" title="Edit node">${ICON_EDIT}</button>
          <button type="button" class="btn ghost danger tiny" data-vpn-act="delete" data-vpn-id="${n.id}" title="Delete node">${ICON_TRASH}</button>
        </td>
      </tr>
    `;
  }).join("");
}

async function loadVpnRouting() {
  try {
    vpnRoutingCache = await API.get("/api/vpn/routing");
    renderVpnRouting(vpnRoutingCache);
  } catch (e) {
    console.debug("Failed to load VPN routing:", e);
  }
}

let currentVpnRoutingTab = "users";

function getDeviceIcon(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("pc") || n.includes("laptop") || n.includes("computer") || n.includes("macbook") || n.includes("desktop")) return "💻";
  if (n.includes("phone") || n.includes("iphone") || n.includes("android") || n.includes("mobile") || n.includes("samsung") || n.includes("xiaomi")) return "📱";
  if (n.includes("tv") || n.includes("smart") || n.includes("screen")) return "📺";
  if (n.includes("pad") || n.includes("tablet") || n.includes("ipad")) return "📟";
  if (n.includes("box") || n.includes("gateway") || n.includes("server") || n.includes("router")) return "🖧";
  return "📱";
}

function renderVpnRouting(data) {
  const container = $("vpn-routing-list");
  if (!container) return;
  const searchInput = $("vpn-routing-search");
  const search = (searchInput && searchInput.value || "").toLowerCase().trim();
  const users = data.users || [];
  const devices = data.devices || [];

  const userMap = new Map();
  users.forEach((u) => {
    userMap.set(u.id, { ...u, devices: [] });
  });

  devices.forEach((d) => {
    if (d.user_id != null && userMap.has(d.user_id)) {
      userMap.get(d.user_id).devices.push(d);
    }
  });

  let html = "";

  if (currentVpnRoutingTab === "users") {
    const matchingUsers = users.filter((u) => !search || u.name.toLowerCase().includes(search));

    if (matchingUsers.length === 0) {
      html = `<p class="muted small" style="text-align: center; padding: 20px;">No matching users found.</p>`;
    } else {
      html = matchingUsers.map((u) => {
        const devCount = (userMap.get(u.id) || {}).devices?.length || 0;
        const devText = devCount === 1 ? "1 device" : `${devCount} devices`;
        const initials = (u.name || "U").substring(0, 2).toUpperCase();

        return `
          <div class="vpn-user-group-card">
            <div class="vpn-user-group-header">
              <div class="vpn-user-meta-left">
                <span class="vpn-user-avatar-badge">${esc(initials)}</span>
                <div class="vpn-user-titles">
                  <span class="vpn-user-name">${esc(u.name)}</span>
                  <span class="vpn-user-count">${devText}</span>
                </div>
              </div>
              <div class="vpn-user-controls">
                <span class="vpn-route-badge ${u.route_vpn ? "routed" : "direct"}">${u.route_vpn ? "VPN" : "Direct"}</span>
                <label class="switch" title="Route all devices of ${esc(u.name)} via VPN">
                  <input type="checkbox" ${u.route_vpn ? "checked" : ""} data-vpn-route-type="user" data-vpn-route-id="${u.id}">
                  <span class="slider"></span>
                </label>
              </div>
            </div>
          </div>
        `;
      }).join("");
    }
  } else if (currentVpnRoutingTab === "devices") {
    const matchingDevices = devices.filter(
      (d) => !search || d.name.toLowerCase().includes(search) || d.mac.toLowerCase().includes(search)
    );

    if (matchingDevices.length === 0) {
      html = `<p class="muted small" style="text-align: center; padding: 20px;">No matching devices found.</p>`;
    } else {
      html = matchingDevices.map((d) => {
        const owner = d.user_id != null && userMap.has(d.user_id) ? userMap.get(d.user_id).name : null;
        const ownerBadge = owner
          ? `<span class="vpn-owner-badge" title="Owned by ${esc(owner)}">👤 ${esc(owner)}</span>`
          : `<span class="vpn-unassigned-badge">Unassigned</span>`;

        return `
          <div class="vpn-flat-device-card">
            <div class="vpn-dev-info">
              <span class="vpn-dev-icon">${getDeviceIcon(d.name)}</span>
              <div>
                <span class="vpn-dev-name">${esc(d.name)}</span>
                ${ownerBadge}
                <div class="vpn-dev-mac">${esc(d.mac)}</div>
              </div>
            </div>
            <div class="vpn-dev-action">
              <span class="vpn-dev-status-tag ${d.route_vpn ? "routed" : "direct"}">${d.route_vpn ? "VPN" : "Direct"}</span>
              <label class="switch tiny" title="Route this device via VPN">
                <input type="checkbox" ${d.route_vpn ? "checked" : ""} data-vpn-route-type="device" data-vpn-route-id="${d.id}">
                <span class="slider"></span>
              </label>
            </div>
          </div>
        `;
      }).join("");
    }
  }

  container.innerHTML = html;
}

async function setVpnRouting(type, id, routeVpn) {
  try {
    await API.post("/api/vpn/routing", { target_type: type, target_id: id, route_vpn: routeVpn });
    await loadVpnRouting();
  } catch (e) {
    alert("Failed to update routing rule: " + e.message);
  }
}

async function connectVpn(nodeId) {
  const btn = document.querySelector(`[data-vpn-act="connect"][data-vpn-id="${nodeId}"]`);
  const heroBtn = $("vpn-toggle-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Connecting…";
  }
  if (heroBtn) {
    heroBtn.disabled = true;
    heroBtn.textContent = "Connecting…";
  }
  try {
    await API.post("/api/vpn/connect", { node_id: nodeId });
    await loadVpnStatus();
    await loadVpnNodes();
    await loadVpnLogs();
  } catch (e) {
    alert("Connection failed: " + e.message);
    await loadVpnStatus();
    await loadVpnLogs();
  } finally {
    if (btn) btn.disabled = false;
    if (heroBtn) heroBtn.disabled = false;
  }
}

async function disconnectVpn() {
  const btn = document.querySelector(`[data-vpn-act="disconnect"]`);
  const heroBtn = $("vpn-toggle-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Disconnecting…";
  }
  if (heroBtn) {
    heroBtn.disabled = true;
    heroBtn.textContent = "Disconnecting…";
  }
  try {
    await API.post("/api/vpn/disconnect");
    await loadVpnStatus();
    await loadVpnNodes();
    await loadVpnLogs();
  } catch (e) {
    alert("Disconnect failed: " + e.message);
    await loadVpnLogs();
  } finally {
    if (btn) btn.disabled = false;
    if (heroBtn) heroBtn.disabled = false;
  }
}

async function pingVpnNode(nodeId) {
  const el = $(`vpn-ping-val-${nodeId}`);
  const btn = $(`vpn-ping-btn-${nodeId}`);
  if (el) el.innerHTML = '<span class="vpn-ping-spinner"></span>';
  if (btn) btn.disabled = true;
  try {
    const res = await API.post(`/api/vpn/nodes/${nodeId}/ping`);
    const ms = Number(res.ping_ms);
    if (vpnNodesCache) {
      const target = vpnNodesCache.find((n) => Number(n.id) === Number(nodeId));
      if (target) target.ping_ms = ms;
    }
    if (el) {
      if (ms > 0) {
        el.textContent = `${ms} ms`;
        el.className = ms < 150 ? "text-accent" : "";
      } else {
        el.textContent = "Timeout";
        el.className = "text-danger";
      }
    }
  } catch (e) {
    if (el) {
      el.textContent = "Error";
      el.className = "text-danger";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteVpnNode(nodeId) {
  if (!confirm("Are you sure you want to delete this VPN node?")) return;
  try {
    await API.delete(`/api/vpn/nodes/${nodeId}`);
    await loadVpnNodes();
    await loadVpnStatus();
  } catch (e) {
    alert("Failed to delete node: " + e.message);
  }
}

function getNodeParams(node) {
  let cfg = {};
  try { cfg = JSON.parse(node.config_json || "{}"); } catch (_) {}
  const tls = cfg.tls || {};
  const reality = tls.reality || {};
  const transport = cfg.transport || {};

  let uriParams = {};
  let uriHost = "";
  let uriPort = 443;
  let uriUuid = "";
  if (node.raw_uri && node.raw_uri.includes("://")) {
    try {
      const rest = node.raw_uri.split("://")[1];
      const atIdx = rest.indexOf("@");
      if (atIdx !== -1) {
        uriUuid = decodeURIComponent(rest.substring(0, atIdx));
        const hostPortAndRest = rest.substring(atIdx + 1);
        const qIdx = hostPortAndRest.indexOf("?");
        const hashIdx = hostPortAndRest.indexOf("#");
        const hostPort = hostPortAndRest.substring(0, qIdx !== -1 ? qIdx : (hashIdx !== -1 ? hashIdx : undefined));
        if (hostPort.includes(":")) {
          uriHost = hostPort.split(":")[0];
          uriPort = parseInt(hostPort.split(":")[1]) || 443;
        } else {
          uriHost = hostPort;
        }
      }
      if (node.raw_uri.includes("?")) {
        const qStr = node.raw_uri.split("?")[1].split("#")[0];
        const sp = new URLSearchParams(qStr);
        uriParams = Object.fromEntries(sp.entries());
      }
    } catch (_) {}
  }

  return {
    name: node.name || "",
    server: cfg.server || uriHost || "",
    server_port: cfg.server_port || uriPort || 443,
    uuid: cfg.uuid || cfg.password || uriUuid || "",
    sni: tls.server_name || uriParams.sni || "",
    flow: cfg.flow || uriParams.flow || "",
    pbk: reality.public_key || uriParams.pbk || "",
    sid: reality.short_id || uriParams.sid || "",
    transport: (transport.type || uriParams.type || "tcp").toUpperCase(),
    path: transport.path || transport.service_name || uriParams.path || "",
  };
}

function editVpnNode(nodeId) {
  try {
    const node = (vpnNodesCache || []).find((n) => n.id == nodeId);
    if (!node) {
      console.warn("VPN node not found for editing:", nodeId, vpnNodesCache);
      return;
    }
    editingVpnNodeId = node.id;
    const p = getNodeParams(node);

    if ($("ve-name")) $("ve-name").value = p.name || "";
    if ($("ve-server")) $("ve-server").value = p.server || "";
    if ($("ve-port")) $("ve-port").value = p.server_port || 443;
    if ($("ve-uuid")) $("ve-uuid").value = p.uuid || "";
    if ($("ve-sni")) $("ve-sni").value = p.sni || "";
    if ($("ve-flow")) $("ve-flow").value = p.flow || "";
    if ($("ve-pbk")) $("ve-pbk").value = p.pbk || "";
    if ($("ve-sid")) $("ve-sid").value = p.sid || "";
    if ($("ve-transport")) $("ve-transport").value = p.transport === "WS" || p.transport === "GRPC" ? p.transport : "TCP";
    if ($("ve-path")) $("ve-path").value = p.path || "";

    const errEl = $("ve-error");
    if (errEl) errEl.classList.add("hidden");
    const modal = $("vpn-edit-proxy-modal");
    if (modal) modal.classList.remove("hidden");
  } catch (err) {
    console.error("Failed to open edit modal:", err);
  }
}

let vpnLogPollTimer = null;

function startVpnLogPolling() {
  if (vpnLogPollTimer) return;
  vpnLogPollTimer = setInterval(async () => {
    const vpnPanel = $("panel-vpn");
    if (vpnPanel && !vpnPanel.classList.contains("hidden")) {
      await loadVpnLogs();
      await loadVpnStatus();
    } else {
      stopVpnLogPolling();
    }
  }, 2500);
}

function stopVpnLogPolling() {
  if (vpnLogPollTimer) {
    clearInterval(vpnLogPollTimer);
    vpnLogPollTimer = null;
  }
}

async function loadVpnLogs() {
  const container = $("vpn-logs-view");
  if (!container) return;
  try {
    const data = await API.get("/api/vpn/logs?limit=300");
    if (!data.logs || !data.logs.length) {
      container.innerHTML = '<div class="muted small" style="padding: 12px 0;">(no VPN core logs recorded yet)</div>';
      return;
    }
    // Auto-scroll to bottom if user is already near bottom (within 80px)
    const isAtBottom = (container.scrollHeight - container.scrollTop - container.clientHeight) < 80;

    const rows = data.logs.map((l) => {
      // Strip ANSI escape codes
      const clean = (l.text || "").replace(/\u001b\[[0-9;]*m/g, "");
      let colorStyle = "color: #94a3b8;";
      if (/FATAL|panic/i.test(clean)) {
        colorStyle = "color: #ef4444; font-weight: 700;";
      } else if (/ERROR|failed|failure|validation failed/i.test(clean)) {
        colorStyle = "color: #f87171; font-weight: 600;";
      } else if (/WARN/i.test(clean)) {
        colorStyle = "color: #fbbf24;";
      } else if (/INFO/i.test(clean)) {
        colorStyle = "color: #38bdf8;";
      } else if (/DEBUG/i.test(clean)) {
        colorStyle = "color: #64748b;";
      }

      const timeHtml = l.time ? `<span class="vpn-log-time">[${esc(l.time)}]</span>` : "";
      return `<div class="vpn-log-row">${timeHtml}<span class="vpn-log-text" style="${colorStyle}">${esc(clean)}</span></div>`;
    }).join("");

    container.innerHTML = rows;
    if (isAtBottom) {
      container.scrollTop = container.scrollHeight;
    }
  } catch (_) {}
}

window.connectVpn = connectVpn;
window.disconnectVpn = disconnectVpn;
window.pingVpnNode = pingVpnNode;
window.deleteVpnNode = deleteVpnNode;
window.editVpnNode = editVpnNode;
window.setVpnRouting = setVpnRouting;



/* ---------------- ambient particle layer ---------------- */

let currentParticleStyle = "mesh";
let particleRaf = null;

function applyParticleStyle(styleName) {
  currentParticleStyle = styleName || "mesh";
  try { localStorage.setItem("quota_particles", currentParticleStyle); } catch (_) {}
  document.querySelectorAll(".particle-option").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.particleStyle === currentParticleStyle);
  });
  if (window.__reinitParticles) {
    window.__reinitParticles();
  }
}

// Multi-mode dynamic ambient particles (Mesh, Dust, Digital Rain, Floating Orbs, Disabled).
// DPR-aware, pauses when hidden, respects reduced motion or user toggle.
function initParticles() {
  const canvas = document.getElementById("bg-particles");
  if (!canvas) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    currentParticleStyle = "off";
  } else {
    try {
      const saved = localStorage.getItem("quota_particles");
      if (saved) currentParticleStyle = saved;
    } catch (_) {}
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let w = 0, h = 0, dpr = 1;
  const particles = [];
  const COUNT = 45;

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function getThemeColor() {
    const curTheme = document.documentElement.getAttribute("data-theme") || "default";
    switch (curTheme) {
      case "emerald": return { r: 16, g: 185, b: 129 };
      case "violet": return { r: 168, g: 85, b: 247 };
      case "crimson": return { r: 244, g: 63, b: 94 };
      case "amber": return { r: 245, g: 158, b: 11 };
      case "nord": return { r: 45, g: 212, b: 191 };
      default: return { r: 56, g: 189, b: 248 };
    }
  }

  function seed() {
    particles.length = 0;
    if (currentParticleStyle === "off") return;

    if (currentParticleStyle === "rain") {
      // Digital cyber rain streams
      const rainCount = Math.floor(w / 35);
      for (let i = 0; i < rainCount; i++) {
        particles.push({
          x: Math.random() * w,
          y: Math.random() * h,
          len: Math.random() * 24 + 14,
          speed: Math.random() * 3.5 + 2.5,
          a: Math.random() * 0.35 + 0.25,
        });
      }
    } else if (currentParticleStyle === "orbs") {
      // Soft large floating bokeh orbs
      for (let i = 0; i < 18; i++) {
        particles.push({
          x: Math.random() * w,
          y: Math.random() * h,
          r: Math.random() * 28 + 14,
          vx: (Math.random() - 0.5) * 0.35,
          vy: (Math.random() - 0.5) * 0.35,
          a: Math.random() * 0.08 + 0.05,
          tw: Math.random() * Math.PI * 2,
        });
      }
    } else {
      // Mesh or Cosmic Dust
      const count = currentParticleStyle === "mesh" ? 42 : COUNT;
      for (let i = 0; i < count; i++) {
        particles.push({
          x: Math.random() * w,
          y: Math.random() * h,
          r: currentParticleStyle === "mesh" ? (Math.random() * 1.4 + 1.2) : (Math.random() * 2 + 1.2),
          vx: (Math.random() - 0.5) * 0.5,
          vy: currentParticleStyle === "dust" ? -(Math.random() * 0.5 + 0.3) : (Math.random() - 0.5) * 0.45,
          a: Math.random() * 0.2 + 0.35,
          tw: Math.random() * Math.PI * 2,
        });
      }
    }
  }

  function tick() {
    ctx.clearRect(0, 0, w, h);
    if (currentParticleStyle === "off") return;

    const rgb = getThemeColor();
    const colorStr = `${rgb.r}, ${rgb.g}, ${rgb.b}`;

    if (currentParticleStyle === "rain") {
      for (const p of particles) {
        p.y += p.speed;
        if (p.y > h + 30) {
          p.y = -30;
          p.x = Math.random() * w;
        }
        ctx.beginPath();
        const grad = ctx.createLinearGradient(p.x, p.y - p.len, p.x, p.y);
        grad.addColorStop(0, `rgba(${colorStr}, 0)`);
        grad.addColorStop(1, `rgba(${colorStr}, ${p.a.toFixed(3)})`);
        ctx.strokeStyle = grad;
        ctx.lineWidth = 1.6;
        ctx.moveTo(p.x, p.y - p.len);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();

        ctx.fillStyle = `rgba(${colorStr}, ${(p.a * 1.3).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 1.2, 0, Math.PI * 2);
        ctx.fill();
      }
    } else if (currentParticleStyle === "orbs") {
      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        p.tw += 0.01;
        if (p.x < -p.r) p.x = w + p.r;
        if (p.x > w + p.r) p.x = -p.r;
        if (p.y < -p.r) p.y = h + p.r;
        if (p.y > h + p.r) p.y = -p.r;

        const pulseAlpha = p.a * (0.8 + 0.2 * Math.sin(p.tw));
        const radGrad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
        radGrad.addColorStop(0, `rgba(${colorStr}, ${pulseAlpha.toFixed(3)})`);
        radGrad.addColorStop(1, `rgba(${colorStr}, 0)`);
        ctx.fillStyle = radGrad;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
    } else {
      // Mesh or Dust
      ctx.shadowColor = `rgba(${colorStr}, 0.7)`;
      ctx.shadowBlur = 8;
      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        p.x += p.vx;
        p.y += p.vy;
        p.tw += 0.01;
        if (p.y < -8) { p.y = h + 8; p.x = Math.random() * w; }
        if (p.y > h + 8) { p.y = -8; p.x = Math.random() * w; }
        if (p.x < -8) p.x = w + 8;
        if (p.x > w + 8) p.x = -8;

        const alpha = p.a * (0.7 + 0.3 * Math.sin(p.tw));
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${colorStr}, ${alpha.toFixed(3)})`;
        ctx.fill();

        // Connected constellation network mesh lines
        if (currentParticleStyle === "mesh") {
          for (let j = i + 1; j < particles.length; j++) {
            const p2 = particles[j];
            const dx = p.x - p2.x;
            const dy = p.y - p2.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < 130) {
              const lineAlpha = (1 - dist / 130) * 0.18;
              ctx.beginPath();
              ctx.moveTo(p.x, p.y);
              ctx.lineTo(p2.x, p2.y);
              ctx.strokeStyle = `rgba(${colorStr}, ${lineAlpha.toFixed(3)})`;
              ctx.lineWidth = 0.9;
              ctx.stroke();
            }
          }
        }
      }
      ctx.shadowBlur = 0;
    }

    particleRaf = requestAnimationFrame(tick);
  }

  function start() {
    if (currentParticleStyle === "off") {
      ctx.clearRect(0, 0, w, h);
      return;
    }
    if (!particleRaf) particleRaf = requestAnimationFrame(tick);
  }

  function stop() {
    if (particleRaf) {
      cancelAnimationFrame(particleRaf);
      particleRaf = null;
    }
  }

  window.__reinitParticles = () => {
    stop();
    ctx.clearRect(0, 0, w, h);
    if (currentParticleStyle !== "off") {
      seed();
      start();
    }
  };

  resize();
  seed();
  start();

  window.addEventListener("resize", () => { resize(); seed(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else start();
  });
}

/* ---------------- Theme Engine ---------------- */
function applyTheme(name) {
  if (!name || name === "default") {
    document.documentElement.removeAttribute("data-theme");
    try { localStorage.setItem("quota_theme", "default"); } catch (_) {}
  } else {
    document.documentElement.setAttribute("data-theme", name);
    try { localStorage.setItem("quota_theme", name); } catch (_) {}
  }
  document.querySelectorAll(".theme-option").forEach((opt) => {
    const isCur = (opt.dataset.setTheme || "default") === (name || "default");
    opt.classList.toggle("active", isCur);
  });
}

/* ---------------- Gauge Style Engine ---------------- */
let currentGaugeStyle = "needle";

function applyGaugeStyle(style) {
  currentGaugeStyle = style || "needle";
  try { localStorage.setItem("quota_gauge_style", currentGaugeStyle); } catch (_) {}
  const bundle = $("sidebar-bundle");
  if (bundle) {
    bundle.classList.remove("gauge-needle", "gauge-segmented", "gauge-minimal", "gauge-cyber", "gauge-racing", "gauge-plasma");
    bundle.classList.add(`gauge-${currentGaugeStyle}`);
  }
  document.querySelectorAll(".gauge-option").forEach((opt) => {
    opt.classList.toggle("active", opt.dataset.gaugeStyle === currentGaugeStyle);
  });
  if (lastBundleState && lastBundleState.b) {
    renderBundle(lastBundleState.b, lastBundleState.devices, lastBundleState.users);
  }
}

// Early gauge and theme restore on script load to prevent flash of unstyled elements
try {
  const _savedTheme = localStorage.getItem("quota_theme");
  if (_savedTheme && _savedTheme !== "default") {
    document.documentElement.setAttribute("data-theme", _savedTheme);
  }
} catch (_) {}

try {
  const _savedGauge = localStorage.getItem("quota_gauge_style") || "needle";
  document.addEventListener("DOMContentLoaded", () => {
    const bundle = $("sidebar-bundle");
    if (bundle) {
      bundle.classList.add(`gauge-${_savedGauge}`);
    }
  });
} catch (_) {}

/* ---------------- init ---------------- */

async function init() {
  initParticles();
  $("login-form").addEventListener("submit", submitLogin);
  $("device-form").addEventListener("submit", submitDevice);
  $("settings-form").addEventListener("submit", submitSettings);
  $("set-total").addEventListener("input", () => { settingsDirty = true; });
  $("set-reset-day").addEventListener("input", () => { settingsDirty = true; });
  $("set-period-type").addEventListener("change", () => {
    settingsDirty = true;
    updateResetDayAvailability();
  });
  $("setup-period-type").addEventListener("change", updateResetDayAvailability);
  $("add-user-btn").addEventListener("click", () => openUserModal(null));

    const layoutToggle = $("layout-toggle");
    if (layoutToggle) {
      const savedLayout = localStorage.getItem("qm-device-layout") || "grid";
      layoutToggle.value = savedLayout;
      const deviceList = $("devices-list");
      deviceList.className = `device-grid layout-${savedLayout}`;
      
      layoutToggle.addEventListener("change", (e) => {
        const val = e.target.value;
        localStorage.setItem("qm-device-layout", val);
        deviceList.className = `device-grid layout-${val}`;
      });
    }
  $("add-device-btn").addEventListener("click", () => openDeviceModal(null));
  $("modal-cancel").addEventListener("click", closeModal);
  $("user-modal-cancel").addEventListener("click", closeUserModal);
  $("user-form").addEventListener("submit", submitUser);
  $("u-mode").addEventListener("change", () => {
    $("u-fixed-wrap").classList.toggle("hidden", $("u-mode").value !== "fixed");
  });
  $("d-user").addEventListener("change", refreshDeviceModalFields);
  $("logout-btn").addEventListener("click", logout);
  // privacy eye: mask MACs + hide the saved PPPoE credentials prefill
  setPrivacyButton();
  $("privacy-eye").addEventListener("click", togglePrivacy);
  $("reset-month-btn").addEventListener("click", doResetMonth);
  $("recharge-btn").addEventListener("click", submitRecharge);
  if ($("recharge-target-type")) $("recharge-target-type").addEventListener("change", updateRechargeTargetOptions);
  window.deleteRechargePack = deleteRechargePack;
  document.querySelectorAll(".nav-tab").forEach((b) =>
    b.addEventListener("click", () => switchPanel(b.dataset.panel)));
  $("guest-mode-toggle").addEventListener("change", toggleGuestMode);
  $("guest-quota-btn").addEventListener("click", submitGuestQuota);
  if ($("guest-limit-btn")) $("guest-limit-btn").addEventListener("click", submitGuestLimit);
  $("guest-speed-btn").addEventListener("click", submitGuestSpeed);
  $("stop-new-toggle").addEventListener("change", toggleStopNew);
  $("decline-random-toggle").addEventListener("change", toggleDeclineRandom);
  $("decline-random-existing").addEventListener("change", cutExistingRandomMacs);
  $("mac-lists-btn").addEventListener("click", submitMacLists);
  $("mac-allow-list").addEventListener("input", () => { macListsDirty = true; });
  $("mac-deny-list").addEventListener("input", () => { macListsDirty = true; });
  $("sl-device-select").addEventListener("change", (ev) => {
    const val = ev.target.value;
    if (val) {
      $("sl-mac").value = val;
      const dev = (dashboard && dashboard.devices || []).find((d) => d.mac.toLowerCase() === val.toLowerCase());
      if (dev && dev.name && !$("sl-hostname").value) {
        $("sl-hostname").value = dev.name;
      }
    }
  });
  $("static-lease-form").addEventListener("submit", submitStaticLease);
  // Recharge modal open/close
  const openRechargeBtn = $("open-recharge-modal-btn");
  if (openRechargeBtn) openRechargeBtn.addEventListener("click", () => $("recharge-modal").classList.remove("hidden"));
  const rechargeModalCancel = $("recharge-modal-cancel");
  if (rechargeModalCancel) rechargeModalCancel.addEventListener("click", () => $("recharge-modal").classList.add("hidden"));
  if ($("recharge-modal")) $("recharge-modal").addEventListener("click", (ev) => { if (ev.target === $("recharge-modal")) $("recharge-modal").classList.add("hidden"); });
  const rechargeTbody = $("recharges-queue-tbody");
  if (rechargeTbody) {
    rechargeTbody.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-pack-del]");
      if (btn && btn.dataset.packDel) deleteRechargePack(+btn.dataset.packDel);
    });
  }
  // MAC rule modal open/close
  const openMacRuleBtn = $("open-mac-rule-btn");
  if (openMacRuleBtn) openMacRuleBtn.addEventListener("click", openMacRuleModal);
  const macRuleCancel = $("mac-rule-modal-cancel");
  if (macRuleCancel) macRuleCancel.addEventListener("click", closeMacRuleModal);
  if ($("mac-rule-modal")) $("mac-rule-modal").addEventListener("click", (ev) => { if (ev.target === $("mac-rule-modal")) closeMacRuleModal(); });
  const macRuleForm = $("mac-rule-form");
  if (macRuleForm) macRuleForm.addEventListener("submit", submitNewMacRule);
  const macRuleDevSelect = $("mac-rule-dev-select");
  if (macRuleDevSelect) macRuleDevSelect.addEventListener("change", (ev) => {
    const val = ev.target.value;
    if (val && $("mac-rule-macs")) {
      const cur = $("mac-rule-macs").value.trim();
      $("mac-rule-macs").value = cur ? cur + "\n" + val : val;
    }
  });
  const macTbody = $("mac-rules-tbody");
  if (macTbody) {
    macTbody.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-mac-rule-del]");
      if (btn && btn.dataset.macRuleDel) removeMacRule(btn.dataset.macRuleDel, btn.dataset.macRuleType);
    });
  }
  // Static lease modal open/close
  const openStaticLeaseBtn = $("open-static-lease-btn");
  if (openStaticLeaseBtn) openStaticLeaseBtn.addEventListener("click", () => {
    populateStaticLeaseDeviceSelect();
    $("static-lease-modal").classList.remove("hidden");
  });
  const slModalCancel = $("sl-modal-cancel");
  if (slModalCancel) slModalCancel.addEventListener("click", () => $("static-lease-modal").classList.add("hidden"));
  if ($("static-lease-modal")) $("static-lease-modal").addEventListener("click", (ev) => { if (ev.target === $("static-lease-modal")) $("static-lease-modal").classList.add("hidden"); });
  const slTbody = $("static-leases-tbody");
  if (slTbody) {
    slTbody.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-lease-del]");
      if (btn && btn.dataset.leaseDel) deleteStaticLease(btn.dataset.leaseDel);
    });
  }
  // speed shaping: saving sends all four fields; the master toggle just
  // marks the current draft — it takes effect together on Save.
  $("shaping-save-btn").addEventListener("click", submitNetwork);
  const vpnTgl = $("vpn-toggle");
  if (vpnTgl) vpnTgl.addEventListener("change", toggleVpnShare);
  // WAN mode: the toggle picks the desired mode; Apply/Revert do the live
  // switch (the gateway rewires itself and restarts automatically). A flip is
  // a DRAFT until Apply/Revert succeeds — wanToggleDirty freezes the 5 s WS
  // render so it can't clobber the pending change (or the creds panel).
  $("wan-toggle").addEventListener("change", (ev) => {
    wanToggleDirty = true;
    const creds = $("wan-creds");
    if (creds) creds.classList.toggle("hidden", !ev.target.checked);
    renderWan(wanStatus || {});
  });
  $("wan-test-btn").addEventListener("click", testPppoe);
  $("wan-apply-btn").addEventListener("click", submitWan);
  $("wan-revert-btn").addEventListener("click", revertWan);
  $("wan-restart-btn").addEventListener("click", renewWanIp);
  $("wan-renew-save").addEventListener("click", submitWanRenew);
  const wanTgSave = $("wan-tg-save-btn");
  if (wanTgSave) wanTgSave.addEventListener("click", saveWanTelegram);
  const wanTgTest = $("wan-tg-test-btn");
  if (wanTgTest) wanTgTest.addEventListener("click", testWanTelegram);
  const wanTgFwLink = $("wan-tg-fw-link");
  if (wanTgFwLink) {
    wanTgFwLink.addEventListener("click", (e) => {
      e.preventDefault();
      switchPanel("firewall");
    });
  }
  const wanTgToggle = $("wan-tg-toggle");
  if (wanTgToggle) wanTgToggle.addEventListener("change", saveWanTelegram);
  $("fw-apply").addEventListener("click", fwApply);
  $("fw-revert").addEventListener("click", fwRevert);
  $("fw-enforce-https").addEventListener("click", fwEnforceHttps);
  $("fw-remove-https").addEventListener("click", () => $("fw-https-modal").classList.remove("hidden"));
  $("fw-https-modal-cancel").addEventListener("click", () => $("fw-https-modal").classList.add("hidden"));
  $("fw-https-modal-confirm").addEventListener("click", fwRemoveHttps);
  $("fw-https-modal").addEventListener("click", (ev) => {
    if (ev.target === $("fw-https-modal")) $("fw-https-modal").classList.add("hidden");
  });
  $("fw-ban-btn").addEventListener("click", fwBan);
  $("fw-ban-ip").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") fwBan();
  });
  $("fw-rule-add").addEventListener("click", fwAddRule);
  $("fw-fwd-add").addEventListener("click", fwAddForward);
  const fwRulesEl = $("fw-rules");
  if (fwRulesEl) {
    fwRulesEl.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-fw-act]");
      if (!btn) return;
      const idx = +btn.dataset.idx;
      if (btn.dataset.fwAct === "edit-rule") fwEditRule(idx);
      else if (btn.dataset.fwAct === "del-rule") fwDeleteRule(idx);
    });
  }
  const fwServicesEl = $("fw-services");
  if (fwServicesEl) {
    fwServicesEl.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-fw-act='del-service']");
      if (btn) fwDeleteService(+btn.dataset.idx);
    });
  }
  const fwForwardsEl = $("fw-forwards");
  if (fwForwardsEl) {
    fwForwardsEl.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-fw-act]");
      if (!btn) return;
      const idx = +btn.dataset.idx;
      if (btn.dataset.fwAct === "edit-fwd") fwEditForward(idx);
      else if (btn.dataset.fwAct === "del-fwd") fwDeleteForward(idx);
    });
  }
  const fwBansEl = $("fw-bans");
  if (fwBansEl) {
    fwBansEl.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-fw-unban]");
      if (btn && btn.dataset.fwUnban) fwUnban(btn.dataset.fwUnban);
    });
  }
  // firewall rule modal
  $("fw-rm-save").addEventListener("click", _saveRuleModal);
  $("fw-rm-cancel").addEventListener("click", () => $("fw-rule-modal").classList.add("hidden"));
  $("fw-rule-modal").addEventListener("click", (ev) => {
    if (ev.target === $("fw-rule-modal")) $("fw-rule-modal").classList.add("hidden");
  });
  // firewall forward modal
  $("fw-fm-save").addEventListener("click", _saveFwdModal);
  $("fw-fm-cancel").addEventListener("click", () => $("fw-fwd-modal").classList.add("hidden"));
  $("fw-fwd-modal").addEventListener("click", (ev) => {
    if (ev.target === $("fw-fwd-modal")) $("fw-fwd-modal").classList.add("hidden");
  });
  // notification bell
  $("notif-bell").addEventListener("click", (ev) => {
    ev.stopPropagation();
    $("notif-dropdown").classList.toggle("hidden");
  });
  $("notif-clear").addEventListener("click", _notifDismissAll);
  document.addEventListener("click", (ev) => {
    const wrap = $("notif-bell-wrap");
    if (wrap && !wrap.contains(ev.target)) $("notif-dropdown").classList.add("hidden");
  });
  // software updates (Admin tab + the notification banner)
  $("upd-check").addEventListener("click", async () => {
    renderUpdate({ ...(dashboard && dashboard.update), checking: true });
    let u;
    try { u = await API.post("/api/updates/check"); }
    catch (e) { alert(e.message); return; }
    if (dashboard) dashboard.update = u;
    renderUpdate(u);
  });
  $("upd-install").addEventListener("click", async () => {
    if (!confirm("Download and install the update now? The gateway restarts into the new version.")) return;
    $("upd-status").textContent = "Installing — the gateway will restart…";
    $("upd-install").disabled = true;
    // the install stops the service (package prerm), so the response may never
    // arrive — that is the expected success path
    try { await API.post("/api/updates/install"); } catch (_) { /* restarting */ }
  });
  $("upd-details").addEventListener("click", openChangelog);
  $("update-banner-details").addEventListener("click", openChangelog);
  $("update-banner-dismiss").addEventListener("click", () => {
    const u = dashboard && dashboard.update;
    if (u && u.latest_version) localStorage.setItem("quota_update_banner", u.latest_version);
    $("update-banner").classList.add("hidden");
  });
  $("security-banner-dismiss").addEventListener("click", () => {
    localStorage.setItem("quota_sec_banner_dismissed",
                         $("security-banner-text").textContent);
    $("security-banner").classList.add("hidden");
  });
  $("upd-enabled").addEventListener("change", async (ev) => {
    try {
      const u = await API.post("/api/updates", { enabled: ev.target.checked });
      if (dashboard) dashboard.update = u;
      renderUpdate(u);
    } catch (e) { alert(e.message); ev.target.checked = !ev.target.checked; }
  });
  $("upd-auto").addEventListener("change", async (ev) => {
    try {
      const u = await API.post("/api/updates", { auto_install: ev.target.checked });
      if (dashboard) dashboard.update = u;
      renderUpdate(u);
    } catch (e) { alert(e.message); ev.target.checked = !ev.target.checked; }
  });
  $("changelog-close").addEventListener("click", () => $("changelog-modal").classList.add("hidden"));
  $("changelog-modal").addEventListener("click", (ev) => {
    if (ev.target === $("changelog-modal")) $("changelog-modal").classList.add("hidden");
  });
  // browsing history: refetch on device/window change or manual refresh
  $("hist-device").addEventListener("change", refreshHistory);
  $("hist-window").addEventListener("change", refreshHistory);
  $("hist-refresh").addEventListener("click", refreshHistory);

  // browsing history view tabs
  document.querySelectorAll(".hist-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchHistoryView(btn.dataset.view));
  });
  const histTop = $("hist-top");
  if (histTop) {
    histTop.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-dns-quick]");
      if (!btn) return;
      const domain = btn.dataset.dnsDomain;
      const action = btn.dataset.dnsQuick;
      const scope = btn.dataset.dnsScope;
      const devId = btn.dataset.dnsDevid ? +btn.dataset.dnsDevid : null;
      quickDnsRule(domain, action, scope, devId);
    });
  }

  // VPN subsystem event listeners
  const vpnToggleBtn = $("vpn-toggle-btn");
  if (vpnToggleBtn) {
    vpnToggleBtn.addEventListener("click", () => {
      if (vpnStatusCache && vpnStatusCache.state === "connected") {
        disconnectVpn();
      } else if (vpnStatusCache && vpnStatusCache.active_node_id) {
        connectVpn(vpnStatusCache.active_node_id);
      } else if (vpnNodesCache && vpnNodesCache.length) {
        connectVpn(vpnNodesCache[0].id);
      } else {
        const modal = $("vpn-node-modal");
        if (modal) modal.classList.remove("hidden");
      }
    });
  }
  const vpnAddBtn = $("vpn-add-node-btn");
  if (vpnAddBtn) vpnAddBtn.addEventListener("click", () => {
    editingVpnNodeId = null;
    const title = $("vpn-node-modal-title");
    if (title) title.textContent = "Add VPN Node";
    const subBtn = $("vpn-node-submit");
    if (subBtn) subBtn.textContent = "Save & Verify Node";
    const errEl = $("vpn-node-error");
    if (errEl) errEl.classList.add("hidden");
    const form = $("vpn-node-form");
    if (form) form.reset();
    const modal = $("vpn-node-modal");
    if (modal) modal.classList.remove("hidden");
  });
  const vpnCancelBtn = $("vpn-node-cancel");
  if (vpnCancelBtn) vpnCancelBtn.addEventListener("click", () => {
    const modal = $("vpn-node-modal");
    if (modal) modal.classList.add("hidden");
  });
  const vpnForm = $("vpn-node-form");
  if (vpnForm) {
    vpnForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = $("vpn-node-error");
      if (errEl) errEl.classList.add("hidden");
      const name = $("vpn-node-name").value.trim();
      const raw = $("vpn-node-raw").value.trim();
      try {
        await API.post("/api/vpn/nodes", { name: name || null, raw });
        $("vpn-node-modal").classList.add("hidden");
        await loadVpnNodes();
        await loadVpnStatus();
      } catch (err) {
        if (errEl) {
          errEl.textContent = err.message || "Failed to add node";
          errEl.classList.remove("hidden");
        }
      }
    });
  }
  const veForm = $("vpn-edit-proxy-form");
  if (veForm) {
    veForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = $("ve-error");
      if (errEl) errEl.classList.add("hidden");
      if (!editingVpnNodeId) return;

      const payload = {
        name: $("ve-name").value.trim() || null,
        server: $("ve-server").value.trim(),
        server_port: parseInt($("ve-port").value) || 443,
        uuid: $("ve-uuid").value.trim(),
        sni: $("ve-sni").value.trim() || null,
        flow: $("ve-flow").value.trim() || null,
        pbk: $("ve-pbk").value.trim() || null,
        sid: $("ve-sid").value.trim() || null,
        transport: $("ve-transport").value,
        path: $("ve-path").value.trim() || null,
      };

      try {
        await API.patch(`/api/vpn/nodes/${editingVpnNodeId}`, payload);
        $("vpn-edit-proxy-modal").classList.add("hidden");
        await loadVpnNodes();
        await loadVpnStatus();
      } catch (err) {
        if (errEl) {
          errEl.textContent = err.message || "Failed to update proxy configuration";
          errEl.classList.remove("hidden");
        }
      }
    });
  }
  const veCloseBtn = $("ve-close-btn");
  if (veCloseBtn) veCloseBtn.addEventListener("click", () => {
    $("vpn-edit-proxy-modal").classList.add("hidden");
  });
  const veCancelBtn = $("ve-cancel");
  if (veCancelBtn) veCancelBtn.addEventListener("click", () => {
    $("vpn-edit-proxy-modal").classList.add("hidden");
  });
  const vpnInsecureSw = $("vpn-allow-insecure-switch");
  if (vpnInsecureSw) {
    vpnInsecureSw.addEventListener("change", async (ev) => {
      try {
        await API.post("/api/vpn/settings", { allow_insecure: ev.target.checked });
        await loadVpnStatus();
      } catch (e) {
        alert("Failed to update VPN settings: " + e.message);
        ev.target.checked = !ev.target.checked;
      }
    });
  }
  const vpnLogRefresh = $("vpn-log-refresh");
  if (vpnLogRefresh) vpnLogRefresh.addEventListener("click", loadVpnLogs);
  const vpnLogClear = $("vpn-log-clear");
  if (vpnLogClear) vpnLogClear.addEventListener("click", () => {
    const pre = $("vpn-logs-view");
    if (pre) pre.innerHTML = '<div class="muted small" style="padding: 12px 0;">(view cleared)</div>';
  });
  const vpnRouteAll = $("vpn-route-all-btn");
  if (vpnRouteAll) {
    vpnRouteAll.addEventListener("click", async () => {
      for (const u of vpnRoutingCache.users || []) {
        await API.post("/api/vpn/routing", { target_type: "user", target_id: u.id, route_vpn: true }).catch(() => {});
      }
      for (const d of vpnRoutingCache.devices || []) {
        await API.post("/api/vpn/routing", { target_type: "device", target_id: d.id, route_vpn: true }).catch(() => {});
      }
      await loadVpnRouting();
    });
  }
  const vpnRouteNone = $("vpn-route-none-btn");
  if (vpnRouteNone) {
    vpnRouteNone.addEventListener("click", async () => {
      for (const u of vpnRoutingCache.users || []) {
        await API.post("/api/vpn/routing", { target_type: "user", target_id: u.id, route_vpn: false }).catch(() => {});
      }
      for (const d of vpnRoutingCache.devices || []) {
        await API.post("/api/vpn/routing", { target_type: "device", target_id: d.id, route_vpn: false }).catch(() => {});
      }
      await loadVpnRouting();
    });
  }
  const vpnSearch = $("vpn-routing-search");
  if (vpnSearch) vpnSearch.addEventListener("input", () => renderVpnRouting(vpnRoutingCache));
  const vpnTabGroup = $("vpn-routing-tab-group");
  if (vpnTabGroup) {
    vpnTabGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".vpn-tab-btn");
      if (!btn) return;
      vpnTabGroup.querySelectorAll(".vpn-tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentVpnRoutingTab = btn.dataset.vtab || "users";
      renderVpnRouting(vpnRoutingCache);
    });
  }
  const vpnNodesTbody = $("vpn-nodes-tbody");
  if (vpnNodesTbody) {
    vpnNodesTbody.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-vpn-act]");
      if (!btn) return;
      const act = btn.dataset.vpnAct;
      const id = btn.dataset.vpnId ? +btn.dataset.vpnId : null;
      if (act === "ping") pingVpnNode(id);
      else if (act === "disconnect") disconnectVpn();
      else if (act === "connect") connectVpn(id);
      else if (act === "edit") editVpnNode(id);
      else if (act === "delete") deleteVpnNode(id);
    });
  }
  const vpnRoutingList = $("vpn-routing-list");
  if (vpnRoutingList) {
    vpnRoutingList.addEventListener("change", (ev) => {
      const input = ev.target.closest("[data-vpn-route-type]");
      if (!input) return;
      const type = input.dataset.vpnRouteType;
      const id = +input.dataset.vpnRouteId;
      setVpnRouting(type, id, input.checked);
    });
  }

  const adblockSw = $("adblock-master-switch");
  if (adblockSw) {
    adblockSw.addEventListener("change", (ev) => toggleAdblockMaster(ev.target.checked));
  }
  document.querySelectorAll('input[name="adblock-tier"]').forEach((radio) => {
    radio.addEventListener("change", async () => {
      document.querySelectorAll(".adblock-tier-card").forEach((card) => {
        const r = card.querySelector('input[type="radio"]');
        card.classList.toggle("active", r && r.checked);
      });
      // Selecting a tier directly activates protection with that tier
      const sw = $("adblock-master-switch");
      if (sw) sw.checked = true;
      await toggleAdblockMaster(true);
    });
  });
  const adblockUpdateBtn = $("adblock-update-btn");
  if (adblockUpdateBtn) {
    adblockUpdateBtn.addEventListener("click", updateAdblockFeeds);
  }
  const adblockWhitelistBtn = $("adblock-whitelist-btn");
  if (adblockWhitelistBtn) {
    adblockWhitelistBtn.addEventListener("click", openAdblockWhitelist);
  }

  $("dns-rule-form").addEventListener("submit", submitDnsRule);
  $("dns-import-form").addEventListener("submit", submitDnsImport);
  $("dns-rule-scope").addEventListener("change", () =>
    populateDnsTargetSelect($("dns-rule-target"), $("dns-rule-scope")));
  $("dns-import-scope").addEventListener("change", () =>
    populateDnsTargetSelect($("dns-import-target"), $("dns-import-scope")));
  $("dns-rule-action").addEventListener("change", () =>
    $("dns-rule-target-ip").classList.toggle("hidden", $("dns-rule-action").value !== "redirect"));
  $("dns-presets-list").addEventListener("change", (ev) => {
    const id = ev.target.dataset && ev.target.dataset.presetToggle;
    if (id) togglePreset(id, ev.target.checked, ev.target);
  });
  $("dns-rules-list").addEventListener("click", async (ev) => {
    const id = ev.target.dataset && ev.target.dataset.delRule;
    if (!id) return;
    await API.del(`/api/dns/rules/${id}`);
    await refreshDns();
  });
  $("password-link").addEventListener("click", () => $("pwd-modal").classList.remove("hidden"));
  $("pwd-cancel").addEventListener("click", () => $("pwd-modal").classList.add("hidden"));
  $("pwd-form").addEventListener("submit", submitPassword);
  $("totp-link").addEventListener("click", openTotp);
  $("welcome-form").addEventListener("submit", submitWelcome);
  $("welcome-skip").addEventListener("click", () => {
    window.__welcomeSkipped = true;
    $("welcome-overlay").classList.add("hidden");
  });
  // logs toolbar: level filter + search + refresh + export (all client-side)
  $("log-refresh").addEventListener("click", refreshLogs);
  $("log-download").addEventListener("click", downloadLogs);
  $("log-search").addEventListener("input", (ev) => {
    logSearch = ev.target.value.trim();
    renderLogs();
  });
  $("log-filters").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".filter-btn");
    if (!btn) return;
    logFilter = btn.dataset.level;
    document.querySelectorAll(".filter-btn").forEach((b) => {
      const active = b === btn;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    });
    renderLogs();
  });

  // Themes & Appearance switcher
  const savedTheme = localStorage.getItem("quota_theme") || "default";
  applyTheme(savedTheme);
  const themeGrid = $("theme-grid");
  if (themeGrid) {
    themeGrid.addEventListener("click", (ev) => {
      const opt = ev.target.closest("[data-set-theme]");
      if (!opt) return;
      const theme = opt.dataset.setTheme;
      applyTheme(theme);
    });
  }

  // Particle Effects switcher
  const savedParticles = localStorage.getItem("quota_particles") || "mesh";
  applyParticleStyle(savedParticles);
  const particleGrid = $("particle-grid");
  if (particleGrid) {
    particleGrid.addEventListener("click", (ev) => {
      const opt = ev.target.closest("[data-particle-style]");
      if (!opt) return;
      const style = opt.dataset.particleStyle;
      applyParticleStyle(style);
    });
  }

  // Speedometer Gauge Style switcher (4 designs)
  const savedGauge = localStorage.getItem("quota_gauge_style") || "needle";
  applyGaugeStyle(savedGauge);
  const gaugeGrid = $("gauge-grid");
  if (gaugeGrid) {
    gaugeGrid.addEventListener("click", (ev) => {
      const opt = ev.target.closest("[data-gauge-style]");
      if (!opt) return;
      const style = opt.dataset.gaugeStyle;
      applyGaugeStyle(style);
    });
  }

  // Initial sliding nav indicator position
  const initialPanel = localStorage.getItem("quota_active_panel") || "management";
  setTimeout(() => updateNavIndicator(initialPanel), 50);
  window.addEventListener("resize", () => {
    const curActive = document.querySelector(".nav-tab.active");
    if (curActive) updateNavIndicator(curActive.dataset.panel);
  });

  $("d-mode").addEventListener("change", () => {
    $("d-fixed-wrap").classList.toggle("hidden", $("d-mode").value !== "fixed");
  });


  // event delegation for dynamic device/user buttons
  $("devices-list").addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.classList.contains("toggle-block")) doAction("toggle", +t.dataset.id);
    else if (t.classList.contains("toggle-user")) doUserAction("toggle", +t.dataset.uid);
  });
  $("devices-list").addEventListener("click", (ev) => {
    // accordion chevron: toggle the device list, persisted in expandedUsers so
    // it survives the 5s WS re-render.
    const acc = ev.target.closest("[data-acc]");
    if (acc) {
      const key = acc.dataset.acc;
      const card = acc.closest(".user-card");
      const devs = card && card.querySelector(".user-devices");
      if (!card || !devs) return;
      const open = expandedUsers.has(key);
      if (open) expandedUsers.delete(key);
      else expandedUsers.add(key);
      devs.classList.toggle("hidden", open);
      acc.classList.toggle("open", !open);
      acc.setAttribute("aria-expanded", String(!open));
      return;
    }
    const btn = ev.target.closest("[data-act],[data-ua]");
    if (!btn) return;
    if (btn.dataset.act) doAction(btn.dataset.act, +btn.dataset.id);
    else if (btn.dataset.ua) doUserAction(btn.dataset.ua, +btn.dataset.uid);
  });

  // auth check
  try {
    const me = await API.get("/api/me");
    if (me.authenticated) {
      showApp();
      await refreshAll();
      await refreshWan(); // prefill saved PPPoE creds on load (only /api/wan carries them)
      // remember the last visited sidebar tab across page reloads
      const savedPanel = localStorage.getItem("quota_active_panel");
      if (savedPanel && document.querySelector(`.nav-tab[data-panel="${savedPanel}"]`)) {
        switchPanel(savedPanel);
      }
      wsConnect();
      await showWelcomeIfNeeded();
    } else {
      showLogin();
    }
  } catch (_) {
    showLogin();
  }
}

document.addEventListener("DOMContentLoaded", init);
