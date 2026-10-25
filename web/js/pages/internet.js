import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { toast } from "../components/toast.js";
import wanCard from "./internetWan.js";

const x = { v6: null, ts: null, loading: false, at: 0, failAt: 0 };
let draft = null, armedRenew = false, armedOut = false, busy = false;

const tsTtl = () => (x.ts && ["installing", "needs_login", "starting"].includes(x.ts.state) ? 3000 : 30000);

async function load(ctx, force) {
  if (x.loading || (!force && x.v6 && Date.now() - x.at < tsTtl())) return;
  x.loading = true;
  try {
    const [v6, ts] = await Promise.all([api.get("/api/network/ipv6/status"), api.get("/api/tailscale")]);
    x.v6 = v6; x.ts = ts; x.at = Date.now();
  } catch { x.failAt = Date.now(); } finally { x.loading = false; ctx.touch(); }
}
const kv = (rows) => h("div", { class: "kv" }, rows.filter(Boolean).map(([k, v, mono]) => h("div", {}, h("span", { class: "k" }, k), h("span", { class: "v" + (mono ? " mono" : "") }, v))));


const TS_CHIP = { connected: "ok", needs_login: "warn", starting: "warn", installing: "warn", error: "danger" };
const tsLink = (href, label) => h("a", { class: "btn primary sm", href, target: "_blank", rel: "noopener noreferrer" }, label);
async function copyText(text) { try { await navigator.clipboard.writeText(text); toast(t("i.ts.copied")); } catch { toast(text); } }

