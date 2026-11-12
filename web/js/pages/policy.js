// Policy Builder Page — Unified Policy Management UI
// Manages policies: create, edit, delete, list, simulate, enforce
import { h, icon, FOCUSABLE } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { sw, chip, fmtBytes } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { api } from "../core/api.js";

const API_BASE = "/api/policies";

// ---- API helpers ----
async function apiGet(path) {
  const r = await api.get(path);
  return r;
}
async function apiPost(path, body) {
  return await api.post(path, body);
}
async function apiPut(path, body) {
  return await api.put(path, body);
}
async function apiDelete(path) {
  return await api.delete(path);
}

const PAGE_TITLE = t("policy.title");
const PAGE_SUB = t("policy.sub");

// ---- State ----
let policies = [];
let templates = [];

const SCOPES = [
  { id: "global", label: t("policy.scope.global") },
  { id: "user", label: t("policy.scope.user") },
  { id: "device", label: t("policy.scope.device") },
  { id: "user_group", label: t("policy.scope.user_group") },
  { id: "device_group", label: t("policy.scope.device_group") },
  { id: "combined", label: t("policy.scope.combined") },
];

const ACTIONS = [
  { id: "block", label: t("policy.action.block") },
  { id: "allow", label: t("policy.action.allow") },
  { id: "throttle", label: t("policy.action.throttle") },
  { id: "daily_limit", label: t("policy.action.daily_limit") },
  { id: "monitor", label: t("policy.action.monitor") },
];

const METHODS = [
  { id: "dns", label: t("policy.method.dns") },
  { id: "firewall", label: t("policy.method.firewall") },
  { id: "tc", label: t("policy.method.tc") },
  { id: "quota", label: t("policy.method.quota") },
];

function scopeLabel(s) {
  const s2 = SCOPES.find(x => x.id === s);
  return s2 ? s2.label : s;
}

function actionLabel(a) {
  const a2 = ACTIONS.find(x => x.id === a);
  return a2 ? a2.label : a;
}

function methodLabel(m) {
  const m2 = METHODS.find(x => x.id === m);
  return m2 ? m2.label : m;
}

function appLabel(appId) {
  const names = {
    tiktok: t("apps.tiktok"), instagram: t("apps.instagram"), facebook: t("apps.facebook"),
    youtube: t("apps.youtube"), snapchat: t("apps.snapchat"), twitter: t("apps.twitter"),
    discord: t("apps.discord"), telegram: t("apps.telegram"), twitch: t("apps.twitch"),
    netflix: t("apps.netflix"), spotify: t("apps.spotify"), steam: t("apps.steam"),
    epic: t("apps.epic"), playstation: t("apps.playstation"), xbox: t("apps.xbox"),
    ads_trackers: t("apps.ads_trackers"), vpn_services: t("apps.vpn_services"),
  };
  return names[appId] || appId;
}

