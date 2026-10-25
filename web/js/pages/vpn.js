import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { vpn, vpnApi, loadAll, loadStatus, loadNodes, loadRouting, loadSettings, loadLogs, nodeParams } from "../core/vpn.js";
import { fmtBytes, toggle } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

let logFilter = "all", routeTop = 0, tab = "users", query = "", timer = null, busy = null, nearBottom = true, lastTop = 0;
const armed = new Set(), pinged = new Set(); // pinged: -1 means both "never tested" and "timed out"
const mine = () => location.hash === "#/vpn";

function ensurePolling(ctx) {
  if (timer) return;
  timer = setInterval(async () => {
    if (!mine()) { clearInterval(timer); timer = null; return; }
    await Promise.allSettled([loadStatus(), loadLogs()]);
    ctx.touch();
  }, 3000);
}
const fail = (e) => toast(`${t("vpn.failed")}: ${e.message}`, "bad");
async function act(ctx, key, fn, ok) {
  busy = key; ctx.touch();
  try { await fn(); if (ok) toast(ok, "ok"); } catch (e) { fail(e); }
  finally { busy = null; await Promise.allSettled([loadStatus(), loadNodes(), loadLogs()]); ctx.touch(); }
}

function nodeDialog(ctx, node) {
  const p = node ? nodeParams(node) : null;
  if (!node) {
    const name = h("input", { class: "input", placeholder: t("vpn.name_ph") });
    const raw = h("textarea", { class: "input", required: true, placeholder: "vless://…\nvmess://…\ntrojan://…\nss://…", spellcheck: "false" });
    openDialog({ title: t("vpn.add_title"), sub: t("vpn.add_d"), submitLabel: t("vpn.save_verify"),
      content: [h("div", { class: "field" }, h("label", {}, t("vpn.name")), name), h("div", { class: "field" }, h("label", {}, t("vpn.link")), raw)],
      onSubmit: async () => { await vpnApi.addNode(name.value.trim(), raw.value.trim()); await Promise.allSettled([loadNodes(), loadStatus()]); ctx.touch(); toast(t("vpn.saved"), "ok"); return true; } });
    return;
  }
  const f = (label, v, extra = {}) => { const i = h("input", { class: "input ltr", value: v ?? "", spellcheck: "false", ...extra }); return [h("div", { class: "field" }, h("label", {}, label), i), i]; };
  const [nameF, name] = f(t("vpn.name"), p.name, { class: "input" });
  const [srvF, server] = f(t("vpn.server"), p.server, { required: true });
  const [portF, port] = f(t("vpn.port"), p.server_port, { type: "number", min: 1, max: 65535, required: true });
  const [uuidF, uuid] = f(t("vpn.uuid"), p.uuid, { required: true });
  const [sniF, sni] = f(t("vpn.sni"), p.sni);
  const [flowF, flow] = f(t("vpn.flow"), p.flow);
  const [pbkF, pbk] = f(t("vpn.pbk"), p.pbk);
  const [sidF, sid] = f(t("vpn.sid"), p.sid);
  const [pathF, path] = f(t("vpn.path"), p.path);
  const transport = h("select", { class: "select" }, ["TCP", "WS", "gRPC"].map((v) => h("option", { value: v, selected: v === p.transport }, v)));
  openDialog({ title: t("vpn.edit_title"), sub: node.name, wide: true, submitLabel: t("vpn.save"),
    content: [nameF, h("div", { class: "form-row", style: { "grid-template-columns": "3fr 1fr" } }, srvF, portF), uuidF,
      h("div", { class: "form-row" }, sniF, flowF), h("div", { class: "form-row" }, pbkF, sidF),
      h("div", { class: "form-row" }, h("div", { class: "field" }, h("label", {}, t("vpn.transport")), transport), pathF)],
    onSubmit: async () => {
      await vpnApi.editNode(node.id, { name: name.value.trim() || null, server: server.value.trim(), server_port: parseInt(port.value, 10) || 443,
        uuid: uuid.value.trim(), sni: sni.value.trim() || null, flow: flow.value.trim() || null, pbk: pbk.value.trim() || null,
        sid: sid.value.trim() || null, transport: transport.value, path: path.value.trim() || null });
      await Promise.allSettled([loadNodes(), loadStatus()]); ctx.touch(); toast(t("vpn.saved"), "ok"); return true;
    } });
}

