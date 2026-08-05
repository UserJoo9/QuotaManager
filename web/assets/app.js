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
let editDeviceId = null;   // device being edited (null = add mode)
let chart = null;
let lastChartFetch = 0;
let settingsDirty = false; // admin typed in the bundle form — freeze its sync

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
  renderBundle(data.bundle, data.devices);
  renderDevices(data.devices);
}

function renderBundle(b, devices) {
  const usedPct = b.total_gb > 0 ? Math.min(100, (b.used_gb / b.total_gb) * 100) : 0;
  $("bundle-ring").style.setProperty("--p", usedPct.toFixed(1));
  $("bundle-used").textContent = fmt(b.used_gb);
  $("bundle-total").textContent = b.total_gb;
  $("bundle-remaining").textContent = fmt(b.remaining_gb);
  // reset_day=0 => period never rolls: show "→ manual" and "—" for days left.
  $("bundle-period").textContent =
    b.period_end ? `${b.period_start || "…"} → ${b.period_end}` : `${b.period_start || "…"} → manual`;
  $("bundle-days").textContent = b.days_left < 0 ? "—" : b.days_left;
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
}

function badge(state) {
  const map = {
    ok: ["ok", "Active"],
    quota: ["quota", "Quota exceeded"],
    admin_off: ["admin_off", "Blocked by admin"],
  };
  const [cls, label] = map[state] || map.ok;
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderDevices(devices) {
  const list = $("devices-list");
  if (!devices.length) {
    list.innerHTML = `<div class="empty">No devices yet. Add one below, or wait — a
      device that asks the router for an IP will appear here automatically.</div>`;
    return;
  }
  list.innerHTML = devices.map((d) => {
    const live = `⇣ ${fmtBytes(d.live_down)} ⇡ ${fmtBytes(d.live_up)}`;
    return `
    <div class="glass card device-card ${d.blocked ? "blocked" : ""}" data-id="${d.id}">
      <div class="device-head">
        <div>
          <div class="device-name">${esc(d.name || "Unnamed device")}</div>
          <div class="device-mac">${esc(d.mac)}${d.ip ? ` · <span class="device-ip">${esc(d.ip)}</span>` : ""}</div>
        </div>
        ${badge(d.block_state)}
      </div>
      <div class="device-bar">
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, d.percent || 0)}%"></div></div>
        <div class="device-numbers">
          <span>${fmt(d.used_gb)} of <b>${fmt(d.allowance_gb)}</b></span>
          <span>${(d.percent || 0).toFixed(1)}%</span>
        </div>
      </div>
      <div class="device-live">
        <span class="live-down">Download <b>${fmtBytes(d.live_down)}</b></span>
        <span class="live-up">Upload <b>${fmtBytes(d.live_up)}</b></span>
      </div>
      <div class="device-actions">
        <label class="switch" title="Toggle internet access">
          <input type="checkbox" class="toggle-block" data-id="${d.id}" ${d.blocked ? "" : "checked"}>
          <span class="slider"></span>
        </label>
        <button class="icon-btn" data-act="edit" data-id="${d.id}" title="Edit / top up">✎</button>
        <button class="icon-btn danger" data-act="delete" data-id="${d.id}" title="Remove">🗑</button>
      </div>
    </div>`;
  }).join("");
}

function renderEvents(events) {
  const ul = $("events-list");
  if (!events.length) {
    ul.innerHTML = `<li class="empty">No activity yet.</li>`;
    return;
  }
  ul.innerHTML = events.map((e) => {
    const t = new Date((e.ts || 0) * 1000);
    const time = t.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    return `<li><span class="ev-msg">${esc(e.message)}</span><span class="ev-time">${time}</span></li>`;
  }).join("");
}

/* ---------------- chart ---------------- */

function gb(n) { return (n || 0) / (1024 ** 3); }

async function refreshChart(force) {
  if (!force && Date.now() - lastChartFetch < 60_000) return;
  lastChartFetch = Date.now();
  const series = await API.get("/api/usage");
  if (!chart) {
    const ctx = $("usage-chart");
    chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: series.map((p) => p.date),
        datasets: [
          { label: "Download", data: series.map((p) => gb(p.down)), backgroundColor: "rgba(96, 165, 250, 0.75)", borderRadius: 4 },
          { label: "Upload", data: series.map((p) => gb(p.up)), backgroundColor: "rgba(244, 114, 182, 0.75)", borderRadius: 4 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { stacked: true, grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: "#a79bc4" } },
          y: { stacked: true, grid: { color: "rgba(255,255,255,0.05)" }, ticks: { color: "#a79bc4", callback: (v) => `${v} GB` } },
        },
      },
    });
  } else {
    chart.data.labels = series.map((p) => p.date);
    chart.data.datasets[0].data = series.map((p) => gb(p.down));
    chart.data.datasets[1].data = series.map((p) => gb(p.up));
    chart.update();
  }
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
    setConn(true);
  };
  ws.onmessage = async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") render(msg.data);
    } catch (_) { /* ignore malformed */ }
  };
  ws.onclose = () => {
    setConn(false);
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

function setConn(ok) {
  const pill = $("conn-status");
  pill.textContent = ok ? "● live" : "● offline — retrying";
  pill.className = `pill ${ok ? "live" : "off"}`;
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

async function refreshAll() {
  const data = await API.get("/api/dashboard");
  render(data);
  const evs = await API.get("/api/events?limit=30");
  renderEvents(evs);
  refreshChart(false).catch(() => {});
}

/* ---------------- device modal ---------------- */

function openDeviceModal(id) {
  editDeviceId = id;
  const dev = id != null ? (dashboard.devices || []).find((d) => d.id === id) : null;

  $("modal-title").textContent = dev ? "Edit device" : "Add device";
  $("modal-sub").textContent = dev
    ? `${esc(dev.mac)} — edit quota or top up.`
    : "New devices from DHCP appear automatically. Add one by MAC.";

  $("d-mac-wrap").classList.toggle("hidden", !!dev);
  $("d-mac").required = !dev;
  if (dev) {
    $("d-name").value = dev.name;
    $("d-mode").value = dev.quota_mode;
    // For AUTO devices the Fixed-GB input is hidden — an out-of-step value
    // there (e.g. 10 on a step=0.5 grid from min=0.1) makes the browser flag
    // the whole form "invalid form control is not focusable" and block Save.
    // Leave it empty: an empty hidden number input passes validation.
    $("d-fixed").value = dev.quota_mode === "fixed" ? (dev.allowance_gb || 10) : "";
    $("d-topup").value = "";
  } else {
    $("device-form").reset();
    $("d-mode").value = "auto";
  }
  $("d-fixed-wrap").classList.toggle("hidden", $("d-mode").value !== "fixed");
  $("d-topup-wrap").classList.toggle("hidden", !dev);
  $("modal-submit").textContent = dev ? "Save" : "Add";
  $("modal").classList.remove("hidden");
  if (!dev) $("d-mac").focus();
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
  const mode = $("d-mode").value;
  const fixed = mode === "fixed" ? Math.max(0.1, parseFloat($("d-fixed").value) || 0.1) : null;

  if (editDeviceId == null) {
    const mac = normalizeMac($("d-mac").value);
    if (!mac) { alert("Invalid MAC address."); return; }
    await API.post("/api/devices", { mac, name, quota_mode: mode, fixed_gb: fixed });
  } else {
    const patch = { name, quota_mode: mode, fixed_gb: fixed };
    const topupRaw = parseFloat($("d-topup").value);
    if (!Number.isNaN(topupRaw) && topupRaw > 0) {
      await API.post(`/api/devices/${editDeviceId}/topup`, { extra_gb: topupRaw });
    }
    await API.patch(`/api/devices/${editDeviceId}`, patch);
  }
  closeModal();
  await refreshAll();
}

/* ---------------- login / settings ---------------- */

async function submitLogin(ev) {
  ev.preventDefault();
  $("login-error").classList.add("hidden");
  try {
    await API.post("/api/login", { password: $("login-password").value });
    $("login-password").value = "";
    showApp();
    await refreshAll();
    wsConnect();
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
  if (!confirm(`Add ${addGb} GB to the bundle and recalculate every device's share?`)) return;
  await API.post("/api/bundle", { add_gb: addGb });
  $("set-recharge").value = "";
  await refreshAll();
}

async function doResetMonth() {
  if (!confirm("Start a new quota period now? All counters restart from today.")) return;
  await API.post("/api/reset-month");
  lastChartFetch = 0;
  await refreshAll();
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
  $("add-device-btn").addEventListener("click", () => openDeviceModal(null));
  $("modal-cancel").addEventListener("click", closeModal);
  $("logout-btn").addEventListener("click", logout);
  $("reset-month-btn").addEventListener("click", doResetMonth);
  $("recharge-btn").addEventListener("click", submitRecharge);
  $("password-link").addEventListener("click", () => $("pwd-modal").classList.remove("hidden"));
  $("pwd-cancel").addEventListener("click", () => $("pwd-modal").classList.add("hidden"));
  $("pwd-form").addEventListener("submit", submitPassword);

  $("d-mode").addEventListener("change", () => {
    $("d-fixed-wrap").classList.toggle("hidden", $("d-mode").value !== "fixed");
  });

  // event delegation for dynamic device buttons
  $("devices-list").addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.classList.contains("toggle-block")) doAction("toggle", +t.dataset.id);
  });
  $("devices-list").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    doAction(btn.dataset.act, +btn.dataset.id);
  });

  // auth check
  try {
    const me = await API.get("/api/me");
    if (me.authenticated) {
      showApp();
      await refreshAll();
      wsConnect();
    } else {
      showLogin();
    }
  } catch (_) {
    showLogin();
  }

  // periodic chart refresh regardless of WS activity
  setInterval(() => refreshChart(false).catch(() => {}), 60_000);
}

document.addEventListener("DOMContentLoaded", init);
