import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { avatar, bar, sw, chip, userStatus, devIcon, fmtBps, netLine } from "../components/ui.js";
import { openUserDrawer, openDeviceDrawer } from "../components/userDrawer.js";
import { ov, loadLive } from "../core/overviewData.js";
import { actions } from "../core/actions.js";
import { toast } from "../components/toast.js";
import { openAddUser, openAddDevice } from "../components/peopleDialogs.js";

let query = "", filter = "all", timer = null;
const openIds = new Set();
const mine = () => location.hash === "#/people";
function poll(ctx) {
  if (timer) return;
  const tick = async () => { if (!mine()) { clearInterval(timer); timer = null; return; } try { await loadLive(); ctx.touch(); } catch { /* 401 handled */ } };
  tick(); timer = setInterval(tick, 5000);
}

export default function people(ctx) {
  const d = ctx.dashboard;
  if (!d) return h("div", { class: "page" }, h("div", { class: "skeleton", style: { height: "320px" } }));
  poll(ctx);
  // The gateway (protected) person always stays on top; stable sort keeps the server order for everyone else.
  const all = (d.users || []).map((u, i) => [u, i]).sort((a, b) => (b[0].protected ? 1 : 0) - (a[0].protected ? 1 : 0) || a[1] - b[1]).map((x) => x[0]);
  const liveBy = new Map((ov.live?.devices || []).map((x) => [x.id, x]));
  const act = (fn) => async () => { try { await fn(); await ctx.refresh(); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } };
  const open = (u) => openUserDrawer(u.id, ctx.getDashboard, ctx.refresh);
  const openDev = (u, x) => openDeviceDrawer(u.id, x.id, ctx.getDashboard, ctx.refresh);
  const is = {
    all: () => true, online: (u) => !u.blocked && (u.devices || []).some((x) => x.connected),
    blocked: (u) => u.blocked && !u.guest, guest: (u) => u.guest,
  };
  const body = h("div", {});
  const fill = () => {
    const q = query.trim().toLowerCase();
    const users = all.filter((u) => is[filter](u) && (!q || u.name.toLowerCase().includes(q) ||
      (u.devices || []).some((x) => (x.name || "").toLowerCase().includes(q) || (x.mac || "").toLowerCase().includes(q) || (x.ip || "").includes(q))));
    body.replaceChildren(...(users.length ? users.map((u) => row(u)) : [h("div", { class: "empty" }, h("b", {}, t("pp.empty")), h("span", {}, t("pp.empty.sub")))]));
  };
  function row(u) {
    const st = userStatus(u), devs = [...(u.devices || [])].sort((a, b) => (b.gateway ? 1 : 0) - (a.gateway ? 1 : 0)), isOpen = openIds.has(u.id);
    const toggle = () => { if (isOpen) openIds.delete(u.id); else openIds.add(u.id); ctx.touch(); };
    const online = devs.filter((x) => x.connected).length;
    return h("div", {},
      h("div", { class: "urow" + (u.blocked ? " is-off" : "") },
        h("button", { type: "button", class: "chev" + (isOpen ? " open" : ""), "aria-expanded": String(isOpen), "aria-label": u.name, onclick: toggle }, icon("chev", 18)),
        h("div", { class: "u-id", onclick: () => open(u) }, avatar(u.name, u.id),
          h("div", {}, h("div", { class: "u-name" }, u.name, u.protected ? chip("", t("common.protected")) : null, u.guest ? chip("accent", t("common.guest")) : null),
            h("div", { class: "u-sub" }, `${num(devs.length, 0)} ${t("pp.devs")} · ${num(online, 0)} ${t("pp.on")}`))),
        h("div", { class: "u-use" }, h("div", { class: "u-num" }, h("b", {}, `${num(u.used_gb)} ${t("common.gb")}`), h("span", {}, u.allowance_gb > 0 ? `${t("ov.of")} ${num(u.allowance_gb)}` : t("pp.unlimited")), u.allowance_gb > 0 ? h("em", {}, `${num(u.percent, 0)}%`) : null),
          bar(u.allowance_gb > 0 ? u.percent : 0), netLine(u.used_gb, u.lan_used_gb)),
        h("div", { class: "u-stat" }, chip(st.cls.replace("chip", "").trim(), st.text)),
        sw(!u.blocked, t("pp.access"), act(() => actions.blockUser(u.id, !u.blocked)))),
      isOpen ? h("div", { class: "drows" }, devs.length ? devs.map((x) => {
        const lv = liveBy.get(x.id);
        return h("div", { class: "drow" },
          h("button", { type: "button", class: "d-main", onclick: () => openDev(u, x) }, h("span", { class: "d-ic" }, icon(devIcon(x), 16)),
            h("span", {}, h("span", { class: "d-name" }, x.name || x.mac), h("span", { class: "d-meta" }, h("span", { class: "mono" }, iso(x.mac)), x.ip ? h("span", { class: "mono" }, iso(x.ip)) : null))),
          h("div", { class: "d-use" }, h("div", { class: "c-num" }, h("b", {}, `${num(x.device_used_gb)} ${t("common.gb")}`), h("span", {}, u.allowance_gb > 0 ? `${num((x.device_used_gb / u.allowance_gb) * 100, 0)}%` : "")),
            bar(u.allowance_gb > 0 ? (x.device_used_gb / u.allowance_gb) * 100 : 0), netLine(x.device_used_gb, x.lan_used_gb)),
          h("div", { class: "d-live" }, x.connected ? [h("span", {}, "↓ ", h("b", {}, fmtBps(lv?.down_bps || 0))), h("span", {}, "↑ ", h("b", {}, fmtBps(lv?.up_bps || 0)))] : h("span", {}, t("pp.st.offline"))),
          x.gateway ? h("span", {}) : sw(!x.blocked, t("pp.access"), act(() => actions.blockDevice(x.id, !x.blocked))));
      }) : h("div", { class: "empty" }, t("pp.nodevices"))) : null);
  }
  const input = h("input", { type: "search", placeholder: t("pp.search"), value: query, "aria-label": t("pp.search"), oninput: (e) => { query = e.target.value; fill(); } });
  const count = (f) => all.filter(is[f]).length;
  const seg = h("div", { class: "seg", role: "group" }, ["all", "online", "blocked", "guest"].map((f) =>
    h("button", { type: "button", class: filter === f ? "on" : "", "aria-pressed": String(filter === f), onclick: () => { filter = f; ctx.touch(); } }, t("pp.f." + f), h("em", {}, num(count(f), 0)))));
  fill();
  return h("div", { class: "page" },
    h("div", { class: "toolbar" }, h("label", { class: "search" }, icon("search", 18), input), seg,
      h("button", { type: "button", class: "btn primary sm", onclick: () => openAddUser(ctx.refresh) }, icon("userplus", 15), t("add.user")),
      h("button", { type: "button", class: "btn sm", onclick: () => openAddDevice(all, ctx.refresh) }, icon("plus", 15), t("add.dev"))),
    h("section", { class: "card list-card" },
      h("div", { class: "lhead" }, h("span"), h("span", {}, t("pp.col.user")), h("span", {}, t("pp.col.usage")), h("span", {}, t("pp.col.status")), h("span", {}, t("pp.col.access"))),
      body),
    (d.rogue || []).length ? h("section", { class: "card", style: { "margin-top": "16px" } },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("pp.rogue")), h("span", { class: "sub" }, t("pp.rogue.d"))), chip("warn", `${num(d.rogue.length, 0)}`)),
      d.rogue.map((r) => h("div", { class: "crow", style: { "grid-template-columns": "36px 1fr auto" } },
        h("span", { class: "d-ic" }, icon("alert", 16)),
        h("div", { class: "c-name" }, r.ip ? iso(r.ip) : "", h("span", { class: "mono" }, [iso(r.mac || ""), r.vendor && r.vendor !== "Unknown" ? r.vendor : ""].filter(Boolean).join(" · "))),
        chip(r.online ? "ok" : "", r.online ? t("pp.st.online") : t("pp.st.offline"))))) : null);
}
