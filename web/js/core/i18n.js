import en from "../i18n/en.js";
import ar from "../i18n/ar.js";
const dicts = { en, ar };
let lang = "en";
const LRI = "⁦", PDI = "⁩"; // isolate numbers/MACs/IPs so they never reorder inside RTL text

export const getLang = () => lang;
export function setLang(l) {
  lang = dicts[l] ? l : "en";
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
}
export const t = (key) => dicts[lang][key] ?? dicts.en[key] ?? key;
// Privacy eye (shared key with the classic UI): MACs are shown masked unless the admin reveals them.
let hide = true;
try { hide = localStorage.getItem("quota_privacy_hide") !== "0"; } catch { /* private mode */ }
export const privacyHidden = () => hide;
export function setPrivacyHidden(v) { hide = !!v; try { localStorage.setItem("quota_privacy_hide", hide ? "1" : "0"); } catch { /* ignore */ } }
const MACRE = /^([0-9a-f]{2})[:-]([0-9a-f]{2})[:-]([0-9a-f]{2})[:-][0-9a-f]{2}[:-][0-9a-f]{2}[:-][0-9a-f]{2}$/i;
export const iso = (s) => LRI + (hide && MACRE.test(String(s)) ? String(s).replace(MACRE, "$1:$2:$3:••:••:••") : s) + PDI;
// Western digits in both languages: consistent with the legacy UI, and what network admins expect.
export function num(n, digits = 1) {
  if (n == null || Number.isNaN(Number(n))) return "–";
  return iso(Number(n).toLocaleString("en-US", { maximumFractionDigits: digits }));
}
