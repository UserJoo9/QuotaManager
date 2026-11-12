// RBAC Page — Roles, Permissions, Assignments
// Manages admin roles, permissions, and user-role assignments
import { h, icon, FOCUSABLE } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { sw, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { api } from "../core/api.js";

const API_BASE = "/api/policies";

// ---- State ----
let roles = [];
let permissions = [];
let userRoles = [];
let activeTab = "roles"; // roles | permissions | assignments

// ---- UI Helpers ----
const roleCard = (r) => h("div", { class: "card rbac-card" },
  h("div", { class: "card-head", style: { display: "flex", gap: "10px", alignItems: "center", marginBottom: "8px" } },
    h("div", { class: "lbl", style: { fontWeight: "600" } }, r.role_name),
    h("div", { style: { flex: "1" } }),
    h("div", { class: "chip", style: { fontSize: "11px", background: "var(--muted)" } }, r.role_id)
  ),
  h("div", { class: "sub", style: { marginBottom: "8px" } }, r.description || "-"),
  h("div", { class: "kv", style: { margin: "8px 0", fontSize: "12px" } },
    h("div", {}, h("span", { class: "k" }, t("rbac.created_at")), h("span", { class: "v" }, r.created_at)),
    h("div", {}, h("span", { class: "k" }, t("rbac.created_by")), h("span", { class: "v" }, r.created_by)),
  ),
  h("div", { class: "fa", style: { marginTop: "10px", gap: "6px" } },
    h("button", { type: "button", class: "btn primary sm", onclick: () => manageRolePermissions(r) },
      icon("shield", 14), t("rbac.permissions")),
    h("button", { type: "button", class: "btn ghost sm", onclick: () => deleteRole(r.role_id) },
      icon("delete", 14), t("common.delete")),
  )
);

const permChip = (p, granted, onToggle) => h("span", {
  class: `chip ${granted ? "ok" : "ghost"}`,
  style: { fontSize: "11px", marginRight: "6px", marginBottom: "6px", display: "inline-block" },
  onclick: () => onToggle(p)
}, granted ? "✓ " : "○ ", p);

// ---- Page ----
export default function rbacPage(ctx) {
  const [view, setView] = { v: "list" };
  let selectedRole = null;

  const loadData = async () => {
    try {
      roles = (await api.get(`${API_BASE}/rbac/roles`)).roles || [];
    } catch (e) { roles = []; toast(t("common.loading"), "error"); }
  };

  const renderRolesTab = () => h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, t("rbac.title")), h("span", { class: "sub" }, t("rbac.sub"))),
      h("div", { class: "fa", style: { gap: "8px" } },
        h("button", { type: "button", class: "btn primary", onclick: () => openRoleDialog() },
          icon("plus", 14), t("rbac.btn.add_role"))
      )
    ),
    h("div", { class: "grid" },
      roles.length ? roles.map(roleCard) : h("div", { class: "empty" }, t("rbac.no_roles"))
    )
  );

  const renderPermissionsTab = () => {
    if (!selectedRole) return h("div", { class: "empty" }, "Select a role");
    // fetch permissions for this role
    return h("div", { class: "page" },
      h("header", { class: "page-head" },
        h("div", {}, h("h1", {}, selectedRole.role_name), h("span", { class: "sub" }, t("rbac.permissions"))),
        h("button", { type: "button", class: "btn ghost", onclick: () => { selectedRole = null; ctx.touch(); } },
          icon("arrow-left", 14), t("rbac.btn.back"))
      ),
      h("div", { class: "grid" },
        h("div", { class: "card", style: { gridColumn: "1 / -1" } },
          h("div", { class: "lbl" }, `Permissions for ${selectedRole.role_name}`),
          h("div", { class: "perm-grid", style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: "8px", marginTop: "10px" } },
            permissions.map(p => permChip(p, false, (perm) => togglePermission(selectedRole.role_id, perm)))
          )
        )
      )
    );
  };

  const renderAssignmentsTab = () => h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, t("rbac.assignments"))),
      h("button", { type: "button", class: "btn ghost", onclick: () => setView.v = "list" },
        icon("arrow-left", 14), t("rbac.btn.back"))
    ),
    h("div", { class: "grid" },
      userRoles.length
        ? userRoles.map(ur =>
            h("div", { class: "card" },
              h("div", { class: "lbl" }, ur.user_id),
              h("div", { class: "sub" }, ur.role_id),
              h("div", { class: "fa", style: { marginTop: "10px" } },
                h("button", { type: "button", class: "btn danger sm", onclick: () => unassignRole(ur.user_id, ur.role_id) },
                  icon("delete", 14), t("rbac.revoke")
                )
              )
            )
          )
        : h("div", { class: "empty" }, "No assignments")
    )
  );

  const render = () => {
    const tabs = [
      { id: "roles", label: t("rbac.roles") },
      { id: "permissions", label: t("rbac.permissions") },
      { id: "assignments", label: t("rbac.assignments") },
    ];
    return h("div", { class: "page" },
      h("div", { class: "fa", style: { gap: "6px", marginBottom: "16px" } },
        tabs.map(tab => h("button", {
          type: "button",
          class: `btn ${activeTab === tab.id ? "primary" : "ghost"} sm`,
          onclick: () => { activeTab = tab.id; selectedRole = null; ctx.touch(); }
        }, tab.label))
      ),
      activeTab === "roles" ? renderRolesTab() :
      activeTab === "permissions" ? renderPermissionsTab() :
      renderAssignmentsTab()
    );
  };

  // ---- Dialogs ----
  const openRoleDialog = () => {
    const nameInput = h("input", { type: "text", placeholder: t("rbac.role_name") });
    const idInput = h("input", { type: "text", placeholder: t("rbac.role_id") });
    const descInput = h("input", { type: "text", placeholder: t("rbac.role_desc") });
    openDialog({
      title: t("rbac.btn.add_role"),
      fields: [nameInput, idInput, descInput],
      onSubmit: async () => {
        try {
          await api.post(`${API_BASE}/rbac/roles`, {
            role_id: idInput.value, role_name: nameInput.value, description: descInput.value
          });
          toast("Role created", "ok");
          await loadData();
          ctx.touch();
        } catch (e) { toast("Failed to create role", "error"); }
      }
    });
  };

  const manageRolePermissions = (role) => {
    selectedRole = role;
    activeTab = "permissions";
    ctx.touch();
  };

  const deleteRole = async (roleId) => {
    if (!confirm(t("rbac.confirm_delete"))) return;
    try {
      await api.delete(`${API_BASE}/rbac/roles/${roleId}`);
      toast("Role deleted", "ok");
      await loadData();
      ctx.touch();
    } catch (e) { toast("Failed to delete role", "error"); }
  };

  const togglePermission = async (roleId, perm) => {
    try {
      const exists = permissions.includes(perm);
      if (exists) {
        await api.delete(`${API_BASE}/rbac/roles/${roleId}/permissions/${perm}`);
      } else {
        await api.post(`${API_BASE}/rbac/roles/${roleId}/permissions/${perm}`);
      }
      toast("Permission updated", "ok");
      ctx.touch();
    } catch (e) { toast("Failed to update permission", "error"); }
  };

  const unassignRole = async (userId, roleId) => {
    try {
      await api.delete(`${API_BASE}/rbac/users/${userId}/roles/${roleId}`);
      toast("Role unassigned", "ok");
      ctx.touch();
    } catch (e) { toast("Failed to unassign role", "error"); }
  };

  loadData();
  return render();
}
