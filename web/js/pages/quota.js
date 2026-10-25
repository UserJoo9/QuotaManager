import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { bar, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

let draft = null, topup = "", armedReset = false, armedDel = null, busy = false;
const DAY = 86400;

export default function quota(ctx) {
  const d = ctx.dashboard;
  if (!d) return h("div", { class: "page" }, h("div", { class: "skeleton", style: { height: "260px" } }));
  const b = d.bundle, users = d.users || [];
  if (!draft) draft = { total_gb: b.base_total_gb ?? b.total_gb, reset_day: b.reset_day ?? 0, period_type: b.period_type === "end_of_month" ? "end_of_month" : "renew_day" };
  const run = (fn, ok) => async () => {
    if (busy) return; busy = true;
    try { await fn(); toast(ok || t("q.saved")); await ctx.refresh(); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { busy = false; ctx.touch(); }
  };
  const field = (label, input, hint) => h("div", { class: "field" }, h("label", {}, label), input, hint ? h("span", { class: "sub" }, hint) : null);
  const total = h("input", { class: "input", type: "number", min: "1", step: "any", value: String(draft.total_gb), oninput: (e) => { draft.total_gb = +e.target.value; } });
  const reset = h("input", { class: "input", type: "number", min: "0", max: "31", step: "1", value: String(draft.reset_day), oninput: (e) => { draft.reset_day = parseInt(e.target.value || "0", 10); } });
  const ptype = h("select", { class: "select", onchange: (e) => { draft.period_type = e.target.value; } },
    ["renew_day", "end_of_month"].map((v) => h("option", { value: v, selected: draft.period_type === v }, t("q.period." + v))));
  const save = run(async () => {
    if (!(draft.total_gb > 0)) throw new Error(t("q.total"));
    await api.post("/api/bundle", { total_gb: draft.total_gb, reset_day: draft.reset_day, period_type: draft.period_type }); draft = null;
  });
  const addGb = h("input", { class: "input", type: "number", min: "0", step: "any", placeholder: "10", value: topup, oninput: (e) => { topup = e.target.value; } });
  const doTop = run(async () => { const gb = +topup; if (!(gb > 0)) throw new Error(t("q.topup.gb")); await api.post("/api/bundle", { add_gb: gb }); topup = ""; });
  const doReset = async (ev) => {
    if (!armedReset) { armedReset = true; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { armedReset = false; }, 4000); return; }
    armedReset = false; await run(() => api.post("/api/reset-month"))();
  };

  const packs = b.recharges || [];
  const who = (p) => p.target_type === "user" ? (users.find((u) => u.id === p.target_id)?.name || "#" + p.target_id)
    : p.target_type === "device" ? (users.flatMap((u) => u.devices || []).find((x) => x.id === p.target_id)?.name || "#" + p.target_id) : t("q.pack.all");
  const expiry = (p) => { const dl = Math.ceil((p.expires_at * 1000 - Date.now()) / 86400000); return dl > 0 ? `${num(dl, 0)} ${t("common.days")}` : "–"; };
  const delPack = (p) => h("button", { type: "button", class: "btn danger sm" + (armedDel === p.id ? " armed" : ""), "aria-label": t("q.pack.del"),
    onclick: async (ev) => {
      if (armedDel !== p.id) { armedDel = p.id; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { if (armedDel === p.id) armedDel = null; }, 4000); return; }
      armedDel = null; await run(() => api.del(`/api/bundle/recharges/${p.id}`), t("dr.done"))();
    } }, armedDel === p.id ? t("dr.confirm") : icon("x", 15));

  function packDialog() {
    const gb = h("input", { class: "input", type: "number", min: "0", step: "any", required: true, placeholder: "50" });
    const days = h("input", { class: "input", type: "number", min: "1", step: "1", value: "30" });
    const rec = h("input", { type: "checkbox" });
    const tt = h("select", { class: "select" }, ["all", "user", "device"].map((v) => h("option", { value: v }, t("q.pack." + v))));
    const tgt = h("select", { class: "select" });
    const tgtF = field(t("q.pack.who"), tgt); tgtF.hidden = true;
    const fill = () => {
      const v = tt.value; tgtF.hidden = v === "all";
      const opts = v === "user" ? users.map((u) => [u.id, u.name]) : users.flatMap((u) => (u.devices || []).map((x) => [x.id, `${x.name || x.mac} · ${u.name}`]));
      tgt.replaceChildren(...opts.map(([id, n]) => h("option", { value: id }, n)));
    };
    tt.addEventListener("change", fill);
    const note = h("input", { class: "input" });
    openDialog({ title: t("q.pack.title"), submitLabel: t("q.pack.create"),
      content: [h("div", { class: "form-row" }, field(t("q.pack.gb"), gb), field(t("q.pack.days"), days)), h("div", { class: "form-row" }, field(t("q.pack.target"), tt), tgtF),
        h("label", { class: "row-sw", style: { cursor: "pointer" } }, h("span", {}, t("q.pack.recurring")), rec), field(t("q.pack.note"), note)],
      onSubmit: async () => {
        const body = { gb: +gb.value, expires_at: Math.floor(Date.now() / 1000) + Math.max(1, +days.value || 30) * DAY, recurring: rec.checked, target_type: tt.value, comment: note.value.trim() };
        if (!(body.gb > 0)) throw new Error(t("q.pack.gb"));
        if (tt.value !== "all") { body.target_id = parseInt(tgt.value, 10); if (!body.target_id) throw new Error(t("q.pack.who")); }
        await api.post("/api/bundle/recharges", body); await ctx.refresh(); toast(t("q.saved")); return true;
      } });
  }

  const pct = b.total_gb > 0 ? (b.used_gb / b.total_gb) * 100 : 0;
  return h("div", { class: "page" }, h("div", { class: "grid" },
    h("section", { class: "card c7" },
      h("header", { class: "card-h" }, h("h3", {}, t("q.bundle")), chip("", b.days_left >= 0 ? `${t("ov.resets")} ${num(b.days_left, 0)} ${t("common.days")}` : t("ov.period.manual"))),
      h("div", { class: "c-num", style: { "margin-bottom": "8px" } }, h("b", {}, `${num(b.used_gb)} ${t("common.gb")} ${t("ov.of")} ${num(b.total_gb)}`), h("span", {}, `${num(pct, 0)}%`)), bar(pct),
      h("div", { class: "form-row", style: { "margin-top": "20px" } }, field(t("q.total"), total), field(t("q.reset"), reset)),
      h("div", { style: { "margin-top": "12px" } }, field(t("q.period"), ptype)),
      h("div", { class: "fa", style: { "margin-top": "18px" } }, h("button", { type: "button", class: "btn primary", onclick: save }, t("q.save")))),
    h("section", { class: "card c5" },
      h("header", { class: "card-h" }, h("h3", {}, t("q.topup"))), h("p", { class: "sub", style: { "margin-bottom": "12px" } }, t("q.topup.d")),
      h("div", { style: { display: "flex", gap: "10px" } }, h("div", { class: "grow" }, addGb), h("button", { type: "button", class: "btn primary", onclick: doTop }, icon("plus", 16), t("q.topup.btn"))),
      h("div", { class: "setting" }, h("div", {}, h("div", { class: "t" }, t("q.newperiod")), h("div", { class: "d" }, t("q.newperiod.d"))),
        h("button", { type: "button", class: "btn danger sm", onclick: doReset }, t("q.newperiod.btn")))),
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("h3", {}, t("q.packs")), h("button", { type: "button", class: "btn primary sm", onclick: packDialog }, icon("plus", 15), t("q.packs.new"))),
      packs.length ? h("div", {}, packs.map((p) => h("div", { class: "crow", style: { "grid-template-columns": "36px minmax(120px,1fr) minmax(130px,1.2fr) 84px auto" } },
        h("span", { class: "d-ic" }, icon("bolt", 16)),
        h("div", { class: "c-name" }, `${num(p.gb)} ${t("common.gb")}`, h("span", {}, [who(p), p.comment].filter(Boolean).join(" · "))),
        h("div", { class: "c-use" }, h("div", { class: "c-num" }, h("b", {}, `${num(p.remaining_gb)} ${t("common.gb")} ${t("q.pack.left")}`), h("span", {}, `${t("q.pack.expires")}: ${expiry(p)}`)), bar(p.gb > 0 ? (p.used_gb / p.gb) * 100 : 0, true)),
        p.recurring ? chip("accent", t("q.pack.recur")) : h("span"), delPack(p))))
        : h("div", { class: "empty" }, t("q.packs.none")))));
}
