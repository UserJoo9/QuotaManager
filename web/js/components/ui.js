import { h, icon } from "../core/dom.js";
import { t, num } from "../core/i18n.js";

// Unit thresholds
const KB = 1024, MB = 1024 ** 2, GB = 1024 ** 3;
const KBPS = 1e3, MBPS = 1e6;
// Avatar gradient: hue(seed) + base offset, second stop + step degrees.
const HUE_MUL = 31, HUE_BASE = 170, HUE_STEP = 42;

/** Progress bar: clipped fill over a .bar track; pct >= 100 (and not neutral) paints "over". */
export const bar = (pct, neutral) => h("div", { class: "bar" + (!neutral && pct >= 100 ? " over" : ""), role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(pct)) },
  h("i", { style: { width: Math.min(100, Math.max(0, pct)) + "%" } }));

const hue = (id) => { let x = 0; for (const c of String(id)) x = (x * HUE_MUL + c.charCodeAt(0)) % 360; return x; };
/** HSL avatar background + text color derived from `id` (fallback to `name`). */
export const avStyle = (id) => { const k = hue(id) + HUE_BASE; return { background: `linear-gradient(135deg,hsl(${k} 72% 58%),hsl(${k + HUE_STEP} 74% 46%))`, color: "#fff" }; };
/** First-letter avatar badge. */
export const avatar = (name, id) => h("span", { class: "av", style: avStyle(id ?? name) }, (name || "?").trim().charAt(0).toUpperCase());

/** Access switch (.sw): on = internet allowed. `label` is the aria-label. */
export const sw = (on, label, onclick) => h("button", { type: "button", role: "switch", class: "sw" + (on ? " on" : ""), "aria-checked": String(!!on), "aria-label": label, onclick }, h("i"));
/** Alias of `sw` used by pages that speak "toggle". */
export const toggle = (checked, label, onToggle) => sw(checked, label, onToggle);

/** Device icon key by name/vendor hints: phone, tv, box, else laptop. */
export function devIcon(d) {
  const s = ((d.name || "") + " " + (d.vendor || "")).toLowerCase();
  if (/iphone|android|galaxy|pixel|phone|xiaomi|redmi|oppo|huawei honor/.test(s)) return "phone";
  if (/tv|chromecast|roku|firestick/.test(s)) return "tv";
  if (/router|gateway|switch|ubiquiti|tp-link|mikrotik|box/.test(s)) return "box";
  return "laptop";
}
/** Bytes → "1.2 GB" etc. (western digits, privacy-isolated via num/iso). */
export function fmtBytes(b) {
  const n = +b || 0;
  if (n >= GB) return `${num(n / GB, 2)} ${t("unit.gb")}`;
  if (n >= MB) return `${num(n / MB, 1)} ${t("unit.mb")}`;
  if (n >= KB) return `${num(n / KB, 0)} ${t("unit.kb")}`;
  return `${num(n, 0)} ${t("unit.b")}`;
}
/** Bit/s → "12 Mbps" etc. */
export function fmtBps(bps) {
  const n = +bps || 0;
  if (n >= MBPS) return `${num(n / MBPS, 1)} ${t("unit.mbps")}`;
  if (n >= KBPS) return `${num(n / KBPS, 0)} ${t("unit.kbps")}`;
  return `${num(n, 0)} ${t("unit.bps")}`;
}
/** Status of a person: {k, text, cls}. */
export function userStatus(u) {
  const online = (u.devices || []).some((d) => d.connected);
  if (u.blocked && u.daily && u.daily.blocked && u.block_state === "quota" && !(u.percent >= 100)) return { k: "daily", text: t("pp.st.daily"), cls: "chip warn" };
  if (u.blocked) return u.guest ? { k: "pending", text: t("pp.st.pending"), cls: "chip accent" } : { k: "blocked", text: t("pp.st.blocked"), cls: "chip danger" };
  return online ? { k: "online", text: t("pp.st.online"), cls: "chip ok" } : { k: "offline", text: t("pp.st.offline"), cls: "chip" };
}
/** Pill label. */
export const chip = (cls, text) => h("span", { class: "chip " + (cls || "") }, text);
export { icon };

/** WAN (metered, counts toward quota) vs LAN (monitoring only, never part of the quota) side by side. */
export function netSplit(o) {
  const box = (cls, label, gb, up, down) => h("div", { class: "ns " + cls },
    h("span", { class: "lbl" }, label), h("b", {}, num(gb), h("small", {}, t("common.gb"))),
    up != null ? h("em", {}, `↑ ${num(up)} · ↓ ${num(down)}`) : null);
  return h("div", { class: "net-split" },
    box("wan", t("ov.net.wan.full"), o.wan, o.wanUp, o.wanDown),
    box("lan", t("ov.net.lan.full"), o.lan, o.lanUp, o.lanDown),
    h("p", { class: "ns-note" }, t("ov.net.split.note")));
}
/** One compact "WAN x GB · LAN y GB" line for list rows (LAN = monitoring only). */
export const netLine = (wanGb, lanGb) => h("span", { class: "lan-line" },
  h("span", { class: "nl wan", title: t("ov.net.wan.full") }, h("i"), `WAN ${num(wanGb || 0)} ${t("common.gb")}`),
  h("span", { class: "nl lan", title: t("ov.net.lan.full") }, h("i"), `LAN ${num(lanGb || 0)} ${t("common.gb")}`));
