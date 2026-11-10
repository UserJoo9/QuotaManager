// Live snapshot feed: WebSocket push with reconnect + polling fallback.
import { api } from "./api";
import type { Dashboard } from "./types";

export type LiveState = "connecting" | "live" | "reconnecting" | "polling" | "offline";
export function startLive(onSnapshot: (d: Dashboard) => void, onState: (s: LiveState) => void) {
  let ws: WebSocket | null = null, retry = 0, stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined, poll: ReturnType<typeof setInterval> | undefined, ping: ReturnType<typeof setInterval> | undefined;
  let last = "";
  let pending: Dashboard | null = null;
  const deliver = (d: Dashboard) => {
    // A background tab never re-renders: it keeps the newest snapshot and draws it once on return.
    if (document.hidden) { pending = d; return; }
    let j = ""; try { j = JSON.stringify(d); } catch { /* draw anyway */ }
    if (j && j === last) return; last = j; onSnapshot(d);
  };
  const onVis = () => { if (!document.hidden && pending) { const d = pending; pending = null; deliver(d); } if (!document.hidden && !ws && !stopped) { clearTimeout(timer); retry = 0; connect(); } };
  document.addEventListener("visibilitychange", onVis);
  const pollOnce = async () => { try { deliver(await api.get<Dashboard>("/api/dashboard")); onState("polling"); } catch (e) { if ((e as { status?: number }).status !== 401) onState("offline"); } };
  const startPoll = () => { if (!poll) { pollOnce(); poll = setInterval(pollOnce, 5000); onState("polling"); } };
  const stopPoll = () => { clearInterval(poll); poll = undefined; };
  function connect() {
    if (stopped) return;
    onState(retry ? "reconnecting" : "connecting");
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { retry = 0; stopPoll(); onState("live"); clearInterval(ping); ping = setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 5000); };
    ws.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (m.type === "snapshot") deliver(m.data); } catch { /* ignore */ } };
    ws.onclose = () => { clearInterval(ping); ws = null; if (stopped) return; startPoll(); clearTimeout(timer); timer = setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++) * (0.8 + Math.random() * 0.4)); };
    ws.onerror = () => { try { ws?.close(); } catch { /* noop */ } };
  }
  connect();
  return () => { stopped = true; document.removeEventListener("visibilitychange", onVis); clearInterval(ping); clearTimeout(timer); stopPoll(); ws?.close(); };
}
