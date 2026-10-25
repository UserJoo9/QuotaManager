import { h, icon } from "../core/dom.js";
import { t, num } from "../core/i18n.js";

export const bar = (pct, neutral) => h("div", { class: "bar" + (!neutral && pct >= 100 ? " over" : "") },
  h("i", { style: { width: Math.min(100, Math.max(0, pct)) + "%" } }));

const hue = (id) => { let x = 0; for (const c of String(id)) x = (x * 31 + c.charCodeAt(0)) % 360; return x; };
export const avStyle = (id) => { const k = hue(id) + 170; return { background: `linear-gradient(135deg,hsl(${k} 72% 58%),hsl(${k + 42} 74% 46%))`, color: "#fff" }; };
export const avatar = (name, id) => h("span", { class: "av", style: avStyle(id ?? name) }, (name || "?").trim().charAt(0).toUpperCase());

// access switch (.sw): on = internet allowed
export const sw = (on, label, onclick) => h("button", { type: "button", role: "switch", class: "sw" + (on ? " on" : ""), "aria-checked": String(!!on), "aria-label": label, onclick }, h("i"));
export const toggle = (checked, label, onToggle) => sw(checked, label, onToggle);

export function devIcon(d) {
  const s = ((d.name || "") + " " + (d.vendor || "")).toLowerCase();
  if (/iphone|android|galaxy|pixel|phone|xiaomi|redmi|oppo|huawei honor/.test(s)) return "phone";
  if (/tv|chromecast|roku|firestick/.test(s)) return "tv";
  if (/router|gateway|switch|ubiquiti|tp-link|mikrotik|box/.test(s)) return "box";
  return "laptop";
}
export function fmtBytes(b) {
  const n = +b || 0;
  if (n >= 1024 ** 3) return `${num(n / 1024 ** 3, 2)} ${t("unit.gb")}`;
  if (n >= 1024 ** 2) return `${num(n / 1024 ** 2, 1)} ${t("unit.mb")}`;
  if (n >= 1024) return `${num(n / 1024, 0)} ${t("unit.kb")}`;
  return `${num(n, 0)} ${t("unit.b")}`;
}
export function fmtBps(bps) {
  const n = +bps || 0;
  if (n >= 1e6) return `${num(n / 1e6, 1)} ${t("unit.mbps")}`;
  if (n >= 1e3) return `${num(n / 1e3, 0)} ${t("unit.kbps")}`;
  return `${num(n, 0)} ${t("unit.bps")}`;
}
// status of a person: {k, text, cls}
export function userStatus(u) {
  const online = (u.devices || []).some((d) => d.connected);
  if (u.blocked) return u.guest ? { k: "pending", text: t("pp.st.pending"), cls: "chip accent" } : { k: "blocked", text: t("pp.st.blocked"), cls: "chip danger" };
  return online ? { k: "online", text: t("pp.st.online"), cls: "chip ok" } : { k: "offline", text: t("pp.st.offline"), cls: "chip" };
}
export const chip = (cls, text) => h("span", { class: "chip " + (cls || "") }, text);
export { icon };

// WAN (metered internet) vs LAN (local) usage side by side. LAN is monitoring only - never part of the quota.
export function netSplit(o) {
  const box = (cls, label, gb, up, down) => h("div", { class: "ns " + cls },
    h("span", { class: "lbl" }, label), h("b", {}, num(gb), h("small", {}, t("common.gb"))),
    up != null ? h("em", {}, `↑ ${num(up)} · ↓ ${num(down)}`) : null);
  return h("div", { class: "net-split" },
    box("wan", t("ov.net.wan.full"), o.wan, o.wanUp, o.wanDown),
    box("lan", t("ov.net.lan.full"), o.lan, o.lanUp, o.lanDown),
    h("p", { class: "ns-note" }, t("ov.net.split.note")));
}
// one compact "WAN x GB · LAN y GB" line for list rows (LAN = monitoring only)
export const netLine = (wanGb, lanGb) => h("span", { class: "lan-line" },
  h("span", { class: "nl wan", title: t("ov.net.wan.full") }, h("i"), `WAN ${num(wanGb || 0)} ${t("common.gb")}`),
  h("span", { class: "nl lan", title: t("ov.net.lan.full") }, h("i"), `LAN ${num(lanGb || 0)} ${t("common.gb")}`));
