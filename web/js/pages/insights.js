import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { chip, devIcon, fmtBps } from "../components/ui.js";
import { toast } from "../components/toast.js";

const st = { device: "all", win: 24, tab: "live", hist: null, an: null, live: null, key: "", loading: false, failAt: 0, at: 0, liveAt: 0, liveBusy: false, timer: null, err: "" };
const WINDOWS = [[1, "1h"], [6, "6h"], [24, "24h"], [72, "3d"], [168, "7d"], [336, "14d"]];
const mine = () => location.hash === "#/insights";

async function loadHist(ctx, force) {
  const key = `${st.device}|${st.win}`;
  if (st.loading || (!force && key === st.key && Date.now() - st.at < 30000)) return;
  st.loading = true; st.key = key;
  try {
    const hist = await api.get(`/api/history/${encodeURIComponent(st.device)}?window=${st.win}&limit=200`);
    if (key !== st.key) return; // selection changed meanwhile
    st.hist = hist; st.an = hist.analytics || null; st.at = Date.now(); st.err = "";
  } catch (e) { st.failAt = Date.now(); st.err = e.message; st.hist = null; st.an = null; } finally { st.loading = false; ctx.touch(); }
}
async function loadLive(ctx) {
  if (st.liveBusy) return; st.liveBusy = true;
  try { st.live = await api.get(`/api/history/live?device_id=${encodeURIComponent(st.device)}`); st.liveAt = Date.now(); } catch { /* keep last */ } finally { st.liveBusy = false; if (mine() && st.tab === "live") ctx.touch(); }
}
function poll(ctx) {
  if (st.timer) return;
  st.timer = setInterval(() => { if (!mine()) { clearInterval(st.timer); st.timer = null; return; } if (st.tab === "live") loadLive(ctx); }, 3000);
}

const bar = (pct, color) => h("div", { class: "bar" }, h("i", { style: { width: `${Math.max(2, Math.min(100, pct))}%`, ...(color ? { background: color } : {}) } }));
const empty = (key) => h("div", { class: "empty" }, h("b", {}, t(key)), h("span", {}, t("ins.empty.sub")));

function rankRow(left, sub, right, pct, color) {
  return h("div", { class: "ins-row" },
    h("div", { class: "ins-top" }, h("div", { class: "ins-l" }, color ? h("i", { class: "dot", style: { background: color } }) : null, h("b", { class: "ltr" }, left), sub ? h("span", { class: "sub" }, sub) : null), h("div", { class: "ins-r" }, right)),
    bar(pct, color));
}

function sites(item) {
  const dest = (item.top_destinations || []).slice(0, 4);
  if (dest.length) return dest.map((d) => h("span", { class: "chip", title: d.domain }, d.domain, " ", h("b", {}, fmtBps((d.up_bps || 0) >= (d.down_bps || 0) ? d.up_bps : d.down_bps))));
  const rs = (item.recent_sites && item.recent_sites.length ? item.recent_sites.map((s) => s.domain) : item.recent_domains || []).slice(0, 4);
  return rs.length ? rs.map((d) => h("span", { class: "chip", title: d }, d)) : [h("span", { class: "sub" }, t("ins.live.nosites"))];
}

