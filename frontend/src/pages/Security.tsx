import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useDashboard } from "../lib/session";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { t, tf, num, iso } from "../lib/i18n";
import { isIPv4, isCidr4 } from "../lib/format";
import { Badge, Banner, Button, Card, Empty, IconButton, KV, Loading, Term } from "../components/ui/primitives";
import { NumberField, Segmented, Select, SwitchRow, Switch, TextField, Combobox, Field, controlClass } from "../components/ui/forms";
import { ConfirmDialog, Dialog } from "../components/ui/overlays";
import { Icon } from "../components/ui/Icon";
import type { Dashboard } from "../lib/types";

type Tab = "firewall" | "dns";
export default function Security() {
  const [tab, setTab] = useState<Tab>("firewall");
  return (
    <div className="flex flex-col gap-[var(--gap)]">
      <Segmented label={t("sec.tabs")} value={tab} onChange={setTab} options={[{ value: "firewall", label: t("sec.tab.firewall") }, { value: "dns", label: t("sec.tab.dns") }]} />
      {tab === "firewall" ? <Firewall /> : <Dns />}
    </div>
  );
}

/* ================================================================ firewall */
interface Rule { name?: string; chain: string; action: string; src?: string; dst?: string; protocol?: string; src_port?: number; dst_port?: number; log?: boolean }
interface Fwd { name?: string; protocol: string; source_port: number; target_ip: string; target_port: number }
interface FwCfg { enabled?: boolean; services?: { name?: string; protocol: string; port: number; source?: string }[]; port_forwards?: Fwd[]; rules?: Rule[]; allow_cidrs?: string[]; deny_cidrs?: string[]; dmz?: string;
  syn_flood?: { rate?: number; burst?: number }; brute_force?: { threshold?: number; ban_seconds?: number }; scan_detect?: { enabled?: boolean; syn_threshold?: number; ban_seconds?: number }; geo_block?: boolean; wan_confirmed?: boolean }
interface FwState { available?: boolean; reason?: string; mode?: string; config?: FwCfg; status?: { apply_ok?: boolean; last_error?: string; bans?: { ip: string; reason?: string; remaining?: number }[] }; log?: { ts: number; level?: string; message: string }[] }
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x ?? null));
const ruleStr = (r: Rule) => {
  const p: string[] = [];
  if (r.src && r.src !== "0.0.0.0/0") p.push("src " + r.src);
  if (r.dst && r.dst !== "0.0.0.0/0") p.push("dst " + r.dst);
  if (r.protocol) p.push(r.protocol);
  if (r.src_port) p.push("sport " + r.src_port);
  if (r.dst_port) p.push("dport " + r.dst_port);
  return p.join(" ") || t("fw.any");
};

