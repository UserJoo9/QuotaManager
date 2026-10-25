import { h, icon } from "../core/dom.js";
import { t, num, iso } from "../core/i18n.js";
import { liveState as L, onLive, sendTo, filesFromDrop, MAX_DIRECT } from "../core/live.js";
import { chip, fmtBytes } from "../components/ui.js";
import { toast } from "../components/toast.js";

let off = null, dragId = "", stage = null, target = null;
const devIcon = (n) => { n = (n || "").toLowerCase(); return /phone|android|pad|tablet/.test(n) ? "phone" : "laptop"; };

export default function liveCard(ctx) {
  if (!off) off = onLive(() => ctx.touch());
  const fileIn = h("input", { type: "file", multiple: true, hidden: true, onchange: (e) => { if (e.target.files.length && target) go(target, e.target.files); e.target.value = ""; } });
  const dirIn = h("input", { type: "file", multiple: true, hidden: true, webkitdirectory: true, onchange: (e) => { if (e.target.files.length && target) addStage(target, e.target.files); e.target.value = ""; } });
  const go = (p, files) => { stage = null; sendTo(p.id, files, p.ip); };
  function addStage(p, files) {
    if (!stage || stage.id !== p.id) stage = { id: p.id, ip: p.ip, name: p.name, files: [], folders: new Set() };
    for (const f of files) { stage.files.push(f); const r = f.webkitRelativePath || (f._folder ? f._folder + "/" : ""); if (r.includes("/")) stage.folders.add(r.split("/")[0]); }
    ctx.touch();
  }
  const size = stage ? stage.files.reduce((a, f) => a + f.size, 0) : 0;
  const peerCard = (p) => h("div", { class: "peer" + (dragId === p.id ? " drag" : ""),
    ondragover: (e) => { e.preventDefault(); if (dragId !== p.id) { dragId = p.id; ctx.touch(); } },
    ondragleave: () => { dragId = ""; ctx.touch(); },
    ondrop: async (e) => { e.preventDefault(); dragId = ""; const { files, dirs } = await filesFromDrop(e.dataTransfer); if (!files.length) { ctx.touch(); return; } if (dirs > 1) addStage(p, files); else go(p, files); } },
    h("div", { class: "peer-av" }, icon(devIcon(p.name), 22)), h("b", {}, p.name), h("span", { class: "mono" }, iso(p.ip)),
    h("div", { class: "fa" },
      h("button", { type: "button", class: "btn ghost sm", disabled: !!L.xfer, onclick: () => { target = p; fileIn.click(); } }, t("lt.files")),
      h("button", { type: "button", class: "btn ghost sm", disabled: !!L.xfer, onclick: () => { target = p; dirIn.click(); } }, t("lt.folder"))));
  return h("section", { class: "card c12" },
    h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("lt.title")), h("span", { class: "sub" }, t("lt.sub").replace("{m}", fmtBytes(MAX_DIRECT)))),
      chip(L.ws && L.ws.readyState === 1 ? "ok" : "", `${num(L.peers.length, 0)} ${t("lt.online")}`)),
    stage ? h("div", { class: "banner info" }, h("div", {}, `${t("lt.stage")}: `, h("b", {}, stage.name), ` — ${num(stage.folders.size, 0)} ${t("lt.folders")} · ${num(stage.files.length, 0)} ${t("lt.files.n")} · ${fmtBytes(size)}`),
      h("div", { class: "fa", style: { "margin-top": "8px" } },
        h("button", { type: "button", class: "btn ghost sm", onclick: () => { target = { id: stage.id, ip: stage.ip, name: stage.name }; dirIn.click(); } }, t("lt.addfolder")),
        h("button", { type: "button", class: "btn primary sm", disabled: !!L.xfer, onclick: () => { const s = stage; stage = null; sendTo(s.id, s.files, s.ip); } }, t("lt.send")),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => { stage = null; ctx.touch(); } }, t("common.cancel")))) : null,
    L.peers.length ? h("div", { class: "peers" }, L.peers.map(peerCard))
      : h("div", { class: "empty" }, h("b", {}, t("lt.wait")), h("br"), t("lt.wait.d")),
    fileIn, dirIn);
}
