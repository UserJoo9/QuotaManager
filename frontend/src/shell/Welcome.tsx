import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { useSession } from "../lib/session";
import { Dialog, useToast } from "../components/ui/overlays";
import { Button } from "../components/ui/primitives";
import { NumberField, TextField } from "../components/ui/forms";
import { RenewalPicker, toRenewal, fromRenewal, type Renewal } from "../components/domain/Renewal";

interface Setup { setup_complete: boolean; total_gb: number; reset_day: number; period_type: string }
let skipped = false;
/** First run, one step at a time: 1) the monthly bundle, 2) optionally a new password. */
export function Welcome() {
  const { refresh } = useSession();
  const toast = useToast();
  const [st, setSt] = useState<Setup | null>(null), [open, setOpen] = useState(false), [step, setStep] = useState(1);
  const [total, setTotal] = useState<number | null>(null), [renew, setRenew] = useState<Renewal>({ mode: "day", day: 1 });
  const [cur, setCur] = useState(""), [nw, setNw] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  useEffect(() => {
    if (skipped) return;
    api.get<Setup>("/api/setup").then((s) => { if (s && !s.setup_complete) { setSt(s); setTotal(s.total_gb); setRenew(toRenewal(s.period_type, s.reset_day)); setOpen(true); } }).catch(() => {});
  }, []);
  if (!st) return null;
  const finish = async () => {
    setErr(null);
    if (!(total && total > 0)) { setStep(1); setErr(t("welcome.bad")); return; }
    if (nw && nw.length < 12) { setErr(t("acct.pw.short")); return; }
    setBusy(true);
    try {
      await api.post("/api/setup/complete", { total_gb: total, ...fromRenewal(renew), current_password: cur, new_password: nw || null });
      await refresh(); toast({ kind: "success", title: t("welcome.done"), sub: t("welcome.done.sub") }); setOpen(false);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) { skipped = true; } setOpen(v); }} title={t("welcome.title")} sub={`${t("welcome.step")} ${step} / 2`} busy={busy}
      footer={<>
        <Button variant="ghost" onClick={() => { skipped = true; setOpen(false); }}>{t("welcome.skip")}</Button>
        {step === 2 && <Button onClick={() => setStep(1)}>{t("common.back")}</Button>}
        {step === 1 ? <Button variant="primary" onClick={() => { if (total && total > 0) { setErr(null); setStep(2); } else setErr(t("welcome.bad")); }}>{t("common.next")}</Button>
          : <Button variant="primary" onClick={finish} pending={busy} pendingText={t("act.saving")}>{t("welcome.save")}</Button>}
      </>}>
      {step === 1 ? (
        <div className="flex flex-col gap-4">
          <p className="text-[13px] text-fg-2">{t("welcome.s1")}</p>
          <NumberField label={t("q.total")} hint={t("q.total.hint")} value={total} onValue={setTotal} unit={t("common.gb")} spec={{ min: 1, max: 100000, decimals: 2 }} stepper presets={[100, 140, 200, 500]} />
          <RenewalPicker value={renew} onChange={setRenew} />
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <p className="text-[13px] text-fg-2">{t("welcome.s2")}</p>
          <TextField label={t("welcome.cur")} hint={t("welcome.pw.h")} type="password" autoComplete="current-password" value={cur} onChange={setCur} />
          <TextField label={t("welcome.new")} hint={t("acct.pw.hint")} type="password" autoComplete="new-password" value={nw} onChange={setNw} minLength={12} optional />
        </div>
      )}
      {err && <p role="alert" className="mt-3 text-[13px] text-danger">{err}</p>}
    </Dialog>
  );
}
