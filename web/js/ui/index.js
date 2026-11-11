// UI layer — presentational components.
// Render inputs and emit user intent. Receive data and callbacks as props.
// Do NOT import stores, contexts, or services directly.

import { h, icon } from "../core/dom.js";
import { clamp } from "../shared/index.js";

// ---- Presentational atoms ----

/**
 * Renders a button. Pure presentation; onClick is provided by caller.
 * @param {Object} props
 * @param {string} [props.className='']
 * @param {"button"|"submit"|"reset"} [props.type='button']
 * @param {boolean} [props.disabled=false]
 * @param {*} props.children
 * @param {Function} [props.onClick]
 * @param {string} [props.ariaLabel]
 * @param {string} [props.title]
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} [props.className='icon-btn']
 * @param {Function} [props.onClick]
 * @param {string} [props.ariaLabel]
 * @param {string} [props.title]
 * @param {string} props.iconName
 * @param {number} [props.iconSize=18]
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} [props.checked=false]
 * @param {string} [props.label]
 * @param {Function} props.onChange
 * @param {string} [props.ariaLabel]
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} props.label
 * @param {Element} props.input
 * @param {string} [props.hint]
 * @param {boolean} [props.required=false]
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} [props.className='card']
 * @param {*} props.children
 * @param {*} [props.header]
 * @returns {Element}
 */
export function Card({ className = "card", children, header }) {
  return h("section", { class: className },
    header ? h("header", { class: "card-h" }, header) : null,
    children);
}

/**
 * Renders a card header with title and optional subtitle/chip.
 * @param {Object} props
 * @param {string} props.title
 * @param {*} [props.sub]
 * @param {*} [props.chip]
 * @returns {Element}
 */
export function CardHeader({ title, sub, chip }) {
  return h("div", {}, h("h2", {}, title), sub ? h("span", { class: "sub" }, sub) : null, chip);
}

/**
 * Renders an empty state.
 * @param {Object} props
 * @param {string} [props.iconName='check']
 * @param {string} props.title
 * @param {string} [props.sub]
 * @returns {Element}
 */
export function EmptyState({ iconName = "check", title, sub }) {
  return h("div", { class: "empty" },
    h("span", { class: "ei" }, icon(iconName, 22)),
    h("b", {}, title),
    sub ? h("span", {}, sub) : null);
}

/**
 * Renders a skeleton loader.
 * @param {Object} props
 * @param {string} [props.height='200px']
 * @returns {Element}
 */
export function Skeleton({ height = "200px" }) {
  return h("div", { class: "skeleton", style: { height } });
}

/**
 * Renders a chip/badge.
 * @param {Object} props
 * @param {string} [props.className='']
 * @param {*} props.children
 * @returns {Element}
 */
export function Chip({ className = "", children }) {
  return h("span", { class: `chip ${className}`.trim() }, children);
}

/**
 * Renders a progress bar.
 * @param {Object} props
 * @param {number} props.pct
 * @param {boolean} [props.neutral=false]
 * @param {number} [props['aria-valuenow']]
 * @returns {Element}
 */
export function ProgressBar({ pct, neutral = false, "aria-valuenow": value }) {
  return h("div", {
    class: `bar${!neutral && pct >= 100 ? " over" : ""}`,
    role: "progressbar",
    "aria-valuemin": "0",
    "aria-valuemax": "100",
    "aria-valuenow": String(Math.round(value ?? pct)),
  }, h("i", { style: { width: clamp(pct, 0, 100) + "%" } }));
}

/**
 * Renders a consumer row (person/device usage).
 * @param {Object} props
 * @param {*} props.avatar
 * @param {string} props.name
 * @param {string} [props.sub]
 * @param {string} props.usage
 * @param {string} [props.percent]
 * @param {number} props.barPct
 * @param {*} props.status
 * @param {*} [props.actions]
 * @returns {Element}
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
      h("div", { class: "bar" }, h("i", { style: { width: `${clamp(barPct, 0, 100)}%` } }))),
    status);
}

/**
 * Renders a dialog.
 * @param {Object} props
 * @param {string} [props.role='dialog']
 * @param {*} props.children
 * @param {*} [props.header]
 * @param {*} [props.actions]
 * @returns {Element}
 */
export function Dialog({ role = "dialog", children, header, actions }) {
  return h("div", { class: "dialog", role, "aria-modal": "true" },
    header ? h("header", {}, header) : null,
    h("div", { class: "dialog-body" }, children),
    actions ? h("div", { class: "dialog-actions" }, actions) : null);
}

/**
 * Renders a toast.
 * @param {Object} props
 * @param {string} [props.kind='']
 * @param {string} props.message
 * @param {string} [props.iconName]
 * @returns {Element}
 */
export function Toast({ kind = "", message, iconName }) {
  return h("div", { class: `toast ${kind}` },
    icon(iconName || (kind === "bad" ? "alert" : "check"), 16),
    h("span", {}, message));
}

