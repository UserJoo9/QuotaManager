import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { LiveTraffic } from "./types";

const GB = 1024 ** 3;
export interface DayRow { date: string; gb: number; lan: number }
/** Per-day totals for the last 30 days (memory cache, 60 s). Gaps are filled so every day is a bar. */
export function useDailyUsage(enabled = true) {
  return useQuery({
    queryKey: ["usage-daily", 30], enabled, staleTime: 60_000,
    queryFn: async ({ signal }) => {
      const r = await api.get<{ days: number; since: string; rows: { date: string; up_bytes: number; down_bytes: number }[]; lan_rows?: { date: string; up_bytes: number; down_bytes: number }[] }>("/api/usage/daily?days=30");
      void signal;
      const by = new Map(r.rows.map((x) => [x.date, x])), lan = new Map((r.lan_rows || []).map((x) => [x.date, x]));
      const out: DayRow[] = [], start = new Date(r.since + "T00:00:00");
      for (let i = 0; i < r.days; i++) {
        const d = new Date(start); d.setDate(start.getDate() + i);
        const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        const w = by.get(key), l = lan.get(key);
        out.push({ date: key, gb: w ? (w.up_bytes + w.down_bytes) / GB : 0, lan: l ? (l.up_bytes + l.down_bytes) / GB : 0 });
      }
      return out;
    },
  });
}
// Short speed history kept across route switches, bounded (no growth over a long session).
export interface SpeedPt { t: number; down: number; up: number; ldown: number; lup: number }
const hist: SpeedPt[] = [];
export const speedHistory = () => hist;
/** Live speeds: polled every 5 s ONLY while a page that shows them is open and the tab is visible. */
export function useLiveTraffic(deviceId: string = "all", interval = 5000) {
  return useQuery({
    queryKey: ["live", deviceId], refetchInterval: interval, refetchIntervalInBackground: false, staleTime: interval - 500, gcTime: 30_000,
    queryFn: async () => {
      const r = await api.get<LiveTraffic>(`/api/history/live${deviceId !== "all" ? `?device_id=${encodeURIComponent(deviceId)}` : ""}`);
      if (deviceId === "all") {
        if (!hist.length && Array.isArray(r.history) && r.history.length > 1) {
          const now = Date.now(), n = r.history.length;
          r.history.forEach((x, i) => hist.push({ t: now - (n - 1 - i) * 5000, down: x.down || 0, up: x.up || 0, ldown: x.lan_down || 0, lup: x.lan_up || 0 }));
        } else hist.push({ t: Date.now(), down: r.total_down_bps || 0, up: r.total_up_bps || 0, ldown: r.total_lan_down_bps || 0, lup: r.total_lan_up_bps || 0 });
        while (hist.length > 60) hist.shift();
      }
      return r;
    },
  });
}
