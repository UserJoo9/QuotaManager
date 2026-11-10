import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { t, tf, num, iso } from "../lib/i18n";
import { fmtBytes } from "../lib/format";
import { useLive, sendTo, filesFromDrop, MAX_DIRECT, type Peer } from "../lib/liveTransfer";
import { drawQr } from "../lib/qr";
import { Badge, Banner, Button, Card, Empty, IconButton, Loading, Meter } from "../components/ui/primitives";
import { Segmented, TextField } from "../components/ui/forms";
import { Checkbox } from "../components/ui/checkbox";
import { ConfirmDialog, Dialog, useToast } from "../components/ui/overlays";
import { Icon } from "../components/ui/Icon";
import { PageActions } from "../shell/App";

interface SFile { id: string; name: string; size: number; sender_ip?: string; download_count: number; expires_at: number; is_expired?: boolean }
interface SText { id: string; text: string; sender_ip?: string }
interface List { files: SFile[]; texts: SText[]; total_files: number; total_size: number }
const TTLS: [number, string][] = [[1800, "30m"], [7200, "2h"], [21600, "6h"], [86400, "24h"], [604800, "7d"]];

function xhrSend(url: string, file: Blob, headers: Record<string, string>, onProgress: (l: number, tot: number) => void) {
  return new Promise<void>((resolve, reject) => {
    const x = new XMLHttpRequest(); x.open("POST", url);
    for (const [k, v] of Object.entries({ "X-QM-CSRF": "1", ...headers })) x.setRequestHeader(k, v);
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    x.onload = () => (x.status >= 200 && x.status < 300 ? resolve() : reject(new Error(x.statusText || `HTTP ${x.status}`)));
    x.onerror = () => reject(new Error(t("sh.neterr")));
    x.send(file);
  });
}
type RelFile = File & { relPath?: string };
async function readEntries(items: DataTransferItem[]) {
  const out: RelFile[] = []; let dirs = 0;
  const walk = async (entry: FileSystemEntry, path: string): Promise<void> => {
    if (entry.isFile) { const f = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej)) as RelFile; f.relPath = path + f.name; out.push(f); }
    else if (entry.isDirectory) {
      const r = (entry as FileSystemDirectoryEntry).createReader(); let batch: FileSystemEntry[];
      do { batch = await new Promise<FileSystemEntry[]>((res, rej) => r.readEntries(res, rej)); for (const e of batch) await walk(e, path + entry.name + "/"); } while (batch.length);
    }
  };
  for (const it of items) { const e = it.webkitGetAsEntry?.(); if (!e) continue; if (e.isDirectory) dirs++; await walk(e, ""); }
  return { files: out, dirs };
}

