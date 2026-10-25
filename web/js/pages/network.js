import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

let macArmed = "";
const net = { cfg: null, guest: null, leases: [], macs: { allow: [], deny: [] }, loading: false, at: 0, failAt: 0 };
const macDraft = { allow: "", deny: "" };
let draft = null, gdraft = null, armedDel = null, busy = false;

async function load(ctx, force) {
  if (net.loading || (!force && net.cfg && Date.now() - net.at < 30000)) return;
  net.loading = true;
  try { const [cfg, leases, macs, guest] = await Promise.all([api.get("/api/network"), api.get("/api/network/static-leases"), api.get("/api/mac-lists"), api.get("/api/guest")]); net.cfg = cfg; net.guest = guest; gdraft = null; net.leases = leases; net.macs = { allow: macs.allow || [], deny: macs.deny || [] }; net.at = Date.now(); }
  catch { net.failAt = Date.now(); } finally { net.loading = false; ctx.touch(); }
}

export default function network(ctx) {
  const d = ctx.dashboard;
  if (Date.now() - net.failAt > 10000 && (!net.cfg || Date.now() - net.at > 30000)) load(ctx);
  if (!net.cfg) return h("div", { class: "page" }, h("div", { class: "skeleton", style: { height: "280px" } }));
  const c = net.cfg, vs = c.vpn_share || {}, live = d?.vpn_share || vs;
  if (!draft) draft = { total_down_mbps: c.total_down_mbps, total_up_mbps: c.total_up_mbps, lan_rate_mbps: c.lan_rate_mbps, vpn_pct: c.vpn_pct, vpn_down_mbps: c.vpn_down_mbps, vpn_up_mbps: c.vpn_up_mbps };
  const run = (fn, ok) => async () => {
    if (busy) return; busy = true;
    try { await fn(); toast(ok || t("n.saved")); await load(ctx, true); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { busy = false; ctx.touch(); }
  };
  const post = (body) => api.post("/api/network", body);
  const g = net.guest || {};
  if (!gdraft) gdraft = { quota_gb: g.quota_gb ?? 0, speed_limit_mbps: g.speed_limit_mbps ?? 0, limit: g.limit ?? 0 };
  const gpost = (body) => api.post("/api/guest", body);
  const gfield = (label, key, hint) => h("div", { class: "field" }, h("label", {}, label),
    h("input", { class: "input", type: "number", min: "0", step: "any", value: String(gdraft[key] ?? 0), oninput: (e) => { gdraft[key] = Math.max(0, +e.target.value || 0); } }), hint ? h("span", { class: "sub" }, hint) : null);
  const gsave = run(async () => { const b = {}; if (gdraft.quota_gb > 0) b.quota_gb = gdraft.quota_gb; if (gdraft.limit >= 1) b.limit = Math.round(gdraft.limit); b.speed_limit_mbps = gdraft.speed_limit_mbps; await gpost(b); });
  const field = (label, key, hint) => h("div", { class: "field" }, h("label", {}, label),
    h("input", { class: "input", type: "number", min: "0", step: "any", value: String(draft[key] ?? 0), oninput: (e) => { draft[key] = Math.max(0, +e.target.value || 0); } }), hint ? h("span", { class: "sub" }, hint) : null);
  const row = (title, sub, on, fn) => h("div", { class: "row-sw", style: { padding: "12px 0" } }, h("div", {}, h("b", {}, title), h("span", {}, sub)), sw(on, title, fn));

  const save = run(async () => { await post({ ...draft }); draft = null; });
  const st = (live.status && live.status.state) || "";
  const sp = d?.shaping, rebuilding = !!(sp && sp.available && !sp.applied), vpnOn = st === "on" || st === "connected" || st === "active";
  const capped = (d?.users || []).flatMap((u) => (u.devices || []).map((x) => ({ ...x, owner: u.name }))).filter((x) => x.limit_down_mbps || x.limit_up_mbps);
  const stChip = st ? chip(st === "connected" || st === "active" ? "ok" : st === "error" ? "danger" : "", st) : null;

  function leaseDialog() {
    const devs = (d?.users || []).flatMap((u) => (u.devices || []).map((x) => ({ ...x, owner: u.name })));
    const mac = h("input", { class: "input ltr", required: true, placeholder: t("n.lease.mac.ph"), list: "qm-macs", spellcheck: "false" });
    const list = h("datalist", { id: "qm-macs" }, devs.map((x) => h("option", { value: x.mac }, `${x.name || x.mac} · ${x.owner}`)));
    const ip = h("input", { class: "input ltr", required: true, placeholder: "192.168.2.50", spellcheck: "false" });
    const ip6 = h("input", { class: "input ltr", spellcheck: "false" }), host = h("input", { class: "input" });
    const f = (l, i) => h("div", { class: "field" }, h("label", {}, l), i);
    openDialog({ title: t("n.lease.title"), submitLabel: t("n.lease.save"),
      content: [f(t("n.lease.mac"), mac), list, h("div", { class: "form-row" }, f(t("n.lease.ip"), ip), f(t("n.lease.ip6"), ip6)), f(t("n.lease.host"), host)],
      onSubmit: async () => { await api.post("/api/network/static-leases", { mac: mac.value.trim(), ip: ip.value.trim(), ip6: ip6.value.trim(), hostname: host.value.trim() }); await load(ctx, true); toast(t("n.saved")); return true; } });
  }
  const delLease = (l) => h("button", { type: "button", class: "btn danger sm", onclick: async (ev) => {
    if (armedDel !== l.mac) { armedDel = l.mac; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { if (armedDel === l.mac) armedDel = null; }, 4000); return; }
    armedDel = null; await run(() => api.del(`/api/network/static-leases/${encodeURIComponent(l.mac)}`), t("dr.done"))();
  } }, icon("x", 15));


  const MAC = /^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$/i;
  const known = new Map((d?.users || []).flatMap((u) => (u.devices || []).map((x) => [String(x.mac).toLowerCase(), `${x.name || x.mac} · ${u.name}`])));
  const macCard = (kind) => {
    const list = net.macs[kind] || [];
    const input = h("input", { class: "input ltr", placeholder: t("n.lease.mac.ph"), list: "qm-macs2", spellcheck: "false", value: macDraft[kind], oninput: (e) => { macDraft[kind] = e.target.value; },
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } } });
    const set = (next) => run(() => api.post("/api/mac-lists", { [kind]: next }), t("n.saved"))();
    const add = () => { const m = macDraft[kind].trim().toLowerCase().replace(/-/g, ":"); if (!MAC.test(m)) { toast(t("n.mac.bad"), "bad"); return; } macDraft[kind] = ""; if (!list.includes(m)) set([...list, m]); };
    return h("section", { class: "card c6" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("n." + kind)), h("span", { class: "sub" }, t("n." + kind + ".d")))),
      h("div", { style: { display: "flex", gap: "10px", "margin-bottom": "12px" } }, h("div", { class: "grow" }, input), h("button", { type: "button", class: "btn primary sm", style: { height: "40px" }, onclick: add }, icon("plus", 15), t("n.mac.add"))),
      list.length ? h("div", {}, list.map((m) => h("div", { class: "crow", style: { "grid-template-columns": "36px 1fr auto" } },
        h("span", { class: "d-ic" }, icon(kind === "allow" ? "check" : "ban", 16)),
        h("div", { class: "c-name" }, known.get(m) || iso(m), known.get(m) ? h("span", { class: "mono" }, iso(m)) : null),
        h("button", { type: "button", class: "btn danger sm", "aria-label": t("q.pack.del"), onclick: (ev) => { const k = kind + m; if (macArmed !== k) { macArmed = k; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { if (macArmed === k) { macArmed = ""; ctx.touch(); } }, 4000); return; } macArmed = ""; set(list.filter((x) => x !== m)); } }, macArmed === kind + m ? t("dr.confirm") : icon("x", 15)))))
        : h("div", { class: "empty" }, t("n.mac.none")));
  };
  return h("div", { class: "page" }, h("div", { class: "grid" },
    h("section", { class: "card c7" },
      h("header", { class: "card-h" }, h("h3", {}, t("n.shape")), sw(c.enabled, t("n.enabled"), run(() => post({ enabled: !c.enabled })))),
      h("p", { class: "sub", style: { "margin-bottom": "14px" } }, t("n.shape.d")),
      h("div", { class: "form-row" }, field(t("n.down"), "total_down_mbps"), field(t("n.up"), "total_up_mbps")),
      h("div", { class: "form-row", style: { "margin-top": "12px" } }, field(t("n.lan"), "lan_rate_mbps", t("n.lan.h")), field(t("n.vpnpct"), "vpn_pct", t("n.vpnpct.h"))),
      h("div", { class: "form-row", style: { "margin-top": "12px" } }, field(t("n.vpndown"), "vpn_down_mbps"), field(t("n.vpnup"), "vpn_up_mbps")),
      h("div", { class: "fa", style: { "margin-top": "18px" } }, h("button", { type: "button", class: "btn primary", onclick: save }, t("n.save")))),
    h("section", { class: "card c5" },
      h("header", { class: "card-h" }, h("h3", {}, t("nav.vpn")), stChip),
      row(t("n.share"), t("n.share.d"), vs.enabled, run(() => post({ vpn_share: !vs.enabled }))),
      vs.interface ? h("div", { class: "kv", style: { margin: "6px 0 10px" } }, h("div", {}, h("span", { class: "k" }, t("n.share.iface")), h("span", { class: "v mono" }, vs.interface))) : null,
      h("div", { style: { "border-top": "1px solid var(--line)" } }, row(t("n.rand"), t("n.rand.d"), c.decline_random_macs, run(() => post({ decline_random_macs: !c.decline_random_macs }))),
        !c.decline_random_macs ? h("button", { type: "button", class: "btn ghost sm", onclick: run(() => post({ decline_random_macs: true, decline_random_macs_existing: true })) }, t("n.rand.old")) : null)),
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("n.status")), h("span", { class: "sub" }, t("n.status.d"))), chip(rebuilding ? "warn" : c.enabled ? "ok" : "", rebuilding ? t("n.st.applying") : c.enabled ? t("n.st.on") : t("n.st.off"))),
      h("div", { class: "kv" },
        h("div", {}, h("span", { class: "k" }, t("n.st.down")), h("span", { class: "v" }, c.total_down_mbps ? `${num(c.total_down_mbps, 0)} ${t("unit.mbps")}` : "—")),
        h("div", {}, h("span", { class: "k" }, t("n.st.up")), h("span", { class: "v" }, c.total_up_mbps ? `${num(c.total_up_mbps, 0)} ${t("unit.mbps")}` : "—")),
        h("div", {}, h("span", { class: "k" }, t("n.st.lan")), h("span", { class: "v" }, `${num(c.lan_rate_mbps || 1000, 0)} ${t("unit.mbps")}`)),
        h("div", {}, h("span", { class: "k" }, t("n.st.vpn")), h("span", { class: "v" }, vpnOn ? `${t("n.st.on")} · ${(live.status && live.status.interface) || vs.interface || ""}` : t("n.st.off"))),
        h("div", {}, h("span", { class: "k" }, t("n.st.caps")), h("span", { class: "v" }, num(capped.length, 0)))),
      capped.length ? h("div", { style: { "margin-top": "10px" } }, capped.slice(0, 30).map((x) => h("div", { class: "crow", style: { "grid-template-columns": "36px 1fr auto" } }, h("span", { class: "d-ic" }, icon("laptop", 16)), h("div", { class: "c-name" }, x.name || iso(x.mac), h("span", { class: "mono" }, x.owner)), h("span", { class: "sub" }, `↓${x.limit_down_mbps || "∞"} ↑${x.limit_up_mbps || "∞"}`)))) : h("div", { class: "empty", style: { "margin-top": "8px" } }, t("n.st.nocaps"))),
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("n.guest")), h("span", { class: "sub" }, t("n.guest.d")))),
      h("div", { class: "grid", style: { "grid-template-columns": "repeat(auto-fit,minmax(280px,1fr))", gap: "0 28px" } },
        h("div", {}, row(t("n.stopnew"), t("n.stopnew.d"), !!g.stop_new, run(() => gpost({ stop_new: !g.stop_new }))),
          h("div", { style: { "border-top": "1px solid var(--line)" } }, row(t("n.guestmode"), t("n.guestmode.d"), !!g.enabled, run(() => gpost({ enabled: !g.enabled }))))),
        h("div", {}, h("div", { class: "form-row" }, gfield(t("n.guest.quota"), "quota_gb"), gfield(t("n.guest.speed"), "speed_limit_mbps", t("n.guest.speed.h"))),
          h("div", { class: "form-row", style: { "margin-top": "12px" } }, gfield(t("n.guest.max"), "limit", t("n.guest.max.h"))),
          h("div", { class: "fa", style: { "margin-top": "14px" } }, h("button", { type: "button", class: "btn primary", onclick: gsave }, t("n.save")))))),
    macCard("allow"), macCard("deny"), h("datalist", { id: "qm-macs2" }, [...known].map(([m, n]) => h("option", { value: m }, n))),
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("n.leases")), h("span", { class: "sub" }, t("n.leases.d"))), h("button", { type: "button", class: "btn primary sm", onclick: leaseDialog }, icon("plus", 15), t("n.leases.new"))),
      net.leases.length ? h("div", {}, net.leases.map((l) => h("div", { class: "crow", style: { "grid-template-columns": "36px minmax(140px,1fr) minmax(160px,1.2fr) auto" } },
        h("span", { class: "d-ic" }, icon("network", 16)),
        h("div", { class: "c-name" }, l.device_name || l.hostname || iso(l.mac), h("span", { class: "mono" }, iso(l.mac))),
        h("div", { class: "c-name" }, h("span", { class: "mono", style: { color: "var(--text)" } }, iso(l.ip)), l.ip6 ? h("span", { class: "mono" }, iso(l.ip6)) : null), delLease(l))))
        : h("div", { class: "empty" }, t("n.leases.none")))));
}