/**
 * Renders a language/theme switch segment.
 * @param {Object} props
 * @param {Array} props.items
 * @param {*} props.value
 * @param {Function} props.onChange
 * @param {string} [props.ariaLabel]
 * @returns {Element}
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
 * @param {Object} props
 * @param {Array} props.tabs
 * @param {*} props.active
 * @param {Function} props.onChange
 * @param {string} [props.ariaLabel]
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} [props.placeholder]
 * @param {string} props.value
 * @param {Function} props.onChange
 * @param {string} [props.ariaLabel]
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} props.lang
 * @param {Function} props.onChange
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} props.dark
 * @param {Function} props.onChange
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} props.hidden
 * @param {Function} props.onChange
 * @returns {Element}
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
 * @param {Object} props
 * @param {Function} props.onClick
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} props.rail
 * @param {Function} props.onClick
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} props.iconName
 * @param {string} props.label
 * @param {boolean} [props.active]
 * @param {*} [props.count]
 * @param {Function} props.onClick
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} props.label
 * @returns {Element}
 */
export function NavGroupLabel({ label }) {
  return h("div", { class: "nav-glabel" }, label);
}

/**
 * Renders a side meter (bundle usage in sidebar).
 * @param {Object} props
 * @param {number} props.pct
 * @param {string} props.remaining
 * @param {string} props.label
 * @param {string} props.leftLabel
 * @returns {Element}
 */
export function SideMeter({ pct, remaining, label, leftLabel }) {
  return h("div", { class: "side-meter" },
    h("div", { class: "sm-top" },
      h("span", {}, label),
      h("b", {}, `${pct}%`)),
    h("div", { class: "bar" }, h("i", { style: { width: `${clamp(pct, 0, 100)}%` } })),
    h("div", { class: "sm-sub" }, `${remaining} ${leftLabel}`));
}

/**
 * Renders a hero stat card.
 * @param {Object} props
 * @param {string} props.label
 * @param {string} props.value
 * @param {string} [props.unit]
 * @returns {Element}
 */
export function HeroStat({ label, value, unit }) {
  return h("div", {},
    h("span", { class: "lbl" }, label),
    h("div", { class: "kpi-big" }, value, unit ? h("small", {}, unit) : null));
}

/**
 * Renders an insight row.
 * @param {Object} props
 * @param {*} props.dot
 * @param {string} [props.color]
 * @param {string} props.label
 * @param {string} [props.sub]
 * @param {*} [props.value]
 * @returns {Element}
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
 * @param {Object} props
 * @param {string} [props.type='info']
 * @param {*} props.children
 * @returns {Element}
 */
export function Banner({ type = "info", children }) {
  return h("p", { class: `banner ${type}`, role: type === "warn" ? "alert" : "status" }, children);
}

/**
 * Renders a form row with two fields side by side.
 * @param {Object} props
 * @param {*} props.left
 * @param {*} props.right
 * @returns {Element}
 */
export function FormRow({ left, right }) {
  return h("div", { class: "form-row" }, left, right);
}

/**
 * Renders a footer action bar.
 * @param {Object} props
 * @param {*} props.children
 * @returns {Element}
 */
export function FooterActions({ children }) {
  return h("div", { class: "fa" }, children);
}

/**
 * Renders a key-value pair.
 * @param {Object} props
 * @param {string} props.key
 * @param {string} props.value
 * @param {boolean} [props.mono=false]
 * @returns {Element}
 */
export function KvRow({ key, value, mono = false }) {
  return h("div", {},
    h("span", { class: "k" }, key),
    h("span", { class: `v${mono ? " mono" : ""}` }, value));
}

/**
 * Renders a settings row with label, description, and switch.
 * @param {Object} props
 * @param {string} props.title
 * @param {string} props.desc
 * @param {boolean} props.checked
 * @param {Function} props.onChange
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} [props.active]
 * @param {Function} props.onClick
 * @param {*} [props.badge]
 * @param {string} props.title
 * @param {string} [props.desc]
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} [props.active]
 * @param {Function} props.onClick
 * @param {string} props.dot1Color
 * @param {string} props.dot2Color
 * @param {string} props.title
 * @param {string} props.desc
 * @returns {Element}
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
 * @param {Object} props
 * @param {boolean} [props.active]
 * @param {Function} props.onClick
 * @param {string} props.gradient
 * @param {string} props.label
 * @returns {Element}
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
 * @param {Object} props
 * @param {*} props.children
 * @returns {Element}
 */
export function PreviewPanel({ children }) {
  return h("div", { class: "pvbox" }, children);
}

/**
 * Renders a preview row.
 * @param {Object} props
 * @param {*} props.children
 * @returns {Element}
 */
export function PreviewRow({ children }) {
  return h("div", { class: "pvrow" }, children);
}
