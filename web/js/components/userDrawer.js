import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { actions } from "../core/actions.js";
import { openDrawer, closeDrawer } from "./drawer.js";
import { toast } from "./toast.js";
import { bar, sw, chip, userStatus, devIcon, netSplit } from "./ui.js";

const armed = new Set(); // destructive buttons armed by a first tap (confirm-by-second-tap)


// Editable settings block shared by person + device cards: speed caps, allowance, quota exemption, VPN bypass, DNS.
function settingsBlock(kind, o, run, withQuota) {
  const isUser = kind === "user";
  const num_ = (v) => h("input", { class: "input", type: "number", min: "0", step: "any", value: String(v || 0), inputmode: "decimal" });
  const down = num_(o.limit_down_mbps), up = num_(o.limit_up_mbps), dns = h("input", { class: "input ltr", value: o.dns_server || "", placeholder: "1.1.1.1", spellcheck: "false", "aria-label": t("dr.dns") });
  const mode = h("select", { class: "select" }, ["auto", "fixed"].map((m) => h("option", { value: m, selected: o.quota_mode === m }, t("dr.quota." + m))));
  const gb = h("input", { class: "input", type: "number", min: "0", step: "any", value: String(o.fixed_gb ?? 0) });
  const patch = isUser ? actions.patchUser : actions.patchDevice, dnsFn = isUser ? actions.dnsUser : actions.dnsDevice;
  const exempt = isUser ? o.exempt_quota : o.bypass;
  const flag = (key, val) => run(() => patch(o.id, { [key]: val }));
  const row = (title, sub, on, fn) => h("div", { class: "row-sw" }, h("div", {}, h("b", {}, title), h("span", {}, sub)), sw(on, title, fn));
  const save = run(async () => {
    const body = { limit_down_mbps: Math.max(0, +down.value || 0), limit_up_mbps: Math.max(0, +up.value || 0) };
    if (withQuota) { body.quota_mode = mode.value; if (mode.value === "fixed") body.fixed_gb = Math.max(0, +gb.value || 0); }
    await patch(o.id, body);
    if (dns.value.trim() !== (o.dns_server || "")) await dnsFn(o.id, dns.value.trim());
  }, t("dr.saved"));
  return h("div", { class: "dr-sec" },
    h("h4", {}, t("dr.limits")),
    h("div", { class: "form-row" }, h("div", { class: "field" }, h("label", {}, t("dr.limit.down")), down), h("div", { class: "field" }, h("label", {}, t("dr.limit.up")), up)),
    h("span", { class: "sub" }, t("dr.limit.hint")),
    withQuota ? [h("h4", {}, t("dr.quota")), h("div", { class: "form-row" }, h("div", { class: "field" }, h("label", {}, t("dr.quota")), mode), h("div", { class: "field" }, h("label", {}, t("dr.quota.gb")), gb))] : null,
    row(t("dr.unlimited"), t("dr.unlimited.sub"), exempt, flag(isUser ? "exempt_quota" : "bypass", !exempt)),
    row(t("dr.vpnbypass"), t("dr.vpnbypass.sub"), o.vpn_bypass, flag("vpn_bypass", !o.vpn_bypass)),
    h("div", { class: "field" }, h("label", {}, t("dr.dns")), dns, h("span", { class: "sub" }, t("dr.dns.hint"))),
    h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: save }, t("dr.save"))));
}

