import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { api } from "../core/api.js";
import { openDialog } from "./dialog.js";
import { toast } from "./toast.js";

let skipped = false;
// First-run setup (same contract as the classic welcome panel): confirm the bundle and optionally change the default password.
export async function maybeWelcome(refresh) {
  if (skipped) return;
  let st;
  try { st = await api.get("/api/setup"); } catch { return; }
  if (!st || st.setup_complete) return;
  const f = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
  const total = h("input", { class: "input", type: "number", min: "1", step: "any", required: true, value: String(st.total_gb) });
  const ptype = h("select", { class: "select" }, ["renew_day", "end_of_month"].map((v) => h("option", { value: v, selected: (st.period_type || "renew_day") === v }, t("q.period." + v))));
  const day = h("input", { class: "input", type: "number", min: "0", max: "31", step: "1", value: String(st.reset_day ?? 0) });
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password" }), nw = h("input", { class: "input", type: "password", minlength: "12", autocomplete: "new-password" });
  const skip = h("button", { class: "btn ghost", type: "button", onclick: () => { skipped = true; document.querySelector(".dialog .icon-btn")?.click(); } }, t("welcome.skip"));
  openDialog({ title: t("welcome.title"), sub: t("welcome.sub"), submitLabel: t("welcome.save"),
    content: [f(t("q.total"), total), h("div", { class: "form-row" }, f(t("q.period"), ptype), f(t("q.reset"), day)),
      f(t("welcome.cur"), cur, t("welcome.pw.h")), f(t("welcome.new"), nw, t("acct.pw.hint")), skip],
    onSubmit: async () => {
      const body = { total_gb: +total.value, reset_day: Math.max(0, Math.min(31, Math.round(+day.value || 0))), period_type: ptype.value, current_password: cur.value, new_password: nw.value || null };
      if (!(body.total_gb > 0)) throw new Error(t("welcome.bad"));
      if (body.new_password && body.new_password.length < 12) throw new Error(t("acct.pw.short"));
      await api.post("/api/setup/complete", body); toast(t("dr.saved")); await refresh(); return true;
    } });
}
