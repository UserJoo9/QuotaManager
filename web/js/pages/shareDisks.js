import { h, icon } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { api } from "../core/api.js";
import { chip, fmtBytes } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

// Share > Network drives: a disk plugged into the gateway, or a folder another PC /
// the router already shares, published over SMB so every phone, PC and TV on the
// network can open it at one address (no password).
const dk = { data: null, loading: false, at: 0, failAt: 0, busy: false };
const fail = (e) => toast(`${t("dr.failed")}: ${e.message}`, "bad");

async function load(ctx, force) {
  if (dk.loading || (!force && dk.data && Date.now() - dk.at < (dk.data.installing ? 4000 : 15000))) return;
  dk.loading = true;
  try { dk.data = await api.get("/api/disks"); dk.at = Date.now(); } catch { dk.failAt = Date.now(); } finally { dk.loading = false; ctx.touch(); }
}
const host = () => location.hostname || "192.168.2.1";
const diskName = (d) => d.label || d.model || d.path;
const suggest = (d) => (diskName(d).replace(/[^A-Za-z0-9_-]+/g, "") || "Disk").slice(0, 32);

function shareDialog(ctx, d) {
  const name = h("input", { class: "input ltr", required: true, maxlength: "32", pattern: "[A-Za-z0-9][A-Za-z0-9_\\-]{0,31}", value: suggest(d) });
  const rw = h("input", { type: "checkbox", id: "dk-rw", checked: true });
  openDialog({ title: t("dk.share.title"), sub: `${diskName(d)} · ${fmtBytes(d.size)} · ${d.fstype}`, submitLabel: t("dk.share.go"),
    content: [
      h("div", { class: "field" }, h("label", {}, t("dk.name")), name, h("span", { class: "sub" }, t("dk.name.h"))),
      h("label", { class: "row-sw", for: "dk-rw", style: { padding: "10px 0", cursor: "pointer" } }, h("div", {}, h("b", {}, t("dk.rw")), h("span", {}, t("dk.rw.d"))), rw)],
    onSubmit: async () => {
      const r = await api.post("/api/disks/share", { uuid: d.uuid, name: name.value.trim(), writable: rw.checked });
      dk.data = r; dk.at = Date.now(); ctx.touch();
      if ((r.problems || []).length) toast(r.problems.join(" · "), "bad"); else toast(t("dk.shared"));
      return true;
    } });
}

function networkDialog(ctx) {
  const remote = h("input", { class: "input ltr", required: true, placeholder: "//192.168.1.5/Movies" });
  const name = h("input", { class: "input ltr", required: true, maxlength: "32", pattern: "[A-Za-z0-9][A-Za-z0-9_\\-]{0,31}" });
  const user = h("input", { class: "input ltr", autocomplete: "off" });
  const pass = h("input", { class: "input ltr", type: "password", autocomplete: "new-password" });
  const rw = h("input", { type: "checkbox", id: "dk-nrw", checked: true });
  remote.addEventListener("input", () => { if (!name.dataset.touched) name.value = (remote.value.split(/[\\/]/).filter(Boolean).pop() || "").replace(/[^A-Za-z0-9_-]+/g, "").slice(0, 32); });
  name.addEventListener("input", () => { name.dataset.touched = "1"; });
  openDialog({ title: t("dk.net.title"), sub: t("dk.net.sub"), submitLabel: t("dk.share.go"),
    content: [
      h("div", { class: "field" }, h("label", {}, t("dk.net.remote")), remote, h("span", { class: "sub" }, t("dk.net.remote.h"))),
      h("div", { class: "field" }, h("label", {}, t("dk.name")), name, h("span", { class: "sub" }, t("dk.name.h"))),
      h("div", { class: "field" }, h("label", {}, t("dk.net.user")), user, h("span", { class: "sub" }, t("dk.net.user.h"))),
      h("div", { class: "field" }, h("label", {}, t("dk.net.pass")), pass),
      h("label", { class: "row-sw", for: "dk-nrw", style: { padding: "10px 0", cursor: "pointer" } }, h("div", {}, h("b", {}, t("dk.rw")), h("span", {}, t("dk.rw.d"))), rw)],
    onSubmit: async () => {
      const r = await api.post("/api/disks/share", { source: "network", remote: remote.value.trim(), name: name.value.trim(), user: user.value.trim(), password: pass.value, writable: rw.checked });
      dk.data = r; dk.at = Date.now(); ctx.touch();
      if ((r.problems || []).length) toast(r.problems.join(" · "), "bad"); else toast(t("dk.shared"));
      return true;
    } });
}

