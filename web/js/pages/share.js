import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { api } from "../core/api.js";
import { chip, fmtBytes } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import liveCard from "./shareLive.js";

const sh = { data: null, loading: false, at: 0, failAt: 0, sel: new Set(), up: null, ttl: 7200, text: "", armed: false, armedFiles: false, drag: false, timer: null, err: "" };
const mine = () => location.hash === "#/share";
const TTLS = [[1800, "30m"], [7200, "2h"], [21600, "6h"], [86400, "24h"], [604800, "7d"]];

async function load(ctx, force) {
  if (sh.loading || (!force && sh.data && Date.now() - sh.at < 8000)) return;
  sh.loading = true;
  try { sh.data = await api.get("/api/share/list"); sh.at = Date.now(); sh.err = ""; const ids = new Set(sh.data.files.map((f) => f.id)); for (const id of [...sh.sel]) if (!ids.has(id)) sh.sel.delete(id); }
  catch (e) { sh.failAt = Date.now(); sh.err = e.message; } finally { sh.loading = false; ctx.touch(); }
}
function poll(ctx) { if (sh.timer) return; sh.timer = setInterval(() => { if (!mine()) { clearInterval(sh.timer); sh.timer = null; return; } if (!sh.up) load(ctx, true); }, 10000); }

const HDR = { "X-QM-CSRF": "1" };
function xhrSend(url, file, headers, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest(); x.open("POST", url);
    for (const [k, v] of Object.entries({ ...HDR, ...headers })) x.setRequestHeader(k, v);
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    x.onload = () => (x.status >= 200 && x.status < 300 ? resolve() : reject(new Error(x.statusText || `HTTP ${x.status}`)));
    x.onerror = () => reject(new Error(t("sh.neterr")));
    x.send(file);
  });
}
async function uploadFiles(ctx, files) {
  const list = [...files]; if (!list.length || sh.up) return;
  const t0 = Date.now();
  try {
    for (let i = 0; i < list.length; i++) {
      const f = list[i];
      sh.up = { name: `${f.name} (${i + 1}/${list.length})`, pct: 0, stat: "" }; ctx.touch();
      await xhrSend(`/api/share/upload?ttl=${sh.ttl}`, f, { "X-Filename": encodeURIComponent(f.name), "Content-Type": f.type || "application/octet-stream" }, (l, tot) => {
        sh.up.pct = Math.round((l / tot) * 100); sh.up.stat = `${fmtBytes(l)} / ${fmtBytes(tot)}`; updateProgress();
      });
    }
    toast(t("sh.uploaded").replace("{n}", list.length));
  } catch (e) { toast(`${t("sh.upfail")}: ${e.message}`, "bad"); } finally { sh.up = null; await load(ctx, true); }
}
async function uploadFolder(ctx, files) {
  const list = [...files]; if (!list.length || sh.up) return;
  const tops = [...new Set(list.map((f) => (f.webkitRelativePath || f.relPath || "").split("/")[0]).filter(Boolean))];
  const name = tops.length === 1 ? tops[0] : tops.length === 2 ? `${tops[0]}_and_${tops[1]}` : tops.length > 2 ? `${tops[0]}_${tops[1]}_and_${tops.length - 2}_more` : "Folder";
  try {
    sh.up = { name: `${name}.zip`, pct: 3, stat: t("sh.init") }; ctx.touch();
    const { folder_id } = await api.post("/api/share/folder/init", { folder_name: name, ttl: sh.ttl });
    for (let i = 0; i < list.length; i++) {
      const f = list[i], rel = f.webkitRelativePath || f.relPath || f.name;
      sh.up.name = `${name}.zip · ${i + 1}/${list.length}`;
      await xhrSend(`/api/share/folder/file?folder_id=${encodeURIComponent(folder_id)}&path=${encodeURIComponent(rel)}`, f, { "Content-Type": f.type || "application/octet-stream" }, (l, tot) => { sh.up.pct = Math.round(((i + l / tot) / list.length) * 100); sh.up.stat = f.name; updateProgress(); });
    }
    await api.post("/api/share/folder/finalize", { folder_id });
    toast(t("sh.uploaded").replace("{n}", 1));
  } catch (e) { toast(`${t("sh.upfail")}: ${e.message}`, "bad"); } finally { sh.up = null; await load(ctx, true); }
}
function updateProgress() {
  const b = document.getElementById("sh-prog-bar"), s = document.getElementById("sh-prog-stat"), n = document.getElementById("sh-prog-name");
  if (!sh.up) return; if (b) b.style.width = `${sh.up.pct}%`; if (s) s.textContent = `${sh.up.pct}% ${sh.up.stat}`; if (n) n.textContent = sh.up.name;
}
async function readEntries(items) {
  const out = []; let dirs = 0;
  const walk = async (entry, path) => {
    if (entry.isFile) { const f = await new Promise((res, rej) => entry.file(res, rej)); f.relPath = path + f.name; out.push(f); }
    else if (entry.isDirectory) {
      const r = entry.createReader(); let batch;
      do { batch = await new Promise((res, rej) => r.readEntries(res, rej)); for (const e of batch) await walk(e, path + entry.name + "/"); } while (batch.length);
    }
  };
  for (const it of items) { const e = it.webkitGetAsEntry && it.webkitGetAsEntry(); if (!e) continue; if (e.isDirectory) dirs++; await walk(e, ""); }
  return { files: out, dirs };
}

