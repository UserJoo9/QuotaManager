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

/* ---------------- API client ---------------- */

const API = {
  async req(method, path, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
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
let logLines = [];         // raw lines from /api/logs
let logMeta = null;        // {total, truncated} from the last /api/logs call
let logFilter = "ALL";     // active log level filter
let logSearch = "";        // current log search string

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
  renderBundle(data.bundle, data.devices, data.users);
  renderUsers(data.users, data.devices);
  renderRogue(data.rogue);
  renderWan(data.wan); // null-safe — {} before the first Gateway tick
  renderNetStatus(data.internet);
  renderNetworkPreview(networkConfig); // null-safe — refreshed by refreshNetwork()
  const v = $("app-version");
  if (v) v.textContent = data.version ? `Quota Manager ${data.version}` : "—";
}

function renderBundle(b, devices, users) {
  const usedPct = b.total_gb > 0 ? Math.min(100, (b.used_gb / b.total_gb) * 100) : 0;
  $("bundle-ring").style.setProperty("--p", usedPct.toFixed(1));
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

  // keep settings form in sync — but NEVER clobber an input the admin is
  // editing. A WS snapshot arrives every 5 s; the old per-field focus guard
  // only protected the ONE field that had focus, so typing the bundle size and
  // then moving to the reset-day field let the next snapshot revert it. Freeze
  // the whole settings section once the admin touches either field, and only
  // unfreeze after a successful save.
  if (!settingsDirty) {
    $("set-total").value = b.total_gb;
    $("set-reset-day").value = b.reset_day;
  }

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

function renderUsers(users, devices) {
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
  const parts = [];
  for (const u of users || []) {
    parts.push(userCard(u, byUser.get(u.id) || []));
  }
  // orphan devices (no user) — should not happen post-migration, but render
  // them so they stay controllable if the DB is mid-migration.
  const orphan = byUser.get(null) || byUser.get(undefined) || [];
  if (orphan.length) {
    parts.push(userCard(
      { id: null, name: "Unassigned devices", quota_mode: "auto",
        allowance_gb: 0, used_gb: 0, percent: 0, blocked: false,
        block_state: "ok" },
      orphan, true));
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
        <div class="rogue-mac">${esc(r.mac)}${vendorTag ? ` · <span class="device-vendor">${vendorTag}</span>` : ""}</div>
      </div>
    </div>`;
  }).join("");
}

function userCard(u, udevs, ghost) {
  ghost = ghost || u.id == null;
  const key = u.id == null ? "orphan" : String(u.id);
  const open = expandedUsers.has(key);
  const connected = (udevs || []).some((d) => d.connected);
  const actions = ghost ? "" : `
      <label class="switch" title="Cut / restore all of this user's devices">
        <input type="checkbox" class="toggle-user" data-uid="${u.id}" ${u.blocked ? "" : "checked"}>
        <span class="slider"></span>
      </label>
      <button class="icon-btn" data-ua="edit" data-uid="${u.id}" title="Edit user">✎</button>
      <button class="icon-btn danger" data-ua="delete" data-uid="${u.id}" title="Remove user + devices">🗑</button>`;
  const devHtml = udevs.map(deviceRow).join("");
  const guestTag = u.guest
    ? ` <span class="guest-tag" title="Guest account — auto-created, deleted on month reset">guest</span>` : "";
  // per-user aggregate speed caps (Mbps; shown only when one is set)
  const speedTag = (u.limit_down_mbps || u.limit_up_mbps)
    ? ` <span class="speed-tag" title="Total speed for all this user's devices">↓${u.limit_down_mbps || "∞"} ↑${u.limit_up_mbps || "∞"}</span>` : "";
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
        <div class="user-name">${statusDot(u.block_state, connected)}${esc(u.name || (u.guest ? "Guest" : "Unnamed user"))}${guestTag}${speedTag}${statusTag(u.block_state)}</div>
        <div class="user-sub">${udevs.length} device${udevs.length === 1 ? "" : "s"} ·
          ${u.quota_mode === "fixed" ? `Fixed ${u.fixed_gb ?? u.allowance_gb} GB` : "Auto (share of remainder)"}</div>
      </div>
    </div>
    <div class="device-bar">
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, u.percent || 0)}%"></div></div>
      <div class="device-numbers">
        <span>${fmt(u.used_gb)} of <b>${fmt(u.allowance_gb)}</b></span>
        <span>${(u.percent || 0).toFixed(1)}%</span>
      </div>
    </div>
    <div class="device-actions actions">${actions}</div>
    <div class="user-devices ${open ? "" : "hidden"}">
      ${devHtml || `<div class="empty small">No devices yet.</div>`}
    </div>
  </div>`;
}

function deviceRow(d) {
  const bypassTag = d.bypass
    ? `<span class="bypass-tag" title="Exempt from this user's quota block">bypass</span>` : "";
  // per-device internet speed caps (Mbps; shown only when one is set)
  const speedTag = (d.limit_down_mbps || d.limit_up_mbps)
    ? ` <span class="speed-tag" title="This device's speed limit">↓${d.limit_down_mbps || "∞"} ↑${d.limit_up_mbps || "∞"}</span>` : "";
  // guests are auto-created period-scoped accounts (deleted on month reset)
  const guestTag = d.guest
    ? `<span class="guest-tag" title="Guest account — auto-created, deleted on month reset">guest</span>` : "";
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
  return `
  <div class="device-row ${d.blocked ? "blocked" : ""}" data-id="${d.id}">
    <div class="device-head">
      <div>
        <div class="device-name">${esc(d.name || (d.guest ? "Guest" : d.vendor) || "Unnamed device")}${speedTag}</div>
        <div class="device-mac">${esc(d.mac)}${statusDot(d.block_state, d.connected)}${d.ip ? ` · <span class="device-ip">${esc(d.ip)}</span>` : ""}${vendorTag}${guestTag}${bypassTag}${statusTag(d.block_state)}</div>
      </div>
    </div>
    ${devBar}
    <div class="device-live">
      <span class="live-down">Download <b>${fmtBytes(d.live_down)}</b></span>
      <span class="live-up">Upload <b>${fmtBytes(d.live_up)}</b></span>
    </div>
    <div class="device-actions actions">
      <label class="switch" title="Toggle internet access">
        <input type="checkbox" class="toggle-block" data-id="${d.id}" ${d.blocked ? "" : "checked"}>
        <span class="slider"></span>
      </label>
      <button class="icon-btn" data-act="edit" data-id="${d.id}" title="Edit / top up">✎</button>
      <button class="icon-btn danger" data-act="delete" data-id="${d.id}" title="Remove">🗑</button>
    </div>
  </div>`;
}

/* ---------------- top-bar panels (management / bundle / admin / logs) ---------------- */

function switchPanel(name) {
  document.querySelectorAll(".nav-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.panel === name));
  document.querySelectorAll(".nav-panel").forEach((p) =>
    p.classList.toggle("hidden", p.id !== `panel-${name}`));
  if (name === "logs") refreshLogs();
  if (name === "network") refreshNetwork();
  if (name === "wan") refreshWan();
}

async function refreshLogs() {
  try {
    const data = await API.get("/api/logs?limit=300");
    logLines = data.lines;
    logMeta = data;
    renderLogs();
  } catch (_) { /* not critical — activity still works */ }
}

/* level filter + search are applied client-side to the raw /api/logs lines;
   the level is the 3rd whitespace token ("2026-08-06 12:00:00,123 INFO name: …") */
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

function wsConnect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    wsRetry = 0;
  };
  ws.onmessage = async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") render(msg.data);
    } catch (_) { /* ignore malformed */ }
  };
  ws.onclose = () => {
    ws = null;
    // fall back to polling while disconnected
    scheduleWsRetry();
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

function wsClose() {
  if (ws) { try { ws.close(); } catch (_) {} }
  ws = null;
  clearTimeout(wsTimer);
}

function scheduleWsRetry() {
  clearTimeout(wsTimer);
  wsTimer = setTimeout(wsConnect, Math.min(1000 * 2 ** wsRetry++, 15000));
}

/* ---------------- actions ---------------- */

async function doAction(act, id) {
  if (act === "toggle") {
    // switch reflects current state; we want the NEW value
    const checkbox = document.querySelector(`.toggle-block[data-id="${id}"]`);
    const blocked = !checkbox.checked;
    await API.patch(`/api/devices/${id}`, { block: blocked });
  } else if (act === "delete") {
    const dev = (dashboard.devices || []).find((d) => d.id === id);
    if (!confirm(`Remove ${dev && dev.name ? `“${dev.name}”` : "this device"}?`)) return;
    await API.del(`/api/devices/${id}`);
  } else if (act === "edit") {
    openDeviceModal(id);
    return;
  }
  await refreshAll();
}

async function doUserAction(act, uid) {
  if (act === "toggle") {
    const checkbox = document.querySelector(`.toggle-user[data-uid="${uid}"]`);
    const blocked = !checkbox.checked;
    await API.patch(`/api/users/${uid}`, { block: blocked });
  } else if (act === "delete") {
    const user = (dashboard.users || []).find((x) => x.id === uid);
    const names = ((user && user.devices) || [])
      .map((d) => `“${d.name || d.mac}”`).join(", ");
    const msg = `Remove ${user && user.name ? `“${user.name}”` : `user #${uid}`}` +
      `${names ? ` and their device(s): ${names}` : ""}? This also deletes their usage history.`;
    if (!confirm(msg)) return;
    await API.del(`/api/users/${uid}`);
  } else if (act === "edit") {
    openUserModal(uid);
    return;
  }
  await refreshAll();
}

async function refreshAll() {
  const data = await API.get("/api/dashboard");
  render(data);
  refreshGuest();
  refreshNetwork();
  if (!$("panel-logs").classList.contains("hidden")) refreshLogs();
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
    html += `<option value="u_${u.id}">${esc(u.name || `User #${u.id}`)}</option>`;
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
    ? `${esc(dev.mac)} — quota lives on the user; reassign or exempt here.`
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
  $("d-topup").value = "";
  // per-device speed caps (Mbps, 0 = unlimited)
  $("d-limit-down").value = dev ? (dev.limit_down_mbps || 0) : 0;
  $("d-limit-up").value = dev ? (dev.limit_up_mbps || 0) : 0;
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
    await API.post("/api/devices", body);
  } else {
    const patch = { name, limit_down_mbps: limitDown, limit_up_mbps: limitUp };
    if (userId != null && userId !== originalUserId) patch.user_id = userId;
    patch.bypass = $("d-bypass").checked;
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
  if (editUserId == null) {
    await API.post("/api/users", { name, quota_mode: mode, fixed_gb: fixed,
      limit_down_mbps: limitDown, limit_up_mbps: limitUp });
  } else {
    await API.patch(`/api/users/${editUserId}`, { name, quota_mode: mode, fixed_gb: fixed,
      limit_down_mbps: limitDown, limit_up_mbps: limitUp });
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
  $("setup-reset-day").value = state.reset_day;
  $("welcome-overlay").classList.remove("hidden");
}

async function submitWelcome(ev) {
  ev.preventDefault();
  const errEl = $("welcome-error");
  errEl.classList.add("hidden");
  const body = {
    total_gb: parseFloat($("setup-total").value),
    reset_day: parseInt($("setup-reset-day").value, 10),
    current_password: $("setup-cur-pw").value,
    new_password: $("setup-new-pw").value || null,
  };
  if (!(body.total_gb > 0)) { errEl.textContent = "Bundle size must be positive."; errEl.classList.remove("hidden"); return; }
  if (!(body.reset_day >= 0 && body.reset_day <= 28)) {
    errEl.textContent = "Reset day must be 0–28 (0 = never auto-reset).";
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
  try {
    await API.post("/api/login", { password: $("login-password").value });
    $("login-password").value = "";
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
  const resetDay = parseInt($("set-reset-day").value, 10);
  if (!(total > 0)) { alert("Bundle size must be positive."); return; }
  if (!(resetDay >= 0 && resetDay <= 28)) {
    alert("Reset day must be 0–28 (0 = never auto-reset; you recharge manually).");
    return;
  }
  await API.post("/api/bundle", { total_gb: total, reset_day: resetDay });
  settingsDirty = false;
  await refreshAll();
}

async function submitRecharge(ev) {
  ev.preventDefault();
  const addGb = parseFloat($("set-recharge").value);
  if (!(addGb > 0)) { alert("Enter how many GB were added to the bundle."); return; }
  if (!confirm(`Add ${addGb} GB to the bundle and recalculate every user's share?`)) return;
  await API.post("/api/bundle", { add_gb: addGb });
  $("set-recharge").value = "";
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

/* ---------------- speed shaping (Network tab) ---------------- */

async function refreshNetwork() {
  try {
    const n = await API.get("/api/network");
    networkConfig = n;
    $("shaping-toggle").checked = n.enabled;
    $("set-total-down").value = n.total_down_mbps || "";
    $("set-total-up").value = n.total_up_mbps || "";
    $("aqm-toggle").checked = n.aqm;
    renderNetworkPreview(n);
  } catch (_) { /* network panel is not critical */ }
}

function renderNetworkPreview(n) {
  if (!n || !$("np-status")) return;
  $("np-status").textContent = n.enabled ? "On" : "Off";
  $("np-status").className = `stat-value ${n.enabled ? "ok" : "off"}`;
  $("np-down").textContent = n.total_down_mbps ? `${n.total_down_mbps} Mbps` : "—";
  $("np-up").textContent = n.total_up_mbps ? `${n.total_up_mbps} Mbps` : "—";
  $("np-aqm").textContent = n.aqm ? "On" : "Off";
  const capped = (dashboard && dashboard.devices
    ? dashboard.devices : []).filter((d) => d.limit_down_mbps || d.limit_up_mbps);
  $("np-capped").textContent = capped.length;
  $("np-devices").innerHTML = capped.length
    ? capped.slice(0, 20).map((d) =>
        `<li><span>${esc(d.name || d.mac)}</span>` +
        `<span class="muted">↓${d.limit_down_mbps || "∞"} ↑${d.limit_up_mbps || "∞"}</span></li>`).join("")
    : `<li class="muted">No device caps set.</li>`;
}

async function submitNetwork() {
  const body = {
    enabled: $("shaping-toggle").checked,
    total_down_mbps: parseFloat($("set-total-down").value) || 0,
    total_up_mbps: parseFloat($("set-total-up").value) || 0,
    aqm: $("aqm-toggle").checked,
  };
  await API.post("/api/network", body);
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
    // mid-editing a draft — a dirty toggle keeps its typed values.
    const user = $("wan-user"), pass = $("wan-pass"), wanif = $("wan-if");
    if (user && !wanToggleDirty) user.value = w.pppoe_user || "";
    if (pass && !wanToggleDirty) pass.value = w.pppoe_password || "";
    if (wanif && !wanToggleDirty) wanif.value = w.wan_if || "";
    renderWan(w);
    await maybeAutoDiagnose(w);
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
    "link-down":
      "\n→ A PPPoE server was found but the session stalled — usually the modem/ISP " +
      "side. Wait a minute and Test again, or check the quota-wan-ppp service (the " +
      "real dial fails the same way).",
    "error":
      "\n→ The test could not run (missing pppd / wrong interface). Check the " +
      "quota-wan-ppp service and that the WAN interface above is the NIC that " +
      "reaches the ONT/modem.",
  }[st] || "";
  msg.textContent = `✗ PPPoE test failed — ${detail}${fix}`;
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

async function submitPassword(ev) {
  ev.preventDefault();
  const cur = $("p-cur").value;
  const next = $("p-new").value;
  if (next.length < 4) { alert("New password must be at least 4 characters."); return; }
  try {
    await API.post("/api/password", { current: cur, new: next });
    $("pwd-modal").classList.add("hidden");
    $("pwd-form").reset();
    alert("Password updated.");
  } catch (err) {
    // A 401 already showed the login screen (session expired) — don't double-alert.
    if (err.message === "unauthorized") return;
    alert(err.message === "current password incorrect" ? "Current password is wrong." : err.message);
  }
}

async function logout() {
  await API.post("/api/logout").catch(() => {});
  showLogin();
}

/* ---------------- init ---------------- */

async function init() {
  $("login-form").addEventListener("submit", submitLogin);
  $("device-form").addEventListener("submit", submitDevice);
  $("settings-form").addEventListener("submit", submitSettings);
  $("set-total").addEventListener("input", () => { settingsDirty = true; });
  $("set-reset-day").addEventListener("input", () => { settingsDirty = true; });
  $("add-user-btn").addEventListener("click", () => openUserModal(null));
  $("add-device-btn").addEventListener("click", () => openDeviceModal(null));
  $("modal-cancel").addEventListener("click", closeModal);
  $("user-modal-cancel").addEventListener("click", closeUserModal);
  $("user-form").addEventListener("submit", submitUser);
  $("u-mode").addEventListener("change", () => {
    $("u-fixed-wrap").classList.toggle("hidden", $("u-mode").value !== "fixed");
  });
  $("d-user").addEventListener("change", refreshDeviceModalFields);
  $("logout-btn").addEventListener("click", logout);
  $("reset-month-btn").addEventListener("click", doResetMonth);
  $("recharge-btn").addEventListener("click", submitRecharge);
  document.querySelectorAll(".nav-tab").forEach((b) =>
    b.addEventListener("click", () => switchPanel(b.dataset.panel)));
  $("guest-mode-toggle").addEventListener("change", toggleGuestMode);
  $("guest-quota-btn").addEventListener("click", submitGuestQuota);
  // speed shaping: saving sends all four fields; the master + AQM toggles just
  // mark the current draft — they take effect together on Save.
  $("shaping-save-btn").addEventListener("click", submitNetwork);
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
  $("password-link").addEventListener("click", () => $("pwd-modal").classList.remove("hidden"));
  $("pwd-cancel").addEventListener("click", () => $("pwd-modal").classList.add("hidden"));
  $("pwd-form").addEventListener("submit", submitPassword);
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