// Who may open a share: every device with internet, or only chosen ones; any
// device can get its own level (read-write / read-only / no access).
function accessDialog(ctx, s) {
  const devs = ((ctx.dashboard && ctx.dashboard.devices) || []).filter((d) => d.mac && d.mac !== "00:00:00:00:00:00")
    .sort((a, b) => (b.connected ? 1 : 0) - (a.connected ? 1 : 0) || String(a.user_name || a.name).localeCompare(String(b.user_name || b.name)));
  const devMode = { ...(s.devices || {}) };
  const mode = h("select", { class: "select" },
    h("option", { value: "all", selected: s.access !== "selected" }, t("dk.acc.all")),
    h("option", { value: "selected", selected: s.access === "selected" }, t("dk.acc.selected")));
  const rw = h("input", { type: "checkbox", id: "dk-acc-rw", checked: s.writable !== false });
  const rwRow = h("label", { class: "row-sw", for: "dk-acc-rw", style: { padding: "8px 0", cursor: "pointer" } }, h("div", {}, h("b", {}, t("dk.acc.default")), h("span", {}, t("dk.acc.default.d"))), rw);
  const opt = (v, label, cur) => h("option", { value: v, selected: cur === v }, label);
  const rows = devs.map((d) => {
    const sel = h("select", { class: "select", style: { width: "auto", height: "34px" }, "aria-label": d.name || d.mac,
      onchange: (e) => { if (e.target.value) devMode[d.mac] = e.target.value; else delete devMode[d.mac]; } },
      opt("", t("dk.lvl.default"), devMode[d.mac] || ""), opt("rw", t("dk.lvl.rw"), devMode[d.mac]), opt("ro", t("dk.lvl.ro"), devMode[d.mac]), opt("none", t("dk.lvl.none"), devMode[d.mac]));
    return h("div", { class: "crow", style: { "grid-template-columns": "1fr auto" } },
      h("div", { class: "c-name" }, d.name || d.mac, h("span", { class: "mono" }, `${d.user_name ? d.user_name + " · " : ""}${d.ip || "—"}${d.connected ? "" : " · " + t("dk.offline")}`)), sel);
  });
  const sync = () => { rwRow.hidden = mode.value === "selected"; };
  mode.addEventListener("change", sync); sync();
  openDialog({ title: t("dk.acc.title"), sub: s.name, submitLabel: t("dk.acc.save"), wide: true,
    content: [
      h("div", { class: "field" }, h("label", {}, t("dk.acc.who")), mode, h("span", { class: "sub" }, t("dk.acc.who.h"))),
      rwRow,
      h("div", { class: "field" }, h("label", {}, t("dk.acc.devices")), h("span", { class: "sub" }, t("dk.acc.devices.h"))),
      rows.length ? h("div", { style: { "max-height": "320px", overflow: "auto" } }, rows) : h("div", { class: "empty" }, t("dk.acc.nodev"))],
    onSubmit: async () => {
      const r = await api.put("/api/disks/share/" + encodeURIComponent(s.name), { access: mode.value, writable: rw.checked, devices: devMode });
      dk.data = r; dk.at = Date.now(); ctx.touch();
      if ((r.problems || []).length) toast(r.problems.join(" · "), "bad"); else toast(t("dk.acc.saved"));
      return true;
    } });
}
const accessLabel = (s) => s.access === "selected"
  ? t("dk.acc.n").replace("{n}", String(Object.values(s.devices || {}).filter((m) => m !== "none").length))
  : s.writable ? t("dk.st.rw") : t("dk.st.ro");

