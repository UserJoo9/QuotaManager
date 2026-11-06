import { h } from "../core/dom.js";
import { t, num } from "../core/i18n.js";
import { api } from "../core/api.js";
import { actions } from "../core/actions.js";
import { sw, chip, bar } from "./ui.js";

// Pending (unsaved) heavy-day edits and the last preview survive the drawer's live rebuilds.
const heavyDraft = new Map(), previews = new Map();

const fmtDay = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
const fmtTime = (iso) => { try { return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); } catch { return ""; } };

// Today's numbers + the reason when the daily limit is what cut the person.
export function dailyToday(u) {
  const q = u.daily;
  if (!q || !q.active) return null;
  return h("div", { class: "dq-today" },
    h("div", { class: "c-num" }, h("b", {}, `${num(q.used_gb)} ${t("common.gb")} ${t("ov.of")} ${num(q.cap_gb)}`), h("span", {}, t("dq.today"))),
    bar(q.cap_gb > 0 ? (q.used_gb / q.cap_gb) * 100 : 100),
    q.blocked ? h("span", { class: "sub" }, `${t("dq.reached")} · ${t("dq.back")} ${fmtTime(q.resets_at)}`) : null);
}

// The "Daily plan" section of a person's drawer. `run(fn, okMsg)` is the drawer's toast+refresh wrapper.
export function dailySection(u, dash, run) {
  if (u.protected) return null;
  const q = u.daily || { mode: "off", gb: 0, carry: "spread", split: "open", heavy: {}, self_edit: false };
  const head = h("h4", {}, t("dq.title"));
  if (u.exempt_quota) return h("div", { class: "dr-sec" }, head, h("p", { class: "sub" }, t("dq.exempt")));
  if (!dash.daily_enabled) return h("div", { class: "dr-sec" }, head, h("p", { class: "sub" }, t("dq.disabled")));

  const hasEnd = !!(dash.bundle && dash.bundle.period_end);
  const opt = (v, label, cur, dis) => h("option", { value: v, selected: cur === v, disabled: !!dis }, label);
  const mode = h("select", { class: "select" }, [opt("off", t("dq.mode.off"), q.mode), opt("fixed", t("dq.mode.fixed"), q.mode), opt("spread", t("dq.mode.spread"), q.mode, !hasEnd)]);
  const gb = h("input", { class: "input", type: "number", min: "0", step: "any", value: String(q.gb || 0), inputmode: "decimal", "aria-label": t("dq.gb") });
  const carry = h("select", { class: "select" }, ["next_day", "spread"].map((v) => opt(v, t("dq.carry." + v), q.carry)));
  const split = h("select", { class: "select" }, ["open", "equal", "fixed"].map((v) => opt(v, t("dq.split." + v), q.split)));
  const gbField = h("div", { class: "field" }, h("label", {}, t("dq.gb")), gb);
  const splitHint = h("span", { class: "sub" }, t("dq.split.fixed.hint"));
  const sync = () => { gbField.hidden = mode.value !== "fixed"; splitHint.hidden = split.value !== "fixed"; };
  mode.addEventListener("change", sync); split.addEventListener("change", sync); sync();

  // heavy days editor (draft survives rebuilds until saved)
  if (!heavyDraft.has(u.id)) heavyDraft.set(u.id, { ...(q.heavy || {}) });
  const heavy = heavyDraft.get(u.id);
  const list = h("div", { class: "dq-heavy" });
  const paint = () => {
    const days = Object.keys(heavy).sort();
    list.replaceChildren(...(days.length ? days.map((d) => h("span", { class: "chip" }, `${fmtDay(d)} · ${num(heavy[d])} ${t("common.gb")}`,
      h("button", { type: "button", class: "dq-x", "aria-label": t("dq.heavy.del"), onclick: () => { delete heavy[d]; paint(); } }, "×")))
      : [h("span", { class: "sub" }, t("dq.heavy.none"))]));
  };
  paint();
  const today = new Date(), min = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const dayIn = h("input", { class: "input", type: "date", min, "aria-label": t("dq.heavy.date") });
  const dayGb = h("input", { class: "input", type: "number", min: "0", step: "any", placeholder: t("dq.heavy.gb"), inputmode: "decimal", "aria-label": t("dq.heavy.gb") });
  const addHeavy = () => {
    const v = +dayGb.value;
    if (!dayIn.value || !(v > 0)) return;
    heavy[dayIn.value] = v; dayIn.value = ""; dayGb.value = ""; paint();
  };

  const form = () => ({ daily_mode: mode.value, daily_gb: mode.value === "fixed" ? Math.max(0, +gb.value || 0) : (q.gb || 0),
    daily_carry: carry.value, daily_split: split.value, daily_heavy: { ...heavy } });
  const box = h("div", { class: "dq-prev" });
  const paintPreview = (p) => {
    if (!p) { box.replaceChildren(); return; }
    if (p.error) { box.replaceChildren(h("p", { class: "banner warn", role: "alert" }, p.error)); return; }
    if (!p.schedule || !p.schedule.length) { box.replaceChildren(); return; }
    const rows = p.schedule.slice(0, 8);
    box.replaceChildren(
      h("div", { class: "kv" },
        h("div", {}, h("span", { class: "k" }, t("dq.prev.today")), h("span", { class: "v" }, `${num(p.cap_gb)} ${t("common.gb")}`)),
        h("div", {}, h("span", { class: "k" }, t("dq.prev.used")), h("span", { class: "v" }, `${num(p.used_gb)} ${t("common.gb")}`))),
      h("div", { class: "dq-days" }, rows.map((r) => h("span", { class: "chip" + (r.heavy ? " accent" : "") }, `${fmtDay(r.date)} · ${num(r.cap_gb)}`))),
      p.schedule.length > rows.length ? h("span", { class: "sub" }, `+${num(p.schedule.length - rows.length, 0)} ${t("common.days")}`) : null);
  };
  paintPreview(previews.get(u.id));
  const doPreview = async () => {
    const f = form();
    if (f.daily_mode === "off") { previews.delete(u.id); paintPreview(null); return; }
    try {
      const p = await api.post(`/api/users/${u.id}/daily-plan/preview`, { mode: f.daily_mode, daily_gb: f.daily_gb, carry: f.daily_carry, split: f.daily_split, heavy: f.daily_heavy });
      previews.set(u.id, p); paintPreview(p);
    } catch (e) { paintPreview({ error: e.message }); }
  };
  const save = run(async () => {
    await actions.patchUser(u.id, form());
    heavyDraft.delete(u.id); previews.delete(u.id);
  }, t("dr.saved"));
  const selfEdit = h("div", { class: "row-sw" }, h("div", {}, h("b", {}, t("dq.selfedit")), h("span", {}, t("dq.selfedit.sub"))),
    sw(q.self_edit, t("dq.selfedit"), run(() => actions.patchUser(u.id, { daily_self_edit: !q.self_edit }))));

  return h("div", { class: "dr-sec" }, head,
    q.active ? dailyToday(u) : null,
    h("div", { class: "field" }, h("label", {}, t("dq.mode")), mode), gbField,
    h("div", { class: "field" }, h("label", {}, t("dq.carry")), carry),
    h("div", { class: "field" }, h("label", {}, t("dq.split")), split),
    splitHint,
    !hasEnd ? h("span", { class: "sub" }, t("dq.mode.spread.no")) : null,
    h("div", { class: "field" }, h("label", {}, t("dq.heavy")), list,
      h("div", { class: "dq-add" }, dayIn, dayGb, h("button", { type: "button", class: "btn sm", onclick: addHeavy }, t("dq.heavy.add"))),
      h("span", { class: "sub" }, t("dq.heavy.hint"))),
    selfEdit, box,
    h("div", { class: "fa" }, h("button", { type: "button", class: "btn ghost sm", onclick: doPreview }, t("dq.preview")), h("button", { type: "button", class: "btn primary sm", onclick: save }, t("dr.save"))));
}