function tailscaleCard(ts, run, ctx) {
  const head = (st) => h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("i.ts")), h("span", { class: "sub" }, t("i.ts.d"))),
    st ? chip(TS_CHIP[st] || "", t("i.ts.st." + st)) : null);
  if (!ts) return h("section", { class: "card c6" }, head(null), h("div", { class: "skeleton", style: { height: "140px" } }));
  const st = ts.state, call = (path, body, ok) => run(async () => { x.ts = await api.post(path, body); }, ok);
  const out = async (ev) => {
    if (!armedOut) { armedOut = true; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { armedOut = false; }, 4000); return; }
    armedOut = false; await call("/api/tailscale/disconnect", { logout: true })();
  };
  let body;
  if (st === "not_installed") {
    body = [h("p", { class: "sub" }, t("i.ts.install.d")),
      ts.message ? h("div", { class: "banner warn", role: "alert" }, t("i.ts.fail")) : null,
      h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: call("/api/tailscale/install") }, t(ts.message ? "i.ts.retry" : "i.ts.install"))),
      ts.message ? h("details", {}, h("summary", { class: "sub" }, t("i.ts.details")),
        h("p", { class: "sub" }, t("i.ts.install.manual")),
        h("code", { class: "mono", style: { "word-break": "break-all" } }, ts.install_command || ""),
        h("pre", { class: "logs", style: { "max-height": "140px" } }, [ts.message, ...(ts.install_log || [])].join("\n"))) : null];
  } else if (st === "installing") {
    const n = Math.min(ts.install_step || 0, 4);
    body = [h("p", { class: "sub" }, t("i.ts.installing")),
      h("div", { class: "bar", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 4, "aria-valuenow": n }, h("i", { style: { width: Math.max(8, n * 25) + "%" } })),
      h("ul", { class: "ts-steps", style: { "list-style": "none", margin: 0, padding: 0, display: "flex", "flex-direction": "column", gap: "6px", "font-size": "13px" } },
        [1, 2, 3, 4].map((i) => h("li", { style: { color: i <= n ? "var(--text)" : "var(--text-2)" } }, (i <= n ? "✓ " : i === n + 1 ? "… " : "· ") + t("i.ts.step." + i))))];
  } else if (st === "needs_login") {
    body = ts.auth_url
      ? [h("div", { class: "banner info", role: "status" }, h("b", {}, t("i.ts.login")), " ", t("i.ts.login.d")), h("div", { class: "fa" }, tsLink(ts.auth_url, t("i.ts.login.btn")))]
      : [h("p", { class: "sub" }, t("i.ts.wait")), h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: call("/api/tailscale/connect") }, t("i.ts.connect")))];
  } else if (st === "connected") {
    body = [kv([[t("i.ts.addr"), ts.url || ts.ip, true], ts.url_name ? [t("i.ts.name"), ts.url_name, true] : null, [t("i.ts.version"), ts.version || "–", true], [t("i.ts.peers"), num(ts.peers || 0, 0)]]),
      h("p", { class: "sub" }, t("i.ts.how")),
      (ts.peer_list && ts.peer_list.length)
        ? h("div", { class: "kv" }, ts.peer_list.map((p) => h("div", {}, h("span", { class: "k" }, (p.name || "?") + (p.os ? " · " + p.os : "")),
            h("span", { class: "v" }, p.online ? t("i.ts.peer.on") + (p.path ? " · " + t("i.ts.peer." + p.path) : "") : t("i.ts.peer.off")))))
        : h("div", { class: "banner info", role: "status" }, t("i.ts.nodev")),
      (ts.peer_list && ts.peer_list.length && !ts.peers_online) ? h("div", { class: "banner warn", role: "status" }, t("i.ts.noonline")) : null,
      (ts.health && ts.health.length) ? h("details", {}, h("summary", { class: "sub" }, t("i.ts.details")), h("pre", { class: "logs", style: { "max-height": "120px" } }, ts.health.join("\n"))) : null,
      ts.wan_exposed ? h("div", { class: "banner warn", role: "status" }, t("i.ts.fw.on"), " ", h("a", { href: "#/security" }, t("i.ts.fw.link"))) : null,
      h("div", { class: "fa" },
        h("button", { type: "button", class: "btn primary sm", onclick: () => copyText(ts.url || ts.ip) }, t("i.ts.copy")),
        h("button", { type: "button", class: "btn ghost sm", onclick: call("/api/tailscale/disconnect", { logout: false }, t("i.ts.off")) }, t("i.ts.disconnect")),
        h("button", { type: "button", class: "btn danger sm", onclick: out }, t("i.ts.logout")))];
  } else if (st === "starting") {
    body = [h("p", { class: "sub" }, t("i.ts.starting"))];
  } else {
    body = [h("p", { class: "sub" }, st === "daemon_down" ? t("i.ts.down") : t("i.ts.stopped")),
      ts.message ? h("div", { class: "banner warn", role: "alert" }, ts.message) : null,
      h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: call("/api/tailscale/connect") }, t("i.ts.connect")))];
  }
  return h("section", { class: "card c6" }, head(st), h("div", { style: { display: "flex", "flex-direction": "column", gap: "12px" } }, body.filter(Boolean)));
}

