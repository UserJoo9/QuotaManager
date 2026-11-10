import { forwardRef, type ButtonHTMLAttributes, type ReactNode, type HTMLAttributes } from "react";
import * as Tooltip from "@radix-ui/react-tooltip";
import * as Popover from "@radix-ui/react-popover";
import clsx from "clsx";
import { Icon } from "./Icon";
import { Illustration, hasArt } from "./Illustration";
import { t } from "../../lib/i18n";
import type { Tone } from "../../lib/format";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "danger-solid";
type Size = "sm" | "md";
const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink hover:bg-accent-hover active:bg-accent-pressed",
  secondary: "bg-surface text-fg border border-line-strong hover:bg-raised active:bg-line",
  ghost: "text-fg-2 hover:bg-raised hover:text-fg active:bg-line",
  danger: "bg-danger-soft text-danger hover:brightness-105 active:brightness-95 border border-danger/30",
  "danger-solid": "bg-danger-solid text-white hover:brightness-110 active:brightness-95",
};
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> { variant?: Variant; size?: Size; icon?: string; pending?: boolean; pendingText?: string }
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({ variant = "secondary", size = "md", icon, pending, pendingText, className, children, disabled, type = "button", ...rest }, ref) {
  return (
    <button ref={ref} type={type} disabled={disabled || pending} aria-busy={pending || undefined}
      className={clsx("inline-flex items-center justify-center gap-2 font-semibold whitespace-nowrap rounded-md transition-[background,color,filter] duration-150 select-none",
        // long (often Arabic) labels wrap on narrow phones instead of pushing the page sideways
        "max-sm:max-w-full max-sm:whitespace-normal max-sm:text-center",
        "disabled:opacity-55 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        size === "sm" ? "min-h-8 py-1 px-3 text-[12.5px]" : "min-h-9 py-1.5 px-4 text-[13.5px] max-md:min-h-11", variants[variant], className)} {...rest}>
      {pending ? <Spinner /> : icon ? <Icon name={icon} size={size === "sm" ? 15 : 16} /> : null}
      {pending && pendingText ? pendingText : children}
    </button>
  );
});
export function Spinner({ size = 14 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" className="animate-spin shrink-0" aria-hidden><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity=".25" strokeWidth="3" /><path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" /></svg>;
}
/** Icon-only action: always has an accessible name, and a tooltip that also shows on keyboard focus. */
export const IconButton = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { icon: string; label: string; size?: number; tone?: "default" | "danger"; tip?: boolean }>(
  function IconButton({ icon, label, size = 18, tone = "default", tip = true, className, type = "button", ...rest }, ref) {
    const btn = (
      <button ref={ref} type={type} aria-label={label} className={clsx("inline-grid place-items-center size-9 max-md:size-11 rounded-md transition-colors disabled:opacity-50",
        tone === "danger" ? "text-danger hover:bg-danger-soft" : "text-fg-2 hover:bg-raised hover:text-fg", className)} {...rest}>
        <Icon name={icon} size={size} />
      </button>
    );
    return tip ? <Tip label={label}>{btn}</Tip> : btn;
  });
export function Tip({ label, children, side = "top" }: { label: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <Tooltip.Root delayDuration={350}>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content side={side} sideOffset={6} className="qm-pop z-[80] max-w-72 rounded-md bg-fg px-2.5 py-1.5 text-[12px] font-medium text-canvas shadow-raised">
          {label}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
const tones: Record<Tone, string> = {
  neutral: "bg-raised text-fg-2 border-line", success: "bg-success-soft text-success border-transparent",
  warning: "bg-warning-soft text-warning border-transparent", danger: "bg-danger-soft text-danger border-transparent",
  info: "bg-info-soft text-info border-transparent", accent: "bg-accent-soft text-accent-fg border-transparent",
};
const toneIcon: Partial<Record<Tone, string>> = { success: "check", warning: "alert", danger: "ban", info: "info" };
/** Status chip: colour + icon + words (never colour alone). */
export function Badge({ tone = "neutral", children, icon, className, title }: { tone?: Tone; children: ReactNode; icon?: string | false; className?: string; title?: string }) {
  const ic = icon === false ? null : icon ?? toneIcon[tone];
  return (
    <span title={title} className={clsx("inline-flex items-center gap-1 h-[22px] px-2 rounded-full border text-[11.5px] font-semibold whitespace-nowrap", tones[tone], className)}>
      {ic ? <Icon name={ic} size={12} /> : tone === "neutral" ? <span className="size-1.5 rounded-full bg-fg-3" aria-hidden /> : null}
      {children}
    </span>
  );
}
/** Progress meter for quotas. Over 90 % turns amber, 100 % red, and the label says so. */
export function Meter({ value, max = 100, neutral, label, className, thin }: { value: number; max?: number; neutral?: boolean; label?: string; className?: string; thin?: boolean }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  const tone = neutral ? "bg-fg-3" : pct >= 100 ? "bg-danger" : pct >= 90 ? "bg-warning" : "bg-accent";
  return (
    <div role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={Math.round(value * 10) / 10} aria-label={label}
      className={clsx("w-full overflow-hidden rounded-full bg-line", thin ? "h-1" : "h-1.5", className)}>
      <div className={clsx("h-full rounded-full transition-[width] duration-500", tone)} style={{ width: pct + "%" }} />
    </div>
  );
}
export function Card({ title, sub, actions, children, className, bare, id }: { title?: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bare?: boolean; id?: string }) {
  return (
    <section id={id} aria-label={typeof title === "string" ? title : undefined}
      className={clsx(bare ? "" : "rounded-lg border border-line bg-surface", "min-w-0 p-[var(--pad)]", className)}>
      {(title || actions) && (
        <header className="mb-3 flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
          <div className="min-w-0">
            {title && <h2 className="text-[14.5px] font-semibold text-fg">{title}</h2>}
            {sub && <p className="mt-0.5 text-[12.5px] text-fg-3">{sub}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}
/** Empty state: illustration + what this is + the next step (never a dead end). */
export function Empty({ icon = "info", title, sub, action, compact }: { icon?: string; title: ReactNode; sub?: ReactNode; action?: ReactNode; compact?: boolean }) {
  return (
    <div className={clsx("flex flex-col items-center gap-1.5 px-3 text-center", compact ? "py-5" : "py-8")}>
      {!compact && hasArt(icon) ? <Illustration name={icon} size={112} className="mb-1" />
        : <span className="mb-1 grid size-10 place-items-center rounded-full bg-raised text-fg-3"><Icon name={icon} size={20} /></span>}
      <b className="text-[14px] text-fg">{title}</b>
      {sub && <p className="max-w-sm text-[12.5px] text-fg-3">{sub}</p>}
      {action && <div className="mt-2 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}
export function Skeleton({ h = 120, className }: { h?: number; className?: string }) {
  return <div className={clsx("qm-skel", className)} style={{ height: h }} aria-hidden />;
}
/** Loading placeholder that also says WHAT it is waiting for. */
export function Loading({ what, h = 160 }: { what: string; h?: number }) {
  return (
    <div className="relative" role="status" aria-live="polite">
      <Skeleton h={h} />
      <span className="absolute inset-0 flex items-center justify-center gap-2 text-[12.5px] text-fg-3"><Spinner />{what}</span>
    </div>
  );
}
/** Persistent message (not a toast): connection lost, failed apply, risky state. */
export function Banner({ tone = "info", title, children, action, className }: { tone?: Exclude<Tone, "neutral" | "accent">; title?: ReactNode; children?: ReactNode; action?: ReactNode; className?: string }) {
  const ic = { info: "info", success: "check", warning: "alert", danger: "alert" }[tone];
  return (
    <div role={tone === "danger" || tone === "warning" ? "alert" : "status"}
      className={clsx("flex items-start gap-3 rounded-md border px-3 py-2.5 text-[13px]", {
        "border-info/30 bg-info-soft text-fg": tone === "info", "border-success/30 bg-success-soft text-fg": tone === "success",
        "border-warning/40 bg-warning-soft text-fg": tone === "warning", "border-danger/40 bg-danger-soft text-fg": tone === "danger",
      }, className)}>
      <span className={clsx("mt-0.5", { "text-info": tone === "info", "text-success": tone === "success", "text-warning": tone === "warning", "text-danger": tone === "danger" })}><Icon name={ic} size={16} /></span>
      <div className="min-w-0 flex-1">{title && <b className="block font-semibold">{title}</b>}{children && <div className="text-fg-2">{children}</div>}</div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
/** A technical term with its plain explanation, reachable by hover, focus or tap. */
export function Term({ k, children }: { k: string; children?: ReactNode }) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" className="inline-flex items-center gap-0.5 underline decoration-dotted decoration-fg-3 underline-offset-2 hover:text-fg" aria-label={`${children ?? t("term." + k)} — ${t("help.what")}`}>
          {children ?? t("term." + k)}<Icon name="help" size={13} className="text-fg-3" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content sideOffset={6} collisionPadding={12} className="qm-pop z-[80] max-w-80 rounded-md border border-line bg-raised p-3 text-[12.5px] leading-relaxed text-fg-2 shadow-raised">
          <b className="mb-1 block text-fg">{t("term." + k)}</b>{t("term." + k + ".d")}
          <Popover.Arrow className="fill-[var(--bg-raised)]" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
export function KV({ items, className }: { items: ([ReactNode, ReactNode, boolean?] | null | false)[]; className?: string }) {
  return (
    <dl className={clsx("grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-3", className)}>
      {items.filter(Boolean).map((it, i) => { const [k, v, mono] = it as [ReactNode, ReactNode, boolean?]; return (
        <div key={i} className="min-w-0"><dt className="text-[12px] text-fg-3">{k}</dt><dd className={clsx("mt-0.5 break-words font-semibold text-fg", mono && "qm-ltr font-mono text-[12.5px]")}>{v}</dd></div>
      ); })}
    </dl>
  );
}
export function Stat({ label, value, unit, sub, className }: { label: ReactNode; value: ReactNode; unit?: ReactNode; sub?: ReactNode } & HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={clsx("min-w-0", className)}>
      <div className="text-[12px] text-fg-3">{label}</div>
      <div className="mt-0.5 text-[20px] font-semibold leading-tight text-fg tabular-nums">{value}{unit && <small className="ms-1 text-[12px] font-medium text-fg-3">{unit}</small>}</div>
      {sub && <div className="text-[12px] text-fg-3">{sub}</div>}
    </div>
  );
}