export default function disksCard(ctx) {
  if (Date.now() - dk.failAt > 10000) load(ctx);
  const d = dk.data;
  const shared = new Set(((d && d.shares) || []).map((s) => s.uuid));
  const free = ((d && d.disks) || []).filter((x) => !shared.has(x.uuid));
  const ready = d && d.samba && d.cifs !== false;
  const install = async () => { dk.busy = true; ctx.touch(); try { dk.data = await api.post("/api/disks/install-samba"); dk.at = Date.now(); toast(t("dk.installing")); } catch (e) { fail(e); } finally { dk.busy = false; ctx.touch(); } };
  const unshare = async (s) => { try { dk.data = await api.del("/api/disks/share/" + encodeURIComponent(s.name)); dk.at = Date.now(); toast(t("dk.unshared")); } catch (e) { fail(e); } finally { ctx.touch(); } };
  const path = (s) => `\\\\${host()}\\${s.name}`;
  return h("section", { class: "card c12" },
    h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dk.title")), h("span", { class: "sub" }, t("dk.d"))),
      h("div", { style: { display: "flex", gap: "8px", "flex-wrap": "wrap" } },
        h("button", { type: "button", class: "btn ghost sm", disabled: !ready, onclick: () => networkDialog(ctx) }, icon("network", 15), t("dk.net.add")),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => load(ctx, true) }, t("logs.refresh")))),
    !d ? h("div", { class: "skeleton", style: { height: "90px" } }) : h("div", {},
      !ready ? h("div", { class: "fa", style: { "margin-bottom": "12px" } },
        h("p", { class: "sub" }, d.installing ? t("dk.installing") : d.install_error ? `${t("dk.install.err")}: ${d.install_error}` : t("dk.nosamba")),
        h("button", { type: "button", class: "btn primary sm", disabled: dk.busy || d.installing, onclick: install }, t("dk.install"))) : null,
      !d.lan_only && (d.shares || []).length ? h("p", { class: "sub", style: { color: "var(--warn)" } }, t("dk.nolan")) : null,
      (d.shares || []).length ? h("div", {}, d.shares.map((s) => h("div", { class: "crow", style: { "grid-template-columns": "1fr auto auto auto" } },
        h("div", { class: "c-name" }, s.name, h("span", { class: "mono ltr", dir: "ltr" }, `${path(s)}  ·  smb://${host()}/${s.name}${s.source === "network" ? "  ←  " + s.remote : ""}`)),
        chip(s.active ? "ok" : "warn", s.active ? accessLabel(s) : s.source === "network" ? t("dk.st.unreach") : s.present ? t("dk.st.err") : t("dk.st.gone")),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => accessDialog(ctx, s) }, icon("lock", 15), t("dk.acc.btn")),
        h("button", { type: "button", class: "btn danger sm", onclick: () => unshare(s) }, t("dk.unshare"))))) : null,
      free.length ? h("div", { style: { "margin-top": "10px" } }, free.map((x) => h("div", { class: "crow", style: { "grid-template-columns": "1fr auto" } },
        h("div", { class: "c-name" }, diskName(x), h("span", { class: "mono" }, `${x.path} · ${fmtBytes(x.size)} · ${x.fstype}${x.mounted ? " · " + x.mounted : ""}`)),
        h("button", { type: "button", class: "btn primary sm", disabled: !ready, onclick: () => shareDialog(ctx, x) }, icon("share", 15), t("dk.share")))))
        : !(d.shares || []).length ? h("div", { class: "empty" }, t("dk.none")) : null));
}
