import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { bar, avatar, chip, userStatus, devIcon, fmtBps } from "../components/ui.js";
import { openUserDrawer } from "../components/userDrawer.js";
import { ov, loadDaily, loadLive } from "../core/overviewData.js";
import { lineChart, barChart, gauge } from "../components/chart.js";
import { actions } from "../core/actions.js";
import { toast } from "../components/toast.js";
import { api } from "../core/api.js";

let chartTab = "speed", timer = null, loadingDaily = false, armedTop = 0;
let netView = (() => { try { return localStorage.getItem("qm.ui.netview") === "lan" ? "lan" : "wan"; } catch { return "wan"; } })();
const setNetView = (v) => { netView = v; try { localStorage.setItem("qm.ui.netview", v); } catch { /* ignore */ } };
const QUICK_GB = [10, 25, 50];
const mine = () => location.hash === "#/overview" || location.hash === "" || location.hash === "#/";
function ensureData(ctx) {
  // only fetch when missing/stale - an unconditional fetch+touch would re-render in a loop
  if ((!ov.daily || Date.now() - ov.dailyAt > 60000) && !loadingDaily) {
    loadingDaily = true;
    loadDaily(true).then(() => ctx.touch()).catch(() => {}).finally(() => { loadingDaily = false; });
  }
  if (timer) return;
  const tick = async () => { if (!mine()) { clearInterval(timer); timer = null; return; } try { await loadLive(); ctx.touch(); } catch { /* 401 handled */ } };
  tick(); timer = setInterval(tick, 5000);
}
const DAY = 86400000;
function period(b) {
  const start = b.period_start ? new Date(b.period_start + "T00:00:00") : null;
  const end = b.period_end ? new Date(b.period_end + "T00:00:00") : null;
  const now = new Date();
  const elapsed = start ? Math.max(1, Math.ceil((now - start) / DAY)) : null;
  const total = start && end ? Math.max(1, Math.round((end - start) / DAY)) : null;
  return { elapsed, total };
}

function tipBlock(ctx, { over, warn, elapsed, avg, b, forecastLeft }) {
  const fill = (s, o) => s.replace(/\{(\w+)\}/g, (_, k) => o[k]);
  let tip = null, actions = false;
  if (over) { tip = t("ov.tip.over"); actions = true; }
  else if (elapsed >= 3 && avg > 0) {
    if (warn && b.days_left > 0) {
      const empty = Math.max(0, Math.floor(b.remaining_gb / avg)), target = b.remaining_gb / b.days_left;
      tip = fill(t("ov.tip.pace"), { d: num(empty, 0), l: num(Math.max(0, b.days_left - empty), 0), t: num(target), a: num(avg) }); actions = true;
    } else if (!warn && forecastLeft != null) tip = fill(t("ov.tip.ok"), { x: num(forecastLeft) });
  }
  if (!tip) return null;
  const quick = (gb) => h("button", { type: "button", class: "btn ghost sm" + (armedTop === gb ? " armed" : ""), onclick: async () => {
    if (armedTop !== gb) { armedTop = gb; ctx.touch(); setTimeout(() => { if (armedTop === gb) { armedTop = 0; ctx.touch(); } }, 4000); return; }
    armedTop = 0;
    try { await api.post("/api/bundle", { add_gb: gb }); await ctx.refresh(); toast(t("q.saved")); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); ctx.touch(); }
  } }, armedTop === gb ? t("dr.confirm") : `+${num(gb, 0)} ${t("common.gb")}`);
  return h("div", { class: "hero-tip" }, h("p", {}, tip),
    actions ? h("div", { class: "tip-actions" }, ...QUICK_GB.map(quick),
      h("a", { class: "btn ghost sm", href: "#/quota" }, t("ov.tip.custom")),
      !over ? h("a", { class: "btn primary sm", href: "#/network" }, t("ov.tip.limits")) : null) : null,
    actions ? h("p", { class: "tip-note" }, t("ov.tip.note")) : null);
}

