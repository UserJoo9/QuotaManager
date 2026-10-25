// Live direct transfer (device-to-device over the box's WebSocket relay, nothing stored on the box).
// Protocol is identical to the classic dashboard and the /share portal:
//   JSON control messages + binary frames = 16-byte transfer id + payload; receivers download via HTTP stream.
import { h } from "./dom.js";
import { t } from "./i18n.js";
import { toast } from "../components/toast.js";

export const MAX_DIRECT = 50 * 1024 * 1024;
const NAME_KEY = "qm_live_dev_name";
const L = { ws: null, selfId: "", peers: [], xfer: null, incoming: null, listeners: new Set(), ping: 0, retry: 0, started: false, wake: null, acceptTimer: 0, stage: null, ui: null };
const emit = () => L.listeners.forEach((f) => { try { f(); } catch { /* ignore */ } });
export const liveState = L;
export const onLive = (f) => { L.listeners.add(f); return () => L.listeners.delete(f); };
const bytes = (n) => { n = +n || 0; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; };
const send = (o) => { if (L.ws && L.ws.readyState === 1) { L.ws.send(JSON.stringify(o)); return true; } return false; };
const idBytes = (id) => { const b = new Uint8Array(16).fill(32); b.set(new TextEncoder().encode(id).subarray(0, 16)); return b; };

async function wake() { try { if ("wakeLock" in navigator && !L.wake) { L.wake = await navigator.wakeLock.request("screen"); L.wake.addEventListener("release", () => { L.wake = null; }); } } catch { /* ignore */ } }
function unwake() { try { L.wake && L.wake.release(); } catch { /* ignore */ } L.wake = null; }

// ---- CRC32 (slicing-by-8) for the streaming ZIP ----
const T = Array.from({ length: 8 }, () => new Uint32Array(256));
for (let i = 0; i < 256; i++) { let c = i; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; T[0][i] = c >>> 0; }
for (let i = 0; i < 256; i++) for (let k = 1; k < 8; k++) T[k][i] = (T[k - 1][i] >>> 8) ^ T[0][T[k - 1][i] & 255];
function crc(run, u8) {
  let c = run >>> 0, n = u8.length, p = 0;
  while (n >= 8) {
    const a = u8[p] ^ (c & 255), b = u8[p + 1] ^ ((c >>> 8) & 255), d = u8[p + 2] ^ ((c >>> 16) & 255), e = u8[p + 3] ^ (c >>> 24);
    c = T[7][a] ^ T[6][b] ^ T[5][d] ^ T[4][e] ^ T[3][u8[p + 4]] ^ T[2][u8[p + 5]] ^ T[1][u8[p + 6]] ^ T[0][u8[p + 7]]; p += 8; n -= 8;
  }
  while (n-- > 0) c = (c >>> 8) ^ T[0][(c ^ u8[p++]) & 255];
  return c >>> 0;
}

// ---- overlay (incoming request / progress) ----
function overlay() {
  if (L.ui) return L.ui;
  const title = h("div", { class: "name" }), sub = h("div", { class: "sub" }), file = h("div", { class: "lt-file" });
  const bar = h("i", {}), stats = h("span", {}), speed = h("span", {});
  const warn = h("p", { class: "banner info" }, t("lt.keepopen"));
  const accept = h("button", { type: "button", class: "btn primary" }, t("lt.accept")), decline = h("button", { type: "button", class: "btn" }, t("lt.decline"));
  const cancel = h("button", { type: "button", class: "btn" }, t("common.cancel")), done = h("button", { type: "button", class: "btn primary" }, t("lt.done"));
  const prog = h("div", {}, h("div", { class: "bar lt-bar" }, bar), h("div", { class: "lt-stats" }, stats, speed));
  const root = h("div", { hidden: true }, h("div", { class: "scrim" }),
    h("div", { class: "dialog", role: "dialog", "aria-modal": "true" }, h("header", {}, h("div", { style: { flex: "1" } }, title, sub)),
      h("div", { class: "dialog-body" }, file, prog, warn, h("div", { class: "dialog-actions" }, decline, accept, cancel, done))));
  document.body.append(root);
  accept.onclick = acceptIncoming; decline.onclick = declineIncoming; cancel.onclick = cancelXfer; done.onclick = () => finish();
  L.ui = { root, title, sub, file, bar, stats, speed, warn, accept, decline, cancel, done, prog };
  return L.ui;
}
function show(mode, o = {}) {
  const u = overlay(); u.root.hidden = false;
  u.title.textContent = o.title || ""; u.sub.textContent = o.sub || ""; u.file.textContent = o.file || "";
  u.prog.hidden = mode === "incoming"; u.warn.hidden = mode === "done";
  u.accept.hidden = u.decline.hidden = mode !== "incoming"; u.cancel.hidden = mode !== "run"; u.done.hidden = mode !== "done";
  if (mode !== "incoming") { u.bar.style.width = "0%"; u.bar.style.background = ""; u.stats.textContent = `0% (0 B / ${bytes(o.size)})`; u.speed.textContent = ""; }
}
let lastP = 0;
function progress(loaded, total, start, force) {
  const now = Date.now(); if (!force && now - lastP < 100) return; lastP = now;
  const u = overlay(), pct = total > 0 ? Math.min(100, Math.round(loaded / total * 100)) : 0, el = (now - start) / 1000;
  u.bar.style.width = pct + "%"; u.stats.textContent = `${pct}% (${bytes(loaded)} / ${bytes(total)})`; u.speed.textContent = el > .1 ? `${bytes(loaded / el)}/s` : "";
}
function finish(msg) {
  clearTimeout(L.acceptTimer); unwake();
  if (L.ui) L.ui.root.hidden = true;
  L.xfer = null; L.incoming = null; emit();
  if (msg) toast(msg);
}
const completeUi = (name, size) => { const u = overlay(); u.title.textContent = t("lt.complete"); u.bar.style.width = "100%"; u.bar.style.background = "var(--ok)"; u.stats.textContent = `100% (${bytes(size)})`; u.speed.textContent = ""; u.warn.hidden = true; u.cancel.hidden = true; u.done.hidden = false; };

