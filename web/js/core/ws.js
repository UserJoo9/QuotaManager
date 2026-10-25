// Live snapshot feed with automatic reconnect + polling fallback (mirrors legacy behaviour).
import { api } from "./api.js";

export function startLive(onSnapshot, onStatus) {
  let ws = null, retry = 0, timer = null, poll = null, ping = null, stopped = false;
  const pollOnce = async () => { try { onSnapshot(await api.get("/api/dashboard")); } catch { /* 401 handled by api */ } };
  const startPoll = () => { if (!poll) { pollOnce(); poll = setInterval(pollOnce, 5000); } };
  const stopPoll = () => { clearInterval(poll); poll = null; };
  function connect() {
    if (stopped) return;
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { retry = 0; stopPoll(); onStatus(true); clearInterval(ping); ping = setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 5000); }; // the server drops idle sockets after 10 s
    ws.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (m.type === "snapshot") onSnapshot(m.data); } catch { /* ignore */ } };
    ws.onclose = () => { clearInterval(ping); ws = null; onStatus(false); startPoll(); clearTimeout(timer); timer = setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++)); };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }
  connect();
  return () => { stopped = true; clearInterval(ping); clearTimeout(timer); stopPoll(); if (ws) ws.close(); };
}
