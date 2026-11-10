import { t, num } from "./i18n";
import type { User, Device } from "./types";

export function fmtBytes(b: number) {
  const n = +b || 0;
  if (n >= 1024 ** 3) return `${num(n / 1024 ** 3, 2)} ${t("unit.gb")}`;
  if (n >= 1024 ** 2) return `${num(n / 1024 ** 2, 1)} ${t("unit.mb")}`;
  if (n >= 1024) return `${num(n / 1024, 0)} ${t("unit.kb")}`;
  return `${num(n, 0)} ${t("unit.b")}`;
}
export function fmtBps(bps: number) {
  const n = +bps || 0;
  if (n >= 1e6) return `${num(n / 1e6, 1)} ${t("unit.mbps")}`;
  if (n >= 1e3) return `${num(n / 1e3, 0)} ${t("unit.kbps")}`;
  return `${num(n, 0)} ${t("unit.bps")}`;
}
export const gb = (v: number, d = 1) => `${num(v, d)} ${t("common.gb")}`;

export type Tone = "neutral" | "success" | "warning" | "danger" | "info" | "accent";
export interface Status { k: "daily" | "pending" | "blocked" | "online" | "offline" | "quota"; text: string; tone: Tone; reason?: string }
/** Status of a person, plus WHY they are cut (never only a colour). */
export function userStatus(u: User): Status {
  const online = (u.devices || []).some((d) => d.connected);
  if (u.blocked && u.daily?.blocked && u.block_state === "quota" && !(u.percent >= 100)) return { k: "daily", text: t("pp.st.daily"), tone: "warning", reason: t("why.daily") };
  if (u.blocked && u.guest) return { k: "pending", text: t("pp.st.pending"), tone: "info", reason: t("why.guest") };
  if (u.blocked && u.block_state === "quota") return { k: "quota", text: t("st.quota"), tone: "danger", reason: t("why.quota") };
  if (u.blocked) return { k: "blocked", text: t("pp.st.blocked"), tone: "danger", reason: t("why.admin") };
  return online ? { k: "online", text: t("pp.st.online"), tone: "success" } : { k: "offline", text: t("pp.st.offline"), tone: "neutral" };
}
export function deviceStatus(d: Device): Status {
  if (d.blocked) return { k: "blocked", text: t("pp.st.blocked"), tone: "danger", reason: d.block_state === "quota" ? t("why.quota") : t("why.admin") };
  return d.connected ? { k: "online", text: t("pp.st.online"), tone: "success" } : { k: "offline", text: t("pp.st.offline"), tone: "neutral" };
}
export function devIcon(d: { name?: string; vendor?: string }) {
  const s = ((d.name || "") + " " + (d.vendor || "")).toLowerCase();
  if (/iphone|android|galaxy|pixel|phone|xiaomi|redmi|oppo|huawei|honor/.test(s)) return "phone";
  if (/tv|chromecast|roku|firestick/.test(s)) return "tv";
  if (/router|gateway|switch|ubiquiti|tp-link|mikrotik|box/.test(s)) return "box";
  return "laptop";
}
export const MAC_RE = /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/;
export const normMac = (s: string) => s.trim().toLowerCase().replace(/-/g, ":");
export function isIPv4(s: string) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s.trim());
  return !!m && m.slice(1).every((x) => +x <= 255 && String(+x) === x);
}
export function isIPv6(s: string) { return /^[0-9a-f:]+$/i.test(s.trim()) && s.includes(":") && s.split("::").length <= 2; }
export function isCidr4(s: string) { const [ip, p] = s.trim().split("/"); return isIPv4(ip) && (p === undefined || (/^\d{1,2}$/.test(p) && +p <= 32)); }
export const allDevices = (users: User[]) => users.flatMap((u) => (u.devices || []).map((d) => ({ ...d, owner: u.name, ownerId: u.id })));