function hero(ctx, st) {
  const state = st?.state || "disconnected";
  const label = { connected: t("vpn.connected"), connecting: t("vpn.connecting"), error: t("vpn.error") }[state] || t("vpn.disconnected");
  const on = state === "connected";
  const onClick = () => {
    if (on || state === "connecting") return act(ctx, "hero", vpnApi.disconnect);
    const id = st?.active_node_id || vpn.nodes[0]?.id;
    if (!id) return nodeDialog(ctx, null);
    return act(ctx, "hero", () => vpnApi.connect(id));
  };
  const up = st?.uptime_seconds || 0;
  const mini = (l, v) => h("div", { class: "mini" }, h("div", { class: "l" }, l), h("div", { class: "v num" }, v));
  return h("div", { class: "card" },
    h("div", { class: "hero" },
      h("div", { class: "state" }, h("span", { class: "badge-dot " + (on ? "on" : state === "connecting" ? "connecting" : state === "error" ? "error" : "") }),
        h("div", {}, h("h2", {}, label),
          h("div", { class: "sub", style: { color: "var(--text-3)", "font-size": "13px" } },
            st?.active_node_name ? `${st.active_node_name} · ${(st.protocol || "vless").toUpperCase()} · ${st.interface || ""}` : t("vpn.nonode")))),
      h("button", { class: "btn " + (on ? "danger" : "primary"), disabled: busy === "hero", onclick: onClick }, busy === "hero" ? t("common.loading") : on ? t("vpn.disconnect") : t("vpn.connect"))),
    st?.last_error ? h("div", { class: "warnbox" }, st.last_error) : null,
    h("div", { class: "grid g-6", style: { "margin-block-start": "16px" } },
      mini(t("vpn.down"), fmtBytes((st?.speed_down_bps || 0) / 8) + t("unit.ps")), mini(t("vpn.up"), fmtBytes((st?.speed_up_bps || 0) / 8) + t("unit.ps")),
      mini(t("vpn.sess_down"), fmtBytes(st?.total_down_bytes)), mini(t("vpn.sess_up"), fmtBytes(st?.total_up_bytes)),
      mini(t("vpn.conns"), num(st?.active_connections || 0, 0)),
      mini(t("vpn.uptime"), on && up ? iso(`${Math.floor(up / 3600)}h ${Math.floor((up % 3600) / 60)}m`) : "—")));
}

function nodes(ctx, st) {
  const activeId = st?.active_node_id != null ? Number(st.active_node_id) : null;
  const connected = st?.state === "connected";
  const del = (n) => { const key = "del" + n.id; const a = armed.has(key);
    return h("button", { class: "btn danger sm" + (a ? " armed" : ""), "aria-label": t("vpn.delete"), title: t("vpn.delete"),
      onclick: () => { if (!armed.has(key)) { armed.add(key); setTimeout(() => { armed.delete(key); ctx.touch(); }, 4000); ctx.touch(); return; }
        armed.delete(key); act(ctx, key, () => vpnApi.deleteNode(n.id), t("dr.done")); } }, a ? t("dr.confirm") : icon("close", 14)); };
  const row = (n) => {
    const isActive = activeId === Number(n.id);
    const ms = n.ping_ms;
    const ping = ms > 0 ? h("span", { class: "pill " + (ms < 150 ? "ok" : "warn") }, iso(`${Math.round(ms)} ms`)) : ms < 0 && pinged.has(n.id) ? h("span", { class: "pill bad" }, t("vpn.timeout")) : h("span", { class: "pill" }, "—");
    return h("div", { class: "node" + (isActive ? " active" : "") },
      h("span", { class: "dot " + (isActive && connected ? "ok" : "") }),
      h("div", { class: "grow" }, h("div", { class: "name" }, n.name), h("div", { class: "sub" }, h("span", { class: "proto" }, (n.protocol || "vless").toUpperCase()), " ", ping)),
      h("div", { class: "acts" },
        isActive && connected ? h("button", { class: "btn danger sm", disabled: busy === "n" + n.id, onclick: () => act(ctx, "n" + n.id, vpnApi.disconnect) }, t("vpn.disconnect"))
          : h("button", { class: "btn primary sm", disabled: busy === "n" + n.id, onclick: () => act(ctx, "n" + n.id, () => vpnApi.connect(n.id)) }, busy === "n" + n.id ? t("vpn.connecting") : t("vpn.connect")),
        h("button", { class: "btn sm", disabled: busy === "p" + n.id, title: t("vpn.ping"), onclick: () => act(ctx, "p" + n.id, async () => { await vpnApi.ping(n.id); pinged.add(n.id); }) }, icon("bolt", 14), busy === "p" + n.id ? "…" : t("vpn.ping")),
        h("button", { class: "btn sm", onclick: () => nodeDialog(ctx, n) }, t("vpn.edit")), del(n)));
  };
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, t("vpn.nodes")), h("div", { class: "grow" }),
      h("button", { class: "btn primary sm", onclick: () => nodeDialog(ctx, null) }, "+ " + t("vpn.add"))),
    vpn.nodes.length ? h("div", { class: "list" }, vpn.nodes.map(row)) : h("div", { class: "empty" }, t("vpn.nonodes")),
    h("div", { class: "setting", style: { "margin-block-start": "12px" } }, h("div", {}, h("div", { class: "t" }, t("vpn.insecure")), h("div", { class: "d" }, t("vpn.insecure.d"))),
      toggle(vpn.settings.allow_insecure, t("vpn.insecure"), async () => {
        const v = !vpn.settings.allow_insecure; vpn.settings.allow_insecure = v; ctx.touch();
        try { await vpnApi.setInsecure(v); } catch (e) { vpn.settings.allow_insecure = !v; fail(e); } await loadSettings().catch(() => {}); ctx.touch(); })));
}