// Fixed-share GB of one device (only meaningful when its owner's split is "fixed").
export function deviceShare(d, u, run) {
  if (!u.daily || u.daily.mode === "off" || u.daily.split !== "fixed") return null;
  const share = h("input", { class: "input", type: "number", min: "0", step: "any", value: String(d.daily_share_gb || 0), inputmode: "decimal", "aria-label": t("dq.share") });
  return h("div", { class: "dr-sec" }, h("h4", {}, t("dq.title")),
    h("div", { class: "field" }, h("label", {}, t("dq.share")), share, h("span", { class: "sub" }, t("dq.share.hint"))),
    d.daily_cap_gb != null ? h("span", { class: "sub" }, `${t("dq.today")}: ${num(d.daily_used_gb || 0)} ${t("ov.of")} ${num(d.daily_cap_gb)} ${t("common.gb")}`) : null,
    h("div", { class: "fa" }, h("button", { type: "button", class: "btn primary sm", onclick: run(() => actions.patchDevice(d.id, { daily_share_gb: Math.max(0, +share.value || 0) }), t("dr.saved")) }, t("dr.save"))));
}

// Master switch card for the Quota page.
export function dailyMasterCard(on, toggle) {
  return h("section", { class: "card c12" },
    h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dq.master")), h("span", { class: "sub" }, t("dq.master.d"))), on ? chip("ok", t("common.active")) : null),
    h("div", { class: "row-sw" }, h("div", {}, h("b", {}, t("dq.master.sw")), h("span", {}, t("dq.master.sw.d"))), sw(on, t("dq.master.sw"), toggle)));
}
