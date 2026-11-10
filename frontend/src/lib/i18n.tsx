import { createContext, useContext, type ReactNode } from "react";
// The dictionaries live in web/js/i18n/{en,ar}.js (one source, Arabic/English parity
// is tested). Only the language in use is downloaded: each is its own chunk.
export type Lang = "en" | "ar";
const dicts: Partial<Record<Lang, Record<string, string>>> = {};
const loaders: Record<Lang, () => Promise<{ default: Record<string, string> }>> = {
  en: () => import("../../../web/js/i18n/en.js"),
  ar: () => import("../../../web/js/i18n/ar.js"),
};
export async function loadLang(l: Lang) { if (!dicts[l]) dicts[l] = (await loaders[l]()).default; }
let lang: Lang = "en";
const LRI = "⁦", PDI = "⁩"; // isolate numbers/MACs/IPs so they never reorder inside RTL text

export const getLang = () => lang;
export function setLang(l: Lang) {
  lang = l === "ar" ? "ar" : "en";
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
}
export const t = (key: string): string => dicts[lang]?.[key] ?? dicts.en?.[key] ?? key;
/** t() with {name} placeholders. */
export const tf = (key: string, vars: Record<string, string | number>) => t(key).replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? ""));

let hide = true;
try { hide = localStorage.getItem("quota_privacy_hide") !== "0"; } catch { /* private mode */ }
export const privacyHidden = () => hide;
export function setPrivacyHidden(v: boolean) { hide = !!v; try { localStorage.setItem("quota_privacy_hide", hide ? "1" : "0"); } catch { /* ignore */ } }
const MACRE = /^([0-9a-f]{2})[:-]([0-9a-f]{2})[:-]([0-9a-f]{2})[:-][0-9a-f]{2}[:-][0-9a-f]{2}[:-][0-9a-f]{2}$/i;
export const mask = (s: unknown) => (hide && MACRE.test(String(s)) ? String(s).replace(MACRE, "$1:$2:$3:••:••:••") : String(s ?? ""));
export const iso = (s: unknown) => LRI + mask(s) + PDI;
// Western digits in both languages: what network admins expect, and consistent with IPs/MACs.
export function num(n: unknown, digits = 1): string {
  if (n == null || Number.isNaN(Number(n))) return "–";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: digits });
}
export function fmtDate(d: Date | string | number, opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" }) {
  const x = typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d) ? new Date(d + "T00:00:00") : new Date(d);
  try { return x.toLocaleDateString(lang === "ar" ? "ar-EG-u-nu-latn" : "en-GB", opts); } catch { return x.toDateString(); }
}
export function fmtTime(d: Date | string | number) {
  try { return new Date(d).toLocaleTimeString(lang === "ar" ? "ar-EG-u-nu-latn" : "en-GB", { hour: "2-digit", minute: "2-digit" }); } catch { return ""; }
}

// Components re-render on language change through this context (the value is the language).
export const LangContext = createContext<Lang>("en");
export const useLang = () => useContext(LangContext);
export function LangProvider({ value, children }: { value: Lang; children: ReactNode }) {
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}
