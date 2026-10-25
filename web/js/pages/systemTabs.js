import { h, icon } from "../core/dom.js";
import { t, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

const field = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
const fail = (e) => toast(`${t("dr.failed")}: ${e.message}`, "bad");

/* ---------------- Account: password + 2FA ---------------- */
const acct = { totp: null, loading: false, at: 0, failAt: 0, armed: false };
async function loadTotp(ctx, force) {
  if (acct.loading || (!force && acct.totp && Date.now() - acct.at < 30000)) return;
  acct.loading = true;
  try { acct.totp = await api.get("/api/totp"); acct.at = Date.now(); } catch { acct.failAt = Date.now(); } finally { acct.loading = false; ctx.touch(); }
}
function loadQr() {
  if (window.QRCode) return Promise.resolve();
  return new Promise((res, rej) => { const s = document.createElement("script"); s.src = "/assets/qrcode.min.js"; s.onload = res; s.onerror = rej; document.head.append(s); });
}
function passwordDialog() {
  const cur = h("input", { class: "input", type: "password", required: true, autocomplete: "current-password" });
  const nw = h("input", { class: "input", type: "password", required: true, minlength: "12", autocomplete: "new-password" });
  const cf = h("input", { class: "input", type: "password", required: true, autocomplete: "new-password" });
  openDialog({ title: t("acct.pw.title"), sub: t("acct.pw.sub"), submitLabel: t("acct.pw.go"),
    content: [field(t("acct.pw.cur"), cur), field(t("acct.pw.new"), nw, t("acct.pw.hint")), field(t("acct.pw.confirm"), cf)],
    onSubmit: async () => {
      if (nw.value.length < 12) throw new Error(t("acct.pw.short"));
      if (nw.value !== cf.value) throw new Error(t("acct.pw.mismatch"));
      await api.post("/api/password", { current: cur.value, new: nw.value });
      toast(t("acct.pw.done")); setTimeout(() => location.reload(), 900); return true;
    } });
}
async function totpDialog(ctx) {
  let info;
  try { info = acct.totp && acct.totp.pending && acct.totp.secret ? acct.totp : await api.post("/api/totp/enroll"); } catch (e) { fail(e); return; }
  const code = h("input", { class: "input ltr", inputmode: "numeric", pattern: "[0-9]*", maxlength: "8", required: true, autocomplete: "one-time-code", placeholder: "123456" });
  const qr = h("div", { style: { background: "#fff", padding: "10px", "border-radius": "10px", width: "200px", height: "200px", margin: "4px auto", display: "grid", "place-items": "center" } });
  loadQr().then(() => { if (window.QRCode && info.otpauth_uri) new window.QRCode(qr, { text: info.otpauth_uri, width: 180, height: 180, correctLevel: window.QRCode.CorrectLevel.M }); }).catch(() => qr.replaceChildren());
  openDialog({ title: t("acct.2fa.title"), sub: t("acct.2fa.scan"), submitLabel: t("acct.2fa.verify"),
    content: [qr, field(t("acct.2fa.secret"), h("input", { class: "input ltr", readonly: true, value: info.secret || "", onfocus: (e) => e.target.select() }), t("acct.2fa.secret.h")), field(t("acct.2fa.code"), code)],
    onSubmit: async () => { await api.post("/api/totp/enable", { code: code.value.trim() }); toast(t("acct.2fa.on")); await loadTotp(ctx, true); return true; } });
}
export function accountTab(ctx) {
  if (Date.now() - acct.failAt > 10000) loadTotp(ctx);
  const tp = acct.totp, d = ctx.dashboard, sec = d?.security || {};
  const disable = async (ev) => {
    if (!acct.armed) { acct.armed = true; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { acct.armed = false; }, 4000); return; }
    acct.armed = false; try { await api.post("/api/totp/disable"); toast(t("acct.2fa.off")); await loadTotp(ctx, true); } catch (e) { fail(e); }
  };
  return h("div", { class: "grid" },
    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("acct.pw")), h("span", { class: "sub" }, t("acct.pw.d"))), sec.default_password ? chip("danger", t("acct.pw.default")) : null),
      h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary", onclick: passwordDialog }, icon("lock", 15), t("acct.pw.title")))),
    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("acct.2fa")), h("span", { class: "sub" }, t("acct.2fa.d"))), tp ? chip(tp.enabled ? "ok" : tp.pending ? "warn" : "", tp.enabled ? t("acct.2fa.enabled") : tp.pending ? t("acct.2fa.pending") : t("acct.2fa.disabled")) : null),
      tp ? h("div", { class: "fa" }, tp.enabled ? h("button", { type: "button", class: "btn danger sm", onclick: disable }, t("acct.2fa.disable"))
        : h("button", { type: "button", class: "btn primary", onclick: () => totpDialog(ctx) }, icon("shield", 15), tp.pending ? t("acct.2fa.resume") : t("acct.2fa.enable"))) : h("div", { class: "skeleton", style: { height: "40px" } })),
    sec.failed_logins_1h != null ? h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("h3", {}, t("acct.activity"))),
      h("div", { class: "kv" }, h("div", {}, h("span", { class: "k" }, t("acct.failed")), h("span", { class: "v" }, iso(sec.failed_logins_1h))), h("div", {}, h("span", { class: "k" }, t("acct.waf")), h("span", { class: "v" }, iso(sec.waf_blocks_1h ?? 0))))) : null);
}

