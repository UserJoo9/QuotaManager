import { h, icon } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { getPrefs, setPref, ACCENTS, TONES, PARTICLES, effTheme } from "../core/prefs.js";
import { chip } from "../components/ui.js";
import { accountTab, updatesTab, logsTab } from "./systemTabs.js";

let tab = "appearance";

function appearance(ctx) {
  const p = getPrefs(), th = effTheme();
  const opts = (key, list, withPv) => h("div", { class: "opts" }, list.map(([v, label, desc, pv]) =>
    h("button", { type: "button", class: "opt" + (p[key] === v ? " on" : ""), "aria-pressed": String(p[key] === v), onclick: () => { setPref(key, v); ctx.rerender(true); } },
      pv ? h("span", { class: "pv " + pv }) : null, h("span", {}, h("b", {}, label), desc ? h("span", {}, desc) : null))));
  return h("div", { class: "grid" },
      h("section", { class: "card c6" }, h("header", { class: "card-h" }, h("h3", {}, t("sys.mode"))),
        opts("theme", [["dark", t("sys.theme.dark"), "", "dk"], ["light", t("sys.theme.light"), "", "lt"], ["auto", t("sys.theme.auto"), "", "au"]])),
      h("section", { class: "card c12" }, h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("sys.tone")), h("span", { class: "sub" }, t("sys.tone.d")))),
        h("div", { class: "tones" }, Object.entries(TONES).map(([k, v]) => {
          return h("button", { type: "button", class: "tone" + (p.tone === k ? " on" : ""), "aria-pressed": String(p.tone === k), onclick: () => { setPref("tone", k); ctx.rerender(true); } },
            h("span", { class: "tone-dots" }, h("i", { style: { background: v.a } }), h("i", { style: { background: v.b } })),
            h("span", {}, h("b", {}, v.name), h("span", {}, t("sys.tone." + k))));
        })),
        p.tone ? h("div", { class: "fa", style: { "margin-top": "12px" } }, h("button", { type: "button", class: "btn ghost sm", onclick: () => { setPref("tone", ""); ctx.rerender(true); } }, t("sys.tone.off"))) : null),
      h("section", { class: "card c12" }, h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("sys.part")), h("span", { class: "sub" }, t("sys.part.d")))),
        opts("particles", PARTICLES.map((k) => [k, t("sys.part." + k), t("sys.part." + k + ".d")]))),
      h("section", { class: "card c6" }, h("header", { class: "card-h" }, h("h3", {}, t("sys.accent"))),
        h("div", { class: "sws" }, Object.entries(ACCENTS).map(([k, v]) =>
          h("button", { type: "button", class: "swatch" + (!p.tone && p.accent === k ? " on" : ""), "aria-pressed": String(!p.tone && p.accent === k), onclick: () => { setPref("tone", ""); setPref("accent", k); ctx.rerender(true); } },
            h("i", { style: { background: `linear-gradient(135deg,${v[th].a},${v[th].b})` } }), t("sys.acc." + k)))),
        h("p", { class: "note" }, t("sys.accent.sub"))),
      h("section", { class: "card c6" }, h("header", { class: "card-h" }, h("h3", {}, t("sys.fx"))),
        opts("fx", [["off", t("sys.fx.off"), t("sys.fx.off.d")], ["subtle", t("sys.fx.subtle"), t("sys.fx.subtle.d")], ["rich", t("sys.fx.rich"), t("sys.fx.rich.d")]])),
      h("section", { class: "card c6" }, h("header", { class: "card-h" }, h("h3", {}, t("sys.density"))),
        opts("density", [["comfortable", t("sys.density.comfortable")], ["compact", t("sys.density.compact")]]),
        h("header", { class: "card-h", style: { "margin-top": "22px" } }, h("h3", {}, t("sys.language"))),
        opts("lang", [["ar", "العربية"], ["en", "English"]])),
      h("section", { class: "card c12" }, h("header", { class: "card-h" }, h("h3", {}, t("sys.preview"))),
        h("p", { class: "note" }, t("sys.pv.note")), h("div", { class: "pvbox", "aria-hidden": "true", style: { "pointer-events": "none" } }, h("div", { class: "pvrow" },
          h("button", { type: "button", class: "btn primary" }, t("sys.pv.primary")), h("button", { type: "button", class: "btn ghost" }, t("sys.pv.ghost")), h("button", { type: "button", class: "btn danger" }, t("sys.pv.danger")),
          chip("accent", t("sys.pv.chip")), chip("ok", t("pp.st.online")), chip("warn", t("pp.st.pending")), chip("danger", t("pp.st.blocked"))),
          h("div", { class: "bar", style: { "max-width": "420px" } }, h("i", { style: { width: "62%" } })))),
      h("section", { class: "card c12" }, h("header", { class: "card-h" }, h("h3", {}, t("sys.about"))),
        h("div", { class: "kv" },
          ctx.dashboard?.version ? h("div", {}, h("span", { class: "k" }, "QuotaManager"), h("span", { class: "v mono" }, "v" + ctx.dashboard.version)) : null)));
}

export default function system(ctx) {
  const tabs = ["appearance", "account", "updates", "logs"];
  return h("div", { class: "page" },
    h("div", { class: "tabs", role: "tablist" }, tabs.map((k) => h("button", { type: "button", role: "tab", class: tab === k ? "on" : "", "aria-selected": String(tab === k), onclick: () => { tab = k; ctx.touch(); } }, t("sys.tab." + k)))),
    tab === "account" ? accountTab(ctx) : tab === "updates" ? updatesTab(ctx) : tab === "logs" ? logsTab(ctx) : appearance(ctx));
}