export default function internet(ctx) {
  const d = ctx.dashboard;
  if (!d) return h("div", { class: "page" }, h("div", { class: "skeleton", style: { height: "260px" } }));
  if (Date.now() - x.failAt > 10000 && (!x.v6 || Date.now() - x.at > tsTtl())) load(ctx);
  const w = d.wan || {}, isWan = w.topology === "wan", up = d.internet;
  const run = (fn, ok) => async () => {
    if (busy) return; busy = true;
    try { await fn(); if (ok) toast(ok); await ctx.refresh(); await load(ctx, true); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { busy = false; ctx.touch(); }
  };
  const canRenew = isWan && w.ppp0 === "up";
  const renew = async (ev) => {
    if (!armedRenew) { armedRenew = true; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { armedRenew = false; }, 4000); return; }
    armedRenew = false; await run(() => api.post("/api/wan/renew"), t("i.renewed"))();
  };
  if (!draft) draft = { enabled: !!w.renew_enabled, minutes: w.renew_minutes || 15 };
  const minutes = h("input", { class: "input", type: "number", min: "5", step: "1", value: String(draft.minutes), oninput: (e) => { draft.minutes = parseInt(e.target.value || "15", 10); } });
  const saveAuto = run(async () => { await api.post("/api/wan/renew-config", { enabled: draft.enabled, minutes: Math.max(5, draft.minutes || 15) }); draft = null; }, t("q.saved"));

  const v6 = x.v6;
  const probe = run(async () => { await api.post("/api/network/ipv6/probe"); }, t("i.v6.probed"));
  const ts = x.ts;
  const field = (label, input, hint) => h("div", { class: "field" }, h("label", {}, label), input, hint ? h("span", { class: "sub" }, hint) : null);

  return h("div", { class: "page" }, h("div", { class: "grid" },
    h("section", { class: "card c7" },
      h("header", { class: "card-h" }, h("div", { class: "h-with" }, h("h3", {}, t("i.status")), up === false ? chip("danger", t("i.offline")) : up ? chip("ok", t("i.online")) : chip("", t("i.checking"))), chip("accent", isWan ? "WAN" : "LAN")),
      kv([[t("i.mode"), isWan ? t("i.mode.wan") : t("i.mode.lan")], isWan ? [t("i.ppp"), w.ppp0 || "–", true] : null, isWan ? [t("i.pubip"), w.ppp_ip ? iso(w.ppp_ip) : "–", true] : null,
        isWan ? [t("i.peer"), w.ppp_peer ? iso(w.ppp_peer) : "–", true] : null, isWan ? [t("i.last"), w.renew_last ? iso(w.renew_last) : t("i.never")] : null]),
      h("div", { class: "setting" }, h("div", {}, h("div", { class: "t" }, t("i.renew")), h("div", { class: "d" }, canRenew ? t("i.renew.d") : t("i.renew.na"))),
        h("button", { type: "button", class: "btn danger sm", disabled: !canRenew, onclick: renew }, icon("bolt", 15), t("i.renew.btn")))),
    h("section", { class: "card c5" },
      h("header", { class: "card-h" }, h("h3", {}, t("i.auto"))),
      isWan ? h("div", {}, h("div", { class: "row-sw", style: { "margin-bottom": "14px" } }, h("div", {}, h("b", {}, t("i.auto.on"))),
        sw(draft.enabled, t("i.auto.on"), () => { draft.enabled = !draft.enabled; ctx.touch(); })),
        field(t("i.auto.min"), minutes), h("div", { class: "fa", style: { "margin-top": "16px" } }, h("button", { type: "button", class: "btn primary sm", onclick: saveAuto }, t("i.save"))))
        : h("p", { class: "sub" }, t("i.auto.na"))),
    h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("h3", {}, t("i.v6")), v6 ? chip(v6.wan_ipv6_active ? "ok" : "", v6.wan_ipv6_active ? t("i.v6.active") : t("i.v6.inactive")) : null),
      v6 ? [kv([[t("i.v6.prefix"), v6.lan_prefix || "–", true], [t("i.v6.gw"), v6.lan_gateway || "–", true], [t("i.v6.mode"), v6.lan_mode || "–"], [t("i.v6.addr"), v6.wan_ipv6_address ? iso(v6.wan_ipv6_address) : "–", true],
        [t("i.v6.direct"), num(v6.direct_devices || 0, 0)], [t("i.v6.last"), v6.last_probe_time ? new Date(v6.last_probe_time * 1000).toLocaleString("en-GB") : t("i.v6.never")]]),
        v6.last_probe_details ? h("p", { class: "sub", style: { "margin-top": "10px" } }, v6.last_probe_details) : null,
        h("div", { class: "fa", style: { "margin-top": "14px" } }, h("button", { type: "button", class: "btn ghost sm", onclick: probe }, t("i.v6.probe")))]
        : h("div", { class: "skeleton", style: { height: "140px" } })),
    tailscaleCard(ts, run, ctx),
    wanCard(ctx)));
}
