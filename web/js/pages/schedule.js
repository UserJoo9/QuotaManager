import { h, icon, mount } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { api } from "../core/api.js";
import { chip, sw, clock } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

// Internet schedule: turn the internet off for chosen people / devices at chosen
// hours of chosen days (bedtime, study time). The gateway enforces it in the
// kernel with a timeout, so a device comes back on time even if the service stops.
const sc = { data: null, loading: false, at: 0, failAt: 0, armed: new Set(), sitesDraft: null };
const fail = (e) => toast(`${t("dr.failed")}: ${e.message}`, "bad");
// Saturday first (the Egyptian week); values are Python weekdays (0 = Monday).
const WEEK = [5, 6, 0, 1, 2, 3, 4];
const SCHOOL = [6, 0, 1, 2, 3];   // Sunday .. Thursday
const ALL = [0, 1, 2, 3, 4, 5, 6];
const PRESETS = [
  ["sc.pre.school", [{ days: SCHOOL, start: "23:00", end: "07:00" }]],
  ["sc.pre.night", [{ days: ALL, start: "00:00", end: "07:00" }]],
  ["sc.pre.study", [{ days: SCHOOL, start: "16:00", end: "19:00" }]],
  ["sc.pre.meals", [{ days: ALL, start: "14:00", end: "15:00" }, { days: ALL, start: "20:00", end: "20:45" }]],
];

async function load(ctx, force) {
  if (sc.loading || (!force && sc.data && Date.now() - sc.at < 20000)) return;
  sc.loading = true;
  try { sc.data = await api.get("/api/schedules"); sc.at = Date.now(); } catch { sc.failAt = Date.now(); } finally { sc.loading = false; ctx.touch(); }
}
const setData = (ctx, d) => { sc.data = d; sc.at = Date.now(); ctx.touch(); ctx.refresh && ctx.refresh(); };
const mins = (s) => { const [a, b] = String(s).split(":").map(Number); return a * 60 + b; };
const dayName = (d) => t(`sc.d${d}`);
const people = (ctx) => ((ctx.dashboard && ctx.dashboard.users) || []).filter((u) => !u.protected);
const devices = (ctx) => ((ctx.dashboard && ctx.dashboard.devices) || []).filter((d) => d.mac && d.mac !== "00:00:00:00:00:00");

// [{day, from, to}] minutes per weekday, a window past midnight split in two
function segments(windows) {
  const out = [];
  for (const w of windows || []) {
    const a = mins(w.start), b = mins(w.end);
    for (const d of w.days) {
      if (b > a) out.push({ day: d, from: a, to: b });
      else { out.push({ day: d, from: a, to: 1440 }); if (b > 0) out.push({ day: (d + 1) % 7, from: 0, to: b }); }
    }
  }
  return out;
}

function timeline(windows, active) {
  const segs = segments(windows);
  const now = new Date(), today = (now.getDay() + 6) % 7, nowPct = ((now.getHours() * 60 + now.getMinutes()) / 1440) * 100;
  return h("div", { class: "sc-week", "aria-hidden": "true" },
    WEEK.map((d) => h("div", { class: "sc-row" + (d === today ? " today" : "") },
      h("span", { class: "sc-day" }, dayName(d)),
      h("div", { class: "sc-bar" },
        segs.filter((s) => s.day === d).map((s) => h("i", { class: "sc-seg" + (active ? " on" : ""), style: { left: `${(s.from / 1440) * 100}%`, width: `${((s.to - s.from) / 1440) * 100}%` }, title: `${String(Math.floor(s.from / 60)).padStart(2, "0")}:${String(s.from % 60).padStart(2, "0")}` })),
        d === today ? h("b", { class: "sc-now", style: { left: `${nowPct}%` } }) : null))),
    h("div", { class: "sc-row sc-axis" }, h("span"), h("div", { class: "sc-ticks" }, ["00", "06", "12", "18", "24"].map((x) => h("span", {}, x)))));
}

function status(s) {
  const st = s.state || {};
  if (!s.enabled) return chip("", t("sc.st.disabled"));
  if (st.active) return chip("danger", `${t("sc.st.on")} ${clock(st.until_ts)}`);
  if (st.paused) return chip("accent", `${t("sc.st.bonus")} ${clock(s.paused_until)}`);
  if (st.skipped_today && !st.next_start_ts) return chip("", t("sc.st.dayoff"));
  if (st.next_start_ts) {
    const d = new Date(st.next_start_ts * 1000);
    return chip("ok", `${t("sc.st.next")} ${dayName((d.getDay() + 6) % 7)} ${clock(st.next_start_ts)}`);
  }
  return chip("", t("sc.st.idle"));
}

function who(ctx, s) {
  const us = people(ctx).filter((u) => s.users.includes(u.id)).map((u) => u.name);
  const ds = devices(ctx).filter((d) => s.devices.includes(d.id)).map((d) => d.name || d.mac);
  return [...us, ...ds].join("، ") || "—";
}

