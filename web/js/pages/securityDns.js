import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
const tr = (k, fb) => { const v = t(k); return v === k ? fb : v; };

const dns = { presets: null, rules: null, loading: false, at: 0, failAt: 0, busy: new Set(), tier: "adblock-ultra", imp: { text: "", format: "auto", scope: "global", target: "", action: "block", result: "" } };
const TIERS = ["adblock-ultra", "ads-tracking"];

async function load(ctx, force) {
  if (dns.loading || (!force && dns.presets && Date.now() - dns.at < 30000)) return;
  dns.loading = true;
  try { const [p, r] = await Promise.all([api.get("/api/dns/presets"), api.get("/api/dns/rules")]); dns.presets = p; dns.rules = r; dns.at = Date.now(); }
  catch { dns.failAt = Date.now(); dns.presets = dns.presets || []; dns.rules = dns.rules || []; } finally { dns.loading = false; ctx.touch(); }
}
const field = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);

function targets(d) {
  const users = (d?.users || []).filter((u) => !u.protected);
  return { user: users.map((u) => [u.id, u.name]), device: users.flatMap((u) => (u.devices || []).map((x) => [x.id, `${x.name || x.mac} · ${u.name}`])) };
}
function scopeLabel(r, d) {
  if (r.scope === "global") return t("dns.scope.global");
  const tg = targets(d), list = r.scope === "user" ? tg.user : tg.device, hit = list.find(([id]) => id === r.scope_id);
  return `${t("dns.scope." + r.scope)}: ${hit ? hit[1] : "#" + r.scope_id}`;
}
function scopeTarget(scopeSel, d) {
  const target = h("select", { class: "select" });
  const fill = () => { const tg = targets(d); target.replaceChildren(...(tg[scopeSel.value] || []).map(([id, n]) => h("option", { value: String(id) }, n))); target.hidden = scopeSel.value === "global"; };
  scopeSel.addEventListener("change", fill); fill(); return target;
}
const scopeSelect = (cur = "global") => h("select", { class: "select" }, ["global", "user", "device"].map((s) => h("option", { value: s, selected: cur === s }, t("dns.scope." + s))));

