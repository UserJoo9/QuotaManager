import { h } from "../core/dom.js";
import { t, num } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { toast } from "../components/toast.js";

const up = { data: null, draft: null, dirty: false, loading: false, at: 0, failAt: 0, busy: false, error: "" };
const STATE_CHIP = { running: "ok", installing: "", off: "", waiting: "", not_installed: "warn", wrong_backend: "warn", cgnat: "warn", no_router_upnp: "warn", need_ext_ip: "warn", error: "danger" };
const LIFETIMES = [[3600, "1"], [21600, "6"], [86400, "24"], [259200, "72"], [604800, "168"]];

async function load(ctx, force) {
  if (up.loading || (!force && up.data && Date.now() - up.at < 8000)) return;
  up.loading = true;
  try {
    const d = await api.get("/api/upnp");
    up.data = d; up.at = Date.now(); up.error = "";
    if (!up.dirty) up.draft = { ...d.settings, blocked: (d.settings.blocked_ports || []).join(", ") };
  } catch (e) { up.failAt = Date.now(); up.error = e.message; } finally { up.loading = false; ctx.touch(); }
}

const field = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
const row = (title, sub, on, fn) => h("div", { class: "row-sw", style: { padding: "12px 0" } }, h("div", {}, h("b", {}, title), h("span", {}, sub)), sw(on, title, fn));

function payload(d) {
  const blocked = String(d.blocked || "").split(/[^0-9]+/).filter(Boolean).map(Number).filter((n) => n >= 1 && n <= 65535);
  return { enabled: !!d.enabled, natpmp: !!d.natpmp, secure_mode: !!d.secure_mode, relay: !!d.relay, port_min: +d.port_min || 1024, port_max: +d.port_max || 65535,
    max_lifetime: +d.max_lifetime || 86400, blocked_ports: blocked, ext_ip: String(d.ext_ip || "").trim() };
}

