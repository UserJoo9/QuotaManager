import { h, icon, mount } from "./core/dom.js";
import { api, setUnauthorizedHandler } from "./core/api.js";
import { startLive } from "./core/ws.js";
import { t, setLang, getLang, num } from "./core/i18n.js";
import { applyPrefs, getPrefs, setPref, effTheme } from "./core/prefs.js";
import { startParticles } from "./core/particles.js";
import { startLiveShare } from "./core/live.js";
import { bar } from "./components/ui.js";
import { createStore } from "./core/store.js";
import { refreshDrawer, closeDrawer } from "./components/drawer.js";
import { privacyHidden, setPrivacyHidden } from "./core/i18n.js";
import { toast } from "./components/toast.js";
import { maybeWelcome } from "./components/welcome.js";
import overview from "./pages/overview.js";
import people from "./pages/people.js";
import system from "./pages/system.js";
import quotaPage from "./pages/quota.js";
import networkPage from "./pages/network.js";
import internetPage from "./pages/internet.js";
import vpnPage from "./pages/vpn.js";
import securityPage from "./pages/security.js";
import insightsPage from "./pages/insights.js";
import sharePage from "./pages/share.js";

const ROUTES = [
  { id: "overview", icon: "overview", group: "nav.main", page: overview },
  { id: "people", icon: "people", group: "nav.main", page: people, count: true },
  { id: "quota", icon: "quota", group: "nav.main", page: quotaPage },
  { id: "network", icon: "network", group: "nav.net", page: networkPage },
  { id: "internet", icon: "internet", group: "nav.net", page: internetPage },
  { id: "security", icon: "security", group: "nav.net", page: securityPage },
  { id: "vpn", icon: "vpn", group: "nav.net", page: vpnPage },
  { id: "insights", icon: "insights", group: "nav.tools", page: insightsPage },
  { id: "share", icon: "share", group: "nav.tools", page: sharePage },
  { id: "system", icon: "system", group: "nav.tools", page: system },
];
const store = createStore({ authed: null, dashboard: null, live: false, route: "overview", navOpen: false });
let stopLive = null, offTimer = null;