/* ---------------- Updates ---------------- */
const upd = { u: null, loading: false, at: 0, failAt: 0, busy: "" };
async function loadUpd(ctx, force) {
  if (upd.loading || (!force && upd.u && Date.now() - upd.at < 20000)) return;
  upd.loading = true;
  try { upd.u = await api.get("/api/updates"); upd.at = Date.now(); } catch { upd.failAt = Date.now(); upd.u = upd.u || { missing: true }; } finally { upd.loading = false; ctx.touch(); }
}
export function updatesTab(ctx) {
  if (Date.now() - upd.failAt > 10000) loadUpd(ctx);
  const u = upd.u;
  if (!u) return h("div", { class: "skeleton", style: { height: "220px" } });
  if (u.missing) return h("div", { class: "empty" }, h("b", {}, t("upd.unavail")));
  const off = !u.enabled;
  const run = (key, fn, ok) => async () => { if (upd.busy) return; upd.busy = key; ctx.touch(); try { const r = await fn(); if (r && typeof r === "object" && "current_version" in r) { upd.u = r; upd.at = Date.now(); } if (ok) toast(ok); } catch (e) { fail(e); } finally { upd.busy = ""; await loadUpd(ctx, true); } };
  const status = off ? t("upd.st.off") : u.checking ? t("upd.st.checking") : u.error ? (/timeout|timed ?out/i.test(u.error) ? t("upd.st.timeout") : t("upd.st.error")) : u.available ? t("upd.st.available") : u.latest_version ? t("upd.st.latest") : "—";
  const details = () => openDialog({ title: t("upd.details"), sub: `v${u.current_version} → v${u.latest_version}`, submitLabel: t("common.close"),
    content: (u.changelog || []).length ? (u.changelog || []).map((c) => h("div", { class: "field" }, h("b", {}, c.title), h("span", { class: "sub", style: { "white-space": "pre-wrap" } }, c.body))) : [h("div", { class: "empty" }, t("upd.nolog"))], onSubmit: async () => true });
  const row = (title, sub, on, fn) => h("div", { class: "row-sw", style: { padding: "12px 0" } }, h("div", {}, h("b", {}, title), h("span", {}, sub)), sw(on, title, fn));
  return h("div", { class: "grid" },
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("upd.title")), h("span", { class: "sub" }, t("upd.title.d"))), chip(u.available && !off ? "accent" : "", status)),
      h("div", { class: "kv" },
        h("div", {}, h("span", { class: "k" }, t("upd.current")), h("span", { class: "v mono" }, u.current_version ? "v" + u.current_version : "—")),
        h("div", {}, h("span", { class: "k" }, t("upd.latest")), h("span", { class: "v mono" }, off ? "—" : u.latest_version ? "v" + u.latest_version : u.checking ? t("upd.st.checking") : "—")),
        h("div", {}, h("span", { class: "k" }, t("upd.checked")), h("span", { class: "v" }, off || !u.checked_at ? (off ? "—" : t("upd.never")) : new Date(u.checked_at).toLocaleString()))),
      u.error && !off ? h("p", { class: "sub", style: { color: "var(--warn)", "margin-top": "10px" } }, /403|forbidden/i.test(u.error) ? t("upd.err.403") : /timeout|timed ?out|urlopen|resolution|unreachable|refused/i.test(u.error) ? t("upd.err.net") : u.error) : null,
      h("div", { class: "fa", style: { "margin-top": "14px" } },
        h("button", { type: "button", class: "btn ghost", disabled: off || u.checking || !!upd.busy, onclick: run("check", () => api.post("/api/updates/check")) }, upd.busy === "check" ? t("upd.checking") : t("upd.check")),
        u.available && !off ? h("button", { type: "button", class: "btn primary", disabled: !!upd.busy || u.installing, onclick: run("install", () => api.post("/api/updates/install"), t("upd.installing")) }, u.installing || upd.busy === "install" ? t("upd.installing") : `${t("upd.install")} v${u.latest_version}`) : null,
        u.available && !off && (u.changelog || []).length ? h("button", { type: "button", class: "btn ghost", onclick: details }, t("upd.details")) : null)),
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("h3", {}, t("upd.prefs"))),
      row(t("upd.enabled"), t("upd.enabled.d"), !!u.enabled, run("en", () => api.post("/api/updates", { enabled: !u.enabled }))),
      h("div", { style: { "border-top": "1px solid var(--line)" } }, row(t("upd.auto"), t("upd.auto.d"), !!u.auto_install, run("auto", () => api.post("/api/updates", { auto_install: !u.auto_install }))))));
}