// Rename / history retention / move / top-up: the classic "Edit" modals, folded into the cards.
function topupRow(id, isUser, run) {
  const gb = h("input", { class: "input", type: "number", min: "0", step: "any", placeholder: t("dr.topup.ph"), inputmode: "decimal", "aria-label": t("dr.topup") });
  const go = run(async () => { const v = +gb.value; if (!(v > 0)) throw new Error(t("dr.topup.bad")); await (isUser ? actions.topupUser : actions.topupDevice)(id, v); gb.value = ""; }, t("dr.topup.done"));
  return h("div", { class: "field" }, h("label", {}, t("dr.topup")), h("div", { style: { display: "flex", gap: "10px" } }, h("div", { class: "grow" }, gb), h("button", { type: "button", class: "btn primary sm", style: { height: "40px" }, onclick: go }, icon("plus", 15), t("dr.topup.add"))), h("span", { class: "sub" }, t("dr.topup.hint")));
}
function userDetails(u, run) {
  const name = h("input", { class: "input", value: u.name || "", "aria-label": t("dr.name") });
  const hist = h("input", { class: "input", type: "number", min: "0", max: "365", step: "1", value: u.history_days == null ? "" : String(u.history_days), placeholder: t("dr.hist.ph"), inputmode: "numeric" });
  const save = run(async () => { const b = { name: name.value.trim() }; if (hist.value !== "") b.history_days = Math.max(0, Math.min(365, Math.round(+hist.value || 0))); await actions.patchUser(u.id, b); }, t("dr.saved"));
  return h("div", { class: "dr-sec" }, h("h4", {}, t("dr.details")),
    h("div", { class: "field" }, h("label", {}, t("dr.name")), name),
    h("div", { class: "field" }, h("label", {}, t("dr.hist")), hist, h("span", { class: "sub" }, t("dr.hist.hint"))),
    h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: save }, t("dr.save"))),
    topupRow(u.id, true, run));
}
function deviceDetails(d, u, users, run, danger) {
  const name = h("input", { class: "input", value: d.name || "", "aria-label": t("dr.name") });
  const owner = h("select", { class: "select" }, users.filter((x) => !x.protected || x.id === u.id).map((x) => h("option", { value: String(x.id), selected: x.id === u.id }, x.name)));
  const save = run(async () => { const b = { name: name.value.trim() }; if (+owner.value !== u.id) b.user_id = +owner.value; await actions.patchDevice(d.id, b); }, t("dr.saved"));
  return h("div", { class: "dr-sec" },
    h("div", { class: "field" }, h("label", {}, t("dr.name")), name),
    d.gateway ? null : h("div", { class: "field" }, h("label", {}, t("dr.move")), owner, h("span", { class: "sub" }, t("dr.move.hint"))),
    h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: save }, t("dr.save"))),
    topupRow(d.id, false, run),
    d.gateway ? null : h("div", { class: "danger-z" },
      h("div", { class: "dz-r" }, h("div", {}, h("b", {}, t("dr.kick")), h("span", {}, t("dr.kick.sub"))), danger(`dkick${d.id}`, t("dr.kick"), () => actions.kickDevice(d.id))),
      h("div", { class: "dz-r" }, h("div", {}, h("b", {}, t("dr.blacklist")), h("span", {}, t("dr.bl.sub"))), danger(`dbl${d.id}`, t("dr.blacklist"), () => actions.blacklistDevice(d.id)))));
}

const mkRun = (refresh) => (fn, okMsg) => async () => {
  try { await fn(); toast(okMsg || t("dr.done")); await refresh(); }
  catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); }
};
const mkDanger = (run) => (key, label, fn, closeAfter) => h("button", {
  type: "button", class: "btn danger sm" + (armed.has(key) ? " armed" : ""),
  onclick: async (ev) => {
    if (!armed.has(key)) { armed.add(key); ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => armed.delete(key), 4000); return; }
    armed.delete(key); await run(fn)(); if (closeAfter) closeDrawer();
  } }, armed.has(key) ? t("dr.confirm") : label);

// One device on its own: usage, access, addresses, limits, rename/move/top-up, kick/blacklist. No owner details.
export function openDeviceDrawer(userId, deviceId, getDashboard, refresh) {
  const run = mkRun(refresh), danger = mkDanger(run);
  openDrawer(() => {
    const users = getDashboard()?.users || [], u = users.find((x) => x.id === userId), d = u && (u.devices || []).find((x) => x.id === deviceId);
    if (!d) { closeDrawer(); return { title: "", body: [], footer: [] }; }
    const pct = u.allowance_gb > 0 ? (d.device_used_gb / u.allowance_gb) * 100 : 0;
    const body = [
      h("div", { class: "dr-sec" }, h("h4", {}, t("dr.thisperiod")),
        h("div", { class: "big" }, num(d.device_used_gb), h("small", {}, `${t("common.gb")} · ${u.name}`)),
        u.allowance_gb > 0 ? bar(pct) : null,
        h("div", {}, chip(d.blocked ? "danger" : d.connected ? "ok" : "", d.blocked ? t("pp.st.blocked") : d.connected ? t("pp.st.online") : t("pp.st.offline")))),
      h("div", { class: "dr-sec" }, netSplit({ wan: d.device_used_gb, wanUp: d.device_up_gb, wanDown: d.device_down_gb, lan: d.lan_used_gb, lanUp: d.lan_up_gb, lanDown: d.lan_down_gb })),
    ];
    if (!d.gateway) body.push(h("div", { class: "kv" }, h("div", { class: "row-sw" }, h("div", {}, h("b", {}, t("pp.access")), h("span", {}, d.blocked ? t("pp.st.blocked") : t("common.active"))),
      sw(!d.blocked, t("pp.access"), run(() => actions.blockDevice(d.id, !d.blocked))))));
    body.push(h("div", { class: "dr-sec" }, h("div", { class: "kv" },
      h("div", {}, h("span", { class: "k" }, t("dr.mac")), h("span", { class: "v mono" }, iso(d.mac))),
      d.ip ? h("div", {}, h("span", { class: "k" }, t("dr.ip")), h("span", { class: "v mono" }, iso(d.ip))) : null,
      d.ip6 ? h("div", {}, h("span", { class: "k" }, t("dr.ip6")), h("span", { class: "v mono" }, iso(d.ip6))) : null,
      d.vendor ? h("div", {}, h("span", { class: "k" }, t("dr.vendor")), h("span", { class: "v" }, d.vendor)) : null)));
    if (!d.gateway) body.push(settingsBlock("device", d, run, false));
    body.push(deviceDetails(d, u, users, run, danger));
    return { icon: devIcon(d), title: d.name || d.mac, sub: `${t("dr.dev.sub")} · ${u.name}`, body, footer: [] };
  });
}

