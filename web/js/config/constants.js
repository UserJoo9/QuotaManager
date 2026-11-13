// Canonical frontend constants — single source of truth for all appearance/runtime values.
// Owned by config; consumed by stores, services, hooks, and UI. No business decisions here.

/** @typedef {Object} AccentShade
 * @property {string} a - primary accent color
 * @property {string} ink - text color on accent
 * @property {string} b - secondary accent color
 */

/** @typedef {Object} Accent
 * @property {AccentShade} dark
 * @property {AccentShade} light
 */

/** @typedef {Object} Tone
 * @property {string} name - display name
 * @property {string} a - primary color
 * @property {string} b - secondary color
 * @property {string} la - light variant of a
 * @property {string} lb - light variant of b
 * @property {string[]|null} bg - gradient stops (null = none)
 */

/** @typedef {Object} Defaults
 * @property {string} tone - active tone key ("" = system default)
 * @property {string} particles - active particle preset
 * @property {string} theme - "dark"|"light"
 * @property {string} lang - detected UI language ("ar"|"en")
 * @property {string} accent - active accent key
 * @property {string} fx - effect level
 * @property {string} density - UI density
 * @property {boolean} rail - rail state
 */

export const STORAGE_KEY_PREFS = "qm.ui.prefs.v2";
export const STORAGE_KEY_PRIVACY = "quota_privacy_hide";
export const STORAGE_KEY_LIVE_DEV = "qm_live_dev_name";

export const ACCENTS = {
  azure:  { dark: { a: "#5B8CFF", ink: "#06101F", b: "#5EEAD4" }, light: { a: "#3B63F0", ink: "#FFFFFF", b: "#0EA5C6" } },
  violet: { dark: { a: "#A58BFF", ink: "#0E0820", b: "#F0A6FF" }, light: { a: "#6D4AE0", ink: "#FFFFFF", b: "#C04AE0" } },
  emerald:{ dark: { a: "#34D6A0", ink: "#03130C", b: "#B7F34D" }, light: { a: "#0A7F58", ink: "#FFFFFF", b: "#5BAA12" } },
  coral:  { dark: { a: "#FF8A73", ink: "#1F0904", b: "#FFC857" }, light: { a: "#C93A25", ink: "#FFFFFF", b: "#E58A00" } },
  amber:  { dark: { a: "#F5B544", ink: "#1A1000", b: "#FFE08A" }, light: { a: "#8A5A00", ink: "#FFFFFF", b: "#C98A00" } },
};

export const TONES = {
  default: { name: "Cobalt Obsidian", a: "#38bdf8", b: "#2563eb", la: "#2563eb", lb: "#0ea5e9", bg: null },
  emerald: { name: "Cyber Emerald", a: "#10b981", b: "#34d399", la: "#047857", lb: "#10b981", bg: ["#030a06", "#0a2014", "#123220", "#1b492f"] },
  violet:  { name: "Tokyo Violet",  a: "#a855f7", b: "#c084fc", la: "#7c3aed", lb: "#a855f7", bg: ["#090614", "#181136", "#251b4f", "#34276e"] },
  crimson: { name: "Sunset Crimson",a: "#f43f5e", b: "#fb7185", la: "#be123c", lb: "#f43f5e", bg: ["#0d0507", "#240f16", "#381622", "#4e1e30"] },
  amber:   { name: "Solar Amber",   a: "#f59e0b", b: "#fbbf24", la: "#b45309", lb: "#d97706", bg: ["#0c0802", "#241908", "#36260d", "#4d3613"] },
  nord:    { name: "Nord Cyan",     a: "#2dd4bf", b: "#5eead4", la: "#0f766e", lb: "#14b8a6", bg: ["#070d12", "#142330", "#1e3549", "#294863"] },
};

export const PARTICLES = ["mesh", "dust", "rain", "orbs", "off"];

export const DEFAULTS = {
  tone: "", particles: "mesh", theme: "dark",
  lang: (navigator.language || "en").toLowerCase().startsWith("ar") ? "ar" : "en",
  accent: "azure", fx: "rich", density: "comfortable", rail: false,
};

export const QUICK_GB = [10, 25, 50];
export const DAY_MS = 86400000; // ms per day
export const MAX_DIRECT = 50 * 1024 * 1024;
export const CHUNK_SIZE = 512 * 1024;

export const LRI = "\u2066"; // Left-to-Right Isolate
export const PDI = "\u2069"; // Pop Directional Isolate