function liveView(ctx) {
  const d = st.live;
  if (!d) return h("div", { class: "skeleton", style: { height: "220px" } });
  const top = d.top_consumer, devs = d.devices || [];
  return h("div", {},
    h("div", { class: "kv", style: { "margin-bottom": "14px" } }, h("div", {}, h("span", { class: "k" }, t("ins.live.down")), h("span", { class: "v" }, fmtBps(d.total_down_bps))), h("div", {}, h("span", { class: "k" }, t("ins.live.up")), h("span", { class: "v" }, fmtBps(d.total_up_bps)))),
    top && !top.is_idle && top.total_bps > 0 ? h("div", { class: "ins-hero" },
      h("div", { class: "lbl" }, t("ins.live.top")),
      h("div", { style: { display: "flex", gap: "12px", "align-items": "center", "flex-wrap": "wrap" } }, h("span", { class: "d-ic" }, icon(devIcon(top), 18)), h("b", { style: { "font-size": "18px" } }, top.name), h("span", { class: "sub" }, `${top.user_name || ""} ${top.ip ? "· " + iso(top.ip) : ""}`), h("span", { style: { "margin-inline-start": "auto" } }, h("b", {}, "↓ " + fmtBps(top.down_bps)), " ", h("b", {}, "↑ " + fmtBps(top.up_bps))), chip("accent", `${num(top.share_pct, 0)}%`)),
      h("div", { class: "ins-chips" }, sites(top)))
      : h("div", { class: "empty" }, h("b", {}, t("ins.live.quiet")), h("span", {}, t("ins.live.quiet.d"))),
    h("div", { style: { "margin-top": "14px" } }, devs.map((x, i) => h("div", { class: "ins-row", style: { cursor: "pointer" }, onclick: () => { st.device = String(x.id); st.tab = "apps"; st.key = ""; ctx.touch(); } },
      h("div", { class: "ins-top" }, h("div", { class: "ins-l" }, h("span", { class: "sub" }, `#${num(i + 1, 0)}`), h("span", { class: "d-ic" }, icon(devIcon(x), 16)), h("b", {}, x.name), h("span", { class: "sub" }, `${x.user_name || ""} ${x.ip ? "· " + iso(x.ip) : ""}`)),
        h("div", { class: "ins-r" }, h("span", {}, "↓ " + fmtBps(x.down_bps)), h("span", {}, "↑ " + fmtBps(x.up_bps)), chip("", x.is_idle ? t("ins.idle") : `${num(x.share_pct, 0)}%`))),
      h("div", { class: "ins-chips" }, sites(x)), bar(x.share_pct || 0)))));
}

async function quick(ctx, domain, action, scope) {
  try { await api.post("/api/dns/rules/quick", { domain, action, scope, device_id: scope === "device" ? +st.device : null }); toast(t("dr.done")); await loadHist(ctx, true); }
  catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); }
}
const badge = (s) => chip(s === "blocked" ? "danger" : s === "allowed" ? "ok" : s === "redirected" ? "accent" : "", t("ins.st." + (s || "none")));

function domainsView(ctx, hist) {
  const total = hist.total_queries || 0, specific = st.device !== "all";
  const hours = new Map();
  (hist.activity || []).forEach((a) => { const k = a.bucket_minute.slice(0, 13) + ":00"; hours.set(k, (hours.get(k) || 0) + a.count); });
  const maxH = Math.max(1, ...hours.values());
  const nameOf = (id) => { const dev = (ctx.dashboard?.users || []).flatMap((u) => (u.devices || []).map((x) => ({ ...x, owner: u.name }))).find((x) => x.id === id); return dev ? dev.owner || dev.name || dev.mac : "#" + id; };
  return h("div", { class: "grid" },
    h("section", { class: "card c12" }, h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("ins.top")), h("span", { class: "sub" }, t("ins.top.d")))),
      (hist.top_domains || []).length ? h("div", {}, hist.top_domains.map((r) => h("div", { class: "ins-dom" },
        h("div", { class: "ins-l" }, h("b", { class: "ltr" }, r.domain), badge(r.status)),
        h("div", { class: "ins-r" }, h("span", { class: "sub" }, `${num(r.hits, 0)} · ${total ? num((r.hits / total) * 100, 1) : "0"}%`),
          specific ? h("button", { type: "button", class: "btn ghost sm", onclick: () => quick(ctx, r.domain, "block", "device") }, t("ins.block.dev")) : null,
          h("button", { type: "button", class: "btn ghost sm", onclick: () => quick(ctx, r.domain, "block", "global") }, t("ins.block.all")),
          h("button", { type: "button", class: "btn ghost sm", onclick: () => quick(ctx, r.domain, "allow", "global") }, t("ins.allow"))))))
        : empty("ins.empty")),
    h("section", { class: "card c6" }, h("header", { class: "card-h" }, h("h3", {}, t("ins.activity"))),
      hours.size ? h("div", { class: "live-body", style: { height: "320px" } }, [...hours.entries()].map(([k, c]) => rankRow(k, "", num(c, 0), (c / maxH) * 100))) : empty("ins.empty")),
    h("section", { class: "card c6" }, h("header", { class: "card-h" }, h("h3", {}, t("ins.recent"))),
      (hist.recent || []).length ? h("div", { class: "live-body", style: { height: "320px" } }, hist.recent.map((r) => h("div", { class: "crow", style: { "grid-template-columns": "1fr auto" } },
        h("div", { class: "c-name ltr" }, (r.device_id ? `[${nameOf(r.device_id)}] ` : "") + r.domain, h("span", { class: "mono" }, `${r.bucket_minute} × ${r.count}`)), badge(r.status)))) : empty("ins.empty")));
}

