import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { actions } from "../core/actions.js";
import { openDialog } from "./dialog.js";
import { toast } from "./toast.js";

/** A label + control row with an optional hint line underneath. `hint` may be a string or a node. */
const field = (label, control, hint) => {
  const hHint = hint == null ? null : (typeof hint === "string" ? h("span", { class: "sub" }, hint) : hint);
  return h("div", { class: "field" }, h("label", {}, label), control, hHint);
};
/** Number input (GB / Mbps style). @param {number} [v] */
const numIn = (v = 0) => h("input", { class: "input", type: "number", min: "0", step: "any", value: String(v), inputmode: "decimal" });
/** Clamped non-negative value of a number input. @param {HTMLInputElement} el @returns {number} */
const nn = (el) => Math.max(0, +el.value || 0);
/** A checkbox + its label. */
const chk = (el, l) => h("label", { class: "chk" }, el, h("span", {}, l));

/**
 * Quota mode + fixed allowance fields shared by the add-user and add-device dialogs.
 * @returns {{mode:HTMLSelectElement, gb:HTMLInputElement, nodes:Node[]}}
 */
function quotaFields() {
  const mode = h("select", { class: "select" }, ["auto", "fixed"].map((m) => h("option", { value: m }, t("dr.quota." + m))));
  const gb = numIn(10), wrap = h("div", { hidden: true }, field(t("dr.quota.gb"), gb));
  mode.onchange = () => { wrap.hidden = mode.value !== "fixed"; };
  return { mode, gb, nodes: [field(t("dr.quota"), mode), wrap] };
}

/**
 * "Add person" dialog: name, quota mode/allowance, speed caps, quota exemption, VPN bypass.
 * @param {() => Promise<void>} refresh
 * @returns {void}
 */
export function openAddUser(refresh) {
  const name = h("input", { class: "input", required: true, placeholder: t("add.user.name.ph") });
  const q = quotaFields(), down = numIn(), up = numIn();
  const exempt = h("input", { type: "checkbox" }), vpn = h("input", { type: "checkbox" });
  openDialog({ title: t("add.user"), sub: t("add.user.sub"), submitLabel: t("add.submit"),
    content: [field(t("add.user.name"), name), ...q.nodes, h("div", { class: "form-row" }, field(t("dr.limit.down"), down), field(t("dr.limit.up"), up)), h("span", { class: "sub" }, t("dr.limit.hint")), chk(exempt, t("dr.unlimited")), chk(vpn, t("dr.vpnbypass"))],
    onSubmit: async () => {
      const body = { name: name.value.trim(), quota_mode: q.mode.value, limit_down_mbps: nn(down), limit_up_mbps: nn(up), exempt_quota: exempt.checked, vpn_bypass: vpn.checked };
      if (q.mode.value === "fixed") body.fixed_gb = nn(q.gb);
      await actions.createUser(body); toast(t("add.done")); await refresh(); return true;
    } });
}

/**
 * "Add device" dialog: MAC, name, owner / new user, quota mode/allowance, speed caps.
 * @param {{id:number, name:string, protected:boolean}[]} users
 * @param {() => Promise<void>} refresh
 * @returns {void}
 */
export function openAddDevice(users, refresh) {
  const mac = h("input", { class: "input ltr", required: true, placeholder: "aa:bb:cc:dd:ee:ff", spellcheck: "false", autocomplete: "off" });
  const name = h("input", { class: "input" });
  const owner = h("select", { class: "select" }, h("option", { value: "" }, t("add.dev.newuser")), users.filter((u) => !u.protected).map((u) => h("option", { value: String(u.id) }, u.name)));
  const uname = h("input", { class: "input", placeholder: t("add.dev.uname.ph") });
  const unameField = field(t("add.user.name"), uname);
  owner.onchange = () => { unameField.hidden = owner.value !== ""; };
  const q = quotaFields(), down = numIn(), up = numIn();
  openDialog({ title: t("add.dev"), sub: t("add.dev.sub"), submitLabel: t("add.submit"),
    content: [field(t("dr.mac"), mac), field(t("add.dev.name"), name), field(t("add.dev.owner"), owner), unameField, ...q.nodes, h("div", { class: "form-row" }, field(t("dr.limit.down"), down), field(t("dr.limit.up"), up))],
    onSubmit: async () => {
      const body = { mac: mac.value.trim().toLowerCase().replace(/-/g, ":"), name: name.value.trim(), quota_mode: q.mode.value, limit_down_mbps: nn(down), limit_up_mbps: nn(up) };
      if (!/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(body.mac)) throw new Error(t("n.mac.bad"));
      if (owner.value) body.user_id = +owner.value; else if (uname.value.trim()) body.user_name = uname.value.trim();
      if (q.mode.value === "fixed" && !owner.value) body.fixed_gb = nn(q.gb);
      await actions.createDevice(body); toast(t("add.done")); await refresh(); return true;
    } });
}
