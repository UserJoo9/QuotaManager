import { h, icon } from "../core/dom.js";
import { t } from "../core/i18n.js";
let current = null, builder = null;
export function closeDrawer() { if (current) { current.remove(); current = null; document.removeEventListener("keydown", onKey); } }
function onKey(e) { if (e.key === "Escape") closeDrawer(); }
// build(): returns {icon, title, sub, body[], footer[]}. Re-called by refreshDrawer() on every snapshot.
export function openDrawer(build) {
  closeDrawer(); builder = build;
  const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true" });
  current = h("div", {}, h("div", { class: "scrim", onclick: closeDrawer }), panel); current._panel = panel;
  document.body.append(current); fill();
  document.addEventListener("keydown", onKey);
  panel.querySelector("button")?.focus();
}
function fill() {
  const v = builder(); const p = current._panel;
  const body = p.querySelector(".dr-body"), top = body ? body.scrollTop : 0;
  p.replaceChildren(...[
    h("header", { class: "dr-h" }, h("span", { class: "d-ic" }, icon(v.icon || "people", 20)),
      h("div", { class: "grow" }, h("h2", {}, v.title), h("span", {}, v.sub || "")),
      h("button", { class: "icon-btn", "aria-label": t("common.close"), onclick: closeDrawer }, icon("x"))),
    h("div", { class: "dr-body" }, v.body), v.footer && v.footer.length ? h("footer", { class: "dr-f" }, v.footer) : null].filter(Boolean));
  const nb = p.querySelector(".dr-body"); if (nb) nb.scrollTop = top;
}
export function refreshDrawer() { if (current && builder) {
  const a = document.activeElement; if (a && current.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return; // never rebuild under a cursor
  if ([...current.querySelectorAll("input")].some((i) => i.value !== i.defaultValue)) return; // unsaved edits stay put
  try { fill(); } catch { closeDrawer(); } } }
