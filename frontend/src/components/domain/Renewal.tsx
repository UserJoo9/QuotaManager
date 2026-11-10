import { Segmented, Select } from "../ui/forms";
import { t, tf } from "../../lib/i18n";

// The backend stores (period_type, reset_day). The admin sees a mode + a day, never a bare "0".
export type RenewMode = "day" | "monthEnd" | "manual" | "billDay";
export interface Renewal { mode: RenewMode; day: number }
export function toRenewal(period_type: string | undefined, reset_day: number | undefined): Renewal {
  const d = reset_day || 0;
  if (period_type === "end_of_month") return d > 0 ? { mode: "billDay", day: d } : { mode: "monthEnd", day: 1 };
  return d > 0 ? { mode: "day", day: d } : { mode: "manual", day: 1 };
}
export function fromRenewal(r: Renewal): { period_type: "renew_day" | "end_of_month"; reset_day: number } {
  switch (r.mode) {
    case "day": return { period_type: "renew_day", reset_day: r.day };
    case "billDay": return { period_type: "end_of_month", reset_day: r.day };
    case "monthEnd": return { period_type: "end_of_month", reset_day: 0 };
    default: return { period_type: "renew_day", reset_day: 0 };
  }
}
const ordinal = (n: number) => tf("rn.dayn", { n });
export function RenewalPicker({ value, onChange }: { value: Renewal; onChange: (r: Renewal) => void }) {
  const opts: { value: RenewMode; label: string }[] = [
    { value: "day", label: t("rn.mode.day") }, { value: "monthEnd", label: t("rn.mode.monthEnd") }, { value: "manual", label: t("rn.mode.manual") },
  ];
  if (value.mode === "billDay") opts.push({ value: "billDay", label: t("rn.mode.billDay") });
  const needsDay = value.mode === "day" || value.mode === "billDay";
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <span className="text-[12.5px] font-medium text-fg-2" id="rn-l">{t("rn.label")}</span>
        <Segmented label={t("rn.label")} value={value.mode} onChange={(m) => onChange({ ...value, mode: m })} options={opts} />
        <p className="text-[12px] text-fg-3">{t("rn.help." + value.mode)}</p>
      </div>
      {needsDay && (
        <Select label={t("rn.daylabel")} value={String(value.day)} onChange={(v) => onChange({ ...value, day: +v })}
          hint={value.day >= 29 ? t("rn.short") : undefined}
          options={Array.from({ length: 31 }, (_, i) => ({ value: String(i + 1), label: ordinal(i + 1) }))} className="max-w-56" />
      )}
    </div>
  );
}
