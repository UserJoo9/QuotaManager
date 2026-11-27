// WAN topology card: the box dials PPPoE itself ("strong" mode) or keeps the router uplink (LAN).
// A mistake here can cut the internet, so every apply/revert is a two-step action with the full warning shown.
import { h } from "../core/dom.js";
import { t, iso, privacyHidden } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { toast } from "../components/toast.js";

const s = { loaded: false, loading: false, failAt: 0, on: null, dirty: false, user: "", pass: "", wanIf: "", hasPass: false, msg: null, testing: false, applying: false, armed: "", autoRan: false, note: "", binding: null, bindingTimer: 0, repairing: false, repairError: "" };
let armTimer = 0;

async function load(ctx) {
  if (s.loading || Date.now() - s.failAt < 10000) return;
  s.loading = true;
  try {
    const w = await api.get("/api/wan");
    if (!s.dirty) { s.on = (w.configured || w.topology) === "wan"; s.user = privacyHidden() ? "" : (w.pppoe_user || ""); s.wanIf = w.wan_if || ""; s.pass = ""; }
    s.hasPass = !!w.pppoe_has_password; s.loaded = true;
  } catch { s.failAt = Date.now(); s.loaded = true; } finally { s.loading = false; ctx.touch(); }
}
const field = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
function arm(ctx, which) { s.armed = which; clearTimeout(armTimer); armTimer = setTimeout(() => { s.armed = ""; ctx.touch(); }, 8000); ctx.touch(); }

async function repairLan(ctx) {
  if (s.repairing) return;
  s.repairing = true; s.repairError = ""; ctx.touch();
  try {
    const result = await api.post("/api/wan/repair-lan");
    s.binding = result.lan_binding || null;
    clearTimeout(s.bindingTimer);
    s.bindingTimer = setTimeout(() => { s.binding = null; ctx.touch(); }, 65000);
    toast(t("wan.lan.repair.ok"));
    await ctx.refresh();
  } catch (e) {
    if (e.detail?.lan_binding) s.binding = e.detail.lan_binding;
    s.repairError = e.detail?.message || e.message || t("wan.lan.repair.fail");
  } finally { s.repairing = false; ctx.touch(); }
}

function verdict(r) {
  if (r && r.ok) return { ok: true, text: `${t("wan.t.ok")}${r.local_ip ? `  ${iso(r.local_ip)} ↔ ${iso(r.peer_ip)}` : ""}  ${t("wan.t.net")}: ${r.internet ? t("wan.yes") : t("wan.no")}${r.internet ? "" : "\n" + (r.detail || "")}` };
  const st = (r && r.status) || "error", known = ["no-pppoe-server", "auth-failed", "concurrent-session", "link-down", "error"].includes(st) ? st : "error";
  const lead = known === "concurrent-session" ? t("wan.t.second") : `${t("wan.t.fail")} — ${(r && r.detail) || t("wan.t.nodial")}`;
  return { ok: known === "concurrent-session" ? null : false, text: `${lead}\n→ ${t("wan.fix." + known)}${r && r.script_output ? "\n" + r.script_output : ""}` };
}