/** "Move a file between devices at home — no cloud, no cable." */
export default function Share() {
  const q = useQuery({ queryKey: ["share"], queryFn: () => api.get<List>("/api/share/list"), refetchInterval: 10_000, refetchIntervalInBackground: false });
  const qc = useQueryClient(); const toast = useToast();
  const { run, pending } = useAction();
  const [ttl, setTtl] = useState(7200), [up, setUp] = useState<{ name: string; pct: number; stat: string } | null>(null), [drag, setDrag] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set()), [confirm, setConfirm] = useState<null | "clear" | "delall" | "delsel">(null), [qr, setQr] = useState(false);
  const fileIn = useRef<HTMLInputElement | null>(null), dirIn = useRef<HTMLInputElement | null>(null);
  const reload = () => qc.invalidateQueries({ queryKey: ["share"] });
  const d = q.data, files = d?.files || [], texts = d?.texts || [];
  useEffect(() => { const ids = new Set(files.map((f) => f.id)); setSel((s) => new Set([...s].filter((x) => ids.has(x)))); }, [d]); // eslint-disable-line react-hooks/exhaustive-deps

  const uploadFiles = async (list: File[]) => {
    if (!list.length || up) return;
    try {
      for (let i = 0; i < list.length; i++) {
        const f = list[i], name = `${f.name} (${i + 1}/${list.length})`;
        setUp({ name, pct: 0, stat: "" });
        await xhrSend(`/api/share/upload?ttl=${ttl}`, f, { "X-Filename": encodeURIComponent(f.name), "Content-Type": f.type || "application/octet-stream" },
          (l, tot) => setUp({ name, pct: Math.round((l / tot) * 100), stat: `${fmtBytes(l)} / ${fmtBytes(tot)}` }));
      }
      toast({ kind: "success", title: t("sh.uploaded").replace("{n}", String(list.length)) });
    } catch (e) { toast({ kind: "error", title: t("sh.upfail"), sub: (e as Error).message }); } finally { setUp(null); reload(); }
  };
  const uploadFolder = async (list: RelFile[]) => {
    if (!list.length || up) return;
    const tops = [...new Set(list.map((f) => (f.webkitRelativePath || f.relPath || "").split("/")[0]).filter(Boolean))];
    const name = tops.length === 1 ? tops[0] : tops.length === 2 ? `${tops[0]}_and_${tops[1]}` : tops.length > 2 ? `${tops[0]}_${tops[1]}_and_${tops.length - 2}_more` : "Folder";
    try {
      setUp({ name: `${name}.zip`, pct: 3, stat: t("sh.init") });
      const { folder_id } = await api.post<{ folder_id: string }>("/api/share/folder/init", { folder_name: name, ttl });
      for (let i = 0; i < list.length; i++) {
        const f = list[i], rel = f.webkitRelativePath || f.relPath || f.name, label = `${name}.zip · ${i + 1}/${list.length}`;
        await xhrSend(`/api/share/folder/file?folder_id=${encodeURIComponent(folder_id)}&path=${encodeURIComponent(rel)}`, f, { "Content-Type": f.type || "application/octet-stream" },
          (l, tot) => setUp({ name: label, pct: Math.round(((i + l / tot) / list.length) * 100), stat: f.name }));
      }
      await api.post("/api/share/folder/finalize", { folder_id });
      toast({ kind: "success", title: t("sh.uploaded").replace("{n}", "1") });
    } catch (e) { toast({ kind: "error", title: t("sh.upfail"), sub: (e as Error).message }); } finally { setUp(null); reload(); }
  };
  const left = (f: SFile) => { const m = Math.max(0, Math.round((f.expires_at * 1000 - Date.now()) / 60000)); return m >= 1440 ? `${num(Math.round(m / 1440), 0)}d` : m >= 60 ? `${num(Math.round(m / 60), 0)}h` : `${num(m, 0)}m`; };
  const live = files.filter((f) => !f.is_expired);
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <PageActions><Button size="sm" icon="info" onClick={() => setQr(true)}>{t("sh.qr")}</Button></PageActions>
      <Card className="col-span-12" title={t("sh.title")} sub={d ? `${num(d.total_files, 0)} ${t("sh.files.n")} · ${fmtBytes(d.total_size)}` : t("sh.title.d")}
        actions={<><a className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line-strong px-3 text-[12.5px] font-semibold text-fg hover:bg-raised" href="/share" target="_blank" rel="noopener">{t("sh.portal")}</a>
          <Button size="sm" variant="danger" disabled={!files.length && !texts.length} onClick={() => setConfirm("clear")}>{t("sh.clear")}</Button></>}>
        <div onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={(e) => { e.preventDefault(); setDrag(false); }}
          onDrop={async (e) => { e.preventDefault(); setDrag(false); const items = [...(e.dataTransfer.items || [])], plain = [...(e.dataTransfer.files || [])];
            if (items.length && typeof items[0].webkitGetAsEntry === "function") { const { files: fl, dirs } = await readEntries(items); if (dirs) uploadFolder(fl); else uploadFiles(fl.length ? fl : plain); } else uploadFiles(plain); }}
          className={clsx("flex flex-col items-center gap-2 rounded-lg border-2 border-dashed p-6 text-center transition-colors", drag ? "border-accent bg-accent-soft" : "border-line-strong")}>
          <Icon name="share" size={22} className="text-fg-3" />
          <b className="text-[14px] text-fg">{t("sh.drop")}</b><span className="text-[12.5px] text-fg-3">{t("sh.drop.sub")}</span>
          <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
            <Button size="sm" variant="primary" icon="plus" disabled={!!up} onClick={() => fileIn.current?.click()}>{t("sh.files")}</Button>
            <Button size="sm" icon="box" disabled={!!up} onClick={() => dirIn.current?.click()}>{t("sh.folder")}</Button>
          </div>
          <div className="mt-1 flex items-center gap-2 text-[12.5px] text-fg-3"><span>{t("sh.keep.for")}</span>
            <Segmented size="sm" label={t("sh.ttl")} value={String(ttl)} onChange={(v) => setTtl(+v)} options={TTLS.map(([v, l]) => ({ value: String(v), label: l }))} /></div>
          {up && <div className="mt-3 w-full max-w-lg text-start" role="status" aria-live="polite"><div className="truncate text-[12.5px] text-fg-2">{up.name}</div><Meter value={up.pct} neutral className="my-1.5" label={up.name} /><div className="text-[12px] text-fg-3 tabular-nums">{up.pct}% {up.stat}</div></div>}
          <input ref={fileIn} type="file" multiple hidden onChange={(e) => { uploadFiles([...(e.target.files || [])]); e.target.value = ""; }} />
          <input ref={dirIn} type="file" multiple hidden {...({ webkitdirectory: "" } as object)} onChange={(e) => { uploadFolder([...(e.target.files || [])]); e.target.value = ""; }} />
        </div>
        <p className="mt-2 text-[12px] text-fg-3">{t("sh.trust")}</p>
      </Card>

      <LiveCard />

      <Card className="col-span-12 lg:col-span-7" title={t("sh.shared")}
        actions={files.length > 0 && <><a className="inline-flex h-8 items-center rounded-md border border-line-strong px-3 text-[12.5px] font-semibold text-fg hover:bg-raised" href={`/api/share/download-zip?ids=${encodeURIComponent(live.map((f) => f.id).join(","))}`} download>{t("sh.zipall")}</a>
          <Button size="sm" variant="danger" onClick={() => setConfirm("delall")}>{t("sh.delall")}</Button></>}>
        {sel.size > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border border-accent/40 bg-accent-soft px-3 py-2">
            <b className="flex-1 text-[13px] text-fg">{num(sel.size, 0)} {t("sh.selected")}</b>
            <a className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-[12.5px] font-semibold text-accent-ink" href={`/api/share/download-zip?ids=${encodeURIComponent([...sel].join(","))}`} download>{t("sh.zip")}</a>
            <Button size="sm" variant="danger" onClick={() => setConfirm("delsel")}>{t("sh.delsel")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setSel(new Set())}>{t("pp.sel.clear")}</Button>
          </div>
        )}
        {!d ? (q.isError ? <Empty icon="alert" title={t("sh.fail")} sub={(q.error as Error).message} action={<Button size="sm" onClick={() => q.refetch()}>{t("conn.retry")}</Button>} /> : <Loading what={t("sh.loading")} h={180} />)
          : !files.length ? <Empty icon="share" title={t("sh.none")} sub={t("sh.none.sub")} action={<Button size="sm" icon="plus" onClick={() => fileIn.current?.click()}>{t("sh.files")}</Button>} /> : (
          <ul className="divide-y divide-line">{files.map((f) => (
            <li key={f.id} className="flex flex-wrap items-center gap-3 py-2.5">
              <Checkbox checked={sel.has(f.id)} onChange={(v) => setSel((s) => { const n = new Set(s); if (v) n.add(f.id); else n.delete(f.id); return n; })} label={tf("sh.sel.one", { n: f.name })} />
              <span className="min-w-0 flex-1"><b className="block break-all text-[13px] text-fg">{f.name}</b>
                <span className="text-[11.5px] text-fg-3 tabular-nums">{fmtBytes(f.size)} · {iso(f.sender_ip || "")} · ↓{num(f.download_count, 0)} · {t("sh.left")} {left(f)}</span></span>
              {f.is_expired ? <Badge tone="warning">{t("sh.expired")}</Badge> : <a className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-[12.5px] font-semibold text-accent-ink" href={`/api/share/download/${f.id}`} download={f.name}>{t("sh.download")}</a>}
              <IconButton icon="x" tone="danger" label={tf("sh.del.n", { n: f.name })} onClick={() => run("d" + f.id, async () => { await api.del(`/api/share/files/${f.id}`); await reload(); }, t("sh.deleted"))} />
            </li>))}</ul>
        )}
      </Card>
      <TextsCard texts={texts} onChange={reload} pending={pending} />

      <ConfirmDialog open={!!confirm} onOpenChange={(v) => !v && setConfirm(null)}
        title={confirm === "clear" ? t("sh.clear") : confirm === "delall" ? t("sh.delall") : t("sh.delsel")} confirmLabel={confirm === "clear" ? t("sh.clear") : t("sh.del.cta")}
        consequences={{ changes: confirm === "clear" ? tf("sh.cf.clear", { f: num(files.length, 0), t: num(texts.length, 0) }) : tf("sh.cf.del", { n: num(confirm === "delsel" ? sel.size : files.length, 0) }), affected: t("sh.cf.who"), undo: t("sh.cf.undo") }}
        onConfirm={async () => {
          const ok = await run("bulk", async () => {
            if (confirm === "clear") await api.post("/api/share/clear");
            else await api.post("/api/share/delete-batch", { ids: confirm === "delsel" ? [...sel] : files.map((f) => f.id) });
            setSel(new Set()); await reload();
          }, t("sh.deleted"));
          if (!ok) throw new Error(t("act.failed"));
        }} />
      <QrDialog open={qr} onOpenChange={setQr} />
    </div>
  );
}

