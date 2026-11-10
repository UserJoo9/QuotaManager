import { useState } from "react";
import { api } from "../../lib/api";
import { t, tf, num } from "../../lib/i18n";
import { useAction } from "../../lib/actions";
import { Dialog } from "../ui/overlays";
import { Button, Banner } from "../ui/primitives";
import { NumberField } from "../ui/forms";

/** Add GB to the shared bundle: exact amount, common presets, and a plain warning about what it does. */
export function TopUpDialog({ open, onOpenChange, initial }: { open: boolean; onOpenChange: (v: boolean) => void; initial?: number }) {
  const [gb, setGb] = useState<number | null>(initial ?? null);
  const [confirm, setConfirm] = useState(false);
  const { run, pending } = useAction();
  const close = (v: boolean) => { if (!v) { setConfirm(false); } onOpenChange(v); };
  const go = async () => { if (await run("topup", () => api.post("/api/bundle", { add_gb: gb }), tf("tu.done", { n: num(gb, 2) }), t("tu.done.sub"))) { setGb(null); close(false); } };
  return (
    <Dialog open={open} onOpenChange={close} title={t("tu.title")} sub={t("tu.sub")} busy={!!pending}
      footer={<><Button onClick={() => close(false)}>{t("common.cancel")}</Button>
        {!confirm ? <Button variant="primary" disabled={!(gb && gb > 0)} onClick={() => setConfirm(true)}>{t("common.next")}</Button>
          : <Button variant="primary" onClick={go} pending={!!pending} pendingText={t("act.saving")}>{tf("tu.cta", { n: num(gb, 2) })}</Button>}</>}>
      <NumberField label={t("tu.amount")} hint={t("tu.hint")} value={gb} onValue={(v) => { setGb(v); setConfirm(false); }} unit={t("common.gb")} spec={{ min: 0.01, max: 100000, decimals: 2 }} presets={[10, 25, 50, 100]} />
      {confirm && <Banner tone="info" className="mt-4" title={tf("tu.confirm", { n: num(gb, 2) })}>{t("ov.tip.note")}</Banner>}
    </Dialog>
  );
}