export default function upnpTab(ctx) {
  if (Date.now() - up.failAt > 10000) load(ctx);
  if (!up.data || !up.draft) return up.error && Date.now() - up.failAt < 10000
    ? h("div", { class: "empty" }, h("b", {}, t("up.unavail")), h("span", {}, up.error))
    : h("div", { class: "skeleton", style: { height: "280px" } });
  const st = up.data.status || {}, d = up.draft, lan = st.topology !== "wan";
  const edit = () => { up.dirty = true; };
  const save = async (extra) => {
    if (up.busy) return; up.busy = true; ctx.touch();
    try { up.data = await api.put("/api/upnp", payload({ ...d, ...(extra || {}) })); up.dirty = false; up.draft = { ...up.data.settings, blocked: (up.data.settings.blocked_ports || []).join(", ") }; up.at = Date.now(); toast(t("up.saved")); }
    catch (e) { toast(`${t("fw.apply.fail")}: ${e.message}`, "bad"); } finally { up.busy = false; ctx.touch(); }
  };
  const act = (path, body, done) => async () => {
    if (up.busy) return; up.busy = true; ctx.touch();
    try { up.data = await api.post(path, body); up.at = Date.now(); if (done) toast(t(done)); }
    catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { up.busy = false; ctx.touch(); }
  };
  const maps = st.mappings || [];
  const msg = {
    off: t("up.st.off"), running: t("up.st.running"), installing: t("up.st.installing"), waiting: t("up.st.waiting"),
    not_installed: t("up.st.not_installed"), wrong_backend: t("up.st.wrong_backend"), cgnat: t("up.st.cgnat"),
    no_router_upnp: t("up.st.no_router_upnp"), need_ext_ip: t("up.st.need_ext_ip"), error: t("up.st.error"),
  }[st.state] || st.state;
  const select = (val, opts, set) => h("select", { class: "select", onchange: (e) => { set(+e.target.value); edit(); } },
    opts.map(([v, l]) => h("option", { value: String(v), selected: v === val }, l)));
  const nIn = (key, min, max) => h("input", { class: "input ltr", type: "number", min: String(min), max: String(max), value: String(d[key]), oninput: (e) => { d[key] = +e.target.value; edit(); } });

  return h("div", { class: "grid" },
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("up.title")), h("span", { class: "sub" }, t("up.sub"))), chip(STATE_CHIP[st.state] || "", t("up.state." + st.state))),
      row(t("up.enable"), t("up.enable.d"), d.enabled, () => { d.enabled = !d.enabled; save(); }),
      h("p", { class: "sub", style: { padding: "4px 0 8px" } }, msg, st.state === "running" && st.ext_ip ? ` ${st.ext_ip}` : "",
        st.reason && ["error", "waiting"].includes(st.state) ? ` — ${st.reason}` : ""),
      st.state === "not_installed" || st.state === "installing" || (d.enabled && !st.client_installed && lan)
        ? h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", disabled: up.busy || st.state === "installing", onclick: act("/api/upnp/install", null, "up.install.started") }, t("up.install")),
          h("span", { class: "sub mono ltr" }, st.install_cmd || "")) : null,
      st.state === "wrong_backend" ? h("p", { class: "sub mono ltr" }, st.install_cmd || "") : null),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("up.opts")), h("span", { class: "sub" }, t("up.opts.d")))),
      row(t("up.secure"), t("up.secure.d"), d.secure_mode, () => { d.secure_mode = !d.secure_mode; edit(); ctx.touch(); }),
      row(t("up.natpmp"), t("up.natpmp.d"), d.natpmp, () => { d.natpmp = !d.natpmp; edit(); ctx.touch(); }),
      lan ? row(t("up.relay"), t("up.relay.d"), d.relay, () => { d.relay = !d.relay; edit(); ctx.touch(); }) : null,
      h("div", { class: "grid", style: { "margin-top": "12px" } },
        h("div", { class: "c4" }, field(t("up.pmin"), nIn("port_min", 1, 65535))),
        h("div", { class: "c4" }, field(t("up.pmax"), nIn("port_max", 1, 65535))),
        h("div", { class: "c4" }, field(t("up.life"), select(+d.max_lifetime, LIFETIMES.map(([v, hrs]) => [v, `${hrs} ${t("up.hours")}`]), (v) => { d.max_lifetime = v; })))),
      field(t("up.blocked"), h("input", { class: "input ltr", value: d.blocked || "", placeholder: "25, 445, 3306", spellcheck: "false", oninput: (e) => { d.blocked = e.target.value; edit(); } }), t("up.blocked.h")),
      lan ? [h("div", { style: { height: "12px" } }), field(t("up.extip"), h("input", { class: "input ltr", value: d.ext_ip || "", placeholder: "203.0.113.7", spellcheck: "false", oninput: (e) => { d.ext_ip = e.target.value; edit(); } }), t("up.extip.h"))] : null,
      h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary", disabled: up.busy || !up.dirty, onclick: () => save() }, t("up.save"))),
      h("p", { class: "sub", style: { "margin-top": "10px" } }, lan ? t("up.note.lan") : t("up.note.wan"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("up.maps")), h("span", { class: "sub" }, t("up.maps.d"))),
        maps.length ? h("button", { type: "button", class: "btn danger sm", disabled: up.busy, onclick: act("/api/upnp/clear", null, "up.cleared") }, t("up.clear")) : null),
      maps.length ? h("div", {}, maps.map((m) => h("div", { class: "crow", style: { "grid-template-columns": "64px 1fr auto auto" } },
        chip("accent", m.proto),
        h("div", { class: "c-name" }, `${m.device || m.ip} · ${m.ext_port} → ${m.ip}:${m.port}`, h("span", { class: "mono" }, `${m.desc || ""}${m.expires_in ? ` · ${num(Math.ceil(m.expires_in / 60), 0)} ${t("fw.min.left")}` : ""}`)),
        lan && d.relay ? chip(m.relayed ? "ok" : "warn", m.relayed ? t("up.relayed") : t("up.notrelayed")) : h("span"),
        h("button", { type: "button", class: "btn ghost sm", disabled: up.busy, onclick: act("/api/upnp/remove", { proto: m.proto, port: m.ext_port }, "up.removed") }, t("up.remove")))))
        : h("div", { class: "empty" }, t("up.maps.none"))));
}