function TextsCard({ texts, onChange, pending }: { texts: SText[]; onChange: () => void; pending: string | null }) {
  const [v, setV] = useState("");
  const { run } = useAction(); const toast = useToast();
  const copy = async (s: string) => { try { await navigator.clipboard.writeText(s); toast({ kind: "success", title: t("sh.copied") }); } catch { toast({ kind: "error", title: t("sh.copyfail") }); } };
  return (
    <Card className="col-span-12 lg:col-span-5" title={t("sh.text")} sub={t("sh.text.d")}>
      <form className="flex items-start gap-2" onSubmit={async (e) => { e.preventDefault(); const s = v.trim(); if (!s) return; if (await run("text", async () => { await api.post("/api/share/text", { text: s }); onChange(); }, t("sh.text.done"))) setV(""); }}>
        <TextField className="flex-1" label={t("sh.text")} labelHidden value={v} onChange={setV} placeholder={t("sh.text.ph")} autoComplete="off" action={<Button type="submit" variant="primary" disabled={!v.trim()} pending={pending === "text"}>{t("sh.text.go")}</Button>} />
      </form>
      {!texts.length ? <p className="mt-3 text-[12.5px] text-fg-3">{t("sh.text.none")}</p> : (
        <ul className="mt-3 max-h-80 divide-y divide-line overflow-y-auto">{texts.map((x) => (
          <li key={x.id} className="flex items-start gap-2 py-2">
            <span className="min-w-0 flex-1 break-words text-[13px] text-fg" dir="auto">{/^https?:\/\//i.test(x.text) ? <a className="text-accent-fg underline" href={x.text} target="_blank" rel="noopener noreferrer">{x.text}</a> : x.text}
              <span className="qm-ltr mt-0.5 block font-mono text-[11px] text-fg-3">{x.sender_ip}</span></span>
            <IconButton icon="copy" label={t("sh.copy")} onClick={() => copy(x.text)} />
            <IconButton icon="x" tone="danger" label={t("sh.text.del")} onClick={() => run("t" + x.id, async () => { await api.del(`/api/share/texts/${x.id}`); onChange(); }, t("sh.deleted"))} />
          </li>))}</ul>
      )}
    </Card>
  );
}

function LiveCard() {
  const L = useLive();
  const [stage, setStage] = useState<{ peer: Peer; files: File[]; folders: Set<string> } | null>(null), [dragId, setDragId] = useState("");
  const target = useRef<Peer | null>(null), fileIn = useRef<HTMLInputElement | null>(null), dirIn = useRef<HTMLInputElement | null>(null);
  const busy = !!L.xfer, connected = L.connected;
  const m = { sendTo, filesFromDrop, MAX_DIRECT };
  const go = (p: Peer, files: File[]) => { setStage(null); m.sendTo(p.id, files, p.ip); };
  const add = (p: Peer, files: File[]) => setStage((s) => {
    const st = s && s.peer.id === p.id ? s : { peer: p, files: [] as File[], folders: new Set<string>() };
    const next = { ...st, files: [...st.files, ...files], folders: new Set(st.folders) };
    for (const f of files) { const r = (f as RelFile).webkitRelativePath || ""; if (r.includes("/")) next.folders.add(r.split("/")[0]); }
    return next;
  });
  return (
    <Card className="col-span-12" title={t("lt.title")} sub={t("lt.sub").replace("{m}", fmtBytes(m.MAX_DIRECT))}
      actions={<Badge tone={connected ? "success" : "neutral"}>{`${num(L.peers.length, 0)} ${t("lt.online")}`}</Badge>}>
      {stage && (
        <Banner tone="info" className="mb-3" title={`${t("lt.stage")}: ${stage.peer.name}`}
          action={<div className="flex flex-wrap gap-2"><Button size="sm" onClick={() => { target.current = stage.peer; dirIn.current?.click(); }}>{t("lt.addfolder")}</Button>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => go(stage.peer, stage.files)}>{t("lt.send")}</Button><Button size="sm" variant="ghost" onClick={() => setStage(null)}>{t("common.cancel")}</Button></div>}>
          {num(stage.folders.size, 0)} {t("lt.folders")} · {num(stage.files.length, 0)} {t("lt.files.n")} · {fmtBytes(stage.files.reduce((a, f) => a + f.size, 0))}
        </Banner>
      )}
      {!L.peers.length ? <Empty icon="network" title={t("lt.wait")} sub={t("lt.wait.d")} /> : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">{L.peers.map((p) => (
          <li key={p.id} onDragOver={(e) => { e.preventDefault(); setDragId(p.id); }} onDragLeave={() => setDragId("")}
            onDrop={async (e) => { e.preventDefault(); setDragId(""); const { files, dirs } = await m.filesFromDrop(e.dataTransfer); if (!files.length) return; if (dirs > 1) add(p, files); else go(p, files); }}
            className={clsx("flex flex-col items-center gap-1 rounded-lg border p-4 text-center transition-colors", dragId === p.id ? "border-accent bg-accent-soft" : "border-line")}>
            <Icon name={/phone|android|pad|tablet/i.test(p.name) ? "phone" : "laptop"} size={22} className="text-fg-3" />
            <b className="text-[13.5px] text-fg">{p.name}</b><span className="qm-ltr font-mono text-[11.5px] text-fg-3">{p.ip}</span>
            <div className="mt-2 flex gap-2"><Button size="sm" disabled={busy} onClick={() => { target.current = p; fileIn.current?.click(); }}>{t("lt.files")}</Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => { target.current = p; dirIn.current?.click(); }}>{t("lt.folder")}</Button></div>
          </li>))}</ul>
      )}
      <input ref={fileIn} type="file" multiple hidden onChange={(e) => { const fl = [...(e.target.files || [])]; if (fl.length && target.current) go(target.current, fl); e.target.value = ""; }} />
      <input ref={dirIn} type="file" multiple hidden {...({ webkitdirectory: "" } as object)} onChange={(e) => { const fl = [...(e.target.files || [])]; if (fl.length && target.current) add(target.current, fl); e.target.value = ""; }} />
    </Card>
  );
}

function QrDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const box = useRef<HTMLDivElement | null>(null);
  const url = `${location.origin}/share`;
  useEffect(() => { if (open) setTimeout(() => { if (box.current) drawQr(box.current, url, 200).catch(() => {}); }, 0); }, [open, url]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("sh.qr")} sub={t("sh.qr.sub")} footer={<Button onClick={() => onOpenChange(false)}>{t("common.close")}</Button>}>
      <div ref={box} className="mx-auto grid size-[224px] place-items-center rounded-md bg-white p-3" role="img" aria-label={t("sh.qr")} />
      <TextField className="mt-4" label={t("sh.qr.url")} value={url} onChange={() => {}} readOnly mono onFocus={(e) => e.currentTarget.select()} />
    </Dialog>
  );
}
