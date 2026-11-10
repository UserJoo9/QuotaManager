import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useDashboard } from "../lib/session";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { t, tf, num } from "../lib/i18n";
import { Badge, Banner, Button, Card, Empty, KV, Loading } from "../components/ui/primitives";
import { Segmented, SwitchRow, TextField, controlClass } from "../components/ui/forms";
import { ConfirmDialog, Dialog, useToast } from "../components/ui/overlays";
import { Icon } from "../components/ui/Icon";
import { drawQr } from "../lib/qr";

/* Release notes are Markdown: render the few constructs they use as React nodes (never raw HTML).
   Each line gets dir="auto" so English notes read left-to-right inside the Arabic UI. */
const inlineMd = (txt: string) => txt.split(/(\*\*[^*]+\*\*|`[^`]+`)/).filter(Boolean).map((p, i) =>
  p.startsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong> : p.startsWith("`") ? <code key={i} className="font-mono text-[12px]">{p.slice(1, -1)}</code> : p);
export function mdLite(body: string): ReactNode[] {
  return String(body || "").split("\n").map((raw) => raw.replace(/\s+$/, "")).filter((l) => l.trim()).map((l, i) => {
    const head = l.match(/^#{2,4}\s+(.*)$/) || l.match(/^([A-Za-z؀-ۿ ]{2,24}):$/);
    if (head) return <div key={i} dir="auto" className="mt-2 font-semibold text-fg">{inlineMd(head[1])}</div>;
    const li = l.match(/^\s*[-*]\s+(.*)$/);
    if (li) return <div key={i} dir="auto" className="ps-4 text-fg-2 before:-ms-3 before:me-1.5 before:content-['•']">{inlineMd(li[1])}</div>;
    return <div key={i} dir="auto" className="text-fg-2">{inlineMd(l.trim())}</div>;
  });
}

/* ---------------------------------------------------------------- account */
interface Totp { enabled?: boolean; pending?: boolean; secret?: string; otpauth_uri?: string }
export function AccountTab() {
  const d = useDashboard();
  const q = useQuery({ queryKey: ["totp"], staleTime: 30_000, queryFn: () => api.get<Totp>("/api/totp") });
  const [pw, setPw] = useState(false), [enroll, setEnroll] = useState(false), [off, setOff] = useState(false);
  const { run } = useAction(); const qc = useQueryClient();
  const sec = d?.security || {}, tp = q.data;
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Card className="col-span-12 lg:col-span-6" title={t("acct.pw")} sub={t("acct.pw.d")} actions={sec.default_password && <Badge tone="danger">{t("acct.pw.default")}</Badge>}>
        <Button variant="primary" icon="lock" onClick={() => setPw(true)}>{t("acct.pw.title")}</Button>
      </Card>
      <Card className="col-span-12 lg:col-span-6" title={t("acct.2fa")} sub={t("acct.2fa.d")}
        actions={tp && <Badge tone={tp.enabled ? "success" : tp.pending ? "warning" : "neutral"}>{tp.enabled ? t("acct.2fa.enabled") : tp.pending ? t("acct.2fa.pending") : t("acct.2fa.disabled")}</Badge>}>
        {!tp ? <Loading what={t("acct.2fa.loading")} h={40} /> : tp.enabled ? <Button variant="danger" onClick={() => setOff(true)}>{t("acct.2fa.disable")}</Button>
          : <Button variant="primary" icon="shield" onClick={() => setEnroll(true)}>{tp.pending ? t("acct.2fa.resume") : t("acct.2fa.enable")}</Button>}
      </Card>
      {sec.failed_logins_1h != null && <Card className="col-span-12" title={t("acct.activity")}><KV items={[[t("acct.failed"), num(sec.failed_logins_1h, 0)], [t("acct.waf"), num(sec.waf_blocks_1h ?? 0, 0)]]} /></Card>}
      {pw && <PasswordDialog onClose={() => setPw(false)} />}
      {enroll && <TotpDialog current={tp} onClose={() => setEnroll(false)} />}
      <ConfirmDialog open={off} onOpenChange={setOff} title={t("acct.2fa.disable")} confirmLabel={t("acct.2fa.disable")}
        consequences={{ changes: t("acct.2fa.off.changes"), affected: t("nav.admin"), undo: t("acct.2fa.off.undo") }}
        onConfirm={async () => { if (!(await run("off", async () => { await api.post("/api/totp/disable"); await qc.invalidateQueries({ queryKey: ["totp"] }); }, t("acct.2fa.off")))) throw new Error(t("act.failed")); }} />
    </div>
  );
}
function PasswordDialog({ onClose }: { onClose: () => void }) {
  const [cur, setCur] = useState(""), [nw, setNw] = useState(""), [cf, setCf] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const toast = useToast();
  const short = nw.length > 0 && nw.length < 12, mismatch = cf.length > 0 && cf !== nw;
  const go = async () => {
    if (nw.length < 12) { setErr(t("acct.pw.short")); return; } if (nw !== cf) { setErr(t("acct.pw.mismatch")); return; }
    setBusy(true); setErr(null);
    try { await api.post("/api/password", { current: cur, new: nw }); toast({ kind: "success", title: t("acct.pw.done"), sub: t("acct.pw.relogin") }); setTimeout(() => location.reload(), 1200); }
    catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("acct.pw.title")} sub={t("acct.pw.sub")} busy={busy}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={go} pending={busy} pendingText={t("act.saving")} disabled={!cur || !nw || !cf}>{t("acct.pw.go")}</Button></>}>
      <div className="flex flex-col gap-4">
        <TextField label={t("acct.pw.cur")} type="password" autoComplete="current-password" value={cur} onChange={setCur} autoFocus />
        <TextField label={t("acct.pw.new")} hint={t("acct.pw.hint")} type="password" autoComplete="new-password" value={nw} onChange={setNw} error={short ? t("acct.pw.short") : null} />
        <TextField label={t("acct.pw.confirm")} type="password" autoComplete="new-password" value={cf} onChange={setCf} error={mismatch ? t("acct.pw.mismatch") : null} />
        <Banner tone="info">{t("acct.pw.note")}</Banner>
        {err && <p role="alert" className="text-[13px] text-danger">{err}</p>}
      </div>
    </Dialog>
  );
}
function TotpDialog({ current, onClose }: { current?: Totp; onClose: () => void }) {
  const [info, setInfo] = useState<Totp | null>(current?.pending && current.secret ? current : null), [code, setCode] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const qr = useRef<HTMLDivElement | null>(null); const qc = useQueryClient(); const toast = useToast();
  useEffect(() => { if (!info) api.post<Totp>("/api/totp/enroll").then(setInfo).catch((e) => setErr((e as Error).message)); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (info?.otpauth_uri && qr.current) drawQr(qr.current, info.otpauth_uri, 180).catch(() => {}); }, [info]);
  const go = async () => { setBusy(true); setErr(null); try { await api.post("/api/totp/enable", { code: code.trim() }); await qc.invalidateQueries({ queryKey: ["totp"] }); toast({ kind: "success", title: t("acct.2fa.on") }); onClose(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); } };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("acct.2fa.title")} sub={t("acct.2fa.scan")} busy={busy}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={go} pending={busy} disabled={!/^\d{6,8}$/.test(code.trim())}>{t("acct.2fa.verify")}</Button></>}>
      {!info ? <Loading what={t("acct.2fa.loading")} h={200} /> : <div className="flex flex-col gap-4">
        <div ref={qr} className="mx-auto grid size-[200px] place-items-center rounded-md bg-white p-2.5" aria-label={t("acct.2fa.qr")} role="img" />
        <TextField label={t("acct.2fa.secret")} hint={t("acct.2fa.secret.h")} value={info.secret || ""} onChange={() => {}} readOnly mono onFocus={(e) => e.currentTarget.select()} />
        <TextField label={t("acct.2fa.code")} value={code} onChange={setCode} inputMode="numeric" autoComplete="one-time-code" placeholder="123456" mono maxLength={8} />
      </div>}
      {err && <p role="alert" className="mt-3 text-[13px] text-danger">{err}</p>}
    </Dialog>
  );
}

