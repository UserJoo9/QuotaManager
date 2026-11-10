/*
 * Device-to-device transfer (relay over the box's WebSocket; nothing is stored on the box).
 * Wire protocol unchanged from the previous dashboard and the public /share portal:
 *   JSON control messages + binary frames = 16-byte transfer id + payload;
 *   receivers download through an HTTP stream (/api/share/live/download/<id>).
 * Multi-file / folder sends are a streaming ZIP (stored, data descriptors, CRC-32).
 * The UI (overlay, toasts) subscribes to this store; no DOM is touched here.
 */
import { useSyncExternalStore } from "react";

export const MAX_DIRECT = 50 * 1024 * 1024;
const NAME_KEY = "qm_live_dev_name";
export interface Peer { id: string; ip: string; name: string }
type RelFile = File & { _folder?: string };
interface Incoming { transfer_id: string; sender_id: string; sender_ip: string; sender_name: string; file_name: string; file_size: number }
export interface Xfer { id: string; role: "sender" | "receiver"; targetId: string; targetIp: string; name: string; size: number; start: number; loaded: number; phase: "waiting" | "run" | "done"; cancelled: boolean; files?: RelFile[]; bundle?: boolean; idBytes?: Uint8Array; peerName?: string }
export interface LiveState { connected: boolean; peers: Peer[]; xfer: Xfer | null; incoming: Incoming | null }
export type Notice = { kind: "success" | "error" | "info"; key: string; vars?: Record<string, string> };

let state: LiveState = { connected: false, peers: [], xfer: null, incoming: null };
let ws: WebSocket | null = null, selfId = "", retry = 0, ping: ReturnType<typeof setInterval> | undefined, acceptTimer: ReturnType<typeof setTimeout> | undefined, started = false;
let wakeLock: { release: () => Promise<void> } | null = null;
const subs = new Set<() => void>();
let notify: (n: Notice) => void = () => {};
export const setNotifier = (f: (n: Notice) => void) => { notify = f; };
const set = (patch: Partial<LiveState>) => { state = { ...state, ...patch }; subs.forEach((f) => f()); };
const setX = (patch: Partial<Xfer>) => { if (state.xfer) set({ xfer: { ...state.xfer, ...patch } }); };
export const getLive = () => state;
export const useLive = () => useSyncExternalStore((cb) => { subs.add(cb); return () => subs.delete(cb); }, () => state, () => state);

const send = (o: object) => { if (ws && ws.readyState === 1) { ws.send(JSON.stringify(o)); return true; } return false; };
const idBytes = (id: string) => { const b = new Uint8Array(16).fill(32); b.set(new TextEncoder().encode(id).subarray(0, 16)); return b; };
async function wake() { try { const n = navigator as Navigator & { wakeLock?: { request: (t: string) => Promise<{ release: () => Promise<void>; addEventListener: (e: string, f: () => void) => void }> } }; if (n.wakeLock && !wakeLock) { const l = await n.wakeLock.request("screen"); wakeLock = l; l.addEventListener("release", () => { wakeLock = null; }); } } catch { /* ignore */ } }
function unwake() { try { wakeLock?.release(); } catch { /* ignore */ } wakeLock = null; }
const vibrate = (p: number[]) => { try { navigator.vibrate?.(p); } catch { /* ignore */ } };

/* CRC-32 (slicing-by-8) for the streaming ZIP */
const T = Array.from({ length: 8 }, () => new Uint32Array(256));
for (let i = 0; i < 256; i++) { let c = i; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; T[0][i] = c >>> 0; }
for (let i = 0; i < 256; i++) for (let k = 1; k < 8; k++) T[k][i] = (T[k - 1][i] >>> 8) ^ T[0][T[k - 1][i] & 255];
export function crc32(run: number, u8: Uint8Array) {
  let c = run >>> 0, n = u8.length, p = 0;
  while (n >= 8) {
    const a = u8[p] ^ (c & 255), b = u8[p + 1] ^ ((c >>> 8) & 255), d = u8[p + 2] ^ ((c >>> 16) & 255), e = u8[p + 3] ^ (c >>> 24);
    c = T[7][a] ^ T[6][b] ^ T[5][d] ^ T[4][e] ^ T[3][u8[p + 4]] ^ T[2][u8[p + 5]] ^ T[1][u8[p + 6]] ^ T[0][u8[p + 7]]; p += 8; n -= 8;
  }
  while (n-- > 0) c = (c >>> 8) ^ T[0][(c ^ u8[p++]) & 255];
  return c >>> 0;
}

