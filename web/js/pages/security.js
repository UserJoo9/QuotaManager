import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import dnsTab from "./securityDns.js";

let tab = "firewall";
const fw = { state: null, cfg: null, tls: null, loading: false, at: 0, failAt: 0, dirty: false, applying: false, warnings: [], banIp: "", armed: null };

const clone = (x) => JSON.parse(JSON.stringify(x ?? null));
async function load(ctx, force) {
  if (fw.loading || (!force && fw.state && fw.dirty)) return;
  if (!force && fw.state && Date.now() - fw.at < 15000) return;
  fw.loading = true;
  try {
    const [state, tls] = await Promise.all([api.get("/api/firewall"), api.get("/api/security/tls").catch(() => null)]);
    fw.state = state; fw.tls = tls; fw.at = Date.now();
    if (!fw.dirty && state && state.config) fw.cfg = clone(state.config);
  } catch (e) { fw.failAt = Date.now(); fw.error = e.message; } finally { fw.loading = false; ctx.touch(); }
}

const ruleStr = (r) => {
  const p = [];
  if (r.src && r.src !== "0.0.0.0/0") p.push("src " + r.src);
  if (r.dst && r.dst !== "0.0.0.0/0") p.push("dst " + r.dst);
  if (r.protocol) p.push(r.protocol);
  if (r.src_port) p.push("sport " + r.src_port);
  if (r.dst_port) p.push("dport " + r.dst_port);
  return p.join(" ") || t("fw.any");
};
const field = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
const nIn = (get, set, min = 0) => h("input", { class: "input", type: "number", min: String(min), step: "any", value: String(get()), oninput: (e) => { set(+e.target.value || 0); fw.dirty = true; } });

function ruleDialog(ctx, idx) {
  const r = idx >= 0 ? { ...fw.cfg.rules[idx] } : { name: "", chain: "forward", action: "deny", src: "", dst: "", protocol: "tcp", src_port: 0, dst_port: 0, log: true };
  const name = h("input", { class: "input", value: r.name || "" });
  const sel = (opts, cur) => h("select", { class: "select" }, opts.map(([v, l]) => h("option", { value: v, selected: cur === v }, l)));
  const action = sel([["deny", t("fw.deny")], ["allow", t("fw.allow")]], r.action), chain = sel([["forward", t("fw.chain.forward")], ["input", t("fw.chain.input")]], r.chain);
  const proto = sel([["", t("fw.any")], ["tcp", "TCP"], ["udp", "UDP"], ["icmp", "ICMP"]], r.protocol || "");
  const src = h("input", { class: "input ltr", value: r.src && r.src !== "0.0.0.0/0" ? r.src : "", placeholder: "192.168.2.0/24", spellcheck: "false" });
  const dst = h("input", { class: "input ltr", value: r.dst && r.dst !== "0.0.0.0/0" ? r.dst : "", placeholder: "8.8.8.8", spellcheck: "false" });
  const sp = h("input", { class: "input", type: "number", min: "0", max: "65535", value: String(r.src_port || 0) }), dp = h("input", { class: "input", type: "number", min: "0", max: "65535", value: String(r.dst_port || 0) });
  openDialog({ title: idx >= 0 ? t("fw.rule.edit") : t("fw.rule.add"), submitLabel: t("fw.keep"),
    content: [field(t("fw.name"), name), h("div", { class: "form-row" }, field(t("fw.action"), action), field(t("fw.chain"), chain)), h("span", { class: "sub" }, t("fw.chain.hint")),
      h("div", { class: "form-row" }, field(t("fw.src"), src), field(t("fw.dst"), dst)), field(t("fw.proto"), proto), h("div", { class: "form-row" }, field(t("fw.sport"), sp), field(t("fw.dport"), dp))],
    onSubmit: async () => {
      const nr = { name: name.value.trim(), action: action.value, chain: chain.value, src: src.value.trim() || "0.0.0.0/0", dst: dst.value.trim() || "0.0.0.0/0", protocol: proto.value, src_port: +sp.value || 0, dst_port: +dp.value || 0, log: true };
      if (idx >= 0) fw.cfg.rules[idx] = nr; else (fw.cfg.rules = fw.cfg.rules || []).push(nr);
      fw.dirty = true; ctx.touch(); return true;
    } });
}
function fwdDialog(ctx, idx) {
  const f = idx >= 0 ? { ...fw.cfg.port_forwards[idx] } : { name: "", protocol: "tcp", source_port: 8080, target_ip: "", target_port: 8080 };
  const name = h("input", { class: "input", value: f.name || "" });
  const proto = h("select", { class: "select" }, ["tcp", "udp"].map((p) => h("option", { value: p, selected: f.protocol === p }, p.toUpperCase())));
  const sp = h("input", { class: "input", type: "number", min: "1", max: "65535", required: true, value: String(f.source_port) });
  const tp = h("input", { class: "input", type: "number", min: "1", max: "65535", required: true, value: String(f.target_port) });
  const ip = h("input", { class: "input ltr", required: true, value: f.target_ip || "", placeholder: "192.168.2.50", spellcheck: "false" });
  openDialog({ title: idx >= 0 ? t("fw.fwd.edit") : t("fw.fwd.add"), submitLabel: t("fw.keep"),
    content: [field(t("fw.name"), name), h("div", { class: "form-row" }, field(t("fw.proto"), proto), field(t("fw.fwd.sport"), sp)), h("div", { class: "form-row" }, field(t("fw.fwd.ip"), ip), field(t("fw.fwd.tport"), tp))],
    onSubmit: async () => {
      const nf = { ...f, name: name.value.trim(), protocol: proto.value, source_port: +sp.value, target_ip: ip.value.trim(), target_port: +tp.value };
      if (idx >= 0) fw.cfg.port_forwards[idx] = nf; else (fw.cfg.port_forwards = fw.cfg.port_forwards || []).push(nf);
      fw.dirty = true; ctx.touch(); return true;
    } });
}