function Firewall() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["firewall"], staleTime: 15_000, queryFn: async () => {
    const [state, tls] = await Promise.all([api.get<FwState>("/api/firewall"), api.get<{ enforced?: boolean }>("/api/security/tls").catch(() => null)]);
    return { state, tls };
  } });
  const [c, setC] = useState<FwCfg | null>(null);
  const [base, setBase] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [dlg, setDlg] = useState<null | { kind: "rule" | "fwd"; idx: number }>(null);
  const [confirm, setConfirm] = useState<null | "apply" | "revert" | "https-on" | "https-off">(null);
  const [banIp, setBanIp] = useState(""), [banErr, setBanErr] = useState<string | null>(null), [filter, setFilter] = useState("");
  const { run, pending } = useAction();
  const st = q.data?.state;
  const dirty = !!c && JSON.stringify(c) !== base;
  useEffect(() => { if (st?.config && !dirty) { setC(clone(st.config)); setBase(JSON.stringify(st.config)); } }, [JSON.stringify(st?.config)]); // eslint-disable-line react-hooks/exhaustive-deps
  if (q.isError) return <Empty icon="alert" title={t("fw.unavail")} sub={(q.error as Error).message} action={<Button size="sm" onClick={() => q.refetch()}>{t("conn.retry")}</Button>} />;
  if (!st || !c) return st && st.available === false && !st.config ? <Empty icon="shield" title={t("fw.unavail")} sub={st.reason} /> : <Loading what={t("fw.loading")} h={280} />;
  const s = st.status || {}, mode = st.mode;
  const set = (patch: Partial<FwCfg>) => setC((x) => ({ ...(x as FwCfg), ...patch }));
  const reload = () => qc.invalidateQueries({ queryKey: ["firewall"] });
  const sf = c.syn_flood || {}, bf = c.brute_force || {}, sd = c.scan_detect || {};
  const apply = async () => {
    const res = await api.post<{ warnings?: string[] }>("/api/firewall", { enabled: c.enabled !== false, services: c.services || [], port_forwards: c.port_forwards || [], rules: c.rules || [],
      allow_cidrs: c.allow_cidrs || [], deny_cidrs: c.deny_cidrs || [], dmz: c.dmz || "",
      syn_flood: { rate: sf.rate || 10, burst: sf.burst || 20 }, brute_force: { threshold: bf.threshold || 10, ban_seconds: bf.ban_seconds || 1800 },
      scan_detect: { enabled: sd.enabled !== false, syn_threshold: sd.syn_threshold || 200, ban_seconds: sd.ban_seconds || 3600 },
      geo_block: c.geo_block === true, wan_confirmed: c.wan_confirmed === true });
    setWarnings(res.warnings || []); setBase(JSON.stringify(c)); await reload();
  };
  const linesBad = (arr?: string[]) => (arr || []).filter((x) => !isCidr4(x));
  const badAllow = linesBad(c.allow_cidrs), badDeny = linesBad(c.deny_cidrs), badDmz = c.dmz && !isIPv4(c.dmz);
  const invalid = badAllow.length > 0 || badDeny.length > 0 || !!badDmz;
  const fl = filter.trim().toLowerCase();
  const rules = (c.rules || []).map((r, i) => [r, i] as const).filter(([r]) => !fl || (r.name || "").toLowerCase().includes(fl) || ruleStr(r).toLowerCase().includes(fl));
  const log = (st.log || []).filter((e) => !fl || e.message.toLowerCase().includes(fl));
  const httpsOn = !!q.data?.tls?.enforced;
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Card className="col-span-12" title={t("fw.title")} sub={t("fw.title.d")}
        actions={<><Badge tone={mode === "wan" ? "accent" : "neutral"} icon={false}>{mode === "wan" ? t("fw.mode.wan") : mode === "lan" ? t("fw.mode.lan") : "—"}</Badge>
          <Badge tone={s.apply_ok === false ? "danger" : s.apply_ok ? "success" : "neutral"}>{s.apply_ok === false ? t("fw.st.failed") : s.apply_ok ? t("fw.st.ok") : t("fw.st.none")}</Badge>
          {dirty && <Badge tone="warning" icon={false}>{t("fw.unsaved")}</Badge>}
          <Switch checked={c.enabled !== false} onChange={(v) => set({ enabled: v })} label={t("fw.enabled")} /></>}>
        {st.available === false && <Banner tone="warning" className="mb-2">{t("fw.nonft")} {st.reason || ""}</Banner>}
        {s.apply_ok === false && s.last_error && <Banner tone="danger" className="mb-2">{s.last_error}</Banner>}
        {warnings.length > 0 && <Banner tone="warning" title={t("fw.ignored")} className="mb-2"><ul className="list-disc ps-5">{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></Banner>}
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" disabled={!dirty || invalid} pending={pending === "apply"} pendingText={t("act.applying")} onClick={() => setConfirm("apply")}>{t("fw.apply")}</Button>
          <Button onClick={() => setConfirm("revert")}>{t("fw.revert")}</Button>
          {dirty && <Button variant="ghost" onClick={() => setC(JSON.parse(base))}>{t("fw.discard")}</Button>}
          {invalid && <span className="text-[12px] text-danger">{t("fw.fix.first")}</span>}
        </div>
        <p className="mt-2 text-[12.5px] text-fg-3">{t("fw.safe")}</p>
      </Card>

      <Card className="col-span-12 lg:col-span-6" title={t("fw.protect")} sub={t("fw.protect.d")}>
        <h3 className="mb-2 text-[12.5px] font-semibold text-fg-2">{t("fw.syn")}</h3>
        <div className="grid grid-cols-2 gap-3">
          <NumberField label={t("fw.syn.rate")} value={sf.rate ?? 10} onValue={(v) => set({ syn_flood: { ...sf, rate: v ?? 10 } })} unit={t("fw.u.persec")} spec={{ min: 1, max: 100000, integer: true }} />
          <NumberField label={t("fw.syn.burst")} value={sf.burst ?? 20} onValue={(v) => set({ syn_flood: { ...sf, burst: v ?? 20 } })} unit={t("fw.u.packets")} spec={{ min: 1, max: 100000, integer: true }} />
        </div>
        <h3 className="mb-2 mt-4 text-[12.5px] font-semibold text-fg-2">{t("fw.bf")}</h3>
        <div className="grid grid-cols-2 gap-3">
          <NumberField label={t("fw.bf.thr")} value={bf.threshold ?? 10} onValue={(v) => set({ brute_force: { ...bf, threshold: v ?? 10 } })} unit={t("fw.u.tries")} spec={{ min: 1, max: 100000, integer: true }} />
          <NumberField label={t("fw.ban.sec")} value={bf.ban_seconds ?? 1800} onValue={(v) => set({ brute_force: { ...bf, ban_seconds: v ?? 1800 } })} unit={t("fw.u.sec")} spec={{ min: 60, max: 604800, integer: true }} presets={[1800, 3600, 86400]} />
        </div>
        <div className="mt-2 divide-y divide-line border-t border-line">
          <SwitchRow title={t("fw.scan")} desc={t("fw.scan.d")} checked={sd.enabled !== false} onChange={(v) => set({ scan_detect: { ...sd, enabled: v } })} />
          {sd.enabled !== false && <div className="grid grid-cols-2 gap-3 py-3">
            <NumberField label={t("fw.scan.thr")} value={sd.syn_threshold ?? 200} onValue={(v) => set({ scan_detect: { ...sd, syn_threshold: v ?? 200 } })} unit={t("fw.u.packets")} spec={{ min: 1, max: 100000, integer: true }} />
            <NumberField label={t("fw.ban.sec")} value={sd.ban_seconds ?? 3600} onValue={(v) => set({ scan_detect: { ...sd, ban_seconds: v ?? 3600 } })} unit={t("fw.u.sec")} spec={{ min: 60, max: 604800, integer: true }} />
          </div>}
          <SwitchRow title={t("fw.geo")} desc={t("fw.geo.d")} checked={c.geo_block === true} onChange={(v) => set({ geo_block: v })} />
          <SwitchRow title={t("fw.wanconf")} desc={t("fw.wanconf.d")} checked={c.wan_confirmed === true} onChange={(v) => set({ wan_confirmed: v })} />
        </div>
      </Card>

      <Card className="col-span-12 lg:col-span-6" title={t("fw.lists")} sub={t("fw.lists.d")}>
        <div className="flex flex-col gap-4">
          <Lines label={<Term k="subnet">{t("fw.list.allow")}</Term>} hint={t("fw.allow.h")} value={c.allow_cidrs || []} onChange={(v) => set({ allow_cidrs: v })} bad={badAllow} placeholder="203.0.113.0/24" />
          <Lines label={t("fw.list.deny")} hint={t("fw.deny.h")} value={c.deny_cidrs || []} onChange={(v) => set({ deny_cidrs: v })} bad={badDeny} placeholder="198.51.100.7" />
          {mode === "wan" && <TextField label={t("fw.dmz")} hint={t("fw.dmz.h")} value={c.dmz || ""} onChange={(v) => set({ dmz: v.trim() })} placeholder="192.168.2.50" mono error={badDmz ? t("n.ip.bad") : null} optional />}
        </div>
      </Card>

      <Card className="col-span-12" title={t("fw.rules")} sub={<>{t("fw.rules.d")}{mode === "lan" && <span className="mt-1 block text-warning">{t("fw.lan.note")}</span>}</>}
        actions={<Button size="sm" variant="primary" icon="plus" onClick={() => setDlg({ kind: "rule", idx: -1 })}>{t("fw.rule.add")}</Button>}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <label className="relative min-w-52 flex-1 sm:max-w-80"><span className="qm-sr">{t("fw.filter")}</span>
            <Icon name="search" size={16} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-fg-3" />
            <input type="search" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t("fw.filter")} className={clsx(controlClass, "ps-9")} /></label>
          {filter && <Button size="sm" variant="ghost" icon="reset" onClick={() => setFilter("")}>{t("f.clearall")}</Button>}
        </div>
        {!rules.length ? <Empty icon={filter ? "filter" : "shield"} title={filter ? t("pp.empty") : t("fw.rules.none")} action={filter ? <Button size="sm" onClick={() => setFilter("")}>{t("f.clearall")}</Button> : undefined} /> : (
          <ul className="divide-y divide-line">{rules.map(([r, i]) => (
            <li key={i} className="flex flex-wrap items-center gap-3 py-2.5">
              <Badge tone={r.action === "allow" ? "success" : "danger"}>{r.action === "allow" ? t("fw.allow") : t("fw.deny")}</Badge>
              <span className="min-w-0 flex-1"><b className="block text-[13px] text-fg">{r.name || `${t("fw.rule")} ${i + 1}`}</b><span className="qm-ltr font-mono text-[12px] text-fg-3">{r.chain} · {ruleStr(r)}</span></span>
              <Button size="sm" variant="ghost" icon="edit" onClick={() => setDlg({ kind: "rule", idx: i })}>{t("fw.edit")}</Button>
              <IconButton icon="x" tone="danger" label={`${t("fw.del")} ${r.name || i + 1}`} onClick={() => set({ rules: (c.rules || []).filter((_, j) => j !== i) })} />
            </li>))}</ul>
        )}
        {dirty && <p className="mt-2 text-[12px] text-fg-3">{t("fw.apply.note")}</p>}
      </Card>

      {(mode === "wan" || (c.services || []).length > 0 || (c.port_forwards || []).length > 0) && (
        <Card className="col-span-12" title={t("fw.fwds")} sub={t("fw.fwds.d")} actions={mode === "wan" && <Button size="sm" variant="primary" icon="plus" onClick={() => setDlg({ kind: "fwd", idx: -1 })}>{t("fw.fwd.add")}</Button>}>
          <h3 className="mb-1 text-[12.5px] font-semibold text-fg-2">{t("fw.services")}</h3>
          {(c.services || []).length ? <ul className="divide-y divide-line">{(c.services || []).map((sv, i) => (
            <li key={i} className="flex items-center gap-3 py-2"><Badge tone="success" icon={false}>OPEN</Badge><span className="min-w-0 flex-1 text-[13px] text-fg">{sv.name || `${t("fw.service")} ${i + 1}`} <span className="qm-ltr font-mono text-[12px] text-fg-3">{sv.protocol}/{sv.port}{sv.source && sv.source !== "0.0.0.0/0" ? " · " + sv.source : ""}</span></span>
              <IconButton icon="x" tone="danger" label={t("fw.del")} onClick={() => set({ services: (c.services || []).filter((_, j) => j !== i) })} /></li>))}</ul> : <p className="text-[12.5px] text-fg-3">{t("fw.services.none")}</p>}
          <h3 className="mb-1 mt-4 text-[12.5px] font-semibold text-fg-2">{t("fw.forwards")}</h3>
          {(c.port_forwards || []).length ? <ul className="divide-y divide-line">{(c.port_forwards || []).map((f, i) => (
            <li key={i} className="flex items-center gap-3 py-2"><Badge tone="info" icon={false}>FWD</Badge><span className="min-w-0 flex-1 text-[13px] text-fg">{f.name || `${t("fw.forward")} ${i + 1}`} <span className="qm-ltr font-mono text-[12px] text-fg-3">{f.protocol}/{f.source_port} → {f.target_ip}:{f.target_port}</span></span>
              <Button size="sm" variant="ghost" icon="edit" onClick={() => setDlg({ kind: "fwd", idx: i })}>{t("fw.edit")}</Button>
              <IconButton icon="x" tone="danger" label={t("fw.del")} onClick={() => set({ port_forwards: (c.port_forwards || []).filter((_, j) => j !== i) })} /></li>))}</ul> : <p className="text-[12.5px] text-fg-3">{t("fw.forwards.none")}</p>}
        </Card>
      )}

      <Card className="col-span-12 lg:col-span-6" title={t("fw.bans")} sub={t("fw.bans.d")}>
        <form className="flex items-start gap-2" onSubmit={(e) => { e.preventDefault(); if (!isIPv4(banIp)) { setBanErr(t("n.ip.bad")); return; }
          run("ban", async () => { await api.post("/api/firewall/ban", { ip: banIp.trim(), seconds: 1800, reason: "manual" }); setBanIp(""); await reload(); }, tf("fw.banned.n", { ip: banIp.trim() })); }}>
          <TextField className="flex-1" label={t("fw.ban")} labelHidden hint={!banErr ? t("fw.ban.h") : undefined} value={banIp} onChange={(v) => { setBanIp(v); setBanErr(null); }} placeholder="203.0.113.9" mono error={banErr} action={<Button type="submit" variant="danger" icon="ban" pending={pending === "ban"}>{t("fw.ban")}</Button>} />
        </form>
        {(s.bans || []).length ? <ul className="mt-2 divide-y divide-line">{(s.bans || []).map((b) => (
          <li key={b.ip} className="flex items-center gap-3 py-2"><Icon name="ban" size={15} className="text-danger" /><span className="min-w-0 flex-1"><b className="qm-ltr font-mono text-[13px] text-fg">{iso(b.ip)}</b><span className="block text-[12px] text-fg-3">{b.reason || ""}</span></span>
            <span className="text-[12px] text-fg-3 tabular-nums">{num(Math.ceil((b.remaining || 0) / 60), 0)} {t("fw.min.left")}</span>
            <Button size="sm" pending={pending === "ub" + b.ip} onClick={() => run("ub" + b.ip, async () => { await api.post("/api/firewall/unban", { ip: b.ip }); await reload(); }, t("fw.unbanned"))}>{t("fw.unban")}</Button></li>))}</ul>
          : <p className="mt-3 text-[12.5px] text-fg-3">{t("fw.bans.none")}</p>}
      </Card>

      <Card className="col-span-12 lg:col-span-6" title={t("fw.log")} sub={t("fw.log.d")}>
        {log.length ? <ul className="max-h-72 divide-y divide-line overflow-y-auto">{log.map((e, i) => (
          <li key={i} className="py-2"><p className="text-[13px] text-fg">{e.message}</p><span className="qm-ltr font-mono text-[11.5px] text-fg-3">{new Date(e.ts * 1000).toLocaleString("en-GB")} · {e.level || ""}</span></li>))}</ul>
          : <p className="text-[12.5px] text-fg-3">{filter ? t("pp.empty") : t("fw.log.none")}</p>}
      </Card>

      <Card className="col-span-12" title="HTTPS" sub={t("fw.https.d")} actions={<Badge tone={httpsOn ? "success" : "neutral"}>{httpsOn ? t("fw.https.on") : t("fw.https.off")}</Badge>}>
        {httpsOn ? <Button variant="danger" onClick={() => setConfirm("https-off")}>{t("fw.https.remove")}</Button> : <Button variant="primary" onClick={() => setConfirm("https-on")}>{t("fw.https.enable")}</Button>}
        <p className="mt-2 text-[12.5px] text-fg-3">{t("fw.https.note")}</p>
      </Card>

      {dlg?.kind === "rule" && <RuleDialog rule={dlg.idx >= 0 ? (c.rules || [])[dlg.idx] : undefined} onClose={() => setDlg(null)} onSave={(r) => { const list = [...(c.rules || [])]; if (dlg.idx >= 0) list[dlg.idx] = r; else list.push(r); set({ rules: list }); setDlg(null); }} />}
      {dlg?.kind === "fwd" && <FwdDialog fwd={dlg.idx >= 0 ? (c.port_forwards || [])[dlg.idx] : undefined} onClose={() => setDlg(null)} onSave={(f) => { const list = [...(c.port_forwards || [])]; if (dlg.idx >= 0) list[dlg.idx] = f; else list.push(f); set({ port_forwards: list }); setDlg(null); }} />}
      <ConfirmDialog open={confirm === "apply"} onOpenChange={(v) => !v && setConfirm(null)} title={t("fw.cf.apply.t")} confirmLabel={t("fw.apply")}
        consequences={{ changes: tf("fw.cf.apply.changes", { r: num((c.rules || []).length, 0), f: num((c.port_forwards || []).length, 0) }), affected: t("cf.everyone"), undo: t("fw.cf.apply.undo"), connection: t("fw.safe") }}
        onConfirm={async () => { if (!(await run("apply", apply, t("fw.applied"), t("fw.applied.sub")))) throw new Error(t("act.failed")); }} />
      <ConfirmDialog open={confirm === "revert"} onOpenChange={(v) => !v && setConfirm(null)} title={t("fw.cf.revert.t")} confirmLabel={t("fw.revert")}
        consequences={{ changes: t("fw.cf.revert.changes"), affected: t("cf.everyone"), undo: t("fw.cf.revert.undo") }}
        onConfirm={async () => { if (!(await run("revert", async () => { const r = await api.post<{ applied?: boolean }>("/api/firewall/revert"); if (!r.applied) throw new Error(t("fw.revert.fail")); setBase(""); await reload(); }, t("fw.reverted")))) throw new Error(t("act.failed")); }} />
      <ConfirmDialog open={confirm === "https-on"} onOpenChange={(v) => !v && setConfirm(null)} title={t("fw.https.enable")} confirmLabel={t("fw.https.enable")} danger={false}
        consequences={{ changes: t("fw.https.on.changes"), affected: t("fw.https.who"), undo: t("fw.https.on.undo"), connection: t("fw.https.conn") }}
        onConfirm={async () => { if (!(await run("https", async () => { await api.post("/api/security/enforce-https"); await reload(); }, t("fw.https.restart")))) throw new Error(t("act.failed")); }} />
      <ConfirmDialog open={confirm === "https-off"} onOpenChange={(v) => !v && setConfirm(null)} title={t("fw.https.remove")} confirmLabel={t("fw.https.remove")}
        consequences={{ changes: t("fw.https.off.changes"), affected: t("fw.https.who"), undo: t("fw.https.off.undo"), connection: t("fw.https.conn") }}
        onConfirm={async () => { if (!(await run("https", async () => { await api.post("/api/security/remove-https"); await reload(); }, t("fw.https.removed")))) throw new Error(t("act.failed")); }} />
    </div>
  );
}
function Lines({ label, hint, value, onChange, bad, placeholder }: { label: React.ReactNode; hint: string; value: string[]; onChange: (v: string[]) => void; bad: string[]; placeholder: string }) {
  const [txt, setTxt] = useState(value.join("\n"));
  useEffect(() => { if (txt.split("\n").map((x) => x.trim()).filter(Boolean).join("\n") !== value.join("\n")) setTxt(value.join("\n")); }, [value.join("\n")]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Field label={label} hint={hint} error={bad.length ? tf("fw.lines.bad", { x: bad.slice(0, 3).join(", ") }) : null}>
      {({ id, describedBy, invalid }) => <textarea id={id} aria-describedby={describedBy} aria-invalid={invalid} rows={4} dir="ltr" spellCheck={false} placeholder={placeholder} value={txt}
        onChange={(e) => { setTxt(e.target.value); onChange(e.target.value.split("\n").map((x) => x.trim()).filter(Boolean)); }}
        className={clsx(controlClass, "h-auto py-2 font-mono text-[12.5px]")} />}
    </Field>
  );
}
function RuleDialog({ rule, onClose, onSave }: { rule?: Rule; onClose: () => void; onSave: (r: Rule) => void }) {
  const r0 = rule || { name: "", chain: "forward", action: "deny", src: "", dst: "", protocol: "tcp", src_port: 0, dst_port: 0, log: true };
  const [name, setName] = useState(r0.name || ""), [action, setAction] = useState(r0.action), [chain, setChain] = useState(r0.chain), [proto, setProto] = useState(r0.protocol || "");
  const [src, setSrc] = useState(r0.src && r0.src !== "0.0.0.0/0" ? r0.src : ""), [dst, setDst] = useState(r0.dst && r0.dst !== "0.0.0.0/0" ? r0.dst : "");
  const [sp, setSp] = useState<number | null>(r0.src_port || 0), [dp, setDp] = useState<number | null>(r0.dst_port || 0), [tried, setTried] = useState(false);
  const srcErr = src && !isCidr4(src) ? t("fw.addr.bad") : null, dstErr = dst && !isCidr4(dst) ? t("fw.addr.bad") : null;
  const save = () => { setTried(true); if (srcErr || dstErr || sp == null || dp == null) return; onSave({ name: name.trim(), action, chain, src: src.trim() || "0.0.0.0/0", dst: dst.trim() || "0.0.0.0/0", protocol: proto, src_port: sp, dst_port: dp, log: true }); };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={rule ? t("fw.rule.edit") : t("fw.rule.add")} sub={t("fw.rule.sub")} wide
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={save}>{t("fw.keep")}</Button></>}>
      <div className="flex flex-col gap-4">
        <TextField label={t("fw.name")} value={name} onChange={setName} optional />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-fg-2">{t("fw.action")}</span><Segmented label={t("fw.action")} value={action} onChange={setAction} options={[{ value: "deny", label: t("fw.deny") }, { value: "allow", label: t("fw.allow") }]} /></div>
          <Select label={t("fw.chain")} hint={t("fw.chain.hint")} value={chain} onChange={setChain} options={[{ value: "forward", label: t("fw.chain.forward") }, { value: "input", label: t("fw.chain.input") }]} />
          <TextField label={t("fw.src")} hint={t("fw.any.h")} value={src} onChange={setSrc} placeholder="192.168.2.0/24" mono error={tried || src ? srcErr : null} />
          <TextField label={t("fw.dst")} hint={t("fw.any.h")} value={dst} onChange={setDst} placeholder="8.8.8.8" mono error={tried || dst ? dstErr : null} />
          <Select label={t("fw.proto")} value={proto} onChange={setProto} options={[{ value: "", label: t("fw.any") }, { value: "tcp", label: "TCP" }, { value: "udp", label: "UDP" }, { value: "icmp", label: "ICMP" }]} />
          <div />
          <NumberField label={t("fw.sport")} hint={t("fw.port.any")} value={sp} onValue={setSp} spec={{ min: 0, max: 65535, integer: true }} />
          <NumberField label={t("fw.dport")} hint={t("fw.port.any")} value={dp} onValue={setDp} spec={{ min: 0, max: 65535, integer: true }} presets={[22, 80, 443]} />
        </div>
      </div>
    </Dialog>
  );
}
function FwdDialog({ fwd, onClose, onSave }: { fwd?: Fwd; onClose: () => void; onSave: (f: Fwd) => void }) {
  const d = useDashboard();
  const f0 = fwd || { name: "", protocol: "tcp", source_port: 8080, target_ip: "", target_port: 8080 };
  const [name, setName] = useState(f0.name || ""), [proto, setProto] = useState(f0.protocol), [sp, setSp] = useState<number | null>(f0.source_port), [ip, setIp] = useState(f0.target_ip), [tp, setTp] = useState<number | null>(f0.target_port), [tried, setTried] = useState(false);
  const devs = useMemo(() => (d?.users || []).flatMap((u) => (u.devices || []).filter((x) => x.ip).map((x) => ({ value: x.ip, label: x.name || x.mac, sub: `${u.name} · ${x.ip}` }))), [d]);
  const ipErr = (tried || ip) && !isIPv4(ip) ? t("n.ip.bad") : null;
  const save = () => { setTried(true); if (ipErr || !isIPv4(ip) || !sp || !tp) return; onSave({ ...f0, name: name.trim(), protocol: proto, source_port: sp, target_ip: ip.trim(), target_port: tp }); };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={fwd ? t("fw.fwd.edit") : t("fw.fwd.add")} sub={t("fw.fwd.sub")} wide
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={save}>{t("fw.keep")}</Button></>}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <TextField label={t("fw.name")} value={name} onChange={setName} optional className="sm:col-span-2" />
        <div className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-fg-2">{t("fw.proto")}</span><Segmented label={t("fw.proto")} value={proto} onChange={setProto} options={[{ value: "tcp", label: "TCP" }, { value: "udp", label: "UDP" }]} /></div>
        <NumberField label={t("fw.fwd.sport")} value={sp} onValue={setSp} spec={{ min: 1, max: 65535, integer: true }} />
        {devs.length > 0 && <Combobox label={t("fw.fwd.pick")} value={devs.find((x) => x.value === ip)?.value || ""} onChange={setIp} options={devs} placeholder={t("fw.fwd.pick.ph")} className="sm:col-span-2" />}
        <TextField label={t("fw.fwd.ip")} value={ip} onChange={setIp} placeholder="192.168.2.50" mono error={ipErr} />
        <NumberField label={t("fw.fwd.tport")} value={tp} onValue={setTp} spec={{ min: 1, max: 65535, integer: true }} />
      </div>
    </Dialog>
  );
}

