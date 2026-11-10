import { useMemo, useState, type ReactNode } from "react";
import clsx from "clsx";

const W = 600, H = 180, PAD_T = 8, PAD_B = 4;
const y = (v: number, max: number) => PAD_T + (1 - Math.min(1, v / max)) * (H - PAD_T - PAD_B);
/** Round a maximum up to a readable axis value (1, 2, 2.5, 5 × 10^n). */
export function niceMax(v: number) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}
/** Average neighbouring points so long series stay cheap to draw (shape preserved). */
export function downsample<T extends Record<string, number>>(pts: T[], maxPts: number): T[] {
  if (pts.length <= maxPts) return pts;
  const step = pts.length / maxPts, out: T[] = [];
  for (let i = 0; i < maxPts; i++) {
    const a = Math.floor(i * step), b = Math.max(a + 1, Math.floor((i + 1) * step)), slice = pts.slice(a, b);
    const o = {} as Record<string, number>;
    for (const k of Object.keys(slice[0])) o[k] = slice.reduce((s, x) => s + x[k], 0) / slice.length;
    out.push(o as T);
  }
  return out;
}
export interface Series { key: string; label: string; color: string; dashed?: boolean }
interface Props<T> { data: T[]; series: Series[]; fmt: (v: number) => string; xLabel: (d: T, i: number) => string; xStart: string; xEnd: string; summary: string; height?: number; footer?: ReactNode }