/* ---------------------------------------------------------------- updates */
interface Upd { missing?: boolean; enabled?: boolean; auto_install?: boolean; checking?: boolean; installing?: boolean; error?: string; available?: boolean; current_version?: string; latest_version?: string; checked_at?: string; changelog?: { title: string; body: string }[] }
export function UpdatesTab() {
  const q = useQuery({ queryKey: ["updates"], staleTime: 20_000, queryFn: () => api.get<Upd>("/api/updates"),
    refetchInterval: (qq) => (qq.state.data?.checking || qq.state.data?.installing ? 3000 : false) });
  const { run, pending } = useAction(); const qc = useQueryClient();
  const [notes, setNotes] = useState(false), [install, setInstall] = useState(false);
  const u = q.data;
  if (q.isError) return <Empty icon="alert" title={t("upd.unavail")} sub={(q.error as Error).message} action={<Button size="sm" onClick={() => q.refetch()}>{t("conn.retry")}</Button>} />;
  if (!u) return <Loading what={t("upd.loading")} h={220} />;
  if (u.missing) return <Empty title={t("upd.unavail")} />;
  const off = !u.enabled;
  const status = off ? t("upd.st.off") : u.checking ? t("upd.st.checking") : u.error ? (/timeout|timed ?out/i.test(u.error) ? t("upd.st.timeout") : t("upd.st.error")) : u.available ? t("upd.st.available") : u.latest_version ? t("upd.st.latest") : "—";
  const set = (k: string, fn: () => Promise<unknown>, ok?: string) => run(k, async () => { const r = await fn(); if (r && typeof r === "object" && "current_version" in (r as object)) qc.setQueryData(["updates"], r); await q.refetch(); }, ok);
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Card className="col-span-12" title={t("upd.title")} sub={t("upd.title.d")} actions={<Badge tone={u.available && !off ? "accent" : u.error && !off ? "warning" : "neutral"} icon={false}>{status}</Badge>}>
        <KV items={[[t("upd.current"), u.current_version ? "v" + u.current_version : "—", true], [t("upd.latest"), off ? "—" : u.latest_version ? "v" + u.latest_version : u.checking ? t("upd.st.checking") : "—", true],
          [t("upd.checked"), off || !u.checked_at ? (off ? "—" : t("upd.never")) : new Date(u.checked_at).toLocaleString("en-GB")]]} />
        {u.error && !off && <Banner tone="warning" className="mt-3">{/403|forbidden/i.test(u.error) ? t("upd.err.403") : /timeout|timed ?out|urlopen|resolution|unreachable|refused/i.test(u.error) ? t("upd.err.net") : u.error}</Banner>}
        <div className="mt-4 flex flex-wrap gap-2">
          <Button disabled={off || u.checking} pending={pending === "check"} pendingText={t("upd.checking")} onClick={() => set("check", () => api.post("/api/updates/check"))}>{t("upd.check")}</Button>
          {u.available && !off && <Button variant="primary" pending={pending === "install" || u.installing} pendingText={t("upd.installing")} onClick={() => setInstall(true)}>{tf("upd.install.n", { v: u.latest_version || "" })}</Button>}
          {u.available && !off && !!(u.changelog || []).length && <Button variant="ghost" onClick={() => setNotes(true)}>{t("upd.details")}</Button>}
        </div>
      </Card>
      <Card className="col-span-12" title={t("upd.prefs")}>
        <div className="divide-y divide-line">
          <SwitchRow title={t("upd.enabled")} desc={t("upd.enabled.d")} checked={!!u.enabled} pending={pending === "en"} onChange={(v) => set("en", () => api.post("/api/updates", { enabled: v }), t("dr.saved"))} />
          <SwitchRow title={t("upd.auto")} desc={t("upd.auto.d")} checked={!!u.auto_install} pending={pending === "auto"} onChange={(v) => set("auto", () => api.post("/api/updates", { auto_install: v }), t("dr.saved"))} />
        </div>
      </Card>
      {/* read-only: the header X plus a single Close button (no redundant Cancel) */}
      <Dialog open={notes} onOpenChange={setNotes} title={t("upd.details")} sub={`v${u.current_version} → v${u.latest_version}`} wide
        footer={<Button onClick={() => setNotes(false)}>{t("common.close")}</Button>}>
        {(u.changelog || []).length ? (u.changelog || []).map((c, i) => <section key={i} className="mb-4 text-[13px]"><b className="block text-fg">{c.title}</b>{mdLite(c.body)}</section>) : <Empty title={t("upd.nolog")} />}
      </Dialog>
      <ConfirmDialog open={install} onOpenChange={setInstall} title={tf("upd.install.n", { v: u.latest_version || "" })} confirmLabel={tf("upd.install.n", { v: u.latest_version || "" })} danger={false}
        consequences={{ changes: t("upd.cf.changes"), affected: t("cf.everyone"), undo: t("upd.cf.undo"), connection: t("upd.cf.conn") }}
        onConfirm={async () => { if (!(await set("install", () => api.post("/api/updates/install"), t("upd.installing")))) throw new Error(t("act.failed")); }} />
    </div>
  );
}

