// Per-browser appearance preferences. Never touches server state; storage may be unavailable.
const KEY = "qm.ui.prefs.v2";
export const ACCENTS = {
  azure: { dark: { a: "#5B8CFF", ink: "#06101F", b: "#5EEAD4" }, light: { a: "#3B63F0", ink: "#FFFFFF", b: "#0EA5C6" } },
  violet: { dark: { a: "#A58BFF", ink: "#0E0820", b: "#F0A6FF" }, light: { a: "#6D4AE0", ink: "#FFFFFF", b: "#C04AE0" } },
  emerald: { dark: { a: "#34D6A0", ink: "#03130C", b: "#B7F34D" }, light: { a: "#0A7F58", ink: "#FFFFFF", b: "#5BAA12" } },
  coral: { dark: { a: "#FF8A73", ink: "#1F0904", b: "#FFC857" }, light: { a: "#C93A25", ink: "#FFFFFF", b: "#E58A00" } },
  amber: { dark: { a: "#F5B544", ink: "#1A1000", b: "#FFE08A" }, light: { a: "#8A5A00", ink: "#FFFFFF", b: "#C98A00" } },
};
// System themes (ported from the classic dashboard): accent pair + a tinted dark background.
export const TONES = {
  default: { name: "Cobalt Obsidian", a: "#38bdf8", b: "#2563eb", la: "#2563eb", lb: "#0ea5e9", bg: null },
  emerald: { name: "Cyber Emerald", a: "#10b981", b: "#34d399", la: "#047857", lb: "#10b981", bg: ["#030a06", "#0a2014", "#123220", "#1b492f"] },
  violet: { name: "Tokyo Violet", a: "#a855f7", b: "#c084fc", la: "#7c3aed", lb: "#a855f7", bg: ["#090614", "#181136", "#251b4f", "#34276e"] },
  crimson: { name: "Sunset Crimson", a: "#f43f5e", b: "#fb7185", la: "#be123c", lb: "#f43f5e", bg: ["#0d0507", "#240f16", "#381622", "#4e1e30"] },
  amber: { name: "Solar Amber", a: "#f59e0b", b: "#fbbf24", la: "#b45309", lb: "#d97706", bg: ["#0c0802", "#241908", "#36260d", "#4d3613"] },
  nord: { name: "Nord Cyan", a: "#2dd4bf", b: "#5eead4", la: "#0f766e", lb: "#14b8a6", bg: ["#070d12", "#142330", "#1e3549", "#294863"] },
};
export const PARTICLES = ["mesh", "dust", "rain", "orbs", "off"];
const DEFAULTS = { tone: "", particles: "mesh", theme: "dark", lang: (navigator.language || "en").toLowerCase().startsWith("ar") ? "ar" : "en", accent: "azure", fx: "rich", density: "comfortable", rail: false };
let prefs = { ...DEFAULTS };
try { Object.assign(prefs, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch { /* private mode */ }
if (!/^(ar|en)$/.test(prefs.lang)) prefs.lang = DEFAULTS.lang;
if (!ACCENTS[prefs.accent]) prefs.accent = "azure";
if (prefs.tone && !TONES[prefs.tone]) prefs.tone = "";
if (!PARTICLES.includes(prefs.particles)) prefs.particles = "mesh";

export const getPrefs = () => ({ ...prefs });
export function effTheme() {
  if (prefs.theme !== "auto") return prefs.theme === "light" ? "light" : "dark";
  try { return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"; } catch { return "dark"; }
}
export function applyPrefs() {
  const r = document.documentElement, th = effTheme(), a = ACCENTS[prefs.accent][th];
  r.dataset.theme = th; r.dataset.fx = prefs.fx; r.dataset.density = prefs.density;
  const tn = prefs.tone && TONES[prefs.tone];
  const col = tn ? { a: th === "dark" ? tn.a : tn.la, ink: th === "dark" ? "#06101F" : "#FFFFFF", b: th === "dark" ? tn.b : tn.lb } : a;
  r.style.setProperty("--accent", col.a); r.style.setProperty("--accent-ink", col.ink); r.style.setProperty("--accent-2", col.b);
  const bg = tn && th === "dark" && tn.bg;
  for (const [i, k] of ["--bg", "--surface", "--surface-2", "--surface-3"].entries()) {
    if (bg) r.style.setProperty(k, bg[i]); else r.style.removeProperty(k);
  }
  r.dataset.tone = prefs.tone || "";
  r.dispatchEvent(new CustomEvent("qm:prefs"));
}
export function setPref(k, v) {
  prefs[k] = v; applyPrefs();
  try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch { /* ignore */ }
}