function firewallTab(ctx) {
  if (Date.now() - fw.failAt > 10000) load(ctx);
  const st = fw.state;
  if (!st || !fw.cfg) return fw.error && Date.now() - fw.failAt < 10000
    ? h("div", { class: "empty" }, h("b", {}, t("fw.unavail")), h("span", {}, fw.error))
    : h("div", { class: "skeleton", style: { height: "280px" } });
  if (st.available === false && !st.config) return h("div", { class: "empty" }, h("b", {}, t("fw.unavail")), h("span", {}, st.reason || ""));
  const c = fw.cfg, s = st.status || {};
  c.syn_flood = c.syn_flood || {}; c.brute_force = c.brute_force || {}; c.scan_detect = c.scan_detect || {};
  const edit = () => { fw.dirty = true; };
  const flag = (obj, key, def) => () => { obj[key] = !(obj[key] ?? def); edit(); ctx.touch(); };
  const row = (title, sub, on, fn) => h("div", { class: "row-sw", style: { padding: "12px 0" } }, h("div", {}, h("b", {}, title), h("span", {}, sub)), sw(on, title, fn));
  const del = (arr, i) => h("button", { type: "button", class: "btn danger sm", "aria-label": t("q.pack.del"), onclick: () => { arr.splice(i, 1); edit(); ctx.touch(); } }, icon("x", 15));
  const editB = (fn) => h("button", { type: "button", class: "btn ghost sm", onclick: fn }, t("fw.edit"));
  const lst = (arr, render, emptyKey) => arr && arr.length ? h("div", {}, arr.map(render)) : h("div", { class: "empty" }, t(emptyKey));
  const cr = (badge, kind, title, sub, actions) => h("div", { class: "crow", style: { "grid-template-columns": "64px 1fr auto" } }, chip(kind, badge), h("div", { class: "c-name" }, title, h("span", { class: "mono" }, sub)), h("div", { style: { display: "flex", gap: "6px" } }, actions));
  const mode = st.mode;

  const apply = async () => {
    if (fw.applying) return; fw.applying = true; ctx.touch();
    try {
      const res = await api.post("/api/firewall", { enabled: c.enabled !== false, services: c.services || [], port_forwards: c.port_forwards || [], rules: c.rules || [],
        allow_cidrs: c.allow_cidrs || [], deny_cidrs: c.deny_cidrs || [], dmz: c.dmz || "",
        syn_flood: { rate: +c.syn_flood.rate || 10, burst: +c.syn_flood.burst || 20 }, brute_force: { threshold: +c.brute_force.threshold || 10, ban_seconds: +c.brute_force.ban_seconds || 1800 },
        scan_detect: { enabled: c.scan_detect.enabled !== false, syn_threshold: +c.scan_detect.syn_threshold || 200, ban_seconds: +c.scan_detect.ban_seconds || 3600 },
        geo_block: c.geo_block === true, wan_confirmed: c.wan_confirmed === true });
      fw.warnings = res.warnings || []; fw.dirty = false; toast(t("fw.applied")); await load(ctx, true);
    } catch (e) { toast(`${t("fw.apply.fail")}: ${e.message}`, "bad"); } finally { fw.applying = false; ctx.touch(); }
  };
  const revert = async () => { try { const r = await api.post("/api/firewall/revert"); fw.dirty = false; toast(r.applied ? t("fw.reverted") : t("fw.revert.fail"), r.applied ? "" : "bad"); await load(ctx, true); } catch (e) { toast(`${t("fw.revert.fail")}: ${e.message}`, "bad"); } };
  const ban = async () => { const ip = fw.banIp.trim(); if (!ip) return; try { await api.post("/api/firewall/ban", { ip, seconds: 1800, reason: "manual" }); fw.banIp = ""; toast(t("fw.banned")); await load(ctx, true); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } };
  const unban = async (ip) => { try { await api.post("/api/firewall/unban", { ip }); await load(ctx, true); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } };
  const lines = (key, ph) => h("textarea", { class: "input ltr", rows: "5", spellcheck: "false", placeholder: ph, style: { height: "auto", padding: "10px 12px", "font-family": "var(--mono, monospace)" }, oninput: (e) => { c[key] = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean); edit(); } }, (c[key] || []).join("\n"));

  const httpsOn = fw.tls && fw.tls.enforced;
  const httpsBusy = { v: false };
  const enforce = async (ev) => { if (httpsBusy.v) return;
    if (fw.armed !== "https-on") { fw.armed = "https-on"; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { if (fw.armed === "https-on") { fw.armed = null; ctx.touch(); } }, 4000); return; }
    fw.armed = null; httpsBusy.v = true; try { const r = await api.post("/api/security/enforce-https"); toast(r.message || t("fw.https.restart")); await load(ctx, true); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { httpsBusy.v = false; } };
  const removeHttps = async (ev) => {
    if (fw.armed !== "https") { fw.armed = "https"; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { if (fw.armed === "https") fw.armed = null; }, 4000); return; }
    fw.armed = null; try { const r = await api.post("/api/security/remove-https"); toast(r.message || t("fw.https.removed")); await load(ctx, true); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); }
  };

  return h("div", { class: "grid" },
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.title")), h("span", { class: "sub" }, t("fw.title.d"))),
        h("div", { style: { display: "flex", gap: "8px", "align-items": "center", "flex-wrap": "wrap" } },
          chip(mode === "wan" ? "accent" : "", mode === "wan" ? t("fw.mode.wan") : mode === "lan" ? t("fw.mode.lan") : "—"),
          chip(s.apply_ok === false ? "danger" : s.apply_ok ? "ok" : "", s.apply_ok === false ? t("fw.st.failed") : s.apply_ok ? t("fw.st.ok") : t("fw.st.none")),
          fw.dirty ? chip("warn", t("fw.unsaved")) : null,
          sw(c.enabled !== false, t("fw.enabled"), flag(c, "enabled", true)))),
      st.available === false ? h("div", { class: "note", style: { color: "var(--warn)" } }, t("fw.nonft") + " " + (st.reason || "")) : null,
      s.apply_ok === false && s.last_error ? h("div", { class: "note", style: { color: "var(--danger)" } }, s.last_error) : null,
      (fw.warnings || []).length ? h("div", { class: "note", style: { color: "var(--warn)" } }, h("b", {}, t("fw.ignored")), fw.warnings.map((w) => h("div", {}, "• " + w))) : null,
      h("div", { class: "fa", style: { "margin-top": "14px" } },
        h("button", { type: "button", class: "btn primary", disabled: fw.applying, onclick: apply }, fw.applying ? t("fw.applying") : t("fw.apply")),
        h("button", { type: "button", class: "btn ghost", onclick: revert }, t("fw.revert")),
        fw.dirty ? h("button", { type: "button", class: "btn ghost", onclick: () => { fw.dirty = false; fw.cfg = clone(st.config); ctx.touch(); } }, t("fw.discard")) : null),
      h("p", { class: "sub", style: { "margin-top": "10px" } }, t("fw.safe"))),

    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.protect")), h("span", { class: "sub" }, t("fw.protect.d")))),
      h("h4", { class: "lbl" }, t("fw.syn")), h("div", { class: "form-row" }, field(t("fw.syn.rate"), nIn(() => c.syn_flood.rate ?? 10, (v) => { c.syn_flood.rate = v; }, 1)), field(t("fw.syn.burst"), nIn(() => c.syn_flood.burst ?? 20, (v) => { c.syn_flood.burst = v; }, 1))),
      h("h4", { class: "lbl", style: { "margin-top": "14px" } }, t("fw.bf")), h("div", { class: "form-row" }, field(t("fw.bf.thr"), nIn(() => c.brute_force.threshold ?? 10, (v) => { c.brute_force.threshold = v; }, 1)), field(t("fw.ban.sec"), nIn(() => c.brute_force.ban_seconds ?? 1800, (v) => { c.brute_force.ban_seconds = v; }, 60))),
      row(t("fw.scan"), t("fw.scan.d"), c.scan_detect.enabled !== false, flag(c.scan_detect, "enabled", true)),
      h("div", { class: "form-row" }, field(t("fw.scan.thr"), nIn(() => c.scan_detect.syn_threshold ?? 200, (v) => { c.scan_detect.syn_threshold = v; }, 1)), field(t("fw.ban.sec"), nIn(() => c.scan_detect.ban_seconds ?? 3600, (v) => { c.scan_detect.ban_seconds = v; }, 60))),
      h("div", { style: { "border-top": "1px solid var(--line)", "margin-top": "8px" } }, row(t("fw.geo"), t("fw.geo.d"), c.geo_block === true, flag(c, "geo_block", false)), row(t("fw.wanconf"), t("fw.wanconf.d"), c.wan_confirmed === true, flag(c, "wan_confirmed", false)))),

    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.lists")), h("span", { class: "sub" }, t("fw.lists.d")))),
      field(t("fw.list.allow"), lines("allow_cidrs", "203.0.113.0/24"), t("fw.allow.h")), h("div", { style: { height: "12px" } }),
      field(t("fw.list.deny"), lines("deny_cidrs", "198.51.100.7"), t("fw.deny.h")),
      mode === "wan" ? [h("div", { style: { height: "12px" } }), field(t("fw.dmz"), h("input", { class: "input ltr", value: c.dmz || "", placeholder: "192.168.2.50", spellcheck: "false", oninput: (e) => { c.dmz = e.target.value.trim(); edit(); } }), t("fw.dmz.h"))] : null),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.rules")), h("span", { class: "sub" }, t("fw.rules.d"), mode === "lan" ? h("span", { class: "sub", style: { display: "block", color: "var(--warn)", "margin-top": "4px" } }, t("fw.lan.note")) : null)), h("button", { type: "button", class: "btn primary sm", onclick: () => ruleDialog(ctx, -1) }, icon("plus", 15), t("fw.rule.add"))),
      lst(c.rules, (r, i) => cr(r.action === "allow" ? t("fw.allow").toUpperCase() : t("fw.deny").toUpperCase(), r.action === "allow" ? "ok" : "danger", r.name || `${t("fw.rule")} ${i + 1}`, `${r.chain} · ${ruleStr(r)}`, [editB(() => ruleDialog(ctx, i)), del(c.rules, i)]), "fw.rules.none")),

    mode === "wan" || (c.services || []).length || (c.port_forwards || []).length ? h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.fwds")), h("span", { class: "sub" }, t("fw.fwds.d"))), mode === "wan" ? h("button", { type: "button", class: "btn primary sm", onclick: () => fwdDialog(ctx, -1) }, icon("plus", 15), t("fw.fwd.add")) : null),
      h("h4", { class: "lbl" }, t("fw.services")),
      lst(c.services, (s2, i) => cr("OPEN", "ok", s2.name || `${t("fw.service")} ${i + 1}`, `${s2.protocol}/${s2.port}${s2.source && s2.source !== "0.0.0.0/0" ? " · " + s2.source : ""}`, [del(c.services, i)]), "fw.services.none"),
      h("h4", { class: "lbl", style: { "margin-top": "16px" } }, t("fw.forwards")),
      lst(c.port_forwards, (f, i) => cr("FWD", "accent", f.name || `${t("fw.forward")} ${i + 1}`, `${f.protocol}/${f.source_port} → ${f.target_ip}:${f.target_port}`, [editB(() => fwdDialog(ctx, i)), del(c.port_forwards, i)]), "fw.forwards.none")) : null,

    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.bans")), h("span", { class: "sub" }, t("fw.bans.d")))),
      h("div", { style: { display: "flex", gap: "10px", "margin-bottom": "12px" } }, h("div", { class: "grow" }, h("input", { class: "input ltr", placeholder: "203.0.113.9", value: fw.banIp, spellcheck: "false", oninput: (e) => { fw.banIp = e.target.value; }, onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); ban(); } } })),
        h("button", { type: "button", class: "btn danger sm", style: { height: "40px" }, onclick: ban }, icon("ban", 15), t("fw.ban"))),
      lst(s.bans || [], (b) => h("div", { class: "crow", style: { "grid-template-columns": "36px 1fr auto auto" } }, h("span", { class: "d-ic" }, icon("ban", 16)),
        h("div", { class: "c-name" }, iso(b.ip), h("span", { class: "mono" }, b.reason || "")), h("span", { class: "sub" }, `${num(Math.ceil((b.remaining || 0) / 60), 0)} ${t("fw.min.left")}`),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => unban(b.ip) }, t("fw.unban"))), "fw.bans.none")),

    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.log")), h("span", { class: "sub" }, t("fw.log.d")))),
      h("div", { class: "live-body", style: { height: "260px" } }, lst(st.log || [], (e) => h("div", { class: "crow", style: { "grid-template-columns": "1fr" } },
        h("div", { class: "c-name", style: { "white-space": "normal" } }, e.message, h("span", { class: "mono" }, `${new Date(e.ts * 1000).toLocaleString()} · ${e.level || ""}`))), "fw.log.none"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("fw.https")), h("span", { class: "sub" }, t("fw.https.d"))), chip(httpsOn ? "ok" : "", httpsOn ? t("fw.https.on") : t("fw.https.off"))),
      h("div", { class: "fa" }, httpsOn ? h("button", { type: "button", class: "btn danger sm", onclick: removeHttps }, t("fw.https.remove")) : h("button", { type: "button", class: "btn primary", onclick: enforce }, t("fw.https.enable"))),
      h("p", { class: "sub", style: { "margin-top": "10px" } }, t("fw.https.note"))));
}

export default function security(ctx) {
  const tabs = [["firewall", t("sec.tab.firewall")], ["dns", t("sec.tab.dns")]];
  return h("div", { class: "page" },
    h("div", { class: "tabs", role: "tablist" }, tabs.map(([k, l]) => h("button", { type: "button", role: "tab", class: tab === k ? "on" : "", "aria-selected": String(tab === k), onclick: () => { tab = k; ctx.touch(); } }, l))),
    tab === "dns" ? dnsTab(ctx) : firewallTab(ctx));
}