export default function insights(ctx) {
  const d = ctx.dashboard;
  poll(ctx);
  if (st.tab === "live") { if (!st.live || Date.now() - st.liveAt > 3500) loadLive(ctx); }
  else if (Date.now() - st.failAt > 10000) loadHist(ctx);
  const devices = (d?.users || []).flatMap((u) => (u.devices || []).map((x) => ({ id: x.id, label: `${x.name || x.mac} · ${u.name}` })));
  const sel = h("select", { class: "select", "aria-label": t("ins.device"), onchange: (e) => { st.device = e.target.value; st.key = ""; st.live = null; st.hist = null; ctx.touch(); } },
    h("option", { value: "all", selected: st.device === "all" }, t("ins.all")), devices.map((x) => h("option", { value: String(x.id), selected: String(x.id) === st.device }, x.label)));
  const win = h("select", { class: "select", "aria-label": t("ins.window"), onchange: (e) => { st.win = +e.target.value; st.key = ""; ctx.touch(); } }, WINDOWS.map(([v, l]) => h("option", { value: String(v), selected: v === st.win }, `${t("ins.last")} ${l}`)));
  const tabs = ["live", "apps", "websites", "timeline", "domains"];
  const hist = st.hist, an = st.an || {};
  let body;
  if (st.tab === "live") body = h("section", { class: "card" }, liveView(ctx));
  else if (!hist) body = st.err && Date.now() - st.failAt < 10000 ? h("div", { class: "empty" }, h("b", {}, t("dr.failed")), h("span", {}, st.err)) : h("div", { class: "skeleton", style: { height: "260px" } });
  else if (!hist.total_queries) body = empty("ins.empty");
  else {
    const topApp = (an.top_apps || [])[0], topWeb = (an.top_websites || [])[0];
    const tiles = h("div", { class: "kv", style: { "margin-bottom": "16px" } },
      h("div", {}, h("span", { class: "k" }, t("ins.queries")), h("span", { class: "v" }, num(hist.total_queries, 0))),
      h("div", {}, h("span", { class: "k" }, t("ins.topapp")), h("span", { class: "v" }, topApp ? `${topApp.name} (${num(topApp.percentage, 0)}%)` : "—")),
      h("div", {}, h("span", { class: "k" }, t("ins.topweb")), h("span", { class: "v ltr" }, topWeb ? `${topWeb.domain} (${num(topWeb.percentage, 0)}%)` : "—")),
      h("div", {}, h("span", { class: "k" }, t("ins.domains")), h("span", { class: "v" }, num((hist.top_domains || []).length, 0))));
    if (st.tab === "apps") body = h("section", { class: "card" }, tiles, (an.top_apps || []).length ? an.top_apps.map((a) => rankRow(a.name, a.category, `${num(a.queries, 0)} · ${num(a.percentage, 0)}%`, a.percentage, a.color || "#38BDF8")) : empty("ins.apps.none"));
    else if (st.tab === "websites") body = h("section", { class: "card" }, tiles, (an.top_websites || []).length ? an.top_websites.map((w) => rankRow(w.domain, "", `${num(w.queries, 0)} · ${num(w.percentage, 0)}%`, w.percentage)) : empty("ins.empty"));
    else if (st.tab === "timeline") { const tl = an.timeline || [], mx = Math.max(1, ...tl.map((x) => x.queries)); body = h("section", { class: "card" }, tiles, tl.length ? h("div", { class: "ins-tl", dir: "ltr" }, tl.map((x) => h("div", { class: "ins-col", title: `${x.slot}: ${x.queries}` }, h("i", { style: { height: `${Math.max(4, Math.round((x.queries / mx) * 100))}%` } }), h("span", {}, x.label)))) : empty("ins.empty")); }
    else body = domainsView(ctx, hist);
  }
  return h("div", { class: "page" },
    h("div", { class: "toolbar" }, h("div", { class: "field", style: { margin: 0, "min-width": "220px" } }, sel), st.tab === "live" ? null : h("div", { class: "field", style: { margin: 0 } }, win),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => { if (st.tab === "live") loadLive(ctx); else loadHist(ctx, true); } }, icon("bolt", 15), t("ins.refresh"))),
    h("div", { class: "tabs", role: "tablist" }, tabs.map((k) => h("button", { type: "button", role: "tab", class: st.tab === k ? "on" : "", "aria-selected": String(st.tab === k), onclick: () => { st.tab = k; ctx.touch(); } }, t("ins.tab." + k)))),
    body);
}