// ---- UI Helpers ----
const policyCard = (p) => {
  const enabledCls = p.enabled ? "on" : "off";
  const actionCls = p.action === "block" ? "danger" : (p.action === "allow" ? "ok" : "accent");
  return h("div", { class: "card policy-card" },
    h("div", { class: "policy-head", style: { display: "flex", gap: "10px", alignItems: "center", marginBottom: "8px" } },
      h("input", { type: "checkbox", checked: selectedPolicies.has(p.id), onchange: () => toggleSelect(p.id) }),
      h("div", { class: `chip ${enabledCls}`, style: { fontSize: "11px" } }, p.enabled ? t("common.enabled") : t("common.disabled")),
      h("div", { class: `chip ${actionCls}`, style: { fontSize: "11px" } }, actionLabel(p.action)),
      h("div", { class: "chip", style: { fontSize: "11px", background: "var(--muted)" } }, methodLabel(p.method)),
      h("div", { style: { flex: "1" } }),
      h("div", { class: "badge", style: { fontSize: "10px" } }, num(p.matches || 0)),
    ),
    h("div", { style: { marginBottom: "6px" } },
      h("div", { class: "lbl" }, p.name),
      h("div", { class: "sub" }, p.desc),
    ),
    h("div", { class: "kv", style: { margin: "8px 0", fontSize: "12px" } },
      h("div", {}, h("span", { class: "k" }, t("policy.field.scope")), h("span", { class: "v" }, scopeLabel(p.scope))),
      h("div", {}, h("span", { class: "k" }, t("policy.field.target")), h("span", { class: "v ltr" }, appLabel(p.target))),
      h("div", {}, h("span", { class: "k" }, t("policy.field.priority")), h("span", { class: "v" }, p.priority)),
    ),
    h("div", { class: "fa", style: { marginTop: "10px", gap: "6px" } },
      h("button", { type: "button", class: "btn primary sm", onclick: () => editPolicy(p) }, icon("edit", 14), t("common.edit")),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => simulatePolicy(p) }, icon("bolt", 14), t("policy.btn.simulate")),
      h("button", { type: "button", class: `btn ${p.enabled ? "danger" : "ghost"} sm`, onclick: () => togglePolicy(p.id) }, icon(p.enabled ? "pause" : "play", 14), p.enabled ? t("common.disable") : t("common.enable")),
      h("button", { type: "button", class: "btn danger sm", onclick: () => deletePolicy(p.id) }, icon("delete", 14), t("common.delete")),
    )
  );
};

