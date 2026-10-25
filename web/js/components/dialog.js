import { h, icon } from "../core/dom.js";
import { t } from "../core/i18n.js";
let current = null;
export function closeDialog() { if (current) { current.remove(); current = null; document.removeEventListener("keydown", onKey); } }
function onKey(e) { if (e.key === "Escape") closeDialog(); }
// content: Node[] shown in a <form>. onSubmit(setError) => Promise; return truthy to close.
export function openDialog({ title, sub, content, submitLabel, onSubmit, wide }) {
  closeDialog();
  const err = h("div", { class: "err", role: "alert", hidden: true, style: { "text-align": "start" } });
  const submit = h("button", { class: "btn primary", type: "submit" }, submitLabel);
  const form = h("form", { class: "dialog-body", onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true; submit.disabled = true;
    try { if (await onSubmit()) closeDialog(); }
    catch (ex) { err.textContent = ex.message || String(ex); err.hidden = false; }
    finally { submit.disabled = false; }
  } },
    content, err,
    h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: closeDialog }, t("common.cancel")), submit));
  current = h("div", {},
    h("div", { class: "scrim", onclick: closeDialog }),
    h("div", { class: "dialog" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
      h("header", {}, h("div", { style: { flex: "1" } }, h("div", { class: "name" }, title), sub ? h("div", { class: "sub" }, sub) : null),
        h("button", { class: "icon-btn", type: "button", "aria-label": t("common.close"), onclick: closeDialog }, icon("close"))),
      form));
  document.body.append(current);
  document.addEventListener("keydown", onKey);
  current.querySelector("input,textarea,select")?.focus();
}