function hero(d, ctx) {
  const b = d.bundle, pct = b.total_gb > 0 ? (b.used_gb / b.total_gb) * 100 : 0;
  const { elapsed, total } = period(b);
  const avg = elapsed ? b.used_gb / elapsed : null;
  const forecastUsed = avg != null && total ? avg * total : null;
  const forecastLeft = forecastUsed != null ? Math.max(0, b.total_gb - forecastUsed) : null;
  const fPct = forecastUsed != null && b.total_gb > 0 ? (forecastUsed / b.total_gb) * 100 : null;
  const over = b.total_gb > 0 && b.used_gb >= b.total_gb;
  const insight = over ? t("ov.insight.over") : fPct != null && fPct > 100 ? t("ov.insight.warn") : t("ov.insight.ok");
  const chipText = b.days_left >= 0 ? `${t("ov.resets")} ${num(b.days_left, 0)} ${t("common.days")}`
    : b.period_type === "manual" ? `${t("ov.period.manual")} · ${iso(b.period_start || "—")}` : `${t("ov.since")} ${iso(b.period_start || "—")}`;
  return h("section", { class: "card c5" },
    h("header", { class: "card-h" }, h("h3", {}, t("side.bundle")), chip("", chipText)),
    h("div", { class: "hero-body" },
      h("div", { class: "gauge" }, gauge(pct, fPct), h("div", { class: "g-center" },
        h("div", { class: "g-num" }, num(b.used_gb)),
        h("div", { class: "g-sub" }, `${t("common.gb")} ${t("ov.of")} ${num(b.total_gb)} ${t("common.gb")}`))),
      h("div", { class: "hero-stats" },
        h("div", {}, h("span", { class: "lbl" }, t("ov.remaining")), h("div", { class: "kpi-big" }, num(Math.max(0, b.remaining_gb)), h("small", {}, t("common.gb")))),
        h("div", { class: "kpi-grid" },
          h("div", {}, h("span", { class: "lbl" }, t("ov.daysleft")), h("b", {}, b.days_left >= 0 ? num(b.days_left, 0) : "–")),
          h("div", {}, h("span", { class: "lbl" }, t("ov.avg")), h("b", {}, avg != null ? num(avg) : "–", avg != null ? h("small", {}, t("common.gb")) : null)),
          h("div", {}, h("span", { class: "lbl" }, t("ov.forecast")), h("b", {}, forecastLeft != null ? num(forecastLeft) : "–", forecastLeft != null ? h("small", {}, t("common.gb")) : null))))),
    h("div", { class: "hero-foot" },
      h("div", { class: "insight" }, h("i", { class: "dot " + (over || (fPct != null && fPct > 100) ? "warn" : "ok") }), h("span", {}, insight))),
    tipBlock(ctx, { over, warn: fPct != null && fPct > 100, elapsed, avg, b, forecastLeft }));
}

