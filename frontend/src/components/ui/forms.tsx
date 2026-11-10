import { useEffect, useId, useMemo, useRef, useState, type ReactNode, type InputHTMLAttributes } from "react";
import * as SwitchP from "@radix-ui/react-switch";
import * as ToggleGroup from "@radix-ui/react-toggle-group";
import * as Popover from "@radix-ui/react-popover";
import * as Menu from "@radix-ui/react-dropdown-menu";
import clsx from "clsx";
import { Icon } from "./Icon";
import { t, num, getLang } from "../../lib/i18n";

/* ------------------------------------------------------------------ field shell */
export interface FieldProps { label: ReactNode; hint?: ReactNode; error?: string | null; children: (ids: { id: string; describedBy?: string; invalid: boolean }) => ReactNode; className?: string; optional?: boolean; labelHidden?: boolean; action?: ReactNode }
/** Labels stay accessible; inline actions align with the control, above helper/error text. */
export function Field({ label, hint, error, children, className, optional, labelHidden, action }: FieldProps) {
  const id = useId(), hintId = id + "-h", errId = id + "-e";
  const describedBy = [hint ? hintId : "", error ? errId : ""].filter(Boolean).join(" ") || undefined;
  return (
    <div className={clsx("qm-field flex min-w-0 flex-col gap-1.5", className)}>
      <label htmlFor={id} className={labelHidden ? "qm-sr" : "text-[12.5px] font-medium text-fg-2"}>{label}{optional && <span className="ms-1 font-normal text-fg-3">({t("f.optional")})</span>}</label>
      {action ? <div className="qm-field-action flex items-start gap-2 [&>button]:shrink-0"><div className="min-w-0 flex-1">{children({ id, describedBy, invalid: !!error })}</div>{action}</div>
        : children({ id, describedBy, invalid: !!error })}
      {error ? <p id={errId} role="alert" className="flex items-start gap-1 text-[12px] text-danger"><Icon name="alert" size={13} className="mt-0.5" />{error}</p>
        : hint ? <p id={hintId} className="text-[12px] leading-snug text-fg-3">{hint}</p> : null}
    </div>
  );
}
const controlBase = "h-9 max-md:h-11 w-full min-w-0 rounded-md border bg-canvas text-[13.5px] text-fg placeholder:text-fg-3 transition-colors outline-none " +
  "border-line-strong hover:border-fg-3 focus-visible:border-focus focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-focus/40 " +
  "disabled:opacity-60 disabled:bg-raised read-only:bg-raised aria-[invalid=true]:border-danger";
const ctl = controlBase + " px-3";
export const controlClass = ctl;

export function TextField({ label, hint, error, value, onChange, mono, className, optional, labelHidden, action, ...rest }: { label: ReactNode; hint?: ReactNode; error?: string | null; value: string; onChange: (v: string) => void; mono?: boolean; optional?: boolean; labelHidden?: boolean; action?: ReactNode } & Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  return (
    <Field label={label} hint={hint} error={error} className={className} optional={optional} labelHidden={labelHidden} action={action}>
      {({ id, describedBy, invalid }) => (
        <input id={id} aria-describedby={describedBy} aria-invalid={invalid} value={value} onChange={(e) => onChange(e.target.value)}
          className={clsx(ctl, mono && "qm-ltr font-mono text-[13px]")} dir={mono ? "ltr" : undefined} spellCheck={mono ? false : undefined}
          autoCapitalize={mono ? "off" : undefined} autoCorrect={mono ? "off" : undefined} {...rest} />
      )}
    </Field>
  );
}

/* ------------------------------------------------------------------ numbers */
export interface NumberSpec { min?: number; max?: number; step?: number; integer?: boolean; decimals?: number; allowEmpty?: boolean }
/** Parse a typed string. Arabic-Indic digits and a comma decimal are accepted; exponent/hex are not. */
export function parseNumber(raw: string, spec: NumberSpec): { value: number | null; error: string | null } {
  const s = raw.trim().replace(/[٠-٩]/g, (d) => String("٠١٢٣٤٥٦٧٨٩".indexOf(d))).replace(/٫|,/g, ".");
  if (s === "") return spec.allowEmpty ? { value: null, error: null } : { value: null, error: t("f.required") };
  if (!/^-?\d*\.?\d+$|^-?\d+\.$/.test(s)) return { value: null, error: t("f.notnumber") };
  const v = Number(s);
  if (!Number.isFinite(v)) return { value: null, error: t("f.notnumber") };
  if (spec.integer && !Number.isInteger(v)) return { value: null, error: t("f.whole") };
  if (spec.decimals != null && !spec.integer) { const dec = (s.split(".")[1] || "").length; if (dec > spec.decimals) return { value: null, error: t("f.decimals").replace("{n}", String(spec.decimals)) }; }
  if (spec.min != null && v < spec.min) return { value: null, error: t("f.min").replace("{n}", num(spec.min, 3)) };
  if (spec.max != null && v > spec.max) return { value: null, error: t("f.max").replace("{n}", num(spec.max, 3)) };
  return { value: v, error: null };
}
const fmtDraft = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "" : String(Math.round(v * 1000) / 1000));