function finish(n?: Notice) { clearTimeout(acceptTimer); unwake(); set({ xfer: null, incoming: null }); if (n) notify(n); }

/* ---------------------------------------------------------------- connection */
export function startLiveShare() {
  if (started) return; started = true; connect();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { if (state.xfer) wake(); if (!ws || ws.readyState > 1) connect(); } });
}
function connect() {
  if (ws && ws.readyState < 2) return;
  let name = ""; try { name = localStorage.getItem(NAME_KEY) || ""; } catch { /* private mode */ }
  let s: WebSocket;
  try { s = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/share/live/ws${name ? "?name=" + encodeURIComponent(name) : ""}`); } catch { return; }
  s.binaryType = "arraybuffer"; ws = s;
  s.onopen = () => { retry = 0; if (name) s.send(JSON.stringify({ type: "join", name })); clearInterval(ping); ping = setInterval(() => send({ type: "ping" }), 3000); set({ connected: true }); };
  s.onmessage = (ev) => { if (typeof ev.data === "string") { try { onMsg(JSON.parse(ev.data)); } catch { /* ignore */ } } };
  s.onclose = () => {
    clearInterval(ping); clearTimeout(acceptTimer);
    if (state.xfer && state.xfer.role === "sender" && !state.xfer.cancelled) finish({ kind: "error", key: "lt.interrupted" });
    set({ peers: [], connected: false }); setTimeout(connect, Math.min(15000, 2000 * 1.5 ** retry++));
  };
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function onMsg(m: any) {
  if (m.type === "peers") {
    selfId = m.self_id || selfId; const myIp = m.self_ip || "", seen = new Set<string>(), peers: Peer[] = [];
    for (const p of (m.peers || []).filter((q: Peer) => q.id !== selfId && (!myIp || q.ip !== myIp))) if (!seen.has(p.ip)) { seen.add(p.ip); peers.push(p); }
    set({ peers });
  } else if (m.type === "transfer-request") {
    if (state.xfer) { send({ type: "transfer-response", transfer_id: m.transfer_id, target_id: m.sender_id, target_ip: m.sender_ip, accepted: false }); return; }
    wake(); vibrate([150, 80, 150]); set({ incoming: m });
  } else if (m.type === "transfer-response") {
    const x = state.xfer;
    if (x && x.id === m.transfer_id && x.role === "sender") { clearTimeout(acceptTimer); if (m.accepted) stream(); else finish({ kind: "info", key: "lt.declined" }); }
  } else if (m.type === "transfer-progress") {
    const x = state.xfer; if (x && x.id === m.transfer_id && x.role === "receiver") setX({ loaded: m.loaded, size: m.total || x.size });
  } else if (m.type === "transfer-error") { clearTimeout(acceptTimer); finish({ kind: "error", key: m.message ? "" : "lt.offline", vars: m.message ? { text: m.message } : undefined }); }
  else if (m.type === "transfer-done") {
    const x = state.xfer;
    if (x && x.id === m.transfer_id && x.role === "receiver") { vibrate([200, 100, 200]); setX({ loaded: x.size, phase: "done" }); notify({ kind: "success", key: "lt.saved", vars: { n: x.name } }); }
  } else if (m.type === "transfer-cancel") {
    if ((state.xfer && state.xfer.id === m.transfer_id) || (state.incoming && state.incoming.transfer_id === m.transfer_id)) finish({ kind: "info", key: "lt.cancelled.other" });
  }
}

/* ---------------------------------------------------------------- receiving */
export function acceptIncoming() {
  const r = state.incoming; if (!r) return; wake();
  set({ incoming: null, xfer: { id: r.transfer_id, role: "receiver", targetId: r.sender_id, targetIp: r.sender_ip, name: r.file_name, size: r.file_size, start: Date.now(), loaded: 0, phase: "run", cancelled: false, peerName: r.sender_name } });
  try { const a = document.createElement("a"); a.href = `/api/share/live/download/${encodeURIComponent(r.transfer_id)}`; a.download = r.file_name; document.body.append(a); a.click(); a.remove(); } catch { /* ignore */ }
  send({ type: "transfer-response", transfer_id: r.transfer_id, target_id: r.sender_id, target_ip: r.sender_ip, accepted: true, http_stream: true });
}
export function declineIncoming() {
  const r = state.incoming; if (!r) return;
  send({ type: "transfer-response", transfer_id: r.transfer_id, target_id: r.sender_id, target_ip: r.sender_ip, accepted: false }); finish({ kind: "info", key: "lt.declined" });
}
export function cancelXfer() {
  const x = state.xfer; if (!x) return; x.cancelled = true;
  send({ type: "transfer-cancel", transfer_id: x.id, target_id: x.targetId, target_ip: x.targetIp }); finish({ kind: "info", key: "lt.cancelled" });
}
export const dismissDone = () => finish();

/* ---------------------------------------------------------------- sending */
const rel = (f: RelFile) => f.webkitRelativePath || (f._folder ? `${f._folder}/${f.name}` : "");
export async function sendTo(peerId: string, fileList: File[] | FileList, fallbackIp = "") {
  const files = Array.from(fileList || []) as RelFile[]; if (!files.length || !peerId) return;
  if (state.xfer) { notify({ kind: "error", key: "lt.busy" }); return; }
  const total = files.reduce((a, f) => a + f.size, 0);
  if (total > MAX_DIRECT) { notify({ kind: "error", key: "lt.toobig" }); return; }
  wake();
  const single = files.length === 1 && !rel(files[0]).includes("/");
  let name: string, type = "application/zip";
  if (single) { name = files[0].name; type = files[0].type || "application/octet-stream"; }
  else {
    const fo = [...new Set(files.filter((f) => rel(f).includes("/")).map((f) => rel(f).split("/")[0]))];
    const d = new Date(), p = (n: number) => String(n).padStart(2, "0");
    name = fo.length === 1 ? `${fo[0]}.zip` : fo.length === 2 ? `${fo[0]}_and_${fo[1]}.zip` : fo.length > 2 ? `${fo[0]}_${fo[1]}_and_${fo.length - 2}_more.zip` : `Files_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}.zip`;
  }
  let peer = state.peers.find((q) => q.id === peerId); if (!peer && fallbackIp) peer = state.peers.find((q) => q.ip === fallbackIp);
  const tid = "t_" + Math.random().toString(36).slice(2, 10);
  set({ xfer: { id: tid, idBytes: idBytes(tid), role: "sender", targetId: peer ? peer.id : peerId, targetIp: peer ? peer.ip : fallbackIp, files, name, size: total, bundle: !single, start: Date.now(), loaded: 0, phase: "waiting", cancelled: false, peerName: peer?.name } });
  acceptTimer = setTimeout(() => { const x = state.xfer; if (x && x.id === tid) { send({ type: "transfer-cancel", transfer_id: tid, target_id: x.targetId, target_ip: x.targetIp }); finish({ kind: "error", key: "lt.timeout" }); } }, 45000);
  if (!ws || ws.readyState !== 1) { connect(); for (let i = 0; i < 30 && !(ws && ws.readyState === 1); i++) await new Promise((r) => setTimeout(r, 100)); }
  const x = state.xfer!;
  if (!send({ type: "transfer-request", target_id: x.targetId, target_ip: x.targetIp, transfer_id: tid, file_name: name, file_size: total, file_type: type })) { clearTimeout(acceptTimer); finish({ kind: "error", key: "lt.notready" }); }
}
let lastP = 0;
const progress = (loaded: number, force = false) => { const now = Date.now(); if (!force && now - lastP < 100) return; lastP = now; setX({ loaded }); };
async function stream() {
  const x = state.xfer; if (!x || !x.files?.length || !x.idBytes) return;
  setX({ phase: "run", start: Date.now() });
  const CH = 512 * 1024, live = () => !!state.xfer && state.xfer.id === x.id && !state.xfer.cancelled && !x.cancelled;
  const put = async (buf: ArrayBuffer) => {
    if (!live()) return false;
    if (ws && ws.bufferedAmount > 4 * 1048576) while (ws && ws.bufferedAmount > 1048576) await new Promise((r) => setTimeout(r, 4));
    if (!ws || ws.readyState !== 1) return false;
    const pk = new Uint8Array(16 + buf.byteLength); pk.set(x.idBytes!, 0); pk.set(new Uint8Array(buf), 16); ws.send(pk.buffer); return true;
  };
  if (!x.bundle) {
    const f = x.files[0]; let off = 0;
    while (off < f.size && live()) { const b = await f.slice(off, off + CH).arrayBuffer(); if (!(await put(b))) break; off += b.byteLength; progress(off); }
  } else {
    let pos = 0, sent = 0; const ents: { nb: Uint8Array; crc: number; size: number; off: number }[] = [];
    for (const f of x.files) {
      if (!live()) break;
      const raw = f.webkitRelativePath || (f._folder ? `${f._folder}/${f.name}` : f.name), nb = new TextEncoder().encode(raw.replace(/\\/g, "/").replace(/^\/+/, ""));
      const lh = new Uint8Array(30 + nb.length), dv = new DataView(lh.buffer);
      dv.setUint32(0, 0x04034b50, true); dv.setUint16(4, 20, true); dv.setUint16(6, 0x0808, true); dv.setUint16(26, nb.length, true); lh.set(nb, 30);
      const at = pos; if (!(await put(lh.buffer))) break; pos += lh.byteLength;
      let off = 0, c = 0xFFFFFFFF;
      while (off < f.size && live()) { const b = await f.slice(off, off + CH).arrayBuffer(); c = crc32(c, new Uint8Array(b)); if (!(await put(b))) break; off += b.byteLength; pos += b.byteLength; sent += b.byteLength; progress(sent); }
      const fc = (c ^ 0xFFFFFFFF) >>> 0, ds = new Uint8Array(16), dd = new DataView(ds.buffer);
      dd.setUint32(0, 0x08074b50, true); dd.setUint32(4, fc, true); dd.setUint32(8, f.size, true); dd.setUint32(12, f.size, true);
      if (!(await put(ds.buffer))) break; pos += 16; ents.push({ nb, crc: fc, size: f.size, off: at });
    }
    if (live()) {
      const cdOff = pos; let cdSize = 0;
      for (const e of ents) {
        const cd = new Uint8Array(46 + e.nb.length), v = new DataView(cd.buffer);
        v.setUint32(0, 0x02014b50, true); v.setUint16(4, 20, true); v.setUint16(6, 20, true); v.setUint16(8, 0x0808, true); v.setUint32(16, e.crc, true); v.setUint32(20, e.size, true); v.setUint32(24, e.size, true); v.setUint16(28, e.nb.length, true); v.setUint32(42, e.off, true); cd.set(e.nb, 46);
        if (!(await put(cd.buffer))) break; pos += cd.byteLength; cdSize += cd.byteLength;
      }
      const eo = new Uint8Array(22), ev = new DataView(eo.buffer);
      ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, ents.length, true); ev.setUint16(10, ents.length, true); ev.setUint32(12, cdSize, true); ev.setUint32(16, cdOff, true);
      await put(eo.buffer);
    }
  }
  if (live()) {
    send({ type: "transfer-done", transfer_id: x.id, target_id: x.targetId, target_ip: x.targetIp });
    progress(x.size, true); setX({ phase: "done" });
    await new Promise((r) => setTimeout(r, 600)); finish({ kind: "success", key: "lt.sent", vars: { n: x.name } });
  }
}

/* ---------------------------------------------------------------- folder-aware drag & drop */
export async function filesFromDrop(dt: DataTransfer) {
  const out: RelFile[] = []; let dirs = 0;
  const readAll = (r: FileSystemDirectoryReader) => new Promise<FileSystemEntry[]>((res) => { const all: FileSystemEntry[] = []; (function more() { r.readEntries((b) => { if (!b.length) res(all); else { all.push(...b); more(); } }, () => res(all)); })(); });
  async function walk(en: FileSystemEntry, top: string): Promise<void> {
    if (en.isFile) { const f = await new Promise<RelFile | null>((res) => (en as FileSystemFileEntry).file((x) => res(x as RelFile), () => res(null))); if (f) { if (top) f._folder = top; Object.defineProperty(f, "webkitRelativePath", { value: en.fullPath.replace(/^\//, ""), configurable: true }); out.push(f); } }
    else if (en.isDirectory) for (const c of await readAll((en as FileSystemDirectoryEntry).createReader())) await walk(c, top || en.name);
  }
  const items = Array.from(dt.items || []).map((i) => i.webkitGetAsEntry?.()).filter(Boolean) as FileSystemEntry[];
  if (items.length) { for (const en of items) { if (en.isDirectory) dirs++; await walk(en, en.isDirectory ? en.name : ""); } }
  else out.push(...(Array.from(dt.files || []) as RelFile[]));
  return { files: out as File[], dirs };
}