const body = (s, patch) => ({ name: s.name, enabled: s.enabled, users: s.users, devices: s.devices, windows: s.windows, skip_dates: s.skip_dates, ...patch });

function editor(ctx, s) {
  const st = s ? JSON.parse(JSON.stringify(body(s))) : { name: "", enabled: true, users: [], devices: [], windows: [{ days: SCHOOL.slice(), start: "23:00", end: "07:00" }], skip_dates: [] };
  const name = h("input", { class: "input", required: true, maxlength: "40", value: st.name, placeholder: t("sc.name.ph") });
  const winBox = h("div", { class: "sc-wins" });
  const preview = h("div");
  const dateList = h("div", { class: "sc-dates" });
  const dateIn = h("input", { class: "input ltr", type: "date", style: { width: "auto" } });
  const redraw = () => {
    mount(preview, timeline(st.windows, false));
    mount(winBox, st.windows.map((w, i) => h("div", { class: "sc-win" },
      h("div", { class: "sc-days", role: "group", "aria-label": t("sc.days") }, WEEK.map((d) => h("button", { type: "button", class: "sc-pill" + (w.days.includes(d) ? " on" : ""), "aria-pressed": String(w.days.includes(d)),
        onclick: () => { w.days = w.days.includes(d) ? w.days.filter((x) => x !== d) : [...w.days, d]; redraw(); } }, dayName(d)))),
      h("div", { class: "sc-times" },
        h("label", {}, t("sc.from"), h("input", { class: "input ltr", type: "time", required: true, value: w.start, onchange: (e) => { w.start = e.target.value; redraw(); } })),
        h("label", {}, t("sc.to"), h("input", { class: "input ltr", type: "time", required: true, value: w.end === "24:00" ? "00:00" : w.end, onchange: (e) => { w.end = e.target.value; redraw(); } })),
        mins(w.end) <= mins(w.start) && mins(w.end) !== 1440 ? h("span", { class: "sub" }, t("sc.nextday")) : null,
        st.windows.length > 1 ? h("button", { type: "button", class: "btn ghost sm", "aria-label": t("sc.win.del"), onclick: () => { st.windows.splice(i, 1); redraw(); } }, icon("x", 15)) : null))));
    mount(dateList, st.skip_dates.map((d) => h("span", { class: "chip" }, d, h("button", { type: "button", class: "sc-x", "aria-label": t("sc.win.del"), onclick: () => { st.skip_dates = st.skip_dates.filter((x) => x !== d); redraw(); } }, "×"))));
  };
  redraw();
  const pick = (list, key, label) => h("div", { class: "sc-pick" }, list.length ? list.map((x) => h("label", { class: "sc-check" },
    h("input", { type: "checkbox", checked: st[key].includes(x.id), onchange: (e) => { st[key] = e.target.checked ? [...st[key], x.id] : st[key].filter((y) => y !== x.id); } }),
    h("span", {}, label(x)))) : h("span", { class: "sub" }, t("sc.none.people")));
  openDialog({ title: s ? t("sc.edit") : t("sc.new"), sub: t("sc.dlg.sub"), submitLabel: t("sc.save"), wide: true,
    content: [
      h("div", { class: "field" }, h("label", {}, t("sc.name")), name),
      h("div", { class: "field" }, h("label", {}, t("sc.presets")),
        h("div", { class: "fa" }, PRESETS.map(([k, w]) => h("button", { type: "button", class: "btn ghost sm", onclick: () => { st.windows = JSON.parse(JSON.stringify(w)); if (!name.value.trim()) name.value = t(k); redraw(); } }, t(k))))),
      h("div", { class: "field" }, h("label", {}, t("sc.when")), winBox,
        h("button", { type: "button", class: "btn ghost sm", style: { "margin-top": "8px" }, onclick: () => { st.windows.push({ days: ALL.slice(), start: "22:00", end: "06:00" }); redraw(); } }, icon("plus", 15), t("sc.win.add")),
        preview),
      h("div", { class: "field" }, h("label", {}, t("sc.people")), pick(people(ctx), "users", (u) => `${u.name} (${(u.devices || []).length})`)),
      h("details", { class: "field", open: st.devices.length > 0 }, h("summary", {}, t("sc.devices")),
        pick(devices(ctx), "devices", (d) => `${d.name || d.mac}${d.user_name ? " · " + d.user_name : ""}`)),
      h("div", { class: "field" }, h("label", {}, t("sc.skip")), h("span", { class: "sub" }, t("sc.skip.h")),
        h("div", { class: "fa" }, dateIn, h("button", { type: "button", class: "btn ghost sm", onclick: () => { const v = dateIn.value; if (v && !st.skip_dates.includes(v)) { st.skip_dates.push(v); st.skip_dates.sort(); redraw(); } } }, icon("plus", 15), t("sc.skip.add"))),
        dateList)],
    onSubmit: async () => {
      st.name = name.value.trim();
      if (st.windows.some((w) => !w.days.length)) throw new Error(t("sc.err.days"));
      if (!st.users.length && !st.devices.length) throw new Error(t("sc.err.who"));
      const r = s ? await api.put(`/api/schedules/${s.id}`, st) : await api.post("/api/schedules", st);
      setData(ctx, r); toast(t("sc.saved"));
      return true;
    } });
}

