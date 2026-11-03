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
// True when the person has changed anything in the drawer that is not saved yet (text, number, checkbox,
// radio, or a <select> pick). A live snapshot must never rebuild the drawer over that.
function hasUnsaved(root) {
  for (const i of root.querySelectorAll("input")) {
    if (i.type === "checkbox" || i.type === "radio") { if (i.checked !== i.defaultChecked) return true; }
    else if (i.value !== i.defaultValue) return true;
  }
  for (const x of root.querySelectorAll("textarea")) if (x.value !== x.defaultValue) return true;
  for (const sel of root.querySelectorAll("select")) for (const o of sel.options) if (o.selected !== o.defaultSelected) return true;
  return false;
}
// A press that started inside the drawer (e.g. on "Save") must reach its click: rebuilding between mouse-down and
// mouse-up would replace the button and swallow the click.
let pressing = false;
const down = () => { pressing = true; }, up = () => { setTimeout(() => { pressing = false; }, 0); };
document.addEventListener("pointerdown", down, true);
document.addEventListener("pointerup", up, true);
document.addEventListener("pointercancel", up, true);
export function refreshDrawer() { if (current && builder) {
  const a = document.activeElement; if (a && current.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return; // never rebuild under a cursor
  if (pressing || hasUnsaved(current)) return; // unsaved edits and in-flight clicks stay put
  try { fill(); } catch { closeDrawer(); } } }
