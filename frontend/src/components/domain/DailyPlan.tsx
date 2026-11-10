import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { api } from "../../lib/api";
import { actions, useAction } from "../../lib/actions";
import { t, tf, num, fmtDate, fmtTime } from "../../lib/i18n";
import type { User, Dashboard, Device } from "../../lib/types";
import { Badge, Banner, Button, Meter } from "../ui/primitives";
import { NumberField, Segmented, Select, SwitchRow, Field, controlClass } from "../ui/forms";

interface Preview { cap_gb?: number; used_gb?: number; schedule?: { date: string; cap_gb: number; heavy?: boolean }[]; error?: string }
// Unsaved heavy-day edits and the last preview survive the sheet's live updates (module scope, per person).
const heavyDraft = new Map<number, Record<string, number>>();
const previews = new Map<number, Preview>();

export function DailyToday({ u }: { u: User }) {
  const q = u.daily; if (!q || !q.active) return null;
  return (
    <div className="rounded-md border border-line bg-canvas p-3">
      <div className="flex justify-between text-[12.5px] tabular-nums"><b className="text-fg">{tf("dq.today.of", { u: num(q.used_gb), c: num(q.cap_gb) })}</b><span className="text-fg-3">{t("dq.today")}</span></div>
      <Meter value={q.used_gb || 0} max={q.cap_gb || 1} className="mt-1.5" label={t("dq.today")} />
      {q.blocked && <p className="mt-1.5 text-[12px] text-warning">{t("dq.reached")} · {t("dq.back")} {q.resets_at ? fmtTime(q.resets_at) : ""}</p>}
    </div>
  );
}