const routeId = () => { const id = location.hash.replace(/^#\/?/, ""); return ROUTES.some((r) => r.id === id) ? id : "overview"; };
const go = (id) => { location.hash = "#/" + id; };
const refresh = async () => { try { store.set({ dashboard: await api.get("/api/dashboard") }); } catch { /* 401 handled */ } };

let loginPw = "";   // password kept in memory between the password and the 2FA step
let loginBusy = false;   // one login request at a time, across every re-render
let loginEpoch = 0;      // a response from an older screen must never redraw a newer one
function renderLogin(step = "password", error = "") {
  const epoch = ++loginEpoch;
  const pw = h("input", { class: "input", type: "password", autocomplete: "current-password", "aria-label": t("login.password"), placeholder: t("login.password") });
  const code = h("input", { class: "input", inputmode: "numeric", autocomplete: "one-time-code", "aria-label": t("login.code"), placeholder: t("login.code") });
  const err = h("div", { class: "err", role: "alert", hidden: !error }, error);
  const label = step === "totp" ? t("login.verify") : t("login.submit");
  const btn = h("button", { class: "btn primary", type: "submit" }, label);
  const busy = (on) => { btn.disabled = on; btn.textContent = on ? t("login.wait") : label; btn.setAttribute("aria-busy", String(on)); };
  const form = h("form", { class: "login-card", onsubmit: async (e) => {
    e.preventDefault();
    if (loginBusy) return;
    loginBusy = true; busy(true);
    try {
      if (step !== "totp") loginPw = pw.value;
      const res = await api.post("/api/login", { password: loginPw, code: step === "totp" ? (code.value.trim() || undefined) : undefined });
      if (store.get().authed) return;
      if (res && res.ok === false && res.totp) { renderLogin("totp"); return; }
      loginPw = "";
      loginBusy = false;
      await boot();
    } catch (ex) {
      if (store.get().authed || epoch !== loginEpoch) return;
      if (step !== "totp") loginPw = "";
      renderLogin(step, /invalid credentials|unauthorized|401|password/i.test(ex.message) ? t(step === "totp" ? "login.wrongcode" : "login.wrong") : ex.message);
    } finally { loginBusy = false; if (epoch === loginEpoch) busy(false); }
  } },
    h("img", { class: "logo-lg", src: "logo.svg", alt: "" }),
    h("div", {}, h("h1", {}, t("login.title")), h("p", {}, t("login.sub"))),
    step === "totp" ? code : pw, err,
    btn);
  mount(document.getElementById("root"), h("div", { class: "login" }, form));
  (step === "totp" ? code : pw).focus();
}

function sidebar(s) {
  const groups = [];
  for (const r of ROUTES) { let g = groups.find((x) => x.k === r.group); if (!g) groups.push(g = { k: r.group, items: [] }); g.items.push(r); }
  const b = s.dashboard?.bundle, pct = b && b.total_gb > 0 ? Math.round((b.used_gb / b.total_gb) * 100) : 0;
  const rail = getPrefs().rail;
  return h("aside", { class: "side" },
    h("div", { class: "brand" }, h("img", { class: "logo", src: "logo.svg", alt: "" }), h("div", { class: "wm" }, "Quota", h("b", {}, "Manager")),
      h("button", { type: "button", class: "collapse-btn", "aria-label": rail ? t("common.expand") : t("common.collapse"), title: rail ? t("common.expand") : t("common.collapse"),
        onclick: () => { setPref("rail", !rail); renderShell(); } }, icon("panel", 18))),
    h("nav", { class: "nav", "aria-label": "Main" }, groups.map((g) => h("div", { class: "nav-group" }, h("div", { class: "nav-glabel" }, t(g.k)),
      g.items.map((r) => h("a", { class: "nav-i" + (s.route === r.id ? " on" : ""), href: "#/" + r.id, title: t("nav." + r.id), "aria-current": s.route === r.id ? "page" : null, onclick: () => store.set({ navOpen: false }) },
        icon(r.icon), h("span", { class: "nav-t" }, t("nav." + r.id)),
        r.count && s.dashboard ? h("span", { class: "nav-badge" }, num(s.dashboard.users.filter((u) => !u.protected).length, 0)) : null))))),
    b ? h("div", { class: "side-meter" }, h("div", { class: "sm-top" }, h("span", {}, t("side.bundle")), h("b", {}, num(pct, 0) + "%")),
      bar(pct), h("div", { class: "sm-sub" }, `${num(Math.max(0, b.remaining_gb))} ${t("common.gb")} ${t("side.left")}`)) : null,
    h("div", { class: "side-foot" }, h("span", { class: "av", style: { background: "linear-gradient(135deg,var(--accent),var(--accent-2))", color: "var(--accent-ink)" } }, "A"),
      h("div", { class: "who" }, h("b", {}, "Admin"), h("span", {}, h("i", { class: "dot " + (s.live ? "ok" : "") }), s.live ? t("common.live") : t("common.offline"))),
      h("button", { type: "button", class: "icon-btn", "aria-label": t("common.logout"), title: t("common.logout"), onclick: logout }, icon("logout", 16))));
}

async function logout() { try { await api.post("/api/logout"); } catch { /* ignore */ } if (stopLive) stopLive(); stopLive = null; store.set({ authed: false, dashboard: null, live: false }); }

let lastRoute = null;
function renderShell() {
  const s = store.get();
  const route = ROUTES.find((r) => r.id === s.route);
  const ctx = { dashboard: s.dashboard, live: s.live, getDashboard: () => store.get().dashboard, refresh, rerender, touch: () => store.set({}) };
  const page = route.page(ctx, { icon: route.icon });
  if (lastRoute !== s.route) { page.classList.add("enter"); lastRoute = s.route; }
  const dark = effTheme() === "dark", lang = getLang();
  const du = s.dashboard;
  const sub = route.id === "people" ? (du ? `${num(du.users.filter((u) => !u.protected).length, 0)} ${t("sub.people")}` : "") : t(({ overview: "sub.overview", system: "sub.system", vpn: "sub.vpn", quota: "sub.quota", network: "sub.network", internet: "sub.internet", security: "sub.security", insights: "sub.insights", share: "sub.share" })[route.id] || "");
  const setLangTo = (l) => { setPref("lang", l); rerender(true); };
  mount(document.getElementById("root"), h("div", { class: "shell" + (s.navOpen ? " nav-open" : "") + (getPrefs().rail ? " rail" : "") },
    sidebar(s), h("div", { class: "nav-scrim", onclick: () => store.set({ navOpen: false }) }),
    h("main", { class: "main", id: "content" },
      h("header", { class: "top" },
        h("div", { class: "top-l" }, h("button", { type: "button", class: "icon-btn menu-btn", "aria-label": t("common.menu"), onclick: () => store.set({ navOpen: !s.navOpen }) }, icon("menu")),
          h("div", {}, h("h1", {}, t("nav." + route.id)), h("p", {}, sub))),
        h("div", { class: "top-actions" },
          h("div", { class: "seg", role: "group", "aria-label": t("common.language") },
            h("button", { type: "button", class: lang === "ar" ? "on" : "", "aria-pressed": String(lang === "ar"), onclick: () => setLangTo("ar") }, "ع"),
            h("button", { type: "button", class: lang === "en" ? "on" : "", "aria-pressed": String(lang === "en"), onclick: () => setLangTo("en") }, "EN")),
          h("button", { type: "button", class: "icon-btn", "aria-label": t(privacyHidden() ? "common.priv.show" : "common.priv.hide"), title: t(privacyHidden() ? "common.priv.show" : "common.priv.hide"), "aria-pressed": String(!privacyHidden()), onclick: () => { setPrivacyHidden(!privacyHidden()); rerender(); } }, icon(privacyHidden() ? "eyeoff" : "eye")),
          h("button", { type: "button", class: "icon-btn", "aria-label": t("common.theme"), title: t("common.theme"), onclick: () => { setPref("theme", dark ? "light" : "dark"); rerender(true); } }, icon(dark ? "sun" : "moon")))),
      page)));
}
function rerender(full) { if (full) { setLang(getPrefs().lang); document.title = "QuotaManager"; } if (store.get().authed) renderShell(); else renderLogin(); }

function drawChanged(prev, s) {
  // A live snapshot only needs the page + drawer refreshed; keep scroll and focus untouched.
  const sc = window.scrollY;
  const active = document.activeElement;
  const typing = active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName);
  // never rebuild under someone's cursor - but the very first draw after signing in must happen
  // even when a first-run dialog already holds focus, otherwise the login card stays underneath.
  if (typing && document.querySelector(".shell")) { refreshDrawer(); return; }
  renderShell();
  window.scrollTo(0, sc);
  refreshDrawer();
}

let loginShown = false;
store.subscribe((s) => {
  // Draw the login page only when we ENTER the signed-out state. Any later
  // store.set (live-status timers, a late 401) must not rebuild it: that threw
  // the user back from the 2FA step to the password step while typing.
  if (s.authed === false) { if (!loginShown) { loginShown = true; renderLogin(); } return; }
  loginShown = false;
  if (s.authed) drawChanged(null, s);
});

async function boot() {
  let me = { authenticated: false };
  try { me = await api.get("/api/me"); } catch { /* server unreachable */ }
  if (!me.authenticated) { store.set({ authed: false }); return; }
  store.set({ authed: true, route: routeId() });
  await refresh();
  maybeWelcome(refresh);
  startLiveShare();
  if (!stopLive) stopLive = startLive((snap) => store.set({ dashboard: snap }), (live) => { clearTimeout(offTimer); if (live) store.set({ live: true }); else offTimer = setTimeout(() => store.set({ live: false }), 2500); });
}

setUnauthorizedHandler(() => { if (stopLive) stopLive(); stopLive = null; closeDrawer(); store.set({ authed: false, dashboard: null, live: false }); });
window.addEventListener("hashchange", () => { if (store.get().authed) { closeDrawer(); store.set({ route: routeId(), navOpen: false }); } });

// A chosen <select> keeps focus, which would pause live refresh; release it once a value is picked.
document.addEventListener("change", (e) => { if (e.target && e.target.tagName === "SELECT") setTimeout(() => e.target.blur(), 0); });
applyPrefs(); setLang(getPrefs().lang); startParticles();
boot();
