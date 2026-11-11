// UI layer — presentational components.
// Render inputs and emit user intent. Receive data and callbacks as props.
// Do NOT import stores, contexts, or services directly.

import { h, icon } from "../core/dom.js";

// ---- Presentational atoms ----

/**
 * Renders a button. Pure presentation; onClick is provided by caller.
 */
export function Button({ className = "", type = "button", disabled = false, children, onClick, ariaLabel, title }) {
  return h("button", {
    type,
    class: `btn ${className}`.trim(),
    disabled,
    "aria-label": ariaLabel,
    title,
    onclick: onClick,
  }, children);
}

/**
 * Renders an icon button.
 */
export function IconButton({ className = "icon-btn", onClick, ariaLabel, title, iconName, iconSize = 18 }) {
  return h("button", {
    type: "button",
    class: className,
    "aria-label": ariaLabel,
    title,
    onclick: onClick,
  }, icon(iconName, iconSize));
}

/**
 * Renders a toggle switch.
 */
export function Switch({ checked = false, label, onChange, ariaLabel }) {
  return h("button", {
    type: "button",
    role: "switch",
    class: `sw ${checked ? "on" : ""}`,
    "aria-checked": String(checked),
    "aria-label": ariaLabel,
    onclick: onChange,
  }, h("i"));
}

/**
 * Renders an input field with label.
 */
export function Field({ label, input, hint, required = false }) {
  const labelEl = h("label", {}, label);
  if (required) {
    labelEl = h("label", {}, label, h("span", { style: { color: "var(--danger)", fontWeight: "700" } }, " *"));
  }
  return h("div", { class: "field" }, labelEl, input, hint ? h("span", { class: "sub" }, hint) : null);
}

/**
 * Renders a card container.
 */
export function Card({ className = "card", children, header }) {
  return h("section", { class: className },
    header ? h("header", { class: "card-h" }, header) : null,
    children);
}

/**
 * Renders a card header with title and optional subtitle/chip.
 */
export function CardHeader({ title, sub, chip }) {
  return h("div", {}, h("h2", {}, title), sub ? h("span", { class: "sub" }, sub) : null, chip);
}

/**
 * Renders an empty state.
 */
export function EmptyState({ iconName = "check", title, sub }) {
  return h("div", { class: "empty" },
    h("span", { class: "ei" }, icon(iconName, 22)),
    h("b", {}, title),
    sub ? h("span", {}, sub) : null);
}

/**
 * Renders a skeleton loader.
 */
export function Skeleton({ height = "200px" }) {
  return h("div", { class: "skeleton", style: { height } });
}

/**
 * Renders a chip/badge.
 */
export function Chip({ className = "", children }) {
  return h("span", { class: `chip ${className}`.trim() }, children);
}

/**
 * Renders a progress bar.
 */
export function ProgressBar({ pct, neutral = false, "aria-valuenow": value }) {
  return h("div", {
    class: `bar${!neutral && pct >= 100 ? " over" : ""}`,
    role: "progressbar",
    "aria-valuemin": "0",
    "aria-valuemax": "100",
    "aria-valuenow": String(Math.round(value ?? pct)),
  }, h("i", { style: { width: Math.min(100, Math.max(0, pct)) + "%" } }));
}

/**
 * Renders a consumer row (person/device usage).
 */
export function ConsumerRow({ avatar, name, sub, usage, percent, barPct, status, actions }) {
  return h("div", { class: "crow" },
    h("span", { class: "d-ic" }, avatar),
    h("div", { class: "c-name" },
      h("b", {}, name),
      sub ? h("span", {}, sub) : null),
    h("div", { class: "c-use" },
      h("div", { class: "c-num" },
        h("b", {}, usage),
        percent ? h("span", {}, percent) : null),
      h("div", { class: "bar" }, h("i", { style: { width: `${Math.min(100, Math.max(0, barPct))}%` } }))),
    status);
}

/**
 * Renders a dialog.
 */
