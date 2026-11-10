import type { ReactNode } from "react";
import * as SliderP from "@radix-ui/react-slider";
import { Field, NumberField } from "./forms";
import { num, getLang } from "../../lib/i18n";

/**
 * Bounded value: slider for quick adjustment + exact number box, kept in sync.
 * Nothing is applied while dragging — the page's Apply button sends it.
 */
export function SliderField({ label, hint, value, onValue, min, max, step = 1, unit, marks, className, zeroLabel, disabled }: {
  label: ReactNode; hint?: ReactNode; value: number; onValue: (v: number) => void; min: number; max: number; step?: number; unit: string;
  marks?: number[]; className?: string; zeroLabel?: string; disabled?: boolean;
}) {
  const clamped = Math.max(min, Math.min(max, value || 0));
  const text = (v: number) => (v === 0 && zeroLabel ? zeroLabel : `${num(v, 2)} ${unit}`);
  return (
    <Field label={label} hint={hint} className={className}>
      {({ id, describedBy }) => (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <SliderP.Root id={id} aria-describedby={describedBy} min={min} max={max} step={step} value={[clamped]} disabled={disabled}
              onValueChange={([v]) => onValue(v)} dir={getLang() === "ar" ? "rtl" : "ltr"}
              className="relative flex h-8 flex-1 touch-none select-none items-center max-md:h-11">
              <SliderP.Track className="relative h-1.5 grow overflow-hidden rounded-full bg-line"><SliderP.Range className="absolute h-full bg-accent" /></SliderP.Track>
              <SliderP.Thumb aria-label={typeof label === "string" ? label : undefined} aria-valuetext={text(clamped)}
                className="block size-5 rounded-full border-2 border-accent bg-canvas shadow-raised outline-none focus-visible:ring-4 focus-visible:ring-focus/40 max-md:size-7" />
            </SliderP.Root>
            <div className="w-28 shrink-0">
              <NumberField label={label} labelHidden value={value} onValue={(v) => v != null && onValue(v)} unit={unit} spec={{ min, max, step }} ariaLabel={typeof label === "string" ? label : undefined} disabled={disabled} />
            </div>
          </div>
          {marks && <div className="flex justify-between px-1 text-[11px] text-fg-3" dir="ltr" aria-hidden>{marks.map((m) => <span key={m}>{text(m)}</span>)}</div>}
        </div>
      )}
    </Field>
  );
}