/* ================================================================ DNS & ad blocking */
interface Preset { id: string; name: string; description?: string; enabled: boolean; domain_count?: number; scope?: string; scope_id?: number | null }
interface DRule { id: number; scope: string; scope_id?: number | null; action: string; domain: string; target_ip?: string; source?: string; enabled?: boolean }
const TIERS = ["adblock-ultra", "ads-tracking"];
const tr = (k: string, fb: string) => { const v = t(k); return v === k ? fb : v; };
function targets(d?: Dashboard) {
  const users = (d?.users || []).filter((u) => !u.protected);
  return { user: users.map((u) => ({ value: String(u.id), label: u.name })), device: users.flatMap((u) => (u.devices || []).map((x) => ({ value: String(x.id), label: x.name || x.mac, sub: u.name }))) };
}
function scopeLabel(r: { scope?: string; scope_id?: number | null }, d?: Dashboard) {
  if (!r.scope || r.scope === "global") return t("dns.scope.global");
  const list = targets(d)[r.scope as "user" | "device"] || [], hit = list.find((x) => x.value === String(r.scope_id));
  return `${t("dns.scope." + r.scope)}: ${hit ? hit.label : "#" + r.scope_id}`;
}
const DOMAIN_RE = /^(\*\.)?([a-z0-9-]+\.)+[a-z]{2,}$/i;