/** Time series: labelled y axis with units, gridlines, legend, hover/touch readout, text summary for screen readers. */
export function LineChart<T extends Record<string, number>>({ data, series, fmt, xLabel, xStart, xEnd, summary, height = 180 }: Props<T>) {
  const [hover, setHover] = useState<number | null>(null);
  const pts = useMemo(() => downsample(data, 120), [data]);
  const max = useMemo(() => niceMax(Math.max(1, ...pts.flatMap((d) => series.map((s) => d[s.key] || 0))) * 1.05), [pts, series]);
  const n = pts.length, step = n > 1 ? W / (n - 1) : W;
  const path = (k: string) => pts.map((d, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)} ${y(d[k] || 0, max).toFixed(1)}`).join("");
  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect(); if (n < 2) return;
    setHover(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1)))));
  };
  const h = hover != null ? pts[hover] : null;
  return (
    <figure className="m-0">
      <Legend series={series} values={h ? series.map((s) => fmt(h[s.key] || 0)) : undefined} />
      <div className="flex gap-2" dir="ltr">
        <YAxis max={max} fmt={fmt} height={height} />
        <div className="relative min-w-0 flex-1 touch-pan-y" style={{ height }} onPointerMove={onMove} onPointerDown={onMove} onPointerLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 size-full" aria-hidden>
            {[0, 0.5, 1].map((f) => <line key={f} x1={0} x2={W} y1={y(max * f, max)} y2={y(max * f, max)} stroke="var(--chart-grid)" strokeWidth={1} vectorEffect="non-scaling-stroke" />)}
            {n >= 2 && series.map((s, si) => (
              <g key={s.key}>
                {si === 0 && <path d={`${path(s.key)}L${W} ${H}L0 ${H}Z`} fill={s.color} opacity={0.1} />}
                <path d={path(s.key)} fill="none" stroke={s.color} strokeWidth={2} strokeDasharray={s.dashed ? "5 4" : undefined} vectorEffect="non-scaling-stroke" opacity={hover != null ? 0.9 : 1} />
              </g>
            ))}
            {hover != null && <line x1={hover * step} x2={hover * step} y1={0} y2={H} stroke="var(--stroke-strong)" strokeWidth={1} vectorEffect="non-scaling-stroke" />}
          </svg>
          {h && hover != null && (
            <div className="pointer-events-none absolute top-1 z-10 rounded-md border border-line bg-raised px-2 py-1 text-[11.5px] shadow-raised"
              style={{ left: `clamp(0px, calc(${(hover / Math.max(1, n - 1)) * 100}% - 60px), calc(100% - 130px))` }}>
              <b className="block text-fg">{xLabel(h, hover)}</b>
              {series.map((s) => <span key={s.key} className="block text-fg-2">{s.label}: <b className="text-fg">{fmt(h[s.key] || 0)}</b></span>)}
            </div>
          )}
        </div>
      </div>
      <div className="mt-1 flex justify-between ps-14 text-[11.5px] text-fg-3" dir="ltr"><span>{xStart}</span><span>{xEnd}</span></div>
      <figcaption className="qm-sr">{summary}</figcaption>
    </figure>
  );
}

/** Day-by-day comparison bars. Each bar is focusable and announces its value. */
export function BarChart({ data, fmt, label, summary, height = 180, highlightLast = true }: { data: { key: string; label: string; value: number }[]; fmt: (v: number) => string; label: string; summary: string; height?: number; highlightLast?: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = niceMax(Math.max(1, ...data.map((d) => d.value)) * 1.05);
  const h = hover != null ? data[hover] : null;
  return (
    <figure className="m-0">
      <div className="mb-2 h-5 text-[12px] text-fg-2" aria-live="polite">{h ? <><b className="text-fg">{h.label}</b> · {fmt(h.value)}</> : <span className="text-fg-3">{label}</span>}</div>
      <div className="flex gap-2" dir="ltr">
        <YAxis max={max} fmt={fmt} height={height} />
        <div className="relative flex min-w-0 flex-1 items-end gap-[3px] border-b border-line" style={{ height }} onPointerLeave={() => setHover(null)}>
          {[0.5, 1].map((f) => <span key={f} className="pointer-events-none absolute inset-x-0 border-t border-[var(--chart-grid)]" style={{ bottom: `${f * 100}%` }} aria-hidden />)}
          {data.map((d, i) => (
            <button key={d.key} type="button" aria-label={`${d.label}: ${fmt(d.value)}`} onPointerEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}
              className="relative h-full flex-1 outline-none focus-visible:bg-accent-soft">
              <span className={clsx("absolute inset-x-0 bottom-0 rounded-t-[3px] transition-opacity",
                highlightLast && i === data.length - 1 ? "bg-chart-1" : "bg-chart-1/60", hover != null && hover !== i && "opacity-45")}
                style={{ height: `${d.value > 0 ? Math.max(2, (d.value / max) * 100) : 0}%` }} />
            </button>
          ))}
        </div>
      </div>
      <div className="mt-1 flex justify-between ps-14 text-[11.5px] text-fg-3" dir="ltr"><span>{data[0]?.label}</span><span>{data[data.length - 1]?.label}</span></div>
      <figcaption className="qm-sr">{summary}</figcaption>
    </figure>
  );
}
function YAxis({ max, fmt, height }: { max: number; fmt: (v: number) => string; height: number }) {
  return (
    <div className="flex w-12 shrink-0 flex-col justify-between text-end text-[11px] leading-none text-fg-3 tabular-nums" style={{ height }} aria-hidden>
      <span>{fmt(max)}</span><span>{fmt(max / 2)}</span><span>{fmt(0)}</span>
    </div>
  );
}
function Legend({ series, values }: { series: Series[]; values?: string[] }) {
  return (
    <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-fg-2">
      {series.map((s, i) => (
        <span key={s.key} className="inline-flex items-center gap-1.5">
          <svg width="18" height="8" aria-hidden><line x1="1" x2="17" y1="4" y2="4" stroke={s.color} strokeWidth="2.5" strokeDasharray={s.dashed ? "4 3" : undefined} /></svg>
          {s.label}{values && <b className="text-fg tabular-nums">{values[i]}</b>}
        </span>
      ))}
    </div>
  );
}
/** Ranked comparison (devices, apps): horizontal bars with the value written out. */
export function RankList({ rows }: { rows: { key: string | number; label: ReactNode; sub?: ReactNode; value: string; pct: number; color?: string; onClick?: () => void; extra?: ReactNode }[] }) {
  return (
    <ul className="divide-y divide-line">
      {rows.map((r) => {
        const inner = (
          <>
            <div className="flex items-baseline justify-between gap-3">
              <span className="min-w-0 truncate text-[13px] font-semibold text-fg">{r.label}{r.sub && <span className="ms-2 text-[12px] font-normal text-fg-3">{r.sub}</span>}</span>
              <span className="shrink-0 text-[12.5px] font-semibold text-fg tabular-nums">{r.value}</span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-line"><div className="h-full rounded-full" style={{ width: `${Math.max(2, Math.min(100, r.pct))}%`, background: r.color || "var(--chart-1)" }} /></div>
            {r.extra}
          </>
        );
        return <li key={r.key} className="py-2.5">{r.onClick ? <button type="button" className="block w-full text-start" onClick={r.onClick}>{inner}</button> : inner}</li>;
      })}
    </ul>
  );
}