function yLabels(max, fmt) { return h("div", { class: "ylab" }, h("span", {}, fmt(max)), h("span", {}, fmt(max / 2)), h("span", {}, fmt(0))); }
function liveCard(ctx, live) {
  const lan = netView === "lan";
  const bpsOf = (x) => (lan ? (x.lan_down_bps || 0) + (x.lan_up_bps || 0) : x.total_bps);
  const downOf = (x) => (lan ? x.lan_down_bps || 0 : x.down_bps), upOf = (x) => (lan ? x.lan_up_bps || 0 : x.up_bps);
  const netSwitch = h("div", { class: "seg net-seg", role: "group", "aria-label": t("ov.net") },
    [["wan", "ov.net.wan"], ["lan", "ov.net.lan"]].map(([k, key]) => h("button", { type: "button", class: netView === k ? "on" : "", "aria-pressed": String(netView === k), onclick: () => { setNetView(k); ctx.touch(); } }, t(key))));
  const tabs = h("div", { class: "seg", role: "group" }, [["speed", "ov.chart.speed"], ["days", "ov.chart.days"], ["devices", "ov.chart.devices"]].map(([k, key]) =>
    h("button", { type: "button", class: chartTab === k ? "on" : "", "aria-pressed": String(chartTab === k), onclick: () => { chartTab = k; ctx.touch(); } }, t(key))));
  let body, legend = null;
  const s = lan ? ov.speedLan : ov.speed, cur = s[s.length - 1];
  if (chartTab === "speed") {
    legend = cur ? h("div", { class: "legend" }, h("span", {}, h("i", { class: "lg d" }), t("ov.down"), h("b", {}, fmtBps(cur.down))), h("span", {}, h("i", { class: "lg u" }), t("ov.up"), h("b", {}, fmtBps(cur.up)))) : null;
    if (!cur) body = h("div", { class: "chart-empty" }, t("common.loading"));
    else {
      const max = Math.max(1e6, ...s.map((x) => Math.max(x.down, x.up))) * 1.1;
      const devs = (ov.live?.devices || []).filter((x) => bpsOf(x) > 0).sort((a, c) => bpsOf(c) - bpsOf(a)).slice(0, 3);
      const dmax = bpsOf(devs[0] || {}) || 1;
      body = h("div", {},
        h("div", { class: "chart", dir: "ltr" }, lineChart(s.map((x) => x.down), s.map((x) => x.up), max), yLabels(max, fmtBps)),
        h("div", { class: "xlab" }, h("span", {}, t("ov.chart.ago")), h("span", {}, t("ov.chart.now"))),
        s.length < 3 ? h("div", { class: "note" }, t("ov.chart.collecting")) : null,
        devs.length ? h("div", { class: "talkers" }, devs.map((x) => h("div", { class: "tk" },
          h("div", { class: "tk-top" }, icon(devIcon(x), 16), h("span", {}, x.name || x.mac)),
          h("b", {}, fmtBps(bpsOf(x))), bar((bpsOf(x) / dmax) * 100, true)))) : null,
        lan ? h("div", { class: "note" }, t("ov.net.lan.note")) : null);
    }
  } else if (chartTab === "days") {
    const dd = lan ? ov.dailyLan : ov.daily;
    if (!dd) body = h("div", { class: "chart-empty" }, t("common.loading"));
    else {
      const max = Math.max(1, ...dd.map((x) => x.gb)) * 1.1, total = dd.reduce((a, x) => a + x.gb, 0), peak = dd.reduce((a, x) => (x.gb > a.gb ? x : a), dd[0]);
      body = h("div", {},
        h("div", { class: "sumrow" },
          h("div", {}, h("span", { class: "lbl" }, t("ov.chart.total30")), h("b", {}, num(total), h("small", {}, t("common.gb")))),
          h("div", {}, h("span", { class: "lbl" }, t("ov.chart.avg")), h("b", {}, num(total / dd.length), h("small", {}, t("common.gb")))),
          h("div", {}, h("span", { class: "lbl" }, t("ov.chart.peak")), h("b", {}, num(peak.gb), h("small", {}, `${t("common.gb")} · ${iso(peak.date.slice(5))}`)))),
        h("div", { class: "chart", dir: "ltr" }, barChart(dd.map((x, i) => ({ value: x.gb, title: `${x.date} · ${num(x.gb)} ${t("common.gb")}`, today: i === dd.length - 1 })), max),
          yLabels(max, (v) => `${num(v, 0)} ${t("common.gb")}`)),
        h("div", { class: "xlab" }, h("span", {}, dd[0].date.slice(5)), h("span", {}, dd[dd.length - 1].date.slice(5))));
    }
  } else {
    const devs = (ov.live?.devices || []).filter((x) => bpsOf(x) > 0).sort((a, c) => bpsOf(c) - bpsOf(a)).slice(0, 6);
    if (!ov.live) body = h("div", { class: "chart-empty" }, t("common.loading"));
    else if (!devs.length) body = h("div", { class: "chart-empty" }, lan ? t("ov.net.lan.idle") : t("ov.chart.idle"));
    else {
      const max = bpsOf(devs[0]);
      body = h("div", { class: "feed" }, devs.map((x) => h("div", { class: "crow", style: { cursor: "default" } },
        h("span", { class: "d-ic" }, icon(devIcon(x), 16)),
        h("div", { class: "c-name" }, x.name || x.mac, h("span", {}, x.user_name || "")),
        h("div", { class: "c-use" }, bar((bpsOf(x) / max) * 100, true)),
        h("div", { class: "d-live" }, h("span", {}, "↓ ", h("b", {}, fmtBps(downOf(x)))), h("span", {}, "↑ ", h("b", {}, fmtBps(upOf(x))))))));
    }
  }
  return h("section", { class: "card c7" },
    h("header", { class: "card-h" }, h("div", { class: "h-with" }, h("h3", {}, t("ov.live")), h("span", { class: "pill-live" + (live ? "" : " off"), style: { "--ph": `-${((performance.now() % 1800) / 1000).toFixed(3)}s` } }, h("i"), live ? t("ov.livebadge") : t("common.offline"))),
      h("div", { class: "card-tools" }, netSwitch, tabs)),
    h("div", { class: "live-body" }, legend ? h("div", { style: { "margin-bottom": "10px" } }, legend) : null, body));
}

function tiles(d, devices) {
  const online = devices.filter((x) => x.connected).length, vpn = d.vpn || {}, sec = d.security || {};
  const net = d.internet, vOn = vpn.state === "connected";
  const attack = (sec.failed_logins_1h || 0) + (sec.waf_blocks_1h || 0);
  const tile = (ic, cls, label, value, sub, dot) => h("div", { class: "tile" }, h("span", { class: "ti " + cls }, icon(ic, 20)),
    h("div", { class: "tt" }, h("span", { class: "lbl" }, label), h("b", {}, value), sub ? h("em", {}, sub) : null), dot ? h("i", { class: "dot " + dot }) : null);
  return h("section", { class: "c12 tiles" },
    tile("internet", net === false ? "bad" : net ? "" : "off", t("ov.tile.internet"), net === false ? t("ov.tile.offline") : net ? t("ov.tile.online") : t("ov.internet.unknown"), "", net === false ? "danger" : net ? "ok" : ""),
    tile("network", "", t("ov.tile.devices"), `${num(online, 0)} ${t("ov.of")} ${num(devices.length, 0)}`, `${num(d.blocked_count || 0, 0)} ${t("ov.tile.blocked")}`, online ? "ok" : ""),
    tile("vpn", vOn ? "" : "off", t("ov.tile.vpn"), vOn ? t("vpn.connected") : t("ov.tile.vpn_off"), vOn ? (vpn.active_node_name || "") : "", vOn ? "ok" : ""),
    tile("security", attack > 5 ? "warn" : "", t("ov.tile.security"), attack > 5 ? t("ov.tile.sec_attack") : t("ov.tile.sec_ok"), `${num(sec.failed_logins_1h || 0, 0)} / ${num(sec.waf_blocks_1h || 0, 0)} · ${t("ov.tile.sec_sub")}`, attack > 5 ? "warn" : "ok"));
}

