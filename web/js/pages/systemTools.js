// System Tools Page — Backup, Alerts, Device Groups
import { h, icon } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { api } from "../core/api.js";

const API_BASE = "/api/policies";

// ---- State ----
let backups = [];
let alerts = [];
let groups = [];
let activeTool = "backup"; // backup | alerts | groups

// ---- Backup UI ----
const backupRow = (b) => h("div", { class: "card tool-card", style: { marginBottom: "8px" } },
  h("div", { class: "kv", style: { fontSize: "13px" } },
    h("div", {}, h("span", { class: "k" }, t("backup.backup_id")), h("span", { class: "v mono" }, b.backup_id)),
    h("div", {}, h("span", { class: "k" }, t("backup.type")), h("span", { class: "v" }, b.backup_type)),
    h("div", {}, h("span", { class: "k" }, t("backup.status")), h("span", { class: `chip ${b.status === "done" ? "ok" : "danger"} sm` }, b.status)),
    h("div", {}, h("span", { class: "k" }, t("backup.size")), h("span", { class: "v" }, b.size_bytes)),
    h("div", {}, h("span", { class: "k" }, t("backup.created_at")), h("span", { class: "v" }, b.created_at)),
    h("div", {}, h("span", { class: "k" }, t("backup.created_by")), h("span", { class: "v" }, b.created_by)),
  ),
  h("div", { class: "fa", style: { marginTop: "8px", gap: "6px" } },
    h("button", { type: "button", class: "btn primary sm", onclick: () => restoreBackup(b.backup_id) },
      icon("refresh", 14), t("backup.btn.restore")),
    h("button", { type: "button", class: "btn danger sm", onclick: () => deleteBackup(b.backup_id) },
      icon("delete", 14), t("backup.btn.delete")),
  )
);

const renderBackup = () => h("div", { class: "page" },
  h("header", { class: "page-head" },
    h("div", {}, h("h1", {}, t("backup.title")), h("span", { class: "sub" }, t("backup.sub"))),
    h("button", { type: "button", class: "btn primary", onclick: createBackup }, icon("save", 14), t("backup.btn.create"))
  ),
  h("div", { class: "grid" },
    backups.length ? backups.map(backupRow) : h("div", { class: "empty" }, t("backup.no_backups"))
  )
);

const createBackup = async () => {
  try {
    const res = await api.post(`${API_BASE}/backup/create`, {
      backup_type: "full",
      note: "Manual backup via UI",
    });
    toast("Backup created", "ok");
    await loadBackups();
  } catch (e) { toast("Backup failed: " + e.message, "error"); }
};

const restoreBackup = async (id) => {
  if (!confirm("تأكيد استعادة النسخة الاحتياطية؟")) return;
  try {
    await api.post(`${API_BASE}/backup/restore/${id}`);
    toast("Restored", "ok");
    await loadBackups();
  } catch (e) { toast("Restore failed: " + e.message, "error"); }
};

const deleteBackup = async (id) => {
  if (!confirm("حذف النسخة الاحتياطية؟")) return;
  try {
    await api.delete(`${API_BASE}/backup/${id}`);
    toast("Deleted", "ok");
    await loadBackups();
  } catch (e) { toast("Delete failed: " + e.message, "error"); }
};

// ---- Alerts UI ----
const severityChip = (s) => {
  const cls = s === "critical" ? "danger" : s === "high" ? "warn" : s === "medium" ? "chip" : "ok";
  return h("span", { class: `chip ${cls} sm`, style: { fontSize: "10px" } }, t("alert.severity." + s));
};

const alertRow = (a) => h("div", { class: "card tool-card", style: { marginBottom: "8px" } },
  h("div", { class: "kv", style: { fontSize: "13px" } },
    h("div", {}, h("span", { class: "k" }, t("alert.alert_type")), h("span", { class: "v" }, a.alert_type)),
    h("div", {}, h("span", { class: "k" }, t("alert.severity")), severityChip(a.severity)),
    h("div", {}, h("span", { class: "k" }, t("alert.title")), h("span", { class: "v", style: { fontWeight: "600" } }, a.title)),
    h("div", {}, h("span", { class: "k" }, t("alert.message")), h("span", { class: "v" }, a.message)),
    h("div", {}, h("span", { class: "k" }, t("alert.target")), h("span", { class: "v" }, a.target || "-")),
    h("div", {}, h("span", { class: "k" }, t("alert.created_at")), h("span", { class: "v" }, a.created_at)),
  ),
  h("div", { class: "fa", style: { marginTop: "8px", gap: "6px" } },
    !a.acknowledged
      ? h("button", { type: "button", class: "btn primary sm", onclick: () => ackAlert(a.alert_id) },
        icon("check", 14), t("alert.acknowledge"))
      : h("span", { class: "chip ok sm" }, t("alert.acknowledged")),
    h("button", { type: "button", class: "btn danger sm", onclick: () => deleteAlert(a.alert_id) },
      icon("delete", 14), t("alert.delete")),
  )
);