export function Dialog({ role = "dialog", children, header, actions }) {
  return h("div", { class: "dialog", role, "aria-modal": "true" },
    header ? h("header", {}, header) : null,
    h("div", { class: "dialog-body" }, children),
    actions ? h("div", { class: "dialog-actions" }, actions) : null);
}

/**
 * Renders a toast.
 */
export function Toast({ kind = "", message, iconName }) {
  return h("div", { class: `toast ${kind}` },
    icon(iconName || (kind === "bad" ? "alert" : "check"), 16),
    h("span", {}, message));
}

/**
 * Renders a language/theme switch segment.
 */
export function Segment({ items, value, onChange, ariaLabel }) {
  return h("div", {
    class: "seg",
    role: "group",
    "aria-label": ariaLabel,
  }, items.map((item) => h("button", {
    type: "button",
    class: value === item.value ? "on" : "",
    "aria-pressed": String(value === item.value),
    onclick: () => onChange(item.value),
  }, item.label)));
}

/**
 * Renders a tabs component.
 */
export function Tabs({ tabs, active, onChange, ariaLabel }) {
  return h("div", {
    class: "tabs",
    role: "tablist",
    "aria-label": ariaLabel,
  }, tabs.map((tab) => h("button", {
    type: "button",
    role: "tab",
    class: active === tab.id ? "on" : "",
    "aria-selected": String(active === tab.id),
    onclick: () => onChange(tab.id),
  }, tab.label)));
}

/**
 * Renders a search input.
 */
export function Search({ placeholder, value, onChange, ariaLabel }) {
  return h("div", { class: "search" },
    icon("search", 18),
    h("input", {
      type: "search",
      placeholder,
      value,
      "aria-label": ariaLabel,
      oninput: (e) => onChange(e.target.value),
    }));
}

/**
 * Renders a language switch.
 */
export function LangSwitch({ lang, onChange }) {
  return h("div", { class: "seg", role: "group", "aria-label": "Language" },
    h("button", {
      type: "button",
      class: lang === "ar" ? "on" : "",
      "aria-pressed": String(lang === "ar"),
      onclick: () => onChange("ar"),
    }, "ع"),
    h("button", {
      type: "button",
      class: lang === "en" ? "on" : "",
      "aria-pressed": String(lang === "en"),
      onclick: () => onChange("en"),
    }, "EN"));
}

/**
 * Renders a theme switch.
 */
export function ThemeSwitch({ dark, onChange }) {
  return h("button", {
    type: "button",
    class: "icon-btn",
    "aria-label": dark ? "Light theme" : "Dark theme",
    title: dark ? "Light theme" : "Dark theme",
    onclick: onChange,
  }, icon(dark ? "sun" : "moon", 16));
}

/**
 * Renders a privacy toggle.
 */
export function PrivacySwitch({ hidden, onChange }) {
  return h("button", {
    type: "button",
    class: "icon-btn",
    "aria-label": hidden ? "Show MACs" : "Hide MACs",
    title: hidden ? "Show MACs" : "Hide MACs",
    "aria-pressed": String(!hidden),
    onclick: onChange,
  }, icon(hidden ? "eyeoff" : "eye", 16));
}

/**
 * Renders a menu button.
 */
export function MenuButton({ onClick }) {
  return h("button", {
    type: "button",
    class: "icon-btn menu-btn",
    "aria-label": "Menu",
    onclick: onClick,
  }, icon("menu", 18));
}

/**
 * Renders a collapse/expand button.
 */
export function CollapseBtn({ rail, onClick }) {
  return h("button", {
    type: "button",
    class: "collapse-btn",
    "aria-label": rail ? "Expand" : "Collapse",
    title: rail ? "Expand" : "Collapse",
    onclick: onClick,
  }, icon("panel", 18));
}

/**
 * Renders a navigation item.
 */
export function NavItem({ iconName, label, active, count, onClick }) {
  return h("a", {
    class: `nav-i${active ? " on" : ""}`,
    href: "#",
    title: label,
    "aria-current": active ? "page" : null,
    onclick: onClick,
  },
    icon(iconName),
    h("span", { class: "nav-t" }, label),
    count !== undefined ? h("span", { class: "nav-badge" }, count) : null);
}