export default function dnsTab(ctx) {
  const d = ctx.dashboard;
  if (Date.now() - dns.failAt > 10000) load(ctx);
  if (!dns.presets) return h("div", { class: "skeleton", style: { height: "280px" } });
  const act = async (key, fn, ok) => {
    if (dns.busy.has(key)) return; dns.busy.add(key); ctx.touch();
    try { await fn(); if (ok) toast(ok); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { dns.busy.delete(key); await load(ctx, true); }
  };
  const by = (id) => dns.presets.find((p) => p.id === id);
  const ultra = by("adblock-ultra"), std = by("ads-tracking");
  const on = !!(ultra?.enabled || std?.enabled);
  if (ultra?.enabled && !std?.enabled) dns.tier = "adblock-ultra"; else if (std?.enabled && !ultra?.enabled) dns.tier = "ads-tracking";
  const blocked = (ultra?.enabled ? ultra.domain_count || 0 : 0) + (std?.enabled ? std.domain_count || 0 : 0);
  const enable = (id) => api.post(`/api/dns/presets/${id}/enable`, { scope: "global" });
  const disable = (id) => api.post(`/api/dns/presets/${id}/disable`, { scope: "global" }).catch(() => {});
  const master = () => act("master", async () => {
    if (on) { await disable("adblock-ultra"); await disable("ads-tracking"); }
    else { const other = dns.tier === "adblock-ultra" ? "ads-tracking" : "adblock-ultra"; if (by(other)?.enabled) await disable(other); await enable(dns.tier); }
  });
  const pickTier = (id) => { dns.tier = id; ctx.touch(); if (on && !by(id)?.enabled) act("master", async () => { await disable(id === "adblock-ultra" ? "ads-tracking" : "adblock-ultra"); await enable(id); }); };
  const update = () => act("update", () => enable(dns.tier), t("dns.updated"));
  const busy = (k) => dns.busy.has(k);

  function ruleDialog(action = "block") {
    const scope = scopeSelect(), target = scopeTarget(scope, d);
    const act2 = h("select", { class: "select" }, ["block", "allow", "redirect"].map((a) => h("option", { value: a, selected: a === action }, t("dns.act." + a))));
    const domain = h("input", { class: "input ltr", required: true, placeholder: "ads.example.com", spellcheck: "false" });
    const ip = h("input", { class: "input ltr", placeholder: "192.168.2.1", spellcheck: "false" }), ipF = field(t("dns.redirect.ip"), ip); ipF.hidden = action !== "redirect";
    act2.onchange = () => { ipF.hidden = act2.value !== "redirect"; };
    openDialog({ title: t("dns.rule.add"), sub: t("dns.rule.sub"), submitLabel: t("add.submit"),
      content: [field(t("dns.domain"), domain), h("div", { class: "form-row" }, field(t("dns.act"), act2), field(t("dns.scope"), scope)), field(t("dns.target"), target), ipF, h("span", { class: "sub" }, t("dns.domain.h"))],
      onSubmit: async () => {
        const body = { scope: scope.value, scope_id: scope.value === "global" ? null : +target.value || null, action: act2.value, domain: domain.value.trim(), enabled: true };
        if (act2.value === "redirect") body.target_ip = ip.value.trim();
        await api.post("/api/dns/rules", body); toast(t("add.done")); await load(ctx, true); return true;
      } });
  }
  const delRule = (r) => h("button", { type: "button", class: "btn danger sm", "aria-label": t("q.pack.del"), onclick: () => act("r" + r.id, () => api.del(`/api/dns/rules/${r.id}`), t("dr.done")) }, icon("x", 15));
  const cats = dns.presets.filter((p) => !TIERS.includes(p.id));
  const tier = (id, name, desc, tag) => h("button", { type: "button", class: "opt" + (dns.tier === id ? " on" : ""), "aria-pressed": String(dns.tier === id), onclick: () => pickTier(id) }, h("span", {}, h("b", {}, name, " ", chip(id === "adblock-ultra" ? "accent" : "", tag)), h("span", {}, desc)));

  const im = dns.imp, impScope = scopeSelect(im.scope), impTarget = scopeTarget(impScope, d);
  impScope.addEventListener("change", () => { im.scope = impScope.value; });
  const doImport = () => act("import", async () => {
    if (!im.text.trim()) throw new Error(t("dns.import.empty"));
    const res = await api.post("/api/dns/import", { text: im.text, format: im.format, scope: impScope.value, scope_id: impScope.value === "global" ? null : +impTarget.value || null, action: im.action });
    im.text = ""; im.result = t("dns.import.done").replace("{c}", res.created).replace("{s}", res.skipped);
  });

  return h("div", { class: "grid" },
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.shield")), h("span", { class: "sub" }, t("dns.shield.d"))),
        h("div", { style: { display: "flex", gap: "10px", "align-items": "center" } }, chip(on ? "ok" : "", on ? t("dns.on") : t("dns.off")), sw(on, t("dns.shield"), master))),
      h("div", { class: "kv", style: { margin: "4px 0 14px" } }, h("div", {}, h("span", { class: "k" }, t("dns.domains")), h("span", { class: "v" }, on ? num(blocked, 0) : "0")),
        h("div", {}, h("span", { class: "k" }, t("dns.mode")), h("span", { class: "v" }, on ? (ultra?.enabled ? t("dns.tier.ultra") : t("dns.tier.std")) : "—"))),
      h("div", { class: "opts" }, tier("adblock-ultra", t("dns.tier.ultra"), t("dns.tier.ultra.d"), t("dns.tag.pro")), tier("ads-tracking", t("dns.tier.std"), t("dns.tier.std.d"), t("dns.tag.light"))),
      h("div", { class: "fa", style: { "margin-top": "14px" } },
        h("button", { type: "button", class: "btn ghost sm", disabled: busy("update") || busy("master"), onclick: update }, busy("update") ? t("dns.updating") : [icon("bolt", 15), t("dns.update")]),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => ruleDialog("allow") }, icon("check", 15), t("dns.whitelist"))),
      h("p", { class: "sub", style: { "margin-top": "10px" } }, t("dns.update.h"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.cats")), h("span", { class: "sub" }, t("dns.cats.d")))),
      cats.length ? h("div", {}, cats.map((p) => h("div", { class: "row-sw", style: { padding: "12px 0" } },
        h("div", {}, h("b", {}, tr("dns.cat." + p.id + ".n", p.name)), h("span", {}, tr("dns.cat." + p.id + ".d", p.description)), p.enabled ? h("span", {}, `${num(p.domain_count, 0)} ${t("dns.domains.n")} · ${scopeLabel({ scope: p.scope, scope_id: p.scope_id }, d)}`) : null),
        sw(p.enabled, p.name, () => act("p" + p.id, () => (p.enabled ? api.post(`/api/dns/presets/${p.id}/disable`, { scope: "global" }) : enable(p.id)))))))
        : h("div", { class: "empty" }, t("dns.cats.none"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.rules")), h("span", { class: "sub" }, t("dns.rules.d"))), h("button", { type: "button", class: "btn primary sm", onclick: () => ruleDialog() }, icon("plus", 15), t("dns.rule.add"))),
      dns.rules.length ? h("div", {}, dns.rules.map((r) => h("div", { class: "crow", style: { "grid-template-columns": "90px minmax(120px,1fr) minmax(120px,1fr) auto" } },
        chip(r.action === "allow" ? "ok" : r.action === "redirect" ? "accent" : "danger", r.action === "redirect" ? `→ ${r.target_ip || ""}` : t("dns.act." + r.action)),
        h("div", { class: "c-name ltr" }, r.domain), h("div", { class: "c-name" }, scopeLabel(r, d), h("span", { class: "mono" }, r.source || "")), delRule(r))))
        : h("div", { class: "empty" }, t("dns.rules.none"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.import")), h("span", { class: "sub" }, t("dns.import.d")))),
      h("textarea", { class: "input ltr", rows: "6", spellcheck: "false", placeholder: "0.0.0.0 ads.example.com\n||tracker.example.net^", style: { height: "auto", padding: "10px 12px", width: "100%" }, oninput: (e) => { im.text = e.target.value; } }, im.text),
      h("div", { class: "form-row", style: { "margin-top": "12px" } },
        field(t("dns.import.format"), h("select", { class: "select", onchange: (e) => { im.format = e.target.value; } }, [["auto", t("dns.fmt.auto")], ["hosts", "hosts"], ["adblock", "AdBlock Plus"]].map(([v, l]) => h("option", { value: v, selected: im.format === v }, l)))),
        field(t("dns.act"), h("select", { class: "select", onchange: (e) => { im.action = e.target.value; } }, ["block", "allow"].map((a) => h("option", { value: a, selected: im.action === a }, t("dns.act." + a)))))),
      h("div", { class: "form-row", style: { "margin-top": "12px" } }, field(t("dns.scope"), impScope), field(t("dns.target"), impTarget)),
      h("div", { class: "fa", style: { "margin-top": "14px" } }, h("button", { type: "button", class: "btn primary", disabled: busy("import"), onclick: doImport }, busy("import") ? t("dns.importing") : t("dns.import.go"))),
      im.result ? h("p", { class: "sub", style: { "margin-top": "10px" } }, im.result) : null));
}
