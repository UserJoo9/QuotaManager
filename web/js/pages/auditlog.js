// Audit Log Page — Policy change history
import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";

const API_BASE = "/api/policies";

async function apiGet(path) {
  const r = await api.get(path);
  return r;
}

const ACTION_LABELS = {
  create: t("audit.action.create"),
  update: t("audit.action.update"),
  delete: t("audit.action.delete"),
};

const ACTION_CHIP = {
  create: "ok",
  update: "accent",
  delete: "danger",
};

export default function auditLogPage(ctx) {
  let logs = [];
  let loading = false;

  async function load() {
    loading = true;
    ctx.touch();
    try {
      const res = await apiGet(`${API_BASE}/audit-log?limit=200`);
      logs = res.audit_log || [];
    } catch (e) {
      logs = [];
      toast(t("audit.load_failed") + ": " + e.message, "bad");
    }
    loading = false;
    ctx.touch();
  }

  const renderLogRow = (entry) => {
    const when = entry.created_at ? new Date(entry.created_at).toLocaleString() : "-";
    const details = entry.details || {};
    const detailText = Object.entries(details)
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
    return h("div", { class: "card audit-row", style: { marginBottom: "8px" } },
      h("div", { style: { display: "flex", gap: "10px", alignItems: "center", marginBottom: "6px" } },
        h("div", { class: `chip ${ACTION_CHIP[entry.action] || ""}`, style: { fontSize: "11px" } }, ACTION_LABELS[entry.action] || entry.action),
        h("div", { class: "chip", style: { fontSize: "11px", background: "var(--muted)" } }, entry.actor),
        h("div", { style: { flex: "1" } }),
        h("div", { class: "badge", style: { fontSize: "11px" } }, when),
      ),
      h("div", { style: { marginBottom: "4px" } },
        h("div", { class: "lbl" }, entry.policy_name),
        h("div", { class: "sub" }, `#${entry.policy_id}`),
      ),
      detailText ? h("div", { class: "sub", style: { fontSize: "11px" } }, detailText) : null,
    );
  };

  return h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, t("audit.title")), h("span", { class: "sub" }, t("audit.sub"))),
      h("button", { type: "button", class: "btn ghost", onclick: () => { ctx._route = "policies"; ctx.touch(); } }, icon("arrow-left", 14), t("common.back"))
    ),
    loading ? h("div", { class: "empty" }, t("audit.loading")) :
    logs.length === 0 ? h("div", { class: "empty" }, t("audit.empty")) :
    h("div", { class: "grid" }, logs.map(renderLogRow))
  );
}

// i18n keys
const AUDIT_I18N = {
  ar: {
    audit: {
      title: "سجل التدقيق",
      sub: "سجل التعديلات على السياسات",
      loading: "جارٍ التحميل...",
      empty: "لا توجد سجلات修 modifications",
      load_failed: "فشل تحميل السجل",
      action: {
        create: "إنشاء",
        update: "تعديل",
        delete: "حذف",
      },
    },
  },
  en: {
    audit: {
      title: "Audit Log",
      sub: "Policy change history",
      loading: "Loading...",
      empty: "No audit records",
      load_failed: "Failed to load log",
      action: {
        create: "Create",
        update: "Update",
        delete: "Delete",
      },
    },
  },
};

if (typeof window !== "undefined" && window.__QM_I18N__) {
  Object.assign(window.__QM_I18N__.ar, AUDIT_I18N.ar);
  Object.assign(window.__QM_I18N__.en, AUDIT_I18N.en);
}