// ---- connection ----
export function startLiveShare() {
  if (L.started) return; L.started = true; connect();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { if (L.xfer) wake(); if (!L.ws || L.ws.readyState > 1) connect(); } });
}
function connect() {
  if (L.ws && L.ws.readyState < 2) return;
  const name = (() => { try { return localStorage.getItem(NAME_KEY) || ""; } catch { return ""; } })();
  let ws;
  try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/share/live/ws${name ? "?name=" + encodeURIComponent(name) : ""}`); } catch { return; }
  ws.binaryType = "arraybuffer"; L.ws = ws;
  ws.onopen = () => { L.retry = 0; if (name) ws.send(JSON.stringify({ type: "join", name })); clearInterval(L.ping); L.ping = setInterval(() => send({ type: "ping" }), 3000); emit(); };
  ws.onmessage = (ev) => { if (typeof ev.data === "string") { try { onMsg(JSON.parse(ev.data)); } catch { /* ignore */ } } };
  ws.onclose = () => {
    clearInterval(L.ping); clearTimeout(L.acceptTimer);
    if (L.xfer && L.xfer.role === "sender" && !L.xfer.cancelled) finish(t("lt.interrupted"));
    L.peers = []; emit(); setTimeout(connect, Math.min(15000, 2000 * 1.5 ** L.retry++));
  };
}
function onMsg(m) {
  if (m.type === "peers") {
    L.selfId = m.self_id || L.selfId; const myIp = m.self_ip || "", seen = new Set(); L.peers = [];
    for (const p of (m.peers || []).filter((q) => q.id !== L.selfId && (!myIp || q.ip !== myIp))) if (!seen.has(p.ip)) { seen.add(p.ip); L.peers.push(p); }
    emit();
  } else if (m.type === "transfer-request") {
    if (L.xfer) { send({ type: "transfer-response", transfer_id: m.transfer_id, target_id: m.sender_id, target_ip: m.sender_ip, accepted: false }); return; }
    L.incoming = m; wake(); try { navigator.vibrate && navigator.vibrate([150, 80, 150]); } catch { /* ignore */ }
    show("incoming", { title: t("lt.incoming"), sub: `"${m.sender_name}" (${m.sender_ip})`, file: `${m.file_name} · ${bytes(m.file_size)}` });
  } else if (m.type === "transfer-response") {
    if (L.xfer && L.xfer.id === m.transfer_id && L.xfer.role === "sender") { clearTimeout(L.acceptTimer); m.accepted ? stream() : finish(t("lt.declined")); }
  } else if (m.type === "transfer-progress") {
    if (L.xfer && L.xfer.id === m.transfer_id && L.xfer.role === "receiver") progress(m.loaded, m.total, L.xfer.start);
  } else if (m.type === "transfer-error") { clearTimeout(L.acceptTimer); finish(m.message || t("lt.offline")); }
  else if (m.type === "transfer-done") {
    if (L.xfer && L.xfer.id === m.transfer_id && L.xfer.role === "receiver") { try { navigator.vibrate && navigator.vibrate([200, 100, 200]); } catch { /* ignore */ } completeUi(L.xfer.name, L.xfer.size); toast(t("lt.saved").replace("{n}", L.xfer.name)); }
  } else if (m.type === "transfer-cancel") { if (L.xfer && L.xfer.id === m.transfer_id) { clearTimeout(L.acceptTimer); finish(t("lt.cancelled.other")); } else if (L.incoming && L.incoming.transfer_id === m.transfer_id) finish(t("lt.cancelled.other")); }
}

// ---- receiving ----
function acceptIncoming() {
  const r = L.incoming; if (!r) return; L.incoming = null; wake();
  L.xfer = { id: r.transfer_id, role: "receiver", targetId: r.sender_id, targetIp: r.sender_ip, name: r.file_name, size: r.file_size, start: Date.now(), cancelled: false };
  show("run", { title: t("lt.receiving").replace("{n}", r.sender_name), file: r.file_name, size: r.file_size });
  try { const a = document.createElement("a"); a.href = `/api/share/live/download/${encodeURIComponent(r.transfer_id)}`; a.download = r.file_name; document.body.append(a); a.click(); a.remove(); } catch { /* ignore */ }
  send({ type: "transfer-response", transfer_id: r.transfer_id, target_id: r.sender_id, target_ip: r.sender_ip, accepted: true, http_stream: true }); emit();
}
function declineIncoming() {
  const r = L.incoming; if (!r) return;
  send({ type: "transfer-response", transfer_id: r.transfer_id, target_id: r.sender_id, target_ip: r.sender_ip, accepted: false }); finish(t("lt.declined"));
}
function cancelXfer() {
  const x = L.xfer; if (!x) return; x.cancelled = true;
  send({ type: "transfer-cancel", transfer_id: x.id, target_id: x.targetId, target_ip: x.targetIp }); finish(t("lt.cancelled"));
}

// ---- sending ----
export async function sendTo(peerId, fileList, fallbackIp = "") {
  const files = Array.from(fileList || []); if (!files.length || !peerId) return;
  if (L.xfer) { toast(t("lt.busy"), "bad"); return; }
  const total = files.reduce((a, f) => a + f.size, 0);
  if (total > MAX_DIRECT) { toast(t("lt.toobig"), "bad"); return; }
  wake();
  const rel = (f) => f.webkitRelativePath || (f._folder ? `${f._folder}/${f.name}` : "");
  const single = files.length === 1 && !rel(files[0]).includes("/");
  let name, type = "application/zip";
  if (single) { name = files[0].name; type = files[0].type || "application/octet-stream"; }
  else {
    const fo = [...new Set(files.filter((f) => rel(f).includes("/")).map((f) => rel(f).split("/")[0]))];
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    name = fo.length === 1 ? `${fo[0]}.zip` : fo.length === 2 ? `${fo[0]}_and_${fo[1]}.zip` : fo.length > 2 ? `${fo[0]}_${fo[1]}_and_${fo.length - 2}_more.zip` : `Files_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}.zip`;
  }
  let peer = L.peers.find((q) => q.id === peerId); if (!peer && fallbackIp) peer = L.peers.find((q) => q.ip === fallbackIp);
  const tid = "t_" + Math.random().toString(36).slice(2, 10);
  L.xfer = { id: tid, idBytes: idBytes(tid), role: "sender", targetId: peer ? peer.id : peerId, targetIp: peer ? peer.ip : fallbackIp, files, name, size: total, bundle: !single, start: Date.now(), cancelled: false };
  show("run", { title: t("lt.waiting"), file: name, size: total }); emit();
  L.acceptTimer = setTimeout(() => { if (L.xfer && L.xfer.id === tid) { send({ type: "transfer-cancel", transfer_id: tid, target_id: L.xfer.targetId, target_ip: L.xfer.targetIp }); finish(t("lt.timeout")); } }, 45000);
  if (!L.ws || L.ws.readyState !== 1) { connect(); for (let i = 0; i < 30 && !(L.ws && L.ws.readyState === 1); i++) await new Promise((r) => setTimeout(r, 100)); }
  if (!send({ type: "transfer-request", target_id: L.xfer.targetId, target_ip: L.xfer.targetIp, transfer_id: tid, file_name: name, file_size: total, file_type: type })) { clearTimeout(L.acceptTimer); finish(t("lt.notready")); }
}
async function stream() {
  const x = L.xfer; if (!x || !x.files.length) return;
  const CH = 512 * 1024, live = () => L.xfer && L.xfer.id === x.id && !x.cancelled;
  overlay().title.textContent = t("lt.sending");
  async function put(buf) {
    if (!live()) return false;
    if (L.ws && L.ws.bufferedAmount > 4 * 1048576) while (L.ws && L.ws.bufferedAmount > 1048576) await new Promise((r) => setTimeout(r, 4));
    if (!L.ws || L.ws.readyState !== 1) return false;
    const pk = new Uint8Array(16 + buf.byteLength); pk.set(x.idBytes, 0); pk.set(new Uint8Array(buf), 16); L.ws.send(pk.buffer); return true;
  }
  if (!x.bundle) {
    const f = x.files[0]; let off = 0;
    while (off < f.size && live()) { const b = await f.slice(off, off + CH).arrayBuffer(); if (!await put(b)) break; off += b.byteLength; progress(off, f.size, x.start); }
  } else {
    let pos = 0, sent = 0; const ents = [];
    for (const f of x.files) {
      if (!live()) break;
      const raw = f.webkitRelativePath || (f._folder ? `${f._folder}/${f.name}` : f.name), nb = new TextEncoder().encode(raw.replace(/\\/g, "/").replace(/^\/+/, ""));
      const lh = new Uint8Array(30 + nb.length), dv = new DataView(lh.buffer);
      dv.setUint32(0, 0x04034b50, true); dv.setUint16(4, 20, true); dv.setUint16(6, 0x0808, true); dv.setUint16(26, nb.length, true); lh.set(nb, 30);
      const at = pos; if (!await put(lh.buffer)) break; pos += lh.byteLength;
      let off = 0, c = 0xFFFFFFFF;
      while (off < f.size && live()) { const b = await f.slice(off, off + CH).arrayBuffer(); c = crc(c, new Uint8Array(b)); if (!await put(b)) break; off += b.byteLength; pos += b.byteLength; sent += b.byteLength; progress(sent, x.size, x.start); }
      const fc = (c ^ 0xFFFFFFFF) >>> 0, ds = new Uint8Array(16), dd = new DataView(ds.buffer);
      dd.setUint32(0, 0x08074b50, true); dd.setUint32(4, fc, true); dd.setUint32(8, f.size, true); dd.setUint32(12, f.size, true);
      if (!await put(ds.buffer)) break; pos += 16; ents.push({ nb, crc: fc, size: f.size, off: at });
    }
    if (live()) {
      const cdOff = pos; let cdSize = 0;
      for (const e of ents) {
        const cd = new Uint8Array(46 + e.nb.length), v = new DataView(cd.buffer);
        v.setUint32(0, 0x02014b50, true); v.setUint16(4, 20, true); v.setUint16(6, 20, true); v.setUint16(8, 0x0808, true); v.setUint32(16, e.crc, true); v.setUint32(20, e.size, true); v.setUint32(24, e.size, true); v.setUint16(28, e.nb.length, true); v.setUint32(42, e.off, true); cd.set(e.nb, 46);
        if (!await put(cd.buffer)) break; pos += cd.byteLength; cdSize += cd.byteLength;
      }
      const eo = new Uint8Array(22), ev = new DataView(eo.buffer);
      ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, ents.length, true); ev.setUint16(10, ents.length, true); ev.setUint32(12, cdSize, true); ev.setUint32(16, cdOff, true);
      await put(eo.buffer);
    }
  }
  if (live()) {
    send({ type: "transfer-done", transfer_id: x.id, target_id: x.targetId, target_ip: x.targetIp });
    progress(x.size, x.size, x.start, true); completeUi(x.name, x.size);
    await new Promise((r) => setTimeout(r, 600)); finish(t("lt.sent").replace("{n}", x.name));
  }
}

// ---- folder-aware drag & drop ----
export async function filesFromDrop(dt) {
  const out = []; let dirs = 0;
  const readAll = (r) => new Promise((res) => { const all = []; (function more() { r.readEntries((b) => { if (!b.length) res(all); else { all.push(...b); more(); } }, () => res(all)); })(); });
  async function walk(en, top) {
    if (en.isFile) { const f = await new Promise((res) => en.file(res, () => res(null))); if (f) { if (top) f._folder = top; Object.defineProperty(f, "webkitRelativePath", { value: en.fullPath.replace(/^\//, ""), configurable: true }); out.push(f); } }
    else if (en.isDirectory) for (const c of await readAll(en.createReader())) await walk(c, top || en.name);
  }
  const items = Array.from(dt.items || []).map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (items.length) { for (const en of items) { if (en.isDirectory) dirs++; await walk(en, en.isDirectory ? en.name : ""); } }
  else out.push(...Array.from(dt.files || []));
  return { files: out, dirs };
}
