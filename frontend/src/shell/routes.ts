import { lazy, type ComponentType, type LazyExoticComponent } from "react";

// Stable route IDs (never translated labels) — URLs stay #/overview, #/people, …
export type RouteId = "overview" | "people" | "quota" | "insights" | "internet" | "network" | "vpn" | "security" | "share" | "system" | "help";
export interface RouteDef { id: RouteId; icon: string; group: GroupId; load: () => Promise<{ default: ComponentType }> }
export type GroupId = "main" | "net" | "tools" | "settings";
export const GROUPS: { id: GroupId; label: string }[] = [
  { id: "main", label: "nav.main" }, { id: "net", label: "nav.net" }, { id: "tools", label: "nav.tools" }, { id: "settings", label: "nav.settings" },
];
// Each page is its own chunk: the first paint only downloads the shell + the current page.
export const ROUTES: RouteDef[] = [
  { id: "overview", icon: "overview", group: "main", load: () => import("../pages/Overview") },
  { id: "people", icon: "people", group: "main", load: () => import("../pages/People") },
  { id: "quota", icon: "quota", group: "main", load: () => import("../pages/Quota") },
  { id: "insights", icon: "insights", group: "main", load: () => import("../pages/Insights") },
  { id: "internet", icon: "internet", group: "net", load: () => import("../pages/Internet") },
  { id: "network", icon: "network", group: "net", load: () => import("../pages/Network") },
  { id: "vpn", icon: "vpn", group: "net", load: () => import("../pages/Vpn") },
  { id: "security", icon: "security", group: "net", load: () => import("../pages/Security") },
  { id: "share", icon: "share", group: "tools", load: () => import("../pages/LegacyPage") /* Share */ },
  { id: "system", icon: "system", group: "settings", load: () => import("../pages/LegacyPage") /* System */ },
  { id: "help", icon: "help", group: "settings", load: () => import("../pages/Help") },
];
export const PAGES = Object.fromEntries(ROUTES.map((r) => [r.id, lazy(r.load)])) as Record<RouteId, LazyExoticComponent<ComponentType>>;
export const routeById = (id: string) => ROUTES.find((r) => r.id === id);

/* ---------------- prefetch: intent first, then one or two likely routes when idle */
const warmed = new Set<string>();
function cheapConnection() {
  const n = navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string }; hardwareConcurrency?: number };
  if (n.connection?.saveData || /2g/.test(n.connection?.effectiveType || "")) return false;
  return (n.hardwareConcurrency || 4) > 2 && !document.hidden;
}
export function prefetchRoute(id: string) {
  if (warmed.has(id)) return; const r = routeById(id); if (!r) return;
  warmed.add(id); r.load().catch(() => warmed.delete(id));
}
const NEXT: Partial<Record<RouteId, RouteId[]>> = { overview: ["people", "quota"], people: ["quota"], quota: ["people"], network: ["internet"] };
export function prefetchLikely(current: RouteId) {
  if (!cheapConnection()) return;
  const ric = (window as Window & { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number }).requestIdleCallback || ((cb: () => void) => setTimeout(cb, 1500));
  // at most two background chunks, one at a time
  const list = (NEXT[current] || []).slice(0, 2);
  const step = (i: number) => { if (i >= list.length) return; ric(() => { prefetchRoute(list[i]); step(i + 1); }, { timeout: 4000 }); };
  step(0);
}

/* ---------------- sidebar order (versioned, stable IDs, new routes keep their default slot) */
import type { NavOrder } from "../lib/prefs";
export function defaultOrder(): Record<string, string[]> {
  const g: Record<string, string[]> = {};
  for (const r of ROUTES) (g[r.group] ||= []).push(r.id);
  return g;
}
export function mergeOrder(saved: NavOrder | null): Record<string, string[]> {
  const def = defaultOrder();
  if (!saved) return def;
  const out: Record<string, string[]> = {};
  for (const [gid, ids] of Object.entries(def)) {
    const keep = (saved.groups[gid] || []).filter((id) => ids.includes(id));
    // routes added in a later version slot in where the default puts them
    ids.forEach((id, i) => { if (!keep.includes(id)) keep.splice(Math.min(i, keep.length), 0, id); });
    out[gid] = keep;
  }
  return out;
}
