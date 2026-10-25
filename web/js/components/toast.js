import { h, icon } from "../core/dom.js";
let host;
export function toast(msg, kind = "") {
  if (!host) { host = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(host); }
  const el = h("div", { class: `toast ${kind}` }, icon(kind === "bad" ? "alert" : "check"), h("span", {}, msg));
  host.append(el);
  setTimeout(() => el.remove(), 3200);
}
