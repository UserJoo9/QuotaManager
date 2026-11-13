import { h, icon } from "../core/dom.js";

/** How long a toast stays on screen (ms). */
const TOAST_MS = 3200;
let host;
/**
 * Show a short-lived status toast (ARIA live region, polite).
 * One host element is appended to <body> on first use; each toast removes itself after TOAST_MS.
 * @param {string} msg - text shown to the user
 * @param {"bad"|""} [kind] - "bad" paints the alert icon, anything else paints the check icon
 * @returns {void}
 */
export function toast(msg, kind = "") {
  if (!host) { host = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(host); }
  const el = h("div", { class: `toast ${kind}` }, icon(kind === "bad" ? "alert" : "check"), h("span", {}, msg));
  host.append(el);
  setTimeout(() => el.remove(), TOAST_MS);
}