/* ---------------- Logs ---------------- */
const lg = { data: null, loading: false, level: "ALL", q: "", failAt: 0 };
// match the level column ("2026-10-24 17:10:49,044 WARNING quota.x: ..."), not any "error" word inside the message
const lvlOk = (l) => { if (lg.level === "ALL") return true; const m = /^\S+\s+\S+\s+([A-Z]+)\b/.exec(l); return m ? m[1] === lg.level : l.toUpperCase().includes(lg.level); };
async function loadLogs(ctx) {
  if (lg.loading) return; lg.loading = true;
  try { lg.data = await api.get("/api/logs?limit=600"); } catch (e) { lg.failAt = Date.now(); lg.err = e.message; } finally { lg.loading = false; ctx.touch(); }
}
export function logsTab(ctx) {
  if (!lg.data && Date.now() - lg.failAt > 10000) loadLogs(ctx);
  const lines = (lg.data?.lines || []).filter((l) => lvlOk(l) && (!lg.q || l.toLowerCase().includes(lg.q.toLowerCase())));
  const view = h("pre", { class: "logs ltr" }, lines.length ? lines.join("\n") : t("logs.none"));
  const exportLogs = () => { const a = h("a", { href: URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/plain" })), download: `quota-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt` }); document.body.append(a); a.click(); a.remove(); };
  const search = h("input", { type: "search", class: "input", placeholder: t("logs.search"), value: lg.q, "aria-label": t("logs.search"), oninput: (e) => { lg.q = e.target.value; const l2 = (lg.data?.lines || []).filter((l) => lvlOk(l) && (!lg.q || l.toLowerCase().includes(lg.q.toLowerCase()))); view.textContent = l2.length ? l2.join("\n") : t("logs.none"); } });
  return h("section", { class: "card" },
    h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("logs.title")), h("span", { class: "sub" }, lg.data ? `${lines.length} / ${lg.data.total}${lg.data.truncated ? " · " + t("logs.trunc") : ""}` : t("logs.title.d")))),
    h("div", { class: "toolbar", style: { "margin-bottom": "12px" } },
      h("div", { class: "seg", role: "group" }, ["ALL", "INFO", "WARNING", "ERROR"].map((l) => h("button", { type: "button", class: lg.level === l ? "on" : "", "aria-pressed": String(lg.level === l), onclick: () => { lg.level = l; ctx.touch(); } }, l))),
      h("label", { class: "search", style: { flex: "1", "min-width": "180px" } }, icon("search", 18), search),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => loadLogs(ctx) }, lg.loading ? t("logs.loading") : t("logs.refresh")),
      h("button", { type: "button", class: "btn primary sm", onclick: exportLogs }, t("logs.export"))),
    lg.err && !lg.data ? h("div", { class: "empty" }, lg.err) : view);
}