function sitesCard(ctx) {
  const sites = (sc.data && sc.data.sites) || [];
  const ta = h("textarea", { class: "input ltr", rows: "5", dir: "ltr", placeholder: "classroom.google.com\nedmodo.com", style: { height: "auto", "min-height": "110px", "font-family": "var(--mono)" },
    oninput: (e) => { sc.sitesDraft = e.target.value; } }, sc.sitesDraft ?? sites.join("\n"));
  const save = async () => {
    try { setData(ctx, await api.put("/api/schedule-sites", { sites: ta.value.split(/[\s,]+/).filter(Boolean) })); sc.sitesDraft = null; toast(t("sc.saved")); } catch (e) { fail(e); }
  };
  return h("section", { class: "card c5" },
    h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("sc.sites")), h("span", { class: "sub" }, t("sc.sites.d")))),
    ta, h("div", { class: "fa", style: { "margin-top": "10px" } }, h("button", { type: "button", class: "btn primary sm", onclick: save }, t("sc.save")),
      h("span", { class: "sub" }, `${sites.length} ${t("sc.sites.n")}`)));
}

export default function schedulePage(ctx) {
  if (Date.now() - sc.failAt > 10000) load(ctx);
  const d = sc.data, list = (d && d.schedules) || [];
  const act = async (fn) => { try { setData(ctx, await fn()); } catch (e) { fail(e); } };
  const bonus = (s, m) => act(() => api.post(`/api/schedules/${s.id}/pause`, { minutes: m }));
  const del = (s) => async (ev) => {
    const k = "del" + s.id;
    if (!sc.armed.has(k)) { sc.armed.add(k); ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { sc.armed.delete(k); ctx.touch(); }, 4000); return; }
    sc.armed.delete(k); act(() => api.del(`/api/schedules/${s.id}`));
  };
  const offNow = ((ctx.dashboard && ctx.dashboard.devices) || []).filter((x) => x.schedule).length;
  const card = (s) => {
    const st = s.state || {};
    return h("div", { class: "sc-card" + (st.active ? " live" : "") },
      h("div", { class: "sc-head" },
        h("div", { class: "c-name" }, s.name, h("span", { class: "sub" }, `${who(ctx, s)} · ${s.device_count} ${t("sc.devs")}`)),
        status(s),
        sw(s.enabled, t("sc.enabled"), () => act(() => api.put(`/api/schedules/${s.id}`, body(s, { enabled: !s.enabled }))))),
      timeline(s.windows, st.active),
      h("div", { class: "fa" },
        st.active || st.paused ? h("div", { class: "fa", role: "group", "aria-label": t("sc.bonus") },
          h("span", { class: "sub" }, t("sc.bonus")),
          [15, 30, 60].map((m) => h("button", { type: "button", class: "btn ghost sm", onclick: () => bonus(s, m) }, `+${m} ${t("sc.min")}`)),
          st.paused ? h("button", { type: "button", class: "btn ghost sm", onclick: () => bonus(s, 0) }, t("sc.bonus.end")) : null) : null,
        h("span", { class: "grow" }),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => editor(ctx, s) }, t("sc.edit.btn")),
        h("button", { type: "button", class: "btn danger sm" + (sc.armed.has("del" + s.id) ? " armed" : ""), onclick: del(s) }, sc.armed.has("del" + s.id) ? t("dr.confirm") : t("sc.del"))));
  };
  return h("div", { class: "page" }, h("div", { class: "grid" },
    h("section", { class: "card c7" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("sc.title")), h("span", { class: "sub" }, t("sc.title.d"))),
        h("button", { type: "button", class: "btn primary sm", onclick: () => editor(ctx, null) }, icon("plus", 15), t("sc.new"))),
      offNow ? h("p", { class: "banner warn", role: "status" }, `${offNow} ${t("sc.offnow")}`) : null,
      !d ? h("div", { class: "skeleton", style: { height: "160px" } })
        : list.length ? h("div", { class: "sc-list" }, list.map(card))
          : h("div", { class: "empty" }, h("p", {}, t("sc.empty")), h("button", { type: "button", class: "btn primary sm", onclick: () => editor(ctx, null) }, icon("plus", 15), t("sc.new")))),
    sitesCard(ctx),
    h("section", { class: "card c5" },
      h("header", { class: "card-h" }, h("h3", {}, t("sc.how"))),
      h("ul", { class: "sc-how" }, ["sc.how.1", "sc.how.2", "sc.how.3", "sc.how.4"].map((k) => h("li", {}, t(k)))))));
}