function routing(ctx) {
  const type = tab === "users" ? "user" : "device";
  const visible = () => { const qq = query.trim().toLowerCase(); return (vpn.routing[tab] || []).filter((x) => !qq || (x.name || "").toLowerCase().includes(qq) || (x.mac || "").toLowerCase().includes(qq)); };
  const set = async (items2, v) => {
    items2.forEach((x) => { x.route_vpn = v; }); ctx.touch();
    try { await Promise.all(items2.map((x) => vpnApi.route(type, x.id, v))); } catch (e) { fail(e); }
    await loadRouting().catch(() => {}); ctx.touch();
  };
  const all = (v) => async () => { const both = [["user", vpn.routing.users], ["device", vpn.routing.devices]];
    both.forEach(([, a]) => a.forEach((x) => { x.route_vpn = v; })); ctx.touch();
    try { await Promise.all(both.flatMap(([ty, a]) => a.map((x) => vpnApi.route(ty, x.id, v)))); } catch (e) { fail(e); }
    await loadRouting().catch(() => {}); ctx.touch(); };
  const list = h("div", { class: "scroll", onscroll: (e) => { routeTop = e.currentTarget.scrollTop; } });
  requestAnimationFrame(() => { list.scrollTop = routeTop; });
  const fill = () => { const items = visible(); list.replaceChildren(...(items.length ? items.map((x) => h("div", { class: "route-row" },
      h("div", { class: "grow" }, h("div", { class: "name" }, x.name), x.mac ? h("div", { class: "sub mono" }, iso(x.mac)) : null),
      h("span", { class: "pill " + (x.route_vpn ? "ok" : "") }, x.route_vpn ? t("vpn.via_vpn") : t("vpn.direct")),
      toggle(x.route_vpn, x.name, () => set([x], !x.route_vpn)))) : [h("div", { class: "empty" }, t("ov.nousers"))])); };
  fill();
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", { style: { margin: "0" } }, t("vpn.routing")), h("div", { class: "sub", style: { color: "var(--text-3)", "font-size": "12.5px" } }, t("vpn.routing.d"))), h("div", { class: "grow" }),
      h("button", { class: "btn sm", onclick: all(true) }, t("vpn.route_all")), h("button", { class: "btn sm", onclick: all(false) }, t("vpn.direct_all"))),
    h("div", { style: { display: "flex", gap: "8px", "margin-block-end": "12px", "flex-wrap": "wrap" } },
      h("div", { class: "seg" }, ["users", "devices"].map((k) => h("button", { "aria-pressed": String(tab === k), onclick: () => { tab = k; ctx.touch(); } }, t("vpn.tab." + k)))),
      h("input", { class: "input", style: { flex: "1", "min-width": "140px" }, placeholder: t("vpn.search"), value: query, "aria-label": t("vpn.search"), oninput: (e) => { query = e.target.value; fill(); } })),
    list);
}

function logs(ctx) {
  const strip = (s) => (s || "").replace(/\u001b\[[0-9;]*m/g, "");
  const cls = (s) => /FATAL|panic|ERROR|failed|failure/i.test(s) ? "e" : /WARN/i.test(s) ? "w" : /INFO/i.test(s) ? "i" : "";
  const shown = vpn.logs.filter((l) => { const k = cls(strip(l.text)); return logFilter === "all" || (logFilter === "errors" ? k === "e" : k === "e" || k === "w"); });
  const term = h("div", { class: "terminal", role: "log", tabindex: "0", onscroll: (e) => { const el = e.currentTarget; lastTop = el.scrollTop; nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80; } },
    shown.length ? shown.map((l) => h("div", {}, l.time ? h("span", { class: "t" }, `[${l.time}]`) : null, h("span", { class: cls(strip(l.text)) }, strip(l.text)))) : (vpn.logs.length ? t("vpn.nomatch") : t("vpn.nologs")));
  requestAnimationFrame(() => { term.scrollTop = nearBottom ? term.scrollHeight : lastTop; });
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, t("vpn.logs")), h("div", { class: "grow" }),
    h("div", { class: "seg" }, [["all", "vpn.f.all"], ["errors", "vpn.f.errors"], ["warn", "vpn.f.warn"]].map(([k, key]) => h("button", { "aria-pressed": String(logFilter === k), onclick: () => { logFilter = k; nearBottom = true; ctx.touch(); } }, t(key)))),
    h("button", { class: "btn sm", onclick: () => loadLogs().then(() => ctx.touch()) }, t("vpn.refresh"))), term);
}

export default function vpnPage(ctx) {
  if (!vpn.loaded && !vpn.loading) loadAll().then(() => ctx.touch());
  ensurePolling(ctx);
  if (!vpn.loaded) return h("div", { class: "page" }, h("div", { class: "skeleton", style: { height: "160px" } }), h("div", { class: "skeleton", style: { height: "260px" } }));
  const st = vpn.status || ctx.dashboard?.vpn;
  return h("div", { class: "page" }, hero(ctx, st), h("div", { class: "grid g-2" }, routing(ctx), nodes(ctx, st)), logs(ctx));
}