// ---- Page ----
export default function policyPage(ctx) {
  const [view, setView] = { v: "list" };

  const renderList = () => {
    const filter = (ctx._filter || "all");
    const shown = filter === "all" ? policies : policies.filter(p => p.enabled === (filter === "enabled"));
    return h("div", { class: "page" },
      h("header", { class: "page-head" },
        h("div", {}, h("h1", {}, PAGE_TITLE), h("span", { class: "sub" }, PAGE_SUB)),
        h("div", { class: "fa", style: { gap: "8px" } },
          h("button", { type: "button", class: `btn ${filter === "all" ? "primary" : "ghost"} sm`, onclick: () => { ctx._filter = "all"; ctx.touch(); } }, t("policy.filter.all")),
          h("button", { type: "button", class: `btn ${filter === "enabled" ? "primary" : "ghost"} sm`, onclick: () => { ctx._filter = "enabled"; ctx.touch(); } }, t("policy.filter.enabled")),
          h("button", { type: "button", class: `btn ${filter === "disabled" ? "primary" : "ghost"} sm`, onclick: () => { ctx._filter = "disabled"; ctx.touch(); } }, t("policy.filter.disabled")),
          h("button", { type: "button", class: "btn primary", onclick: () => openPolicyDialog() }, icon("plus", 14), t("common.add")),
        )
      ),
      h("div", { class: "grid" },
        shown.length ? shown.map(policyCard) : h("div", { class: "empty" }, t("policy.empty"))
      ),
    );
  };

  const renderTemplates = () => h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, t("policy.templates.title")), h("span", { class: "sub" }, t("policy.templates.sub"))),
      h("button", { type: "button", class: "btn ghost", onclick: () => setView.v = "list" }, icon("arrow-left", 14), t("common.back"))
    ),
    h("div", { class: "grid" },
      templates.map(tpl => h("div", { class: "card template-card" },
        h("div", { class: "lbl" }, tpl.name),
        h("div", { class: "sub" }, tpl.desc),
        h("div", { class: "kv", style: { margin: "8px 0" } },
          h("div", {}, h("span", { class: "k" }, t("policy.field.category")), h("span", { class: "v" }, tpl.category)),
          h("div", {}, h("span", { class: "k" }, t("policy.field.action")), h("span", { class: "v" }, actionLabel(tpl.action))),
        ),
        h("div", { class: "sub", style: { fontSize: "11px" } }, tpl.apps.map(appLabel).join(", ")),
        h("div", { class: "fa", style: { marginTop: "10px" } },
          h("button", { type: "button", class: "btn primary sm", onclick: () => applyTemplate(tpl) }, icon("add", 14), t("policy.btn.apply")),
        )
      ))
    )
  );

  const renderSimulation = () => h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", {}, h("h1", {}, t("policy.sim.title")), h("span", { class: "sub" }, t("policy.sim.sub"))),
      h("button", { type: "button", class: "btn ghost", onclick: () => setView.v = "list" }, icon("arrow-left", 14), t("common.back"))
    ),
    h("div", { class: "card" },
      h("div", { class: "form-row" },
        field(t("policy.sim.target"), h("select", { class: "select", id: "simTarget", onchange: (e) => { ctx._simTarget = e.target.value; ctx.touch(); } }),
          ["tiktok", "instagram", "youtube", "facebook", "snapchat", "twitter", "discord", "telegram", "steam", "netflix", "spotify"].map(appId =>
            h("option", { value: appId, selected: (ctx._simTarget || "tiktok") === appId }, appLabel(appId))
          )
        ),
        field(t("policy.sim.scope"), h("select", { class: "select", id: "simScope", onchange: (e) => { ctx._simScope = e.target.value; ctx.touch(); } }),
          SCOPES.map(s => h("option", { value: s.id, selected: (ctx._simScope || "global") === s.id }, s.label))
        ),
        field(t("policy.sim.user"), h("input", { class: "input", id: "simUser", value: ctx._simUser || "", oninput: (e) => { ctx._simUser = e.target.value; ctx.touch(); } })),
      ),
      h("div", { class: "fa", style: { marginTop: "14px" } },
        h("button", { type: "button", class: "btn primary", onclick: () => runSimulation() }, icon("bolt", 14), t("policy.btn.run_sim"))
      ),
      ctx._simResult ? h("div", { class: "sim-result", style: { marginTop: "12px" } },
        h("div", { class: "lbl" }, t("policy.sim.result")),
        h("div", { class: `chip ${ctx._simResult.resolved_action === "block" ? "danger" : "ok"}` }, ctx._simResult.resolved_action.toUpperCase()),
        h("div", { class: "sub" }, ctx._simResult.resolved_reason),
        ctx._simResult.explain.map((e, i) => h("div", { style: { fontSize: "11px", margin: "2px 0" } }, `• ${e}`))
      ) : null
    )
  );

  const togglePolicy = async (id) => {
    const p = policies.find(x => x.id === id);
    if (!p) return;
    try {
      await apiPut(`${API_BASE}/${id}`, { ...p, enabled: !p.enabled });
      p.enabled = !p.enabled;
      ctx.touch();
      toast(p.enabled ? t("policy.enabled") : t("policy.disabled"));
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); }
  };

  const deletePolicy = async (id) => {
    if (!confirm(t("policy.confirm_delete"))) return;
    try {
      await apiDelete(`${API_BASE}/${id}`);
      policies = policies.filter(x => x.id !== id);
      ctx.touch();
      toast(t("policy.deleted"));
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); }
  };

  const editPolicy = (p) => openPolicyDialog(p);
  const simulatePolicy = (p) => { ctx._simTarget = p.target; ctx._simScope = p.scope; ctx._simUser = ""; setView.v = "sim"; ctx.touch(); };

  const runSimulation = async () => {
    const target = ctx._simTarget || "tiktok";
    const scope = ctx._simScope || "global";
    const context = { user_id: ctx._simUser || undefined, device_id: scope === "device" ? (ctx._simUser || undefined) : undefined };
    try {
      const res = await apiPost(`${API_BASE}/simulate`, { target, context });
      ctx._simResult = {
        resolved_action: res.resolved_action,
        resolved_reason: res.resolved_reason,
        explain: res.explain || [],
      };
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); }
    ctx.touch();
  };

  const openPolicyDialog = (existing = null) => {
    const isEdit = !!existing;
    const name = h("input", { class: "input", value: existing ? existing.name : "", oninput: (e) => { ctx._pdName = e.target.value; } });
    const desc = h("input", { class: "input", value: existing ? existing.desc : "", oninput: (e) => { ctx._pdDesc = e.target.value; } });
    const scopeSel = h("select", { class: "select", value: existing ? existing.scope : "global", onchange: (e) => { ctx._pdScope = e.target.value; } },
      SCOPES.map(s => h("option", { value: s.id, selected: (existing ? existing.scope : "global") === s.id }, s.label))
    );
    const targetSel = h("select", { class: "select", value: existing ? existing.target : "tiktok", onchange: (e) => { ctx._pdTarget = e.target.value; } },
      Object.entries({ tiktok: t("apps.tiktok"), instagram: t("apps.instagram"), facebook: t("apps.facebook"), youtube: t("apps.youtube"), snapchat: t("apps.snapchat"), twitter: t("apps.twitter"), discord: t("apps.discord"), telegram: t("apps.telegram"), steam: t("apps.steam"), netflix: t("apps.netflix"), spotify: t("apps.spotify"), ads_trackers: t("apps.ads_trackers") }).map(([id, lbl]) =>
        h("option", { value: id, selected: (existing ? existing.target : "tiktok") === id }, lbl)
      )
    );
    const actionSel = h("select", { class: "select", value: existing ? existing.action : "block", onchange: (e) => { ctx._pdAction = e.target.value; } },
      ACTIONS.map(a => h("option", { value: a.id, selected: (existing ? existing.action : "block") === a.id }, a.label))
    );
    const methodSel = h("select", { class: "select", value: existing ? existing.method : "dns", onchange: (e) => { ctx._pdMethod = e.target.value; } },
      METHODS.map(m => h("option", { value: m.id, selected: (existing ? existing.method : "dns") === m.id }, m.label))
    );
    const priority = h("input", { class: "input", type: "number", value: existing ? existing.priority : 0, oninput: (e) => { ctx._pdPriority = +e.target.value || 0; } });
    const enabled = h("input", { type: "checkbox", checked: existing ? existing.enabled : true, onchange: (e) => { ctx._pdEnabled = e.target.checked; } });

    openDialog({
      title: isEdit ? t("policy.edit_title") : t("policy.create_title"),
      submitLabel: t("common.save"),
      content: [
        field(t("policy.field.name"), name),
        field(t("policy.field.desc"), desc),
        field(t("policy.field.scope"), scopeSel),
        field(t("policy.field.target"), targetSel),
        field(t("policy.field.action"), actionSel),
        field(t("policy.field.method"), methodSel),
        field(t("policy.field.priority"), priority),
        field(t("policy.field.enabled"), h("div", { style: { display: "flex", alignItems: "center", gap: "6px" } }, enabled, h("span", { class: "sub" }, t("policy.field.enabled_d")))),
      ],
      onSubmit: async () => {
        const np = {
          name: ctx._pdName || name.value,
          description: ctx._pdDesc || desc.value,
          scope: ctx._pdScope || scopeSel.value,
          target: ctx._pdTarget || targetSel.value,
          action: ctx._pdAction || actionSel.value,
          method: ctx._pdMethod || methodSel.value,
          priority: ctx._pdPriority || +priority.value || 0,
          enabled: ctx._pdEnabled !== undefined ? ctx._pdEnabled : enabled.checked,
        };
        try {
          if (existing) {
            await apiPut(`${API_BASE}/${existing.id}`, { ...existing, ...np });
          } else {
            await apiPost(`${API_BASE}/`, np);
          }
          await loadPolicies();
          ctx.touch();
          toast(t("policy.saved"));
          return true;
        } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); return false; }
      }
    });
  };

  const applyTemplate = async (tpl) => {
    const np = {
      name: tpl.name + " (copy)",
      desc: tpl.desc,
      scope: "global",
      target: tpl.apps[0] || "tiktok",
      action: tpl.action,
      priority: 10,
      enabled: true,
      method: "dns",
    };
    try {
      await apiPost(`${API_BASE}/`, np);
      await loadPolicies();
      ctx.touch();
      toast(t("policy.template_applied"));
      setView.v = "list";
      ctx.touch();
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); }
  };

  // ---- Bulk selection state ----
  let selectedPolicies = new Set();

  const toggleSelect = (id) => {
    if (selectedPolicies.has(id)) selectedPolicies.delete(id);
    else selectedPolicies.add(id);
    ctx.touch();
  };

  const selectAllPolicies = () => {
    const shown = (ctx._filter || "all") === "all" ? policies : policies.filter(p => p.enabled === (ctx._filter === "enabled"));
    shown.forEach(p => selectedPolicies.add(p.id));
    ctx.touch();
    toast(`${selectedPolicies.size} ${t("policy.btn.selected")}`);
  };

  const bulkSetEnabled = async (enabled) => {
    if (selectedPolicies.size === 0) { toast(t("policy.btn.select_none")); return; }
    try {
      await Promise.all([...selectedPolicies].map(id => apiPut(`${API_BASE}/${id}`, { ...policies.find(p => p.id === id), enabled })));
      await loadPolicies();
      selectedPolicies.clear();
      ctx.touch();
      toast(enabled ? t("policy.btn.enabled_all") : t("policy.btn.disabled_all"));
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); }
  };

  const bulkDelete = async () => {
    if (selectedPolicies.size === 0) { toast(t("policy.btn.select_none")); return; }
    if (!confirm(`${t("policy.confirm_delete_multi")} (${selectedPolicies.size})`)) return;
    try {
      await Promise.all([...selectedPolicies].map(id => apiDelete(`${API_BASE}/${id}`)));
      await loadPolicies();
      selectedPolicies.clear();
      ctx.touch();
      toast(t("policy.deleted_multi"));
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); }
  };

  const exportPolicies = () => {
    const data = JSON.stringify(policies, null, 2);
    const blob = new Blob([data], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `quota-policies-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast(t("policy.btn.exported"));
  };

  const importPolicies = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json";
    input.onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const text = await file.text();
      try {
        const imported = JSON.parse(text);
        if (!Array.isArray(imported)) throw new Error("Invalid format");
        await Promise.all(imported.map(p => apiPost(`${API_BASE}/`, p)));
        await loadPolicies();
        ctx.touch();
        toast(`${imported.length} ${t("policy.btn.imported")}`);
      } catch (err) { toast(t("policy.import_failed") + ": " + err.message, "bad"); }
    };
    input.click();
  };

  // ---- Load policies from API ----
  async function loadPolicies() {
    try {
      const res = await apiGet(`${API_BASE}/`);
      policies = res.policies || [];
    } catch (e) { toast(t("policy.failed") + ": " + e.message, "bad"); policies = []; }
  }

  const field = (label, control) => h("div", { class: "field" }, h("label", {}, label), control);

  // ---- Page ----
  export default function policyPage(ctx) {
    loadPolicies().then(() => ctx.touch());
    const [view, setView] = { v: "list" };

  // Top action bar
  const topBar = h("div", { class: "card c12", style: { marginBottom: "14px" } },
    h("div", { class: "fa", style: { gap: "8px", flexWrap: "wrap" } },
      h("button", { type: "button", class: "btn primary", onclick: () => { setView.v = "templates"; ctx.touch(); } }, icon("layers", 14), t("policy.btn.templates")),
      h("button", { type: "button", class: "btn ghost", onclick: () => { setView.v = "sim"; ctx.touch(); } }, icon("shield-check", 14), t("policy.btn.sim")),
      h("div", { class: "sep", style: { width: "1px", height: "24px", background: "var(--line)", margin: "0 4px" } }),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => selectAllPolicies() }, icon("check", 14), t("policy.btn.select_all")),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => bulkSetEnabled(true) }, icon("play", 14), t("policy.btn.enable_all")),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => bulkSetEnabled(false) }, icon("pause", 14), t("policy.btn.disable_all")),
      h("button", { type: "button", class: "btn danger sm", onclick: () => bulkDelete() }, icon("delete", 14), t("policy.btn.delete_all")),
      h("div", { class: "sep", style: { width: "1px", height: "24px", background: "var(--line)", margin: "0 4px" } }),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => exportPolicies() }, icon("save", 14), t("policy.btn.export")),
      h("button", { type: "button", class: "btn ghost sm", onclick: () => importPolicies() }, icon("upload", 14), t("policy.btn.import")),
    )
  );

  return h("div", { class: "page" },
    topBar,
    content
  );
}

// ---- i18n keys used by this page (register in i18n.js) ----
const POLICY_I18N = {
  ar: {
    policy: {
      title: "مدير السياسات",
      sub: "أنشئ وعدّل وسيطر على سياسات الوصول للشبكة",
      field: {
        name: "الاسم", desc: "الوصف", scope: "النطاق", target: "الهدف", action: "الإجراء",
        method: "طريقة التنفيذ", priority: "الأولوية", enabled: "مفعل", enabled_d: "تفعيل السياسة",
        category: "الفئة", target_d: "التطبيق أو الخدمة المستهدفة",
      },
      scope: { global: "الشبكة كلها", user: "مستخدم", device: "جهاز", user_group: "مجموعة مستخدمين", device_group: "مجموعة أجهزة", combined: "مConditions مختلطة" },
      action: { block: "حظر", allow: "سماح", throttle: "تقييد سرعة", daily_limit: "حد يومي", monitor: "مراقبة فقط" },
      method: { dns: "DNS", firewall: "جدار ناري", tc: "تحديد سرعة", quota: "حصص" },
      examples: {
        block_tiktok: "حظر تيك توك على الشبكة كلها", block_tiktok_d: "يمنع الوصول إلى تيك توك لكل الأجهزة",
        allow_tiktok_user: "سماح تيك توك لمستخدم واحد", allow_tiktok_user_d: "يسمح لمستخدم محدد باستخدام تيك توك رغم الحظر العام",
        allow_yt_reels: "حظر短视يوهات يوتيوب على مجموعة أجهزة", allow_yt_reels_d: "يمنع الريدلز/شورتس على أجهزة معينة فقط",
      },
      btn: { simulate: "محاكاة", edit: "تعديل", enable: "تفعيل", disable: "إلغاء تفعيل", delete: "حذف", run_sim: "تشغيل المحاكاة", sim: "محاكاة", templates: "القوالب الجاهزة", export: "تصدير", import: "استيراد", refresh: "تحديث", apply: "تطبيق القالب", coming_soon: "قريباً" },
      filter: { all: "الكل", enabled: "المفعل", disabled: "المعطل" },
      empty: "لا توجد سياسات مُضافة بعد",
      confirm_delete: "هل تريد حذف هذه السياسة؟",
      deleted: "تم الحذف", saved: "تم الحفظ", enabled: "تم التفعيل", disabled: "تم الإلغاء", template_applied: "تم تطبيق القالب",
      edit_title: "تعديل سياسة", create_title: "إنشاء سياسة جديدة",
      templates: { title: "القوالب الجاهزة", sub: "ابدأ بسياسة جاهزة وعدّلها كما تريد", block_social: "حظر وسائل التواصل", block_social_d: "حظر تيك توك وإنستجرام وفيسبوك على الشبكة كلها", allow_study: "سماح أثناء أوقات الدراسة", allow_study_d: "سماح يوتيوب وتيك توك في أيام الدراسة من 8 صباحاً إلى 3 عصراً", throttle_gaming: "تقييد سرعة الألعاب", throttle_gaming_d: "تقليل سرعة ستيم وإيبك إلى 512 كيلوبت" },
      sim: { title: "محاكاة السياسة", sub: "اختبر نتيجة السياسة قبل تفعيلها", target: "الهدف", scope: "النطاق", user: "المستخدم", result: "النتيجة", no_match: "لا سياسة تطابق", blocked: "سيتم الحظر", allowed: "سيتم السماح", coming_soon: "قريباً" },
    },
    apps: {
      tiktok: "تيك توك", instagram: "إنستجرام", facebook: "فيسبوك", youtube: "يوتيوب", snapchat: "سnapchat",
      twitter: "تويتر", discord: "ديسكورد", telegram: "تيليجرام", twitch: "تويتش", netflix: "نتفليكس",
      spotify: "سبوتيفاي", steam: "ستيم", epic: "إيبك", playstation: "بلايستيشن", xbox: "إكس بوكس",
      ads_trackers: "إعلانات ومتتبعين", vpn_services: "خدمات VPN",
    },
    common: { enabled: "مفعل", disabled: "معطل", add: "إضافة", edit: "تعديل", delete: "حذف", save: "حفظ", back: "عودة", enabled_d: "مفعل", disabled_d: "معطل" },
  },
  en: {
    policy: {
      title: "Policy Manager",
      sub: "Create, edit, and control network access policies",
      field: { name: "Name", desc: "Description", scope: "Scope", target: "Target", action: "Action", method: "Enforcement", priority: "Priority", enabled: "Enabled", enabled_d: "Enable policy", category: "Category", target_d: "Target app or service" },
      scope: { global: "Entire Network", user: "User", device: "Device", user_group: "User Group", device_group: "Device Group", combined: "Combined Conditions" },
      action: { block: "Block", allow: "Allow", throttle: "Throttle", daily_limit: "Daily Limit", monitor: "Monitor Only" },
      method: { dns: "DNS", firewall: "Firewall", tc: "Traffic Control", quota: "Quota" },
      examples: {
        block_tiktok: "Block TikTok network-wide", block_tiktok_d: "Prevents access to TikTok for all devices",
        allow_tiktok_user: "Allow TikTok for one user", allow_tiktok_user_d: "Allows a specific user to use TikTok despite global block",
        allow_yt_reels: "Block YouTube Shorts on device group", allow_yt_reels_d: "Blocks shorts/reels on specific devices only",
      },
      btn: { simulate: "Simulate", edit: "Edit", enable: "Enable", disable: "Disable", delete: "Delete", run_sim: "Run Simulation", sim: "Simulate", templates: "Templates", export: "Export", import: "Import", refresh: "Refresh", apply: "Apply Template", coming_soon: "Coming Soon" },
      filter: { all: "All", enabled: "Enabled", disabled: "Disabled" },
      empty: "No policies added yet",
      confirm_delete: "Delete this policy?",
      deleted: "Deleted", saved: "Saved", enabled: "Enabled", disabled: "Disabled", template_applied: "Template applied",
      edit_title: "Edit Policy", create_title: "Create New Policy",
      templates: { title: "Ready Templates", sub: "Start from a ready template and customize", block_social: "Block Social Media", block_social_d: "Block TikTok, Instagram, Facebook network-wide", allow_study: "Allow During Study", allow_study_d: "Allow YouTube/TikTok on school days 8am-3pm", throttle_gaming: "Throttle Gaming", throttle_gaming_d: "Limit Steam/Epic to 512 kbps" },
      sim: { title: "Policy Simulation", sub: "Test policy result before enabling", target: "Target", scope: "Scope", user: "User", result: "Result", no_match: "No matching policy", blocked: "Will be blocked", allowed: "Will be allowed" },
    },
    apps: { tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook", youtube: "YouTube", snapchat: "Snapchat", twitter: "Twitter/X", discord: "Discord", telegram: "Telegram", twitch: "Twitch", netflix: "Netflix", spotify: "Spotify", steam: "Steam", epic: "Epic Games", playstation: "PlayStation", xbox: "Xbox", ads_trackers: "Ads & Trackers", vpn_services: "VPN Services" },
    common: { enabled: "Enabled", disabled: "Disabled", add: "Add", edit: "Edit", delete: "Delete", save: "Save", back: "Back" },
  },
};

// Register i18n keys
if (typeof window !== "undefined" && window.__QM_I18N__) {
  Object.assign(window.__QM_I18N__.ar, POLICY_I18N.ar);
  Object.assign(window.__QM_I18N__.en, POLICY_I18N.en);
}