export default function overview(ctx) {
  const d = ctx.dashboard;
  ensureData(ctx);
  if (!d) return h("div", { class: "page" }, h("div", { class: "skeleton", style: { height: "260px" } }), h("div", { class: "skeleton", style: { height: "200px" } }));
  const devices = d.devices || [], users = d.users || [];
  const open = (u) => openUserDrawer(u.id, ctx.getDashboard, ctx.refresh);
  const act = (fn) => async () => { try { await fn(); toast(t("dr.done")); await ctx.refresh(); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } };
  const top = [...users].filter((u) => !u.protected).sort((a, c) => c.used_gb - a.used_gb).slice(0, 6);

  const feed = [];
  const fi = (cls, ic, title, meta, acts) => h("div", { class: "feed-i" }, h("span", { class: "fi " + cls }, icon(ic, 18)), h("div", { class: "fb" }, h("b", {}, title), meta ? h("span", {}, meta) : null, acts ? h("div", { class: "fa" }, acts) : null));
  if (d.internet === false) feed.push(fi("danger", "alert", t("feed.down"), t("feed.down.sub")));
  for (const u of users.filter((x) => x.guest && x.blocked))
    feed.push(fi("", "userplus", `${u.name} ${t("feed.guest")}`, `${(u.devices || []).length} ${t("pp.devs")}`, [
      h("button", { class: "btn primary sm", onclick: act(() => actions.approveGuest(u.id)) }, t("dr.approve")),
      h("button", { class: "btn ghost sm", onclick: act(() => actions.rejectGuest(u.id)) }, t("dr.reject"))]));
  for (const r of d.rogue || []) feed.push(fi("warn", "alert", t("feed.rogue"), [iso(r.mac || ""), r.ip ? iso(r.ip) : "", r.vendor || ""].filter(Boolean).join(" · ")));
  if (d.security?.default_password) feed.push(fi("warn", "lock", t("feed.defpw"), t("feed.defpw.sub")));
  if ((d.security?.attack_new || 0) > 5)
    feed.push(fi("danger", "shield", t("feed.attack"), `${num(d.security.attack_new, 0)} ${t("feed.attack.sub")}`, [
      h("button", { class: "btn primary sm", onclick: act(() => api.post("/api/security/ack-attack")) }, t("feed.ack"))]));

  return h("div", { class: "page" }, h("div", { class: "grid" },
    hero(d, ctx), liveCard(ctx, ctx.live), tiles(d, users.flatMap((u) => u.devices || [])),
    h("section", { class: "card c7" },
      h("header", { class: "card-h" }, h("h3", {}, t("ov.topusers")), h("a", { class: "btn ghost sm", href: "#/people" }, t("ov.viewall"))),
      top.length ? h("div", {}, top.map((u) => {
        const st = userStatus(u), online = (u.devices || []).filter((x) => x.connected).length;
        return h("button", { type: "button", class: "crow", onclick: () => open(u) }, avatar(u.name, u.id),
          h("div", { class: "c-name" }, u.name, h("span", {}, `${num((u.devices || []).length, 0)} ${t("pp.devs")} · ${num(online, 0)} ${t("pp.on")}`)),
          h("div", { class: "c-use" }, h("div", { class: "c-num" }, h("b", {}, `${num(u.used_gb)} ${t("common.gb")}`), h("span", {}, u.allowance_gb > 0 ? `${num(u.percent, 0)}%` : t("pp.unlimited"))), bar(u.allowance_gb > 0 ? u.percent : 0)),
          chip(st.cls.replace("chip", "").trim(), st.text));
      })) : h("div", { class: "empty" }, t("ov.nousers"))),
    h("section", { class: "card c5" },
      h("header", { class: "card-h" }, h("h3", {}, t("ov.attention")), feed.length ? chip("warn", num(feed.length, 0)) : null),
      feed.length ? h("div", { class: "feed" }, feed)
        : h("div", { class: "empty" }, h("span", { class: "ei" }, icon("check", 22)), h("b", {}, t("feed.allclear")), h("span", {}, t("feed.allclear.sub"))))));
}