export function DailySection({ u, dash, onDirty }: { u: User; dash: Dashboard; onDirty: (v: boolean) => void }) {
  const q = u.daily || { mode: "off", gb: 0, carry: "spread", split: "open", heavy: {}, self_edit: false, active: false } as NonNullable<User["daily"]>;
  const hasEnd = !!dash.bundle?.period_end;
  const [mode, setMode] = useState(q.mode), [dgb, setDgb] = useState<number | null>(q.gb || 0), [carry, setCarry] = useState(q.carry), [split, setSplit] = useState(q.split);
  if (!heavyDraft.has(u.id)) heavyDraft.set(u.id, { ...(q.heavy || {}) });
  const [heavy, setHeavyState] = useState<Record<string, number>>(heavyDraft.get(u.id)!);
  const setHeavy = (h: Record<string, number>) => { heavyDraft.set(u.id, h); setHeavyState(h); };
  const [day, setDay] = useState(""), [dayGb, setDayGb] = useState<number | null>(null);
  const [prev, setPrev] = useState<Preview | undefined>(previews.get(u.id));
  const { run, pending } = useAction();
  const dirty = mode !== q.mode || (mode === "fixed" && dgb !== q.gb) || carry !== q.carry || split !== q.split || JSON.stringify(heavy) !== JSON.stringify(q.heavy || {});
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
  const today = useMemo(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }, []);

  const head = <h3 className="text-[13.5px] font-semibold text-fg">{t("dq.title")}</h3>;
  if (u.protected) return null;
  if (u.exempt_quota) return <section className="border-b border-line py-4">{head}<p className="mt-1 text-[12.5px] text-fg-3">{t("dq.exempt")}</p></section>;
  if (!dash.daily_enabled) return <section className="border-b border-line py-4">{head}<p className="mt-1 text-[12.5px] text-fg-3">{t("dq.disabled")} <a href="#/quota" className="font-semibold text-accent-fg">{t("dq.enable.cta")}</a></p></section>;

  const form = () => ({ daily_mode: mode, daily_gb: mode === "fixed" ? Math.max(0, dgb || 0) : q.gb || 0, daily_carry: carry, daily_split: split, daily_heavy: { ...heavy } });
  const preview = async () => {
    const f = form();
    if (f.daily_mode === "off") { previews.delete(u.id); setPrev(undefined); return; }
    try {
      const p = await api.post<Preview>(`/api/users/${u.id}/daily-plan/preview`, { mode: f.daily_mode, daily_gb: f.daily_gb, carry: f.daily_carry, split: f.daily_split, heavy: f.daily_heavy });
      previews.set(u.id, p); setPrev(p);
    } catch (e) { setPrev({ error: (e as Error).message }); }
  };
  const save = () => run("daily", async () => { await actions.patchUser(u.id, form()); heavyDraft.delete(u.id); previews.delete(u.id); }, t("dq.saved"));
  const addHeavy = () => { if (!day || !(dayGb && dayGb > 0)) return; setHeavy({ ...heavy, [day]: dayGb }); setDay(""); setDayGb(null); };
  return (
    <section className="flex flex-col gap-3 border-b border-line py-4" aria-label={t("dq.title")}>
      <div className="flex items-center justify-between">{head}{dirty && <Badge tone="warning" icon={false}>{t("sheet.unsaved")}</Badge>}</div>
      <DailyToday u={u} />
      <Segmented label={t("dq.mode")} value={mode} onChange={setMode} options={[{ value: "off", label: t("dq.mode.off") }, { value: "fixed", label: t("dq.mode.fixed") }, { value: "spread", label: hasEnd ? t("dq.mode.spread") : `${t("dq.mode.spread")} (${t("dq.mode.spread.need")})`, disabled: !hasEnd }]} />
      <p className="text-[12px] text-fg-3">{t("dq.mode." + mode + ".d")}{!hasEnd ? ` ${t("dq.mode.spread.no")}` : ""}</p>
      {mode !== "off" && <>
        {mode === "fixed" && <NumberField label={t("dq.gb")} value={dgb} onValue={setDgb} unit={t("common.gb")} spec={{ min: 0, max: 100000, decimals: 2 }} stepper />}
        <Select label={t("dq.carry")} value={carry} onChange={setCarry} options={[{ value: "next_day", label: t("dq.carry.next_day") }, { value: "spread", label: t("dq.carry.spread") }]} />
        <Select label={t("dq.split")} hint={split === "fixed" ? t("dq.split.fixed.hint") : undefined} value={split} onChange={setSplit} options={(["open", "equal", "fixed"] as const).map((v) => ({ value: v, label: t("dq.split." + v) }))} />
        <Field label={t("dq.heavy")} hint={t("dq.heavy.hint")}>
          {({ id }) => (
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap gap-1.5">
                {Object.keys(heavy).sort().map((k) => (
                  <span key={k} className="inline-flex h-7 items-center gap-1 rounded-full border border-line bg-canvas ps-2.5 text-[12px] text-fg">
                    {fmtDate(k)} · {num(heavy[k])} {t("common.gb")}
                    <button type="button" aria-label={`${t("dq.heavy.del")} ${fmtDate(k)}`} className="grid size-7 place-items-center rounded-full text-fg-3 hover:text-danger" onClick={() => { const h = { ...heavy }; delete h[k]; setHeavy(h); }}>×</button>
                  </span>
                ))}
                {!Object.keys(heavy).length && <span className="text-[12px] text-fg-3">{t("dq.heavy.none")}</span>}
              </div>
              <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-start gap-2">
                <input id={id} type="date" min={today} value={day} onChange={(e) => setDay(e.target.value)} aria-label={t("dq.heavy.date")} className={clsx(controlClass)} />
                <NumberField label={t("dq.heavy.gb")} labelHidden ariaLabel={t("dq.heavy.gb")} value={dayGb} onValue={setDayGb} unit={t("common.gb")} spec={{ min: 0.01, max: 100000, decimals: 2, allowEmpty: true }} />
                <Button onClick={addHeavy} disabled={!day || !(dayGb && dayGb > 0)}>{t("dq.heavy.add")}</Button>
              </div>
            </div>
          )}
        </Field>
      </>}
      <SwitchRow title={t("dq.selfedit")} desc={t("dq.selfedit.sub")} checked={q.self_edit} pending={pending === "self"} onChange={(v) => run("self", () => actions.patchUser(u.id, { daily_self_edit: v }), t("dr.saved"))} />
      {prev?.error && <Banner tone="warning">{prev.error}</Banner>}
      {prev && !prev.error && prev.schedule && prev.schedule.length > 0 && (
        <div className="rounded-md border border-line bg-canvas p-3">
          <div className="mb-2 flex gap-4 text-[12.5px]"><span className="text-fg-3">{t("dq.prev.today")} <b className="text-fg">{num(prev.cap_gb)} {t("common.gb")}</b></span><span className="text-fg-3">{t("dq.prev.used")} <b className="text-fg">{num(prev.used_gb)} {t("common.gb")}</b></span></div>
          <div className="flex flex-wrap gap-1.5">{prev.schedule.slice(0, 8).map((r) => <Badge key={r.date} tone={r.heavy ? "accent" : "neutral"} icon={false}>{fmtDate(r.date)} · {num(r.cap_gb)}</Badge>)}</div>
          {prev.schedule.length > 8 && <p className="mt-1 text-[12px] text-fg-3">+{num(prev.schedule.length - 8, 0)} {t("common.days")}</p>}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="ghost" onClick={preview} disabled={mode === "off"}>{t("dq.preview")}</Button>
        <Button size="sm" variant="primary" onClick={save} disabled={!dirty} pending={pending === "daily"} pendingText={t("act.saving")}>{t("dq.save")}</Button>
      </div>
    </section>
  );
}

/** Fixed-share GB of one device (only meaningful when its owner's split is "fixed"). */
export function DeviceShare({ x, u }: { x: Device; u: User }) {
  const [v, setV] = useState<number | null>(x.daily_share_gb || 0);
  const { run, pending } = useAction();
  if (!u.daily || u.daily.mode === "off" || u.daily.split !== "fixed") return null;
  return (
    <section className="flex flex-col gap-3 border-b border-line py-4">
      <h3 className="text-[13.5px] font-semibold text-fg">{t("dq.title")}</h3>
      <NumberField label={t("dq.share")} hint={t("dq.share.hint")} value={v} onValue={setV} unit={t("common.gb")} spec={{ min: 0, max: 100000, decimals: 2 }} />
      {x.daily_cap_gb != null && <p className="text-[12px] text-fg-3">{t("dq.today")}: {num(x.daily_used_gb || 0)} {t("ov.of")} {num(x.daily_cap_gb)} {t("common.gb")}</p>}
      <div><Button size="sm" variant="primary" disabled={v === (x.daily_share_gb || 0)} pending={pending === "share"} onClick={() => run("share", () => actions.patchDevice(x.id, { daily_share_gb: Math.max(0, v || 0) }), t("dr.saved"))}>{t("dq.share.save")}</Button></div>
    </section>
  );
}
