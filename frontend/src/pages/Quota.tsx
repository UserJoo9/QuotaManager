import { useEffect, useMemo, useState } from "react";
import { useDashboard } from "../lib/session";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { t, tf, num, fmtDate } from "../lib/i18n";
import { gb } from "../lib/format";
import type { Recharge } from "../lib/types";
import { Badge, Button, Card, Empty, Loading, Meter, Term, IconButton } from "../components/ui/primitives";
import { NumberField, SwitchRow, Segmented, Combobox, TextField } from "../components/ui/forms";
import { ConfirmDialog, Dialog } from "../components/ui/overlays";
import { RenewalPicker, toRenewal, fromRenewal, type Renewal } from "../components/domain/Renewal";
import { TopUpDialog } from "../components/domain/TopUp";
import { Icon } from "../components/ui/Icon";
import { PageActions } from "../shell/App";

const DAY = 86400;
export default function Quota() {
  const d = useDashboard();
  const { run, pending } = useAction();
  const [total, setTotal] = useState<number | null>(null), [renew, setRenew] = useState<Renewal>({ mode: "day", day: 1 });
  const [base, setBase] = useState<string>("");
  const [topup, setTopup] = useState(false), [reset, setReset] = useState(false), [pack, setPack] = useState(false), [del, setDel] = useState<Recharge | null>(null);
  const b = d?.bundle;
  const server = b ? JSON.stringify({ total: b.base_total_gb ?? b.total_gb, r: toRenewal(b.period_type, b.reset_day) }) : "";
  const dirty = !!b && JSON.stringify({ total, r: renew }) !== base;
  // only follow the server while the form is untouched
  useEffect(() => { if (b && (!base || !dirty)) { setTotal(b.base_total_gb ?? b.total_gb); setRenew(toRenewal(b.period_type, b.reset_day)); setBase(server); } }, [server]); // eslint-disable-line react-hooks/exhaustive-deps
  const users = d?.users || [];
  const who = useMemo(() => (p: Recharge) => p.target_type === "user" ? users.find((u) => u.id === p.target_id)?.name || "#" + p.target_id
    : p.target_type === "device" ? users.flatMap((u) => u.devices || []).find((x) => x.id === p.target_id)?.name || "#" + p.target_id : t("q.pack.all"), [users]);
  if (!d || !b) return <Loading what={t("boot.reading")} h={300} />;
  const pct = b.total_gb > 0 ? (b.used_gb / b.total_gb) * 100 : 0;
  const save = () => run("bundle", async () => { await api.post("/api/bundle", { total_gb: total, ...fromRenewal(renew) }); setBase(JSON.stringify({ total, r: renew })); }, t("q.saved"), t("q.saved.sub"));
  const packs = b.recharges || [];
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <PageActions><Button size="sm" variant="primary" icon="plus" onClick={() => setTopup(true)}>{t("tu.title")}</Button></PageActions>
      <Card className="col-span-12 lg:col-span-7" title={t("q.bundle")} sub={t("q.bundle.sub")}
        actions={<Badge icon={false}>{b.days_left >= 0 ? tf("ov.resets.in", { n: num(b.days_left, 0) }) : t("ov.period.manual")}</Badge>}>
        <div className="flex items-baseline justify-between text-[13px] tabular-nums"><b className="text-fg">{tf("ov.used.of", { u: num(b.used_gb), t: num(b.total_gb) })}</b><span className="text-fg-3">{num(pct, 0)}%</span></div>
        <Meter value={b.used_gb} max={b.total_gb || 1} className="mt-1.5" label={t("q.bundle")} />
        {b.total_gb !== b.base_total_gb && <p className="mt-1.5 text-[12px] text-fg-3">{tf("q.base.note", { b: num(b.base_total_gb), x: num(b.total_gb - b.base_total_gb) })}</p>}
        <div className="mt-5 flex flex-col gap-4 border-t border-line pt-5">
          <NumberField label={t("q.total")} hint={t("q.total.hint")} value={total} onValue={setTotal} unit={t("common.gb")} spec={{ min: 1, max: 100000, decimals: 2 }} stepper presets={[100, 140, 200, 500]} />
          <RenewalPicker value={renew} onChange={setRenew} />
          {b.period_start && <p className="flex items-center gap-2 text-[12.5px] text-fg-3"><Icon name="calendar" size={14} />{tf("q.period.now", { s: fmtDate(b.period_start), e: b.period_end ? fmtDate(b.period_end) : t("q.period.open") })}</p>}
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={save} disabled={!dirty || !(total && total > 0)} pending={pending === "bundle"} pendingText={t("act.saving")}>{t("q.save")}</Button>
            {dirty && <Button variant="ghost" onClick={() => { const s = JSON.parse(base); setTotal(s.total); setRenew(s.r); }}>{t("sheet.revert")}</Button>}
            {dirty && <span className="text-[12px] text-fg-3">{t("sheet.pending")}</span>}
          </div>
        </div>
      </Card>

      <div className="col-span-12 flex flex-col gap-[var(--gap)] lg:col-span-5">
        <Card title={t("q.topup")} sub={t("q.topup.d")}>
          <Button variant="primary" icon="plus" onClick={() => setTopup(true)}>{t("tu.title")}</Button>
        </Card>
        <Card title={t("q.newperiod")} sub={t("q.newperiod.d")}>
          <Button variant="danger" icon="reset" onClick={() => setReset(true)}>{t("q.newperiod.btn")}</Button>
        </Card>
        <Card title={t("fm.title")} sub={t("fm.d")}>
          {d.free_mode && <p role="status" className="mb-3 text-[13px] text-warning">{t("fm.active")}. {t("fm.active.d")}</p>}
          <SwitchRow title={t("fm.sw")} desc={t("fm.sw.d")} checked={d.free_mode_enabled !== false} pending={pending === "free"}
            onChange={(v) => run("free", () => api.post("/api/free-mode", { enabled: v }))} />
        </Card>
        <Card title={<Term k="daily">{t("dq.master")}</Term>} sub={t("dq.master.d")}>
          <SwitchRow title={t("dq.master.sw")} desc={t("dq.master.sw.d")} checked={!!d.daily_enabled} pending={pending === "daily"}
            onChange={(v) => run("daily", () => api.post("/api/daily", { enabled: v }), v ? t("dq.master.on") : t("dq.master.off"))} />
        </Card>
      </div>

      <Card className="col-span-12" title={t("q.packs")} sub={t("q.packs.sub")} actions={<Button size="sm" variant="primary" icon="plus" onClick={() => setPack(true)}>{t("q.packs.new")}</Button>}>
        {!packs.length ? <Empty icon="bolt" title={t("q.packs.none")} sub={t("q.packs.none.sub")} action={<Button size="sm" icon="plus" onClick={() => setPack(true)}>{t("q.packs.new")}</Button>} /> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-[13px]">
              <thead><tr className="border-b border-line text-start text-[12px] text-fg-3">
                <th className="py-2 text-start font-semibold">{t("q.pack.col.pack")}</th><th className="py-2 text-start font-semibold">{t("q.pack.col.for")}</th>
                <th className="py-2 text-end font-semibold">{t("q.pack.col.left")}</th><th className="py-2 text-end font-semibold">{t("q.pack.expires")}</th><th className="py-2" /></tr></thead>
              <tbody className="divide-y divide-line">
                {packs.map((p) => { const dl = Math.ceil((p.expires_at * 1000 - Date.now()) / 86400000); return (
                  <tr key={p.id}>
                    <td className="py-2.5"><b className="text-fg tabular-nums">{gb(p.gb)}</b>{p.recurring && <Badge tone="accent" icon="reset" className="ms-2">{t("q.pack.recur")}</Badge>}{p.comment && <span className="block text-[12px] text-fg-3">{p.comment}</span>}</td>
                    <td className="py-2.5 text-fg-2">{who(p)}</td>
                    <td className="py-2.5 text-end tabular-nums"><b className="text-fg">{gb(p.remaining_gb)}</b><Meter value={p.used_gb} max={p.gb || 1} neutral thin className="ms-auto mt-1 max-w-28" label={t("q.pack.col.left")} /></td>
                    <td className="py-2.5 text-end text-fg-2 tabular-nums">{dl > 0 ? <>{fmtDate(p.expires_at * 1000)}<span className="block text-[12px] text-fg-3">{tf("q.pack.inN", { n: num(dl, 0) })}</span></> : t("q.pack.expired")}</td>
                    <td className="py-2.5 text-end"><IconButton icon="x" tone="danger" label={tf("q.pack.del.n", { n: gb(p.gb) })} onClick={() => setDel(p)} /></td>
                  </tr>
                ); })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <TopUpDialog open={topup} onOpenChange={setTopup} />
      <ConfirmDialog open={reset} onOpenChange={setReset} title={t("cf.reset.t")} confirmLabel={t("cf.reset.cta")}
        consequences={{ changes: t("cf.reset.changes"), affected: tf("cf.reset.affected", { n: num(users.filter((u) => !u.protected).length, 0) }), undo: t("cf.reset.undo") }}
        onConfirm={async () => { if (!(await run("reset", () => api.post("/api/reset-month"), t("cf.reset.done")))) throw new Error(t("act.failed")); }} />
      <ConfirmDialog open={!!del} onOpenChange={(v) => !v && setDel(null)} title={tf("q.pack.del.t", { n: del ? gb(del.gb) : "" })} confirmLabel={t("q.pack.del")}
        consequences={{ changes: tf("q.pack.del.changes", { n: del ? gb(del.remaining_gb) : "" }), affected: del ? who(del) : "", undo: t("q.pack.del.undo") }}
        onConfirm={async () => { if (del && !(await run("del", () => api.del(`/api/bundle/recharges/${del.id}`), t("q.pack.deleted")))) throw new Error(t("act.failed")); }} />
      <PackDialog open={pack} onOpenChange={setPack} />
    </div>
  );
}

function PackDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const d = useDashboard();
  const users = d?.users || [];
  const [g, setG] = useState<number | null>(null), [days, setDays] = useState<number | null>(30), [rec, setRec] = useState(false);
  const [target, setTarget] = useState<"all" | "user" | "device">("all"), [tid, setTid] = useState(""), [note, setNote] = useState(""), [tried, setTried] = useState(false);
  const { run, pending } = useAction();
  useEffect(() => { setTid(""); }, [target]);
  const opts = target === "user" ? users.map((u) => ({ value: String(u.id), label: u.name, sub: tf("pp.devs.n", { n: num((u.devices || []).length, 0) }) }))
    : users.flatMap((u) => (u.devices || []).map((x) => ({ value: String(x.id), label: x.name || x.mac, sub: u.name })));
  const expiry = days && days > 0 ? new Date(Date.now() + days * DAY * 1000) : null;
  const submit = async () => {
    setTried(true);
    if (!(g && g > 0) || !(days && days >= 1) || (target !== "all" && !tid)) return;
    const body: Record<string, unknown> = { gb: g, expires_at: Math.floor(Date.now() / 1000) + days * DAY, recurring: rec, target_type: target, comment: note.trim() };
    if (target !== "all") body.target_id = +tid;
    if (await run("pack", () => api.post("/api/bundle/recharges", body), t("q.pack.created"))) { setG(null); setNote(""); setTried(false); onOpenChange(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("q.pack.title")} sub={t("q.pack.sub")} busy={!!pending}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="primary" onClick={submit} pending={!!pending} pendingText={t("act.saving")}>{g ? tf("q.pack.create.n", { n: num(g, 2) }) : t("q.pack.create")}</Button></>}>
      <div className="flex flex-col gap-4">
        <NumberField label={t("q.pack.gb")} value={g} onValue={setG} unit={t("common.gb")} spec={{ min: 0.01, max: 100000, decimals: 2, allowEmpty: true }} presets={[5, 10, 25, 50]} error={tried && !(g && g > 0) ? t("f.required") : null} />
        <NumberField label={t("q.pack.days")} hint={expiry ? tf("q.pack.expiry", { d: fmtDate(expiry, { weekday: "short", month: "short", day: "numeric", year: "numeric" }) }) : undefined}
          value={days} onValue={setDays} unit={t("common.days")} spec={{ min: 1, max: 3650, integer: true }} stepper presets={[7, 30, 90]} />
        <div className="flex flex-col gap-1.5">
          <span className="text-[12.5px] font-medium text-fg-2">{t("q.pack.target")}</span>
          <Segmented label={t("q.pack.target")} value={target} onChange={setTarget} options={[{ value: "all", label: t("q.pack.all") }, { value: "user", label: t("q.pack.user") }, { value: "device", label: t("q.pack.device") }]} />
          <p className="text-[12px] text-fg-3">{t("q.pack.target." + target)}</p>
        </div>
        {target !== "all" && <Combobox label={t("q.pack.who")} value={tid} onChange={setTid} options={opts} error={tried && !tid ? t("f.required") : null} />}
        <SwitchRow title={t("q.pack.recurring")} desc={t("q.pack.recurring.d")} checked={rec} onChange={setRec} />
        <TextField label={t("q.pack.note")} value={note} onChange={setNote} optional />
      </div>
    </Dialog>
  );
}