export default function wanCard(ctx) {
  const d = ctx.dashboard, w = d?.wan || {};
  if (!s.loaded) load(ctx);
  if (s.on === null) s.on = (w.configured || w.topology) === "wan";
  const desired = (w.configured || w.topology) === "wan", live = w.topology === "wan", linkUp = (w.ppp0 || "") === "up", on = s.on;
  const pending = w.pending && w.pending !== w.topology;
  const dirty = s.dirty;
  const binding = s.binding || w.lan_binding || { state: "unknown" };
  const bindingMismatch = !!binding.active && (
    (binding.dhcp_v4 && binding.dhcp_v4 !== binding.active) ||
    (binding.dhcp_v6 && binding.dhcp_v6 !== binding.active) ||
    (binding.shaper && binding.shaper !== binding.active));
  const bindingBad = binding.state === "degraded" || bindingMismatch;

  const runTest = async (auto) => {
    if (s.testing) return; s.testing = true; s.msg = { text: t(auto ? "wan.t.auto" : "wan.t.run") }; ctx.touch();
    try { s.msg = verdict(await api.post("/api/wan/test", { pppoe_user: s.user.trim(), pppoe_password: s.pass })); }
    catch (e) { s.msg = { ok: false, text: `${t("wan.t.err")}: ${e.message}` }; } finally { s.testing = false; ctx.touch(); }
  };
  if (!s.autoRan && s.loaded && !dirty && desired && !linkUp && w.ppp0 !== undefined) { s.autoRan = true; setTimeout(() => runTest(true), 0); }

  const apply = async (target) => {
    if (s.armed !== target) { arm(ctx, target); return; }
    s.armed = ""; s.applying = true; ctx.touch();
    try {
      const body = { topology: target };
      if (target === "wan") { body.pppoe_user = s.user.trim(); body.pppoe_password = s.pass; body.wan_if = s.wanIf.trim(); }
      await api.post("/api/wan", body); s.dirty = false; s.pass = ""; s.note = t(target === "wan" ? "wan.applied.wan" : "wan.applied.lan"); toast(s.note);
      setTimeout(() => { s.note = ""; ctx.touch(); }, 8000);
    } catch (e) { s.dirty = false; toast(`${t("dr.failed")}: ${e.message}`, "bad"); }
    finally { s.applying = false; s.loaded = false; await ctx.refresh(); ctx.touch(); }
  };
  const warn = (which) => s.armed === which ? h("p", { class: "banner warn", role: "alert" }, t(which === "wan" ? "wan.warn.wan" : "wan.warn.lan")) : null;
  const banner = dirty ? t("wan.b.pending") : w.restart_scheduled ? t(desired ? "wan.b.rs.wan" : "wan.b.rs.lan")
    : pending ? t("wan.b.next").replace("{p}", w.pending).replace("{t}", w.topology)
    : desired ? t(linkUp ? "wan.b.up" : "wan.b.down") : "";
  const dim = !dirty && desired && linkUp && w.internet === true;

  return h("section", { class: "card c12" },
    h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("wan.title")), h("span", { class: "sub" }, t("wan.sub"))),
      h("div", { style: { display: "flex", gap: "10px", "align-items": "center" } }, chip(live ? "warn" : "ok", live ? "WAN" : "LAN"), chip(linkUp ? "ok" : "", `ppp0 ${w.ppp0 || "n/a"}`))),
    banner ? h("p", { class: "banner " + (desired && !linkUp && !dirty ? "warn" : "info"), role: "status" }, banner) : null,
    s.note ? h("p", { class: "sub" }, s.note) : null,
    h("div", { class: "kv", style: { margin: "6px 0 14px" } },
      h("div", {}, h("span", { class: "k" }, t("wan.k.source")), h("span", { class: "v" }, w.source === "dashboard" ? "dashboard" : "config.yaml")),
      h("div", {}, h("span", { class: "k" }, t("wan.k.ip")), h("span", { class: "v mono" }, w.ppp_ip ? iso(w.ppp_ip) : "—")),
      h("div", {}, h("span", { class: "k" }, "IPv6"), h("span", { class: "v mono" }, w.public_ip6 ? iso(w.public_ip6) : t("wan.k.v6none")))),
    h("hr", { class: "sep" }),
    h("div", { class: "h-with", style: { "justify-content": "space-between", "align-items": "center" } },
      h("h4", {}, t("wan.lan.title")),
      chip(bindingBad ? "danger" : binding.state === "healthy" ? "ok" : "", t(`wan.lan.${binding.state || "unknown"}`))),
    h("div", { class: "kv", style: { margin: "8px 0" } },
      h("div", {}, h("span", { class: "k" }, t("wan.lan.active")), h("span", { class: "v mono" }, binding.active || "—")),
      h("div", {}, h("span", { class: "k" }, t("wan.lan.dhcp4")), h("span", { class: "v mono" }, binding.dhcp_v4 || "—")),
      h("div", {}, h("span", { class: "k" }, t("wan.lan.dhcp6")), h("span", { class: "v mono" }, binding.dhcp_v6 || "—")),
      h("div", {}, h("span", { class: "k" }, t("wan.lan.shaper")), h("span", { class: "v mono" }, binding.shaper || "—"))),
    bindingBad ? h("p", { class: "banner warn", role: "alert" },
      bindingMismatch ? t("wan.lan.mismatch") : t("wan.lan.degraded.message")) : null,
    s.repairError ? h("p", { class: "banner warn", role: "alert" }, s.repairError) : null,
    h("div", { class: "fa" }, h("button", { type: "button", class: "btn ghost sm",
      disabled: s.repairing, "aria-busy": String(s.repairing),
      onclick: () => repairLan(ctx) }, s.repairing ? t("wan.lan.repair.running")
        : t("wan.lan.repair.button"))),
    h("div", { class: "row-sw", style: { padding: "10px 0" } }, h("div", {}, h("b", {}, t("wan.mode")), h("span", {}, t("wan.mode.d"))),
      sw(on, t("wan.mode"), () => { s.on = !s.on; s.dirty = true; s.armed = ""; ctx.touch(); })),
    on ? h("div", { style: { display: "flex", "flex-direction": "column", gap: "12px", "margin-top": "6px" } },
      h("p", { class: "sub" }, t("wan.creds.d")),
      h("div", { class: "form-row" },
        field(t("wan.user"), h("input", { class: "input ltr", value: s.user, placeholder: "user@isp", spellcheck: "false", autocomplete: "username", oninput: (e) => { s.user = e.target.value; s.dirty = true; } })),
        field(t("wan.pass"), h("input", { class: "input ltr", type: "password", value: s.pass, autocomplete: "new-password", placeholder: s.hasPass ? t("wan.pass.stored") : t("wan.pass.enter"), oninput: (e) => { s.pass = e.target.value; s.dirty = true; } }))),
      field(t("wan.if"), h("input", { class: "input ltr", value: s.wanIf, placeholder: "eth1", spellcheck: "false", oninput: (e) => { s.wanIf = e.target.value; s.dirty = true; } }), t("wan.if.h")),
      h("div", { class: "fa" }, h("button", { type: "button", class: "btn ghost sm", disabled: s.testing, onclick: () => runTest(false) }, s.testing ? t("wan.t.running") : t("wan.t.btn"))),
      s.msg ? h("pre", { class: "wan-msg " + (s.msg.ok ? "ok" : s.msg.ok === false ? "bad" : "") }, s.msg.text) : null,
      h("p", { class: "sub" }, t("wan.t.h"))) : null,
    s.armed ? warn(s.armed) : null,
    h("div", { class: "fa", style: { "margin-top": "14px" } },
      h("button", { type: "button", class: "btn " + (s.armed === (on ? "wan" : "lan") ? "danger" : "primary"), disabled: s.applying || (dim && on && desired), onclick: () => apply(on ? "wan" : "lan") }, s.applying ? t("wan.applying") : s.armed === (on ? "wan" : "lan") ? t("dr.confirm") : t("wan.apply")),
      on && (desired || live) ? h("button", { type: "button", class: "btn ghost", disabled: s.applying, onclick: () => apply("lan") }, s.armed === "lan" ? t("dr.confirm") : t("wan.revert")) : null,
      dirty ? h("button", { type: "button", class: "btn ghost", onclick: () => { s.dirty = false; s.on = desired; s.loaded = false; ctx.touch(); } }, t("wan.cancel")) : null),
    h("hr", { class: "sep" }),
    h("h4", {}, t("wan.guide")),
    h("ol", { class: "guide" }, h("li", {}, t("wan.g1")), h("li", {}, t("wan.g2")), h("li", {}, t("wan.g3"))),
    h("p", { class: "sub" }, t("wan.g.rev")));
}