function qrDialog() {
  const url = `${location.origin}/share`;
  const box = h("div", { style: { background: "#fff", padding: "12px", "border-radius": "12px", width: "224px", height: "224px", margin: "4px auto", display: "grid", "place-items": "center" } });
  const loadQr = window.QRCode ? Promise.resolve() : new Promise((res, rej) => { const s = document.createElement("script"); s.src = "/assets/qrcode.min.js"; s.onload = res; s.onerror = rej; document.head.append(s); });
  loadQr.then(() => new window.QRCode(box, { text: url, width: 200, height: 200, correctLevel: window.QRCode.CorrectLevel.M })).catch(() => {});
  openDialog({ title: t("sh.qr"), sub: t("sh.qr.sub"), submitLabel: t("common.close"), content: [box, h("div", { class: "field" }, h("input", { class: "input ltr", readonly: true, value: url, onfocus: (e) => e.target.select() }))], onSubmit: async () => true });
}

export default function share(ctx) {
  if (Date.now() - sh.failAt > 10000) load(ctx);
  poll(ctx);
  const d = sh.data;
  const fail = (e) => toast(`${t("dr.failed")}: ${e.message}`, "bad");
  const fileIn = h("input", { type: "file", multiple: true, hidden: true, onchange: (e) => { uploadFiles(ctx, e.target.files); e.target.value = ""; } });
  const dirIn = h("input", { type: "file", multiple: true, hidden: true, webkitdirectory: true, onchange: (e) => { uploadFolder(ctx, e.target.files); e.target.value = ""; } });
  const clearAll = async (ev) => {
    if (!sh.armed) { sh.armed = true; ev.currentTarget.classList.add("armed"); ev.currentTarget.textContent = t("dr.confirm"); setTimeout(() => { sh.armed = false; }, 4000); return; }
    sh.armed = false; try { await api.post("/api/share/clear"); toast(t("dr.done")); await load(ctx, true); } catch (e) { fail(e); }
  };
  const files = d?.files || [], texts = d?.texts || [];
  const delAllFiles = async (ev) => {
    const b = ev.currentTarget;
    if (!sh.armedFiles) { sh.armedFiles = true; b.classList.add("armed"); b.textContent = t("dr.confirm"); setTimeout(() => { sh.armedFiles = false; ctx.touch(); }, 4000); return; }
    sh.armedFiles = false;
    try { await api.post("/api/share/delete-batch", { ids: files.map((f) => f.id) }); sh.sel.clear(); toast(t("dr.done")); await load(ctx, true); } catch (e) { fail(e); }
  };
  const del = async (id) => { try { await api.del(`/api/share/files/${id}`); sh.sel.delete(id); await load(ctx, true); } catch (e) { fail(e); } };
  const delText = async (id) => { try { await api.del(`/api/share/texts/${id}`); await load(ctx, true); } catch (e) { fail(e); } };
  const addText = async () => { const v = sh.text.trim(); if (!v) return; try { await api.post("/api/share/text", { text: v }); sh.text = ""; const ti = document.getElementById("sh-text-in"); if (ti) ti.value = ""; await load(ctx, true); ctx.rerender(); const t2 = document.getElementById("sh-text-in"); if (t2) t2.focus(); } catch (e) { fail(e); } };
  const copy = async (s) => { try { await navigator.clipboard.writeText(s); toast(t("sh.copied")); } catch { toast(t("sh.copyfail"), "bad"); } };
  const left = (f) => { const m = Math.max(0, Math.round((f.expires_at * 1000 - Date.now()) / 60000)); return m >= 1440 ? `${num(Math.round(m / 1440), 0)}d` : m >= 60 ? `${num(Math.round(m / 60), 0)}h` : `${num(m, 0)}m`; };
  const batch = sh.sel.size ? h("div", { class: "ins-hero", style: { "flex-direction": "row", "align-items": "center", "flex-wrap": "wrap", "margin-bottom": "12px" } },
    h("b", {}, `${num(sh.sel.size, 0)} ${t("sh.selected")}`),
    h("a", { class: "btn primary sm", href: `/api/share/download-zip?ids=${encodeURIComponent([...sh.sel].join(","))}`, download: "" }, t("sh.zip")),
    h("button", { type: "button", class: "btn danger sm", onclick: async () => { try { await api.post("/api/share/delete-batch", { ids: [...sh.sel] }); sh.sel.clear(); await load(ctx, true); } catch (e) { fail(e); } } }, t("sh.delsel")),
    h("button", { type: "button", class: "btn ghost sm", onclick: () => { sh.sel.clear(); ctx.touch(); } }, t("common.cancel"))) : null;

  const zone = h("div", { class: "dropz" + (sh.drag ? " on" : ""),
    ondragover: (e) => { e.preventDefault(); if (!sh.drag) { sh.drag = true; e.currentTarget.classList.add("on"); } },
    ondragleave: (e) => { e.preventDefault(); sh.drag = false; e.currentTarget.classList.remove("on"); },
    ondrop: async (e) => { e.preventDefault(); sh.drag = false; e.currentTarget.classList.remove("on"); const items = [...(e.dataTransfer.items || [])], plain = [...(e.dataTransfer.files || [])];
      if (items.length && items[0].webkitGetAsEntry) { const { files: fl, dirs } = await readEntries(items); if (dirs) uploadFolder(ctx, fl); else uploadFiles(ctx, fl.length ? fl : plain); } else uploadFiles(ctx, plain); } },
    h("span", { class: "d-ic" }, icon("share", 20)), h("b", {}, t("sh.drop")), h("span", { class: "sub" }, t("sh.drop.sub")),
    h("div", { class: "fa", style: { "justify-content": "center" } },
      h("button", { type: "button", class: "btn primary sm", disabled: !!sh.up, onclick: () => fileIn.click() }, icon("plus", 15), t("sh.files")),
      h("button", { type: "button", class: "btn ghost sm", disabled: !!sh.up, onclick: () => dirIn.click() }, icon("box", 15), t("sh.folder")),
      h("select", { class: "select", style: { width: "auto", height: "36px" }, "aria-label": t("sh.ttl"), onchange: (e) => { sh.ttl = +e.target.value; } }, TTLS.map(([v, l]) => h("option", { value: String(v), selected: v === sh.ttl }, `${t("sh.keep")} ${l}`)))),
    sh.up ? h("div", { style: { width: "100%", "max-width": "520px", "margin-top": "12px", "text-align": "start" } }, h("div", { class: "sub", id: "sh-prog-name" }, sh.up.name), h("div", { class: "bar" }, h("i", { id: "sh-prog-bar", style: { width: `${sh.up.pct}%` } })), h("div", { class: "sub", id: "sh-prog-stat" }, `${sh.up.pct}% ${sh.up.stat}`)) : null,
    fileIn, dirIn);

  return h("div", { class: "page" }, h("div", { class: "grid" },
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("sh.title")), h("span", { class: "sub" }, d ? `${num(d.total_files, 0)} ${t("sh.files.n")} · ${fmtBytes(d.total_size)}` : t("sh.title.d"))),
        h("div", { style: { display: "flex", gap: "8px", "flex-wrap": "wrap" } },
          h("button", { type: "button", class: "btn ghost sm", onclick: qrDialog }, t("sh.qr")),
          h("a", { class: "btn ghost sm", href: "/share", target: "_blank", rel: "noopener" }, t("sh.portal")),
          h("button", { type: "button", class: "btn danger sm", disabled: !files.length && !texts.length, onclick: clearAll }, t("sh.clear")))),
      zone),
    liveCard(ctx),
    h("section", { class: "card c7" },
      h("header", { class: "card-h" }, h("h3", {}, t("sh.shared")), files.length ? h("div", { style: { display: "flex", gap: "8px", "flex-wrap": "wrap" } },
        h("a", { class: "btn ghost sm", href: `/api/share/download-zip?ids=${encodeURIComponent(files.filter((f) => !f.is_expired).map((f) => f.id).join(","))}`, download: "" }, t("sh.zipall")),
        h("button", { type: "button", class: "btn danger sm", onclick: delAllFiles }, t("sh.delall"))) : null),
      batch,
      !d ? (sh.err ? h("div", { class: "empty" }, sh.err) : h("div", { class: "skeleton", style: { height: "180px" } })) : files.length ? h("div", {}, files.map((f) => h("div", { class: "crow", style: { "grid-template-columns": "28px minmax(0,1fr) auto auto" } },
        h("input", { type: "checkbox", checked: sh.sel.has(f.id), "aria-label": f.name, onchange: (e) => { if (e.target.checked) sh.sel.add(f.id); else sh.sel.delete(f.id); ctx.touch(); } }),
        h("div", { class: "c-name" }, f.name, h("span", { class: "mono" }, `${fmtBytes(f.size)} · ${iso(f.sender_ip || "")} · ↓${num(f.download_count, 0)} · ${t("sh.left")} ${left(f)}`)),
        f.is_expired ? chip("warn", t("sh.expired")) : h("a", { class: "btn primary sm", href: `/api/share/download/${f.id}`, download: f.name }, t("sh.download")),
        h("button", { type: "button", class: "btn danger sm", "aria-label": t("q.pack.del"), onclick: () => del(f.id) }, icon("x", 15))))) : h("div", { class: "empty" }, t("sh.none"))),
    h("section", { class: "card c5" },
      h("header", { class: "card-h" }, h("h3", {}, t("sh.text"))),
      h("form", { style: { display: "flex", gap: "10px", "margin-bottom": "12px" }, onsubmit: (e) => { e.preventDefault(); addText(); } }, h("div", { class: "grow" }, h("input", { class: "input", id: "sh-text-in", value: sh.text, placeholder: t("sh.text.ph"), autocomplete: "off", oninput: (e) => { sh.text = e.target.value; } })), h("button", { type: "submit", class: "btn primary sm", style: { height: "40px" } }, t("sh.text.go"))),
      texts.length ? h("div", { class: "live-body", style: { height: "320px" } }, texts.map((x) => h("div", { class: "crow", style: { "grid-template-columns": "1fr auto auto" } },
        h("div", { class: "c-name", style: { "white-space": "normal", "word-break": "break-word" } }, /^https?:\/\//i.test(x.text) ? h("a", { href: x.text, target: "_blank", rel: "noopener noreferrer" }, x.text) : x.text, h("span", { class: "mono" }, iso(x.sender_ip || ""))),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => copy(x.text) }, t("sh.copy")), h("button", { type: "button", class: "btn danger sm", "aria-label": t("q.pack.del"), onclick: () => delText(x.id) }, icon("x", 15))))) : h("div", { class: "empty" }, t("sh.text.none")))));
}