/* ---------------------------------------------------------------- logs */
type Level = "ALL" | "INFO" | "WARNING" | "ERROR";
// match the level column ("2026-10-24 17:10:49,044 WARNING quota.x: ..."), not any "error" word inside the message
const lvlOk = (l: string, lv: Level) => { if (lv === "ALL") return true; const m = /^\S+\s+\S+\s+([A-Z]+)\b/.exec(l); return m ? m[1] === lv : l.toUpperCase().includes(lv); };
export function LogsTab() {
  const q = useQuery({ queryKey: ["logs"], staleTime: 15_000, queryFn: () => api.get<{ lines: string[]; total: number; truncated?: boolean }>("/api/logs?limit=600") });
  const [lv, setLv] = useState<Level>("ALL"), [s, setS] = useState("");
  const lines = useMemo(() => (q.data?.lines || []).filter((l) => lvlOk(l, lv) && (!s || l.toLowerCase().includes(s.toLowerCase()))), [q.data, lv, s]);
  const exportLogs = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/plain" })); a.download = `quota-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); };
  const filtered = lv !== "ALL" || !!s;
  return (
    <Card title={t("logs.title")} sub={q.data ? `${num(lines.length, 0)} / ${num(q.data.total, 0)}${q.data.truncated ? " · " + t("logs.trunc") : ""}` : t("logs.title.d")}
      actions={<><Button size="sm" icon="reset" pending={q.isFetching} onClick={() => q.refetch()}>{t("logs.refresh")}</Button><Button size="sm" variant="primary" icon="down" disabled={!lines.length} onClick={exportLogs}>{t("logs.download")}</Button></>}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented size="sm" label={t("logs.level")} value={lv} onChange={setLv} options={(["ALL", "INFO", "WARNING", "ERROR"] as Level[]).map((l) => ({ value: l, label: l === "ALL" ? t("pp.f.all") : l }))} />
        <label className="relative min-w-48 flex-1"><span className="qm-sr">{t("logs.search")}</span><Icon name="search" size={16} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-fg-3" />
          <input type="search" value={s} onChange={(e) => setS(e.target.value)} placeholder={t("logs.search")} className={clsx(controlClass, "ps-9")} /></label>
        {filtered && <Button size="sm" variant="ghost" icon="reset" onClick={() => { setLv("ALL"); setS(""); }}>{t("f.clearall")}</Button>}
      </div>
      {q.isError && !q.data ? <Empty icon="alert" title={t("logs.fail")} sub={(q.error as Error).message} action={<Button size="sm" onClick={() => q.refetch()}>{t("conn.retry")}</Button>} />
        : !q.data ? <Loading what={t("logs.loading")} h={300} />
        : <pre className="qm-ltr h-[460px] overflow-auto whitespace-pre-wrap rounded-md border border-line bg-canvas p-3 font-mono text-[11.5px] leading-relaxed text-fg-2" tabIndex={0}>{lines.length ? lines.join("\n") : t("logs.none")}</pre>}
    </Card>
  );
}
