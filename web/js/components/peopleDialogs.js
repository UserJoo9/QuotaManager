import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { actions } from "../core/actions.js";
import { openDialog } from "./dialog.js";
import { toast } from "./toast.js";

const f = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
const numIn = (v = 0) => h("input", { class: "input", type: "number", min: "0", step: "any", value: String(v), inputmode: "decimal" });
const nn = (el) => Math.max(0, +el.value || 0);

function quotaFields() {
  const mode = h("select", { class: "select" }, ["auto", "fixed"].map((m) => h("option", { value: m }, t("dr.quota." + m))));
  const gb = numIn(10), wrap = h("div", { hidden: true }, f(t("dr.quota.gb"), gb));
  mode.onchange = () => { wrap.hidden = mode.value !== "fixed"; };
  return { mode, gb, nodes: [f(t("dr.quota"), mode), wrap] };
}

export function openAddUser(refresh) {
  const name = h("input", { class: "input", required: true, placeholder: t("add.user.name.ph") });
  const q = quotaFields(), down = numIn(), up = numIn();
  const exempt = h("input", { type: "checkbox" }), vpn = h("input", { type: "checkbox" });
  const chk = (el, l) => h("label", { class: "chk" }, el, h("span", {}, l));
  openDialog({ title: t("add.user"), sub: t("add.user.sub"), submitLabel: t("add.submit"),
    content: [f(t("add.user.name"), name), ...q.nodes, h("div", { class: "form-row" }, f(t("dr.limit.down"), down), f(t("dr.limit.up"), up), ), h("span", { class: "sub" }, t("dr.limit.hint")), chk(exempt, t("dr.unlimited")), chk(vpn, t("dr.vpnbypass"))],
    onSubmit: async () => {
      const body = { name: name.value.trim(), quota_mode: q.mode.value, limit_down_mbps: nn(down), limit_up_mbps: nn(up), exempt_quota: exempt.checked, vpn_bypass: vpn.checked };
      if (q.mode.value === "fixed") body.fixed_gb = nn(q.gb);
      await actions.createUser(body); toast(t("add.done")); await refresh(); return true;
    } });
}

export function openAddDevice(users, refresh) {
  const mac = h("input", { class: "input ltr", required: true, placeholder: "aa:bb:cc:dd:ee:ff", spellcheck: "false" });
  const name = h("input", { class: "input" });
  const owner = h("select", { class: "select" }, h("option", { value: "" }, t("add.dev.newuser")), users.filter((u) => !u.protected).map((u) => h("option", { value: String(u.id) }, u.name)));
  const uname = h("input", { class: "input", placeholder: t("add.dev.uname.ph") });
  const unameField = f(t("add.user.name"), uname);
  owner.onchange = () => { unameField.hidden = owner.value !== ""; };
  const q = quotaFields(), down = numIn(), up = numIn();
  openDialog({ title: t("add.dev"), sub: t("add.dev.sub"), submitLabel: t("add.submit"),
    content: [f(t("dr.mac"), mac), f(t("add.dev.name"), name), f(t("add.dev.owner"), owner), unameField, ...q.nodes, h("div", { class: "form-row" }, f(t("dr.limit.down"), down), f(t("dr.limit.up"), up))],
    onSubmit: async () => {
      const body = { mac: mac.value.trim().toLowerCase().replace(/-/g, ":"), name: name.value.trim(), quota_mode: q.mode.value, limit_down_mbps: nn(down), limit_up_mbps: nn(up) };
      if (!/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(body.mac)) throw new Error(t("n.mac.bad"));
      if (owner.value) body.user_id = +owner.value; else if (uname.value.trim()) body.user_name = uname.value.trim();
      if (q.mode.value === "fixed" && !owner.value) body.fixed_gb = nn(q.gb);
      await actions.createDevice(body); toast(t("add.done")); await refresh(); return true;
    } });
}