export function openUserDrawer(userId, getDashboard, refresh) {
  const find = () => (getDashboard()?.users || []).find((u) => u.id === userId);
  const run = mkRun(refresh), danger = mkDanger(run);

  openDrawer(() => {
    const u = find();
    if (!u) { closeDrawer(); return { title: "", body: [], footer: [] }; }
    const devices = u.devices || [], st = userStatus(u);
    const body = [
      h("div", { class: "dr-sec" }, h("h4", {}, t("dr.thisperiod")),
        h("div", { class: "big" }, num(u.used_gb), h("small", {}, u.allowance_gb > 0 ? `${t("common.gb")} ${t("ov.of")} ${num(u.allowance_gb)} ${t("common.gb")}` : `${t("common.gb")} · ${t("pp.unlimited")}`)),
        bar(u.allowance_gb > 0 ? u.percent : 0), h("div", {}, chip(st.cls.replace("chip", "").trim(), st.text))),
      h("div", { class: "dr-sec" }, netSplit({ wan: u.used_gb, lan: u.lan_used_gb })),
    ];
    body.push(h("div", { class: "kv" }, h("div", { class: "row-sw" }, h("div", {}, h("b", {}, t("pp.access")), h("span", {}, u.blocked ? t("pp.st.blocked") : t("common.active"))),
      sw(!u.blocked, t("pp.access"), run(() => actions.blockUser(u.id, !u.blocked))))));
    const gw = getDashboard()?.gateway;
    if (u.protected && gw && gw.blocked_desired && gw.blocked_programmed === false) body.push(h("p", { class: "banner warn", role: "alert" }, t("dr.gw.warn")));
    else if (u.protected && u.blocked) body.push(h("p", { class: "banner info" }, t("dr.gw.cut")));
    if (u.guest) {
      const a = [];
      if (u.blocked) a.push(h("button", { class: "btn primary sm", onclick: run(() => actions.approveGuest(u.id)) }, icon("check", 15), t("dr.approve")));
      a.push(h("button", { class: "btn ghost sm", onclick: run(() => actions.makeRegular(u.id)) }, t("dr.regular")));
      a.push(danger(`rej${u.id}`, t("dr.reject"), () => actions.rejectGuest(u.id)));
      body.push(h("div", { class: "fa" }, a));
    }
    body.push(settingsBlock("user", u, run, true));
    if (!u.protected) body.push(userDetails(u, run));
    if (!u.protected) body.push(h("div", { class: "dr-sec" }, h("h4", {}, t("dr.danger")),
      h("div", { class: "danger-z" },
        h("div", { class: "dz-r" }, h("div", {}, h("b", {}, t("dr.kick")), h("span", {}, t("dr.kick.sub"))), danger(`kick${u.id}`, t("dr.kick"), () => actions.kickUser(u.id), true)),
        h("div", { class: "dz-r" }, h("div", {}, h("b", {}, t("dr.blacklist")), h("span", {}, t("dr.bl.sub"))), danger(`bl${u.id}`, t("dr.blacklist"), () => actions.blacklistUser(u.id), true)))));
    return { icon: "people", title: u.name, sub: u.protected ? t("common.protected") : u.guest ? t("common.guest") : t("dr.person_sub"), body, footer: [] };
  });
}