const renderAlerts = () => h("div", { class: "page" },
  h("header", { class: "page-head" },
    h("div", {}, h("h1", {}, t("alert.title")), h("span", { class: "sub" }, t("alert.sub"))),
    h("div", { style: { marginLeft: "auto" } },
      h("button", { type: "button", class: `btn ${alerts.filter(a => !a.acknowledged).length > 0 ? "danger" : "ghost"} sm`, onclick: () => { /* refresh */ } },
        icon("bell", 14), alerts.filter(a => !a.acknowledged).length > 0 ? alerts.filter(a => !a.acknowledged).length : ""),
    )
  ),
  h("div", { class: "grid" },
    alerts.length ? alerts.map(alertRow) : h("div", { class: "empty" }, t("alert.no_alerts"))
  )
);

const ackAlert = async (id) => {
  try {
    await api.patch(`${API_BASE}/alerts/${id}/ack`);
    toast("Acknowledged", "ok");
    await loadAlerts();
  } catch (e) { toast("Failed: " + e.message, "error"); }
};

const deleteAlert = async (id) => {
  try {
    await api.delete(`${API_BASE}/alerts/${id}`);
    toast("Deleted", "ok");
    await loadAlerts();
  } catch (e) { toast("Failed: " + e.message, "error"); }
};

// ---- Device Groups UI ----
const groupRow = (g) => h("div", { class: "card tool-card", style: { marginBottom: "8px" } },
  h("div", { class: "kv", style: { fontSize: "13px" } },
    h("div", {}, h("span", { class: "k" }, "Group"), h("span", { class: "v", style: { fontWeight: "600" } }, g.group_name)),
    h("div", {}, h("span", { class: "k" }, "ID"), h("span", { class: "v mono" }, g.group_id)),
    h("div", {}, h("span", { class: "k" }, "Members"), h("span", { class: "v" }, g.member_count)),
    h("div", {}, h("span", { class: "k" }, "Desc"), h("span", { class: "v" }, g.description || "-")),
  ),
  h("div", { class: "fa", style: { marginTop: "8px", gap: "6px" } },
    h("button", { type: "button", class: "btn primary sm", onclick: () => viewGroupMembers(g.group_id) },
      icon("people", 14), "Members"),
    h("button", { type: "button", class: "btn danger sm", onclick: () => deleteGroup(g.group_id) },
      icon("delete", 14), "Delete"),
  )
);

const renderGroups = () => h("div", { class: "page" },
  h("header", { class: "page-head" },
    h("div", {}, h("h1", {}, "Device Groups"), h("span", { class: "sub" }, "Manage device groups for policies")),
    h("button", { type: "button", class: "btn primary", onclick: () => openGroupDialog() }, icon("plus", 14), "Add Group")
  ),
  h("div", { class: "grid" },
    groups.length ? groups.map(groupRow) : h("div", { class: "empty" }, "No device groups")
  )
);

const openGroupDialog = () => {
  const nameInput = h("input", { type: "text", placeholder: "Group name" });
  const descInput = h("input", { type: "text", placeholder: "Description" });
  openDialog({
    title: "Add Device Group",
    fields: [nameInput, descInput],
    onSubmit: async () => {
      try {
        await api.post(`${API_BASE}/device-groups`, {
          group_id: "grp_" + Math.random().toString(36).slice(2, 10),
          group_name: nameInput.value,
          description: descInput.value,
        });
        toast("Group created", "ok");
        await loadGroups();
      } catch (e) { toast("Failed: " + e.message, "error"); }
    }
  });
};

const viewGroupMembers = async (groupId) => {
  try {
    const res = await api.get(`${API_BASE}/device-groups/${groupId}/members`);
    openDialog({
      title: "Group Members",
      fields: [h("div", {}, res.members.map(m => h("div", { style: { marginBottom: "4px" } }, m.device_id)).join("") || h("div", {}, "No members"))],
      onSubmit: () => {},
      showClose: true,
    });
  } catch (e) { toast("Failed: " + e.message, "error"); }
};

const deleteGroup = async (id) => {
  if (!confirm("Delete group?")) return;
  try {
    await api.delete(`${API_BASE}/device-groups/${id}`);
    toast("Deleted", "ok");
    await loadGroups();
  } catch (e) { toast("Failed: " + e.message, "error"); }
};

// ---- Loaders ----
const loadBackups = async () => {
  try { backups = (await api.get(`${API_BASE}/backup/list`)).backups || []; } catch { backups = []; }
};
const loadAlerts = async () => {
  try { alerts = (await api.get(`${API_BASE}/alerts`)).alerts || []; } catch { alerts = []; }
};
const loadGroups = async () => {
  try { groups = (await api.get(`${API_BASE}/device-groups`)).groups || []; } catch { groups = []; }
};

// ---- Page ----
export default function systemToolsPage(ctx) {
  const tools = [
    { id: "backup", label: t("backup.title"), icon: "save" },
    { id: "alerts", label: t("alert.title"), icon: "bell" },
    { id: "groups", label: "Device Groups", icon: "people" },
  ];

  const render = () => h("div", { class: "page" },
    h("div", { class: "fa", style: { gap: "6px", marginBottom: "16px" } },
      tools.map(tl => h("button", {
        type: "button",
        class: `btn ${activeTool === tl.id ? "primary" : "ghost"} sm`,
        onclick: () => { activeTool = tl.id; ctx.touch(); }
      }, icon(tl.icon, 14), tl.label))
    ),
    activeTool === "backup" ? renderBackup() :
    activeTool === "alerts" ? renderAlerts() :
    renderGroups()
  );

  loadBackups();
  loadAlerts();
  loadGroups();
  return render();
}
