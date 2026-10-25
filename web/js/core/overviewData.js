// Data for the Overview chart: per-day usage (cached) + a short live speed history.
import { api } from "./api.js";
export const ov = { daily: null, dailyLan: null, dailyAt: 0, live: null, speed: [], speedLan: [], timer: null };
const GB = 1024 ** 3;

export async function loadDaily(force) {
  if (!force && ov.daily && Date.now() - ov.dailyAt < 60000) return;
  const r = await api.get("/api/usage/daily?days=30");
  // fill gaps so every one of the 30 days is a bar
  const byDate = new Map(r.rows.map((x) => [x.date, x]));
  const out = [], start = new Date(r.since + "T00:00:00");
  for (let i = 0; i < r.days; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    const row = byDate.get(key);
    out.push({ date: key, gb: row ? (row.up_bytes + row.down_bytes) / GB : 0 });
  }
  ov.daily = out;
  // LOCAL (LAN) traffic per day, same 30-day shape (older gateways have no lan_rows)
  const lanBy = new Map((r.lan_rows || []).map((x) => [x.date, x]));
  ov.dailyLan = out.map((o) => { const row = lanBy.get(o.date); return { date: o.date, gb: row ? (row.up_bytes + row.down_bytes) / GB : 0 }; });
  ov.dailyAt = Date.now();
}
export async function loadLive() {
  const r = await api.get("/api/history/live");
  ov.live = r;
  // First load after a refresh: the gateway remembers the last minutes, so draw them at once.
  if (!ov.speed.length && Array.isArray(r.history) && r.history.length > 1) {
    ov.speed = r.history.map((x) => ({ down: x.down || 0, up: x.up || 0 }));
    ov.speedLan = r.history.map((x) => ({ down: x.lan_down || 0, up: x.lan_up || 0 }));
    return;
  }
  ov.speed.push({ down: r.total_down_bps || 0, up: r.total_up_bps || 0 });
  if (ov.speed.length > 60) ov.speed.shift();
  ov.speedLan.push({ down: r.total_lan_down_bps || 0, up: r.total_lan_up_bps || 0 });
  if (ov.speedLan.length > 60) ov.speedLan.shift();
}