function Dns() {
  const d = useDashboard();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["dns"], staleTime: 30_000, queryFn: async () => {
    const [presets, rules] = await Promise.all([api.get<Preset[]>("/api/dns/presets"), api.get<DRule[]>("/api/dns/rules")]); return { presets, rules };
  } });
  const { run, pending } = useAction();
  const [tierPick, setTierPick] = useState<string>("adblock-ultra");
  const [add, setAdd] = useState<null | string>(null);
  const [rf, setRf] = useState(""), [ra, setRa] = useState<"all" | "block" | "allow" | "redirect">("all");
  const [masterOff, setMasterOff] = useState(false);
  if (q.isError) return <Empty icon="alert" title={t("dns.fail")} sub={(q.error as Error).message} action={<Button size="sm" onClick={() => q.refetch()}>{t("conn.retry")}</Button>} />;
  if (!q.data) return <Loading what={t("dns.loading")} h={280} />;
  const { presets, rules } = q.data;
  const reload = () => qc.invalidateQueries({ queryKey: ["dns"] });
  const by = (id: string) => presets.find((p) => p.id === id);
  const ultra = by("adblock-ultra"), std = by("ads-tracking");
  const on = !!(ultra?.enabled || std?.enabled);
  const tier = ultra?.enabled && !std?.enabled ? "adblock-ultra" : std?.enabled && !ultra?.enabled ? "ads-tracking" : tierPick;
  const blocked = (ultra?.enabled ? ultra.domain_count || 0 : 0) + (std?.enabled ? std.domain_count || 0 : 0);
  const enable = (id: string) => api.post(`/api/dns/presets/${id}/enable`, { scope: "global" });
  const disable = (id: string) => api.post(`/api/dns/presets/${id}/disable`, { scope: "global" }).catch(() => {});
  const other = (id: string) => (id === "adblock-ultra" ? "ads-tracking" : "adblock-ultra");
  const turnOn = () => run("master", async () => { if (by(other(tier))?.enabled) await disable(other(tier)); await enable(tier); await reload(); }, t("dns.on.done"), t("dns.on.sub"));
  const pick = (id: string) => { setTierPick(id); if (on && !by(id)?.enabled) run("master", async () => { await disable(other(id)); await enable(id); await reload(); }, t("dns.tier.done")); };
  const cats = presets.filter((p) => !TIERS.includes(p.id));
  const fl = rf.trim().toLowerCase();
  const shown = rules.filter((r) => (ra === "all" || r.action === ra) && (!fl || r.domain.toLowerCase().includes(fl)));
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Card className="col-span-12" title={t("dns.shield")} sub={<>{t("dns.shield.d")} <Term k="dns" /></>}
        actions={<><Badge tone={on ? "success" : "neutral"}>{on ? t("dns.on") : t("dns.off")}</Badge><Switch checked={on} pending={pending === "master"} onChange={(v) => (v ? turnOn() : setMasterOff(true))} label={t("dns.shield")} /></>}>
        <KV items={[[t("dns.domains"), on ? num(blocked, 0) : "0"], [t("dns.mode"), on ? (ultra?.enabled ? t("dns.tier.ultra") : t("dns.tier.std")) : "—"]]} />
        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label={t("dns.mode")}>
          {[["adblock-ultra", t("dns.tier.ultra"), t("dns.tier.ultra.d"), t("dns.tag.pro")], ["ads-tracking", t("dns.tier.std"), t("dns.tier.std.d"), t("dns.tag.light")]].map(([id, name, desc, tag]) => (
            <button key={id} type="button" role="radio" aria-checked={tier === id} onClick={() => pick(id)}
              className={clsx("rounded-md border p-3 text-start transition-colors", tier === id ? "border-accent bg-accent-soft" : "border-line hover:bg-raised")}>
              <b className="flex items-center gap-2 text-[13.5px] text-fg">{name}<Badge tone={id === "adblock-ultra" ? "accent" : "neutral"} icon={false}>{tag}</Badge></b>
              <span className="mt-0.5 block text-[12.5px] text-fg-3">{desc}</span>
            </button>
          ))}
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button icon="bolt" pending={pending === "update"} pendingText={t("dns.updating")} disabled={!on} onClick={() => run("update", async () => { await enable(tier); await reload(); }, t("dns.updated"))}>{t("dns.update")}</Button>
          <Button icon="check" onClick={() => setAdd("allow")}>{t("dns.whitelist")}</Button>
        </div>
        <p className="mt-2 text-[12.5px] text-fg-3">{t("dns.update.h")}</p>
      </Card>

      <Card className="col-span-12" title={t("dns.cats")} sub={t("dns.cats.d")}>
        {!cats.length ? <p className="text-[12.5px] text-fg-3">{t("dns.cats.none")}</p> : (
          <div className="divide-y divide-line">{cats.map((p) => (
            <SwitchRow key={p.id} title={tr("dns.cat." + p.id + ".n", p.name)} desc={tr("dns.cat." + p.id + ".d", p.description || "")} checked={p.enabled} pending={pending === "p" + p.id}
              onChange={(v) => run("p" + p.id, async () => { await (v ? enable(p.id) : api.post(`/api/dns/presets/${p.id}/disable`, { scope: "global" })); await reload(); }, t(v ? "dns.cat.on" : "dns.cat.off"))}>
              {p.enabled && <p className="mt-1 text-[12px] text-fg-3">{num(p.domain_count, 0)} {t("dns.domains.n")} · {scopeLabel(p, d)}</p>}
            </SwitchRow>))}</div>
        )}
      </Card>

      <Card className="col-span-12" title={t("dns.rules")} sub={t("dns.rules.d")} actions={<Button size="sm" variant="primary" icon="plus" onClick={() => setAdd("block")}>{t("dns.rule.add")}</Button>}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <label className="relative min-w-52 flex-1 sm:max-w-80"><span className="qm-sr">{t("dns.filter")}</span>
            <Icon name="search" size={16} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-fg-3" />
            <input type="search" value={rf} onChange={(e) => setRf(e.target.value)} placeholder={t("dns.filter")} className={clsx(controlClass, "ps-9")} /></label>
          <Segmented size="sm" label={t("dns.act")} value={ra} onChange={setRa} options={(["all", "block", "allow", "redirect"] as const).map((a) => ({ value: a, label: a === "all" ? t("pp.f.all") : t("dns.act." + a), count: a === "all" ? rules.length : rules.filter((r) => r.action === a).length }))} />
          {(rf || ra !== "all") && <Button size="sm" variant="ghost" icon="reset" onClick={() => { setRf(""); setRa("all"); }}>{t("f.clearall")}</Button>}
        </div>
        {!shown.length ? <Empty icon={rules.length ? "filter" : "shield"} title={rules.length ? t("pp.empty") : t("dns.rules.none")} sub={rules.length ? undefined : t("dns.rules.none.sub")}
          action={rules.length ? <Button size="sm" onClick={() => { setRf(""); setRa("all"); }}>{t("f.clearall")}</Button> : <Button size="sm" icon="plus" onClick={() => setAdd("block")}>{t("dns.rule.add")}</Button>} /> : (
          <ul className="divide-y divide-line">{shown.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-3 py-2.5">
              <Badge tone={r.action === "allow" ? "success" : r.action === "redirect" ? "info" : "danger"}>{r.action === "redirect" ? `→ ${r.target_ip || ""}` : t("dns.act." + r.action)}</Badge>
              <b className="qm-ltr min-w-0 flex-1 truncate text-[13px] text-fg" title={r.domain}>{r.domain}</b>
              <span className="text-[12.5px] text-fg-3">{scopeLabel(r, d)}{r.source ? ` · ${r.source}` : ""}</span>
              <IconButton icon="x" tone="danger" label={tf("dns.rule.del", { d: r.domain })} onClick={() => run("r" + r.id, async () => { await api.del(`/api/dns/rules/${r.id}`); await reload(); }, tf("dns.rule.deleted", { d: r.domain }))} />
            </li>))}</ul>
        )}
      </Card>

      <ImportCard onDone={reload} />
      {add && <DnsRuleDialog action={add} onClose={() => setAdd(null)} onSaved={reload} />}
      <ConfirmDialog open={masterOff} onOpenChange={setMasterOff} title={t("dns.off.t")} confirmLabel={t("dns.off.cta")}
        consequences={{ changes: tf("dns.off.changes", { n: num(blocked, 0) }), affected: t("cf.everyone"), undo: t("dns.off.undo") }}
        onConfirm={async () => { if (!(await run("master", async () => { await disable("adblock-ultra"); await disable("ads-tracking"); await reload(); }, t("dns.off.done")))) throw new Error(t("act.failed")); }} />
    </div>
  );
}
function ScopePick({ scope, setScope, target, setTarget }: { scope: string; setScope: (s: string) => void; target: string; setTarget: (s: string) => void }) {
  const d = useDashboard(), tg = targets(d);
  return (
    <>
      <div className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-fg-2">{t("dns.scope")}</span>
        <Segmented label={t("dns.scope")} value={scope} onChange={(v) => { setScope(v); setTarget(""); }} options={["global", "user", "device"].map((s) => ({ value: s, label: t("dns.scope." + s) }))} /></div>
      {scope !== "global" && <Combobox label={t("dns.target")} value={target} onChange={setTarget} options={tg[scope as "user" | "device"]} />}
    </>
  );
}
function DnsRuleDialog({ action, onClose, onSaved }: { action: string; onClose: () => void; onSaved: () => void }) {
  const [act, setAct] = useState(action), [domain, setDomain] = useState(""), [ip, setIp] = useState(""), [scope, setScope] = useState("global"), [target, setTarget] = useState(""), [tried, setTried] = useState(false);
  const { run, pending } = useAction();
  const dErr = (tried || domain) && !DOMAIN_RE.test(domain.trim()) ? t("dns.domain.bad") : null, ipErr = act === "redirect" && (tried || ip) && !isIPv4(ip) ? t("n.ip.bad") : null, tErr = tried && scope !== "global" && !target ? t("f.required") : null;
  const save = async () => {
    setTried(true); if (!DOMAIN_RE.test(domain.trim()) || (act === "redirect" && !isIPv4(ip)) || (scope !== "global" && !target)) return;
    const body: Record<string, unknown> = { scope, scope_id: scope === "global" ? null : +target, action: act, domain: domain.trim().toLowerCase(), enabled: true };
    if (act === "redirect") body.target_ip = ip.trim();
    if (await run("add", () => api.post("/api/dns/rules", body), tf("dns.rule.added", { d: domain.trim() }))) { onSaved(); onClose(); }
  };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("dns.rule.add")} sub={t("dns.rule.sub")} busy={!!pending}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={save} pending={!!pending}>{t(act === "allow" ? "dns.cta.allow" : act === "redirect" ? "dns.cta.redirect" : "dns.cta.block")}</Button></>}>
      <div className="flex flex-col gap-4">
        <TextField label={t("dns.domain")} hint={t("dns.domain.h")} value={domain} onChange={setDomain} placeholder="ads.example.com" mono error={dErr} autoFocus />
        <div className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-fg-2">{t("dns.act")}</span>
          <Segmented label={t("dns.act")} value={act} onChange={setAct} options={["block", "allow", "redirect"].map((a) => ({ value: a, label: t("dns.act." + a) }))} /></div>
        {act === "redirect" && <TextField label={t("dns.redirect.ip")} value={ip} onChange={setIp} placeholder="192.168.2.1" mono error={ipErr} />}
        <ScopePick scope={scope} setScope={setScope} target={target} setTarget={setTarget} />
        {tErr && <p className="text-[12px] text-danger">{tErr}</p>}
      </div>
    </Dialog>
  );
}
function ImportCard({ onDone }: { onDone: () => void }) {
  const [text, setText] = useState(""), [fmt, setFmt] = useState("auto"), [act, setAct] = useState("block"), [scope, setScope] = useState("global"), [target, setTarget] = useState(""), [result, setResult] = useState("");
  const { run, pending } = useAction();
  const lines = text.split("\n").filter((x) => x.trim() && !x.trim().startsWith("#") && !x.trim().startsWith("!")).length;
  return (
    <Card className="col-span-12" title={t("dns.import")} sub={t("dns.import.d")}>
      <Field label={t("dns.import.text")} hint={lines ? tf("dns.import.lines", { n: num(lines, 0) }) : t("dns.import.h")}>
        {({ id, describedBy }) => <textarea id={id} aria-describedby={describedBy} rows={6} dir="ltr" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
          placeholder={"0.0.0.0 ads.example.com\n||tracker.example.net^"} className={clsx(controlClass, "h-auto py-2 font-mono text-[12.5px]")} />}
      </Field>
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Select label={t("dns.import.format")} value={fmt} onChange={setFmt} options={[{ value: "auto", label: t("dns.fmt.auto") }, { value: "hosts", label: "hosts" }, { value: "adblock", label: "AdBlock Plus" }]} />
        <div className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-fg-2">{t("dns.act")}</span><Segmented label={t("dns.act")} value={act} onChange={setAct} options={["block", "allow"].map((a) => ({ value: a, label: t("dns.act." + a) }))} /></div>
        <ScopePick scope={scope} setScope={setScope} target={target} setTarget={setTarget} />
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button variant="primary" disabled={!lines || (scope !== "global" && !target)} pending={pending === "imp"} pendingText={t("dns.importing")}
          onClick={() => run("imp", async () => {
            const res = await api.post<{ created: number; skipped: number }>("/api/dns/import", { text, format: fmt, scope, scope_id: scope === "global" ? null : +target, action: act });
            setText(""); setResult(t("dns.import.done").replace("{c}", String(res.created)).replace("{s}", String(res.skipped))); onDone();
          }, t("dns.import.ok"))}>{lines ? tf("dns.import.go.n", { n: num(lines, 0) }) : t("dns.import.go")}</Button>
        {result && <span className="text-[12.5px] text-fg-2" role="status">{result}</span>}
      </div>
    </Card>
  );
}