/**
 * Renders a nav group label.
 */
export function NavGroupLabel({ label }) {
  return h("div", { class: "nav-glabel" }, label);
}

/**
 * Renders a side meter (bundle usage in sidebar).
 */
export function SideMeter({ pct, remaining, label, leftLabel }) {
  return h("div", { class: "side-meter" },
    h("div", { class: "sm-top" },
      h("span", {}, label),
      h("b", {}, `${pct}%`)),
    h("div", { class: "bar" }, h("i", { style: { width: `${Math.min(100, Math.max(0, pct))}%` } })),
    h("div", { class: "sm-sub" }, `${remaining} ${leftLabel}`));
}

/**
 * Renders a hero stat card.
 */
export function HeroStat({ label, value, unit }) {
  return h("div", {},
    h("span", { class: "lbl" }, label),
    h("div", { class: "kpi-big" }, value, unit ? h("small", {}, unit) : null));
}

/**
 * Renders an insight row.
 */
export function InsightRow({ dot, color, label, sub, value }) {
  return h("div", { class: "insight" },
    h("i", { class: `dot ${color || ""}` }),
    h("span", {}, label),
    sub ? h("span", { class: "sub" }, sub) : null,
    value ? h("b", {}, value) : null);
}

/**
 * Renders a warning/info banner.
 */
export function Banner({ type = "info", children }) {
  return h("p", { class: `banner ${type}`, role: type === "warn" ? "alert" : "status" }, children);
}

/**
 * Renders a form row with two fields side by side.
 */
export function FormRow({ left, right }) {
  return h("div", { class: "form-row" }, left, right);
}

/**
 * Renders a footer action bar.
 */
export function FooterActions({ children }) {
  return h("div", { class: "fa" }, children);
}

/**
 * Renders a key-value pair.
 */
export function KvRow({ key, value, mono = false }) {
  return h("div", {},
    h("span", { class: "k" }, key),
    h("span", { class: `v${mono ? " mono" : ""}` }, value));
}

/**
 * Renders a settings row with label, description, and switch.
 */
export function SettingRow({ title, desc, checked, onChange }) {
  return h("div", { class: "setting" },
    h("div", {},
      h("div", { class: "t" }, title),
      h("div", { class: "d" }, desc)),
    Switch({ checked, onChange, ariaLabel: title }));
}

/**
 * Renders an option card.
 */
export function OptCard({ active, onClick, badge, title, desc }) {
  return h("button", {
    type: "button",
    class: `opt${active ? " on" : ""}`,
    "aria-pressed": String(active),
    onclick: onClick,
  }, badge, h("span", {}, h("b", {}, title), desc ? h("span", {}, desc) : null));
}

/**
 * Renders a tone card.
 */
export function ToneCard({ active, onClick, dot1Color, dot2Color, title, desc }) {
  return h("button", {
    type: "button",
    class: `tone${active ? " on" : ""}`,
    "aria-pressed": String(active),
    onclick: onClick,
  },
    h("span", { class: "tone-dots" },
      h("i", { style: { background: dot1Color } }),
      h("i", { style: { background: dot2Color } })),
    h("span", {}, h("b", {}, title), h("span", {}, desc)));
}

/**
 * Renders a swatch card.
 */
export function SwatchCard({ active, onClick, gradient, label }) {
  return h("button", {
    type: "button",
    class: `swatch${active ? " on" : ""}`,
    "aria-pressed": String(active),
    onclick: onClick,
  },
    h("i", { style: { background: gradient } }),
    label);
}

/**
 * Renders a preview panel.
 */
export function PreviewPanel({ children }) {
  return h("div", { class: "pvbox" }, children);
}

/**
 * Renders a preview row.
 */
export function PreviewRow({ children }) {
  return h("div", { class: "pvrow" }, children);
}
