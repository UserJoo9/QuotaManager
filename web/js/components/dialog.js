import { h, icon, FOCUSABLE, onKeyTrap } from "../core/dom.js";
import { t } from "../core/i18n.js";

/** The open modal element, and the element that had focus before it opened. */
let current = null, prevFocus = null;

/** Close the open dialog, restore focus, and unbind the Escape/Tab handler. */
export function closeDialog() {
  if (current) {
    current.remove(); current = null;
    onKeyTrap.close();
    if (prevFocus && typeof prevFocus.focus === "function") { try { prevFocus.focus(); } catch {} }
    prevFocus = null;
  }
}
/**
 * Open a modal dialog rendering `content` inside a <form>.
 * @typedef {Object} OpenDialogOptions
 * @property {string} title - dialog title
 * @property {string} [sub] - subtitle line under the title
 * @property {Node[]} content - form body nodes
 * @property {string} submitLabel - primary button label
 * @property {() => Promise<boolean>} onSubmit - returning truthy closes the dialog
 * @property {boolean} [wide] - wide variant
 * @property {boolean} [infoOnly] - read-only: header X plus a single Close button (no redundant Cancel)
 * @returns {void}
 */
export function openDialog({ title, sub, content, submitLabel, onSubmit, wide, infoOnly }) {
  prevFocus = document.activeElement;
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
    h("div", { class: "dialog-actions" }, infoOnly ? null : h("button", { class: "btn", type: "button", onclick: closeDialog }, t("common.cancel")), submit));
  current = h("div", {},
    h("div", { class: "scrim", onclick: closeDialog }),
    h("div", { class: "dialog" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
      h("header", {}, h("div", { style: { flex: "1" } }, h("div", { class: "name" }, title), sub ? h("div", { class: "sub" }, sub) : null),
        h("button", { class: "icon-btn", type: "button", "aria-label": t("common.close"), onclick: closeDialog }, icon("close"))),
      form));
  document.body.append(current);
  onKeyTrap.open(current, closeDialog);
  current.querySelector("input,textarea,select")?.focus();
}
