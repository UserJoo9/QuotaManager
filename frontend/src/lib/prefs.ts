// Per-browser UI preferences (never server state). Same storage key as before, so a
// language/theme/density chosen in the old dashboard carries over. The retired
// colour themes (accent/tone/particles/fx) are dropped on read; nothing else is reset.
import { useSyncExternalStore } from "react";
import type { Lang } from "./i18n";

const KEY = "qm.ui.prefs.v2";
export type Theme = "dark" | "light" | "auto";
export interface NavOrder { v: 1; groups: Record<string, string[]> }
export interface Prefs {
  theme: Theme; lang: Lang; density: "comfortable" | "compact"; motion: "full" | "reduced"; rail: boolean;
  navOrder: NavOrder | null;
  peopleFilter: string; peopleSort: string; netView: "wan" | "lan"; insightsWindow: number;
}
const sysLang: Lang = (typeof navigator !== "undefined" && (navigator.language || "en").toLowerCase().startsWith("ar")) ? "ar" : "en";
export const DEFAULTS: Prefs = { theme: "dark", lang: sysLang, density: "comfortable", motion: "full", rail: false, navOrder: null, peopleFilter: "all", peopleSort: "default", netView: "wan", insightsWindow: 24 };
const RETIRED = ["accent", "tone", "particles", "fx", "fxChosen"];

export function sanitize(raw: Record<string, unknown>): Prefs {
  const p = { ...DEFAULTS } as Prefs & Record<string, unknown>;
  for (const [k, v] of Object.entries(raw || {})) if (!RETIRED.includes(k) && k in DEFAULTS) (p as Record<string, unknown>)[k] = v;
  if (!["dark", "light", "auto"].includes(p.theme)) p.theme = "dark";
  if (!["ar", "en"].includes(p.lang)) p.lang = sysLang;
  if (!["comfortable", "compact"].includes(p.density)) p.density = "comfortable";
  if (!["full", "reduced"].includes(p.motion)) p.motion = "full";
  if (!["wan", "lan"].includes(p.netView)) p.netView = "wan";
  if (typeof p.insightsWindow !== "number") p.insightsWindow = 24;
  if (p.navOrder && (p.navOrder.v !== 1 || typeof p.navOrder.groups !== "object")) p.navOrder = null;
  p.rail = !!p.rail;
  return p;
}
let saved: Record<string, unknown> = {};
try { saved = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch { /* private mode */ }
// the old netview lived in its own key
try { const nv = localStorage.getItem("qm.ui.netview"); if (nv && !saved.netView) saved.netView = nv; } catch { /* ignore */ }
let prefs = sanitize(saved);
const subs = new Set<() => void>();

export const getPrefs = () => prefs;
export function effTheme(): "dark" | "light" {
  if (prefs.theme !== "auto") return prefs.theme;
  try { return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"; } catch { return "dark"; }
}
export function applyPrefs() {
  const r = document.documentElement;
  r.dataset.theme = effTheme(); r.dataset.density = prefs.density; r.dataset.motion = prefs.motion;
  for (const k of ["--accent", "--accent-ink", "--accent-2", "--bg", "--surface", "--surface-2", "--surface-3"]) r.style.removeProperty(k);
}
/** Returns false when the browser refused to store it (the value still applies for this visit). */
export function setPref<K extends keyof Prefs>(k: K, v: Prefs[K]): boolean {
  prefs = { ...prefs, [k]: v };
  saved = { ...saved, [k]: v };
  for (const r of RETIRED) delete saved[r];
  applyPrefs();
  subs.forEach((f) => f());
  try { localStorage.setItem(KEY, JSON.stringify(saved)); return true; } catch { return false; }
}
export function usePrefs(): Prefs {
  return useSyncExternalStore((cb) => { subs.add(cb); return () => subs.delete(cb); }, () => prefs, () => prefs);
}
try { matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (prefs.theme === "auto") { applyPrefs(); subs.forEach((f) => f()); } }); } catch { /* old browser */ }