/**
 * Number field with a visible unit. Keeps a STRING draft while typing (never turns
 * an empty box into 0), validates on blur/submit, ignores the mouse wheel, and has
 * optional −/+ steppers. `onValue` only receives valid numbers (or null when empty+allowed).
 */
export function NumberField({ label, hint, error: extErr, value, onValue, unit, spec = {}, stepper, presets, className, placeholder, ariaLabel, disabled, onValidity, labelHidden }: {
  label: ReactNode; hint?: ReactNode; error?: string | null; value: number | null; onValue: (v: number | null) => void; unit?: string; spec?: NumberSpec;
  stepper?: boolean; presets?: number[]; className?: string; placeholder?: string; ariaLabel?: string; disabled?: boolean; onValidity?: (ok: boolean) => void; labelHidden?: boolean;
}) {
  const [draft, setDraft] = useState(fmtDraft(value));
  const [err, setErr] = useState<string | null>(null);
  const focused = useRef(false);
  // A new server value only replaces the box when the person is not editing it.
  useEffect(() => { if (!focused.current) { setDraft(fmtDraft(value)); setErr(null); } }, [value]);
  const commit = (s: string, final: boolean) => {
    const r = parseNumber(s, spec);
    if (r.error) { if (final) setErr(r.error); onValidity?.(false); return; }
    setErr(null); onValidity?.(true); onValue(r.value);
  };
  const stepBy = (dir: 1 | -1) => {
    const st = spec.step ?? 1, cur = parseNumber(draft, { ...spec, min: undefined, max: undefined }).value ?? 0;
    let v = Math.round((cur + dir * st) * 1000) / 1000;
    if (spec.min != null) v = Math.max(spec.min, v); if (spec.max != null) v = Math.min(spec.max, v);
    setDraft(fmtDraft(v)); commit(String(v), true);
  };
  const error = extErr ?? err;
  return (
    <Field label={label} hint={hint} error={error} className={className} labelHidden={labelHidden}>
      {({ id, describedBy, invalid }) => (
        <div className="flex flex-col gap-2">
          <div className="flex items-stretch gap-1.5">
            {stepper && <StepBtn icon="minus" label={t("f.decrease")} onClick={() => stepBy(-1)} disabled={disabled || (spec.min != null && (parseNumber(draft, {}).value ?? 0) <= spec.min)} />}
            <div aria-invalid={invalid} className={clsx(controlBase, "flex min-w-0 flex-1 items-center focus-within:border-focus focus-within:outline-2 focus-within:outline-offset-0 focus-within:outline-focus/40", disabled && "bg-raised opacity-60")}>
              <input id={id} aria-describedby={describedBy} aria-invalid={invalid} aria-label={ariaLabel} disabled={disabled}
                type="text" inputMode={spec.integer ? "numeric" : "decimal"} autoComplete="off" dir="ltr" placeholder={placeholder}
                className={clsx("h-full w-full min-w-0 rounded-md bg-transparent px-3 tabular-nums outline-none placeholder:text-fg-3 disabled:cursor-not-allowed", getLang() === "ar" ? "text-right" : "text-left")} value={draft}
                onFocus={() => { focused.current = true; }}
                onChange={(e) => { setDraft(e.target.value); if (err) setErr(null); commit(e.target.value, false); }}
                onBlur={() => { focused.current = false; commit(draft, true); }}
                onKeyDown={(e) => { if (e.key === "ArrowUp") { e.preventDefault(); stepBy(1); } else if (e.key === "ArrowDown") { e.preventDefault(); stepBy(-1); } }} />
              {unit && <span className="max-w-[55%] shrink-0 truncate px-3 text-[12.5px] font-medium text-fg-3" dir="auto" aria-hidden>{unit}</span>}
            </div>
            {stepper && <StepBtn icon="plus" label={t("f.increase")} onClick={() => stepBy(1)} disabled={disabled || (spec.max != null && (parseNumber(draft, {}).value ?? 0) >= spec.max)} />}
          </div>
          {presets && presets.length > 0 && (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={t("f.presets")}>
              {presets.map((p) => (
                <button key={p} type="button" disabled={disabled} onClick={() => { setDraft(fmtDraft(p)); commit(String(p), true); }}
                  className={clsx("h-7 rounded-full border px-2.5 text-[12px] font-semibold transition-colors max-md:h-9",
                    parseNumber(draft, {}).value === p ? "border-accent bg-accent-soft text-accent-fg" : "border-line text-fg-2 hover:bg-raised")}>
                  {num(p, 2)}{unit ? " " + unit : ""}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </Field>
  );
}
function StepBtn({ icon, label, onClick, disabled }: { icon: string; label: string; onClick: () => void; disabled?: boolean }) {
  return <button type="button" aria-label={label} onClick={onClick} disabled={disabled}
    className="grid w-9 shrink-0 place-items-center rounded-md border border-line-strong text-fg-2 hover:bg-raised disabled:opacity-40 max-md:w-11"><Icon name={icon} size={16} /></button>;
}

/* ------------------------------------------------------------------ choices */
export function Switch({ checked, onChange, label, disabled, pending }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean; pending?: boolean }) {
  return (
    <SwitchP.Root checked={checked} onCheckedChange={onChange} disabled={disabled || pending} aria-label={label} aria-busy={pending || undefined}
      className={clsx("relative inline-flex h-6 w-[42px] shrink-0 items-center rounded-full border transition-colors disabled:opacity-55",
        checked ? "border-transparent bg-accent" : "border-line-strong bg-line", pending && "animate-pulse")}>
      <SwitchP.Thumb className={clsx("block size-[18px] rounded-full shadow transition-transform",
        checked ? "bg-accent-ink translate-x-[20px] rtl:-translate-x-[20px]" : "bg-fg-2 translate-x-[2px] rtl:-translate-x-[2px]")} />
    </SwitchP.Root>
  );
}
/** A setting row: title + what it actually does + the switch. */
export function SwitchRow({ title, desc, checked, onChange, disabled, pending, children }: { title: ReactNode; desc?: ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; pending?: boolean; children?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0"><b className="block text-[13.5px] font-semibold text-fg">{title}</b>{desc && <p className="mt-0.5 text-[12.5px] text-fg-3">{desc}</p>}{children}</div>
      <Switch checked={checked} onChange={onChange} label={typeof title === "string" ? title : ""} disabled={disabled} pending={pending} />
    </div>
  );
}
/** 2–5 finite modes. Arrow keys move between options (roving focus). */
export function Segmented<T extends string>({ value, onChange, options, label, size = "md", className }: { value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode; count?: number; disabled?: boolean }[]; label: string; size?: "sm" | "md"; className?: string }) {
  return (
    <ToggleGroup.Root type="single" value={value} onValueChange={(v) => v && onChange(v as T)} aria-label={label} dir={getLang() === "ar" ? "rtl" : "ltr"}
      className={clsx("inline-flex max-w-full gap-0.5 overflow-x-auto rounded-md border border-line bg-canvas p-0.5", className)}>
      {options.map((o) => (
        <ToggleGroup.Item key={o.value} value={o.value} disabled={o.disabled}
          className={clsx("inline-flex shrink-0 items-center gap-1.5 rounded-[6px] px-3 font-semibold text-fg-2 transition-colors hover:text-fg disabled:opacity-45",
            size === "sm" ? "h-7 text-[12.5px]" : "h-8 text-[13px] max-md:h-10", "data-[state=on]:bg-raised data-[state=on]:text-fg data-[state=on]:shadow-sm")}>
          {o.label}{o.count != null && <em className="not-italic text-[11px] font-medium text-fg-3 tabular-nums">{num(o.count, 0)}</em>}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
/** Themed fixed choices with keyboard navigation, typeahead and focus restoration. */
export function Select<T extends string>({ label, hint, error, value, onChange, options, className, disabled, labelHidden }: { label: ReactNode; hint?: ReactNode; error?: string | null; value: T; onChange: (v: T) => void; options: { value: T; label: string; disabled?: boolean }[]; className?: string; disabled?: boolean; labelHidden?: boolean }) {
  return (
    <Field label={label} hint={hint} error={error} className={className} labelHidden={labelHidden}>
      {({ id, describedBy, invalid }) => (
        <Menu.Root dir={getLang() === "ar" ? "rtl" : "ltr"}>
          <Menu.Trigger asChild>
            <button type="button" id={id} aria-describedby={describedBy} aria-invalid={invalid} disabled={disabled}
              className={clsx(ctl, "flex items-center justify-between gap-3 text-start")}>
              <span className="truncate">{options.find((o) => o.value === value)?.label || t("f.choose")}</span>
              <Icon name="chev" size={15} className="shrink-0 rotate-90 text-fg-3" />
            </button>
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content align="start" sideOffset={4} collisionPadding={8}
              className="qm-pop z-[90] max-h-[min(320px,var(--radix-dropdown-menu-content-available-height))] w-[var(--radix-dropdown-menu-trigger-width)] min-w-40 overflow-y-auto overscroll-contain rounded-md border border-line bg-raised p-1 shadow-raised">
              <Menu.RadioGroup value={value} onValueChange={(next) => {
                const option = options.find((o) => o.value === next && !o.disabled);
                if (option) onChange(option.value);
              }}>
                {options.map((o) => (
                  <Menu.RadioItem key={o.value} value={o.value} disabled={o.disabled}
                    className="flex min-h-9 cursor-pointer items-center justify-between gap-3 rounded-[6px] px-2.5 py-2 text-[13px] text-fg outline-none data-[highlighted]:bg-canvas data-[state=checked]:font-semibold data-[disabled]:pointer-events-none data-[disabled]:opacity-45 max-md:min-h-11">
                    <span>{o.label}</span>
                    <Menu.ItemIndicator><Icon name="check" size={15} className="text-accent-fg" /></Menu.ItemIndicator>
                  </Menu.RadioItem>
                ))}
              </Menu.RadioGroup>
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
      )}
    </Field>
  );
}

/** Long or growing lists (devices, people): searchable listbox in a popover. */
export interface ComboOption { value: string; label: string; sub?: string; group?: string }
export function Combobox({ label, hint, error, value, onChange, options, placeholder, className, emptyText }: { label: ReactNode; hint?: ReactNode; error?: string | null; value: string; onChange: (v: string) => void; options: ComboOption[]; placeholder?: string; className?: string; emptyText?: string }) {
  const [open, setOpen] = useState(false), [q, setQ] = useState(""), [active, setActive] = useState(0);
  const listId = useId();
  const filtered = useMemo(() => { const s = q.trim().toLowerCase(); return s ? options.filter((o) => (o.label + " " + (o.sub || "")).toLowerCase().includes(s)) : options; }, [q, options]);
  const cur = options.find((o) => o.value === value);
  const pick = (o: ComboOption) => { onChange(o.value); setOpen(false); setQ(""); };
  useEffect(() => { setActive(0); }, [q, open]);
  return (
    <Field label={label} hint={hint} error={error} className={className}>
      {({ id, describedBy, invalid }) => (
        <Popover.Root open={open} onOpenChange={setOpen}>
          <Popover.Trigger asChild>
            <button id={id} type="button" aria-describedby={describedBy} aria-invalid={invalid} aria-haspopup="listbox" aria-expanded={open}
              className={clsx(ctl, "flex items-center justify-between gap-2 text-start")}>
              <span className={clsx("truncate", !cur && "text-fg-3")}>{cur ? cur.label : placeholder || t("f.choose")}{cur?.sub && <span className="ms-2 text-fg-3">{cur.sub}</span>}</span>
              <Icon name="search" size={15} className="text-fg-3" />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content align="start" sideOffset={4} collisionPadding={8} className="qm-pop z-[90] w-[var(--radix-popover-trigger-width)] min-w-64 rounded-md border border-line bg-raised p-1.5 shadow-raised"
              onOpenAutoFocus={(e) => { e.preventDefault(); (e.currentTarget as HTMLElement)?.querySelector("input")?.focus(); }}>
              <div className="relative mb-1">
                <Icon name="search" size={15} className="pointer-events-none absolute inset-y-0 start-2.5 my-auto text-fg-3" />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("f.search")} aria-label={t("f.search")} role="combobox" aria-controls={listId} aria-expanded aria-activedescendant={filtered[active] ? `${listId}-${active}` : undefined}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(filtered.length - 1, a + 1)); }
                    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
                    else if (e.key === "Enter") { e.preventDefault(); if (filtered[active]) pick(filtered[active]); }
                  }}
                  className={clsx(ctl, "h-9 ps-8")} />
              </div>
              <ul id={listId} role="listbox" className="max-h-64 overflow-y-auto overscroll-contain">
                {filtered.length === 0 && <li className="px-3 py-4 text-center text-[12.5px] text-fg-3">{emptyText || t("f.nomatch")}{q && <button type="button" className="ms-2 font-semibold text-accent-fg" onClick={() => setQ("")}>{t("f.clear")}</button>}</li>}
                {filtered.map((o, i) => (
                  <li key={o.value} id={`${listId}-${i}`} role="option" aria-selected={o.value === value} onMouseEnter={() => setActive(i)} onClick={() => pick(o)}
                    className={clsx("flex cursor-pointer items-center gap-2 rounded-[6px] px-2.5 py-2 text-[13px]", i === active ? "bg-canvas" : "", o.value === value && "font-semibold")}>
                    <span className="min-w-0 flex-1 truncate">{o.label}{o.sub && <span className="ms-2 text-[12px] text-fg-3">{o.sub}</span>}</span>
                    {o.value === value && <Icon name="check" size={15} className="text-accent-fg" />}
                  </li>
                ))}
              </ul>
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      )}
    </Field>
  );
}
