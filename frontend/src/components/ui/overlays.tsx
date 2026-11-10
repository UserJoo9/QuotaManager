import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import * as DialogP from "@radix-ui/react-dialog";
import clsx from "clsx";
import { Icon } from "./Icon";
import { Button, IconButton } from "./primitives";
import { t, getLang } from "../../lib/i18n";

/* ------------------------------------------------------------------ modal dialog */
export function Dialog({ open, onOpenChange, title, sub, children, footer, wide, busy }: { open: boolean; onOpenChange: (v: boolean) => void; title: ReactNode; sub?: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean; busy?: boolean }) {
  return (
    <DialogP.Root open={open} onOpenChange={(v) => { if (!busy) onOpenChange(v); }}>
      <DialogP.Portal>
        <DialogP.Overlay className="qm-sheet-overlay qm-overlay-anim" />
        <DialogP.Content aria-describedby={undefined}
          className={clsx("qm-pop fixed start-1/2 top-1/2 z-[61] flex max-h-[min(88dvh,760px)] w-[calc(100vw-24px)] -translate-y-1/2 flex-col rounded-lg border border-line bg-raised shadow-raised outline-none",
            getLang() === "ar" ? "translate-x-1/2" : "-translate-x-1/2", wide ? "max-w-2xl" : "max-w-md")}>
          <header className="flex items-start gap-3 border-b border-line px-5 py-4">
            <div className="min-w-0 flex-1"><DialogP.Title className="text-[15px] font-semibold text-fg">{title}</DialogP.Title>{sub && <p className="mt-0.5 text-[12.5px] text-fg-3">{sub}</p>}</div>
            <DialogP.Close asChild><IconButton icon="x" label={t("common.close")} tip={false} disabled={busy} /></DialogP.Close>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <footer className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</footer>}
        </DialogP.Content>
      </DialogP.Portal>
    </DialogP.Root>
  );
}

/**
 * Risky operations say what changes, who is affected, how to undo it and what
 * happens if the connection drops — and only then offer the literal action.
 */
export interface Consequences { changes: ReactNode; affected?: ReactNode; undo?: ReactNode; connection?: ReactNode }
export function ConfirmDialog({ open, onOpenChange, title, consequences, confirmLabel, onConfirm, danger = true, children }: {
  open: boolean; onOpenChange: (v: boolean) => void; title: ReactNode; consequences: Consequences; confirmLabel: string; onConfirm: () => Promise<unknown>; danger?: boolean; children?: ReactNode;
}) {
  const [busy, setBusy] = useState(false), [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (open) setErr(null); }, [open]);
  const go = async () => { setBusy(true); setErr(null); try { await onConfirm(); onOpenChange(false); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); } };
  const row = (ic: string, k: string, v?: ReactNode) => v ? (
    <div className="flex gap-3 py-2"><span className="mt-0.5 text-fg-3"><Icon name={ic} size={16} /></span><div className="min-w-0"><b className="block text-[12px] font-semibold uppercase tracking-wide text-fg-3 rtl:normal-case rtl:tracking-normal">{t(k)}</b><div className="text-[13px] text-fg">{v}</div></div></div>
  ) : null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title} busy={busy}
      footer={<><Button onClick={() => onOpenChange(false)} disabled={busy}>{t("common.cancel")}</Button>
        <Button variant={danger ? "danger-solid" : "primary"} onClick={go} pending={busy} pendingText={t("act.applying")}>{confirmLabel}</Button></>}>
      <div className="divide-y divide-line">
        {row("edit", "cf.changes", consequences.changes)}
        {row("people", "cf.affected", consequences.affected)}
        {row("reset", "cf.undo", consequences.undo)}
        {row("wifioff", "cf.connection", consequences.connection)}
      </div>
      {children}
      {err && <p role="alert" className="mt-3 rounded-md bg-danger-soft px-3 py-2 text-[13px] text-danger">{err}</p>}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ adaptive sheet */
/** Tracks the VISIBLE viewport (iOS keyboard, browser chrome) while a sheet is open. */
function useVisualViewport(active: boolean) {
  useEffect(() => {
    const vv = window.visualViewport, root = document.documentElement;
    if (!active || !vv) return;
    let raf = 0;
    const update = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        // keyboard = layout viewport bottom minus visible viewport bottom (0 with a hardware keyboard)
        const kb = Math.max(0, window.innerHeight - (vv.height + vv.offsetTop));
        root.style.setProperty("--qm-vvh", vv.height + "px");
        root.style.setProperty("--qm-kb", kb + "px");
      });
    };
    update();
    vv.addEventListener("resize", update); vv.addEventListener("scroll", update);
    return () => { cancelAnimationFrame(raf); vv.removeEventListener("resize", update); vv.removeEventListener("scroll", update); root.style.removeProperty("--qm-vvh"); root.style.removeProperty("--qm-kb"); };
  }, [active]);
}
/** Keep the focused field (and its helper/error text) visible inside the sheet's own scroller. */
function keepFocusedVisible(scroller: HTMLElement | null) {
  if (!scroller) return () => {};
  let tm: ReturnType<typeof setTimeout> | undefined;
  const run = (el: Element) => {
    const field = (el.closest("[data-field]") as HTMLElement) || (el.parentElement as HTMLElement) || (el as HTMLElement);
    const box = scroller.getBoundingClientRect(), r = field.getBoundingClientRect();
    const vvBottom = window.visualViewport ? window.visualViewport.height + window.visualViewport.offsetTop : window.innerHeight;
    const bottom = Math.min(box.bottom, vvBottom) - 16, top = box.top + 12;
    if (r.bottom > bottom) scroller.scrollTop += r.bottom - bottom;
    else if (r.top < top) scroller.scrollTop -= top - r.top;
  };
  const onFocus = (e: FocusEvent) => {
    const el = e.target as Element;
    if (!/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
    clearTimeout(tm); run(el);
    tm = setTimeout(() => run(el), 320); // again once the on-screen keyboard has finished opening
  };
  scroller.addEventListener("focusin", onFocus);
  return () => { clearTimeout(tm); scroller.removeEventListener("focusin", onFocus); };
}
export function Sheet({ open, onOpenChange, title, sub, icon, dirty, children, footer, size = "auto" }: {
  open: boolean; onOpenChange: (v: boolean) => void; title: ReactNode; sub?: ReactNode; icon?: string; dirty?: boolean; children: ReactNode; footer?: ReactNode; size?: "auto" | "full";
}) {
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const scroller = useRef<HTMLDivElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ y: number; dy: number } | null>(null);
  useVisualViewport(open);
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  useEffect(() => keepFocusedVisible(scrollEl), [scrollEl]);
  useEffect(() => { if (!open) setConfirmDiscard(false); }, [open]);
  const requestClose = () => { if (dirty) setConfirmDiscard(true); else onOpenChange(false); };
  // swipe down on the grip only (never on the form: text selection and sliders keep working)
  const onGripDown = (e: React.PointerEvent) => { drag.current = { y: e.clientY, dy: 0 }; (e.target as Element).setPointerCapture(e.pointerId); };
  const onGripMove = (e: React.PointerEvent) => { if (!drag.current || !panel.current) return; drag.current.dy = Math.max(0, e.clientY - drag.current.y); panel.current.style.transform = `translateY(${drag.current.dy}px)`; };
  const onGripUp = () => { if (!drag.current || !panel.current) return; const far = drag.current.dy > 90; panel.current.style.transform = ""; drag.current = null; if (far) requestClose(); };
  return (
    <DialogP.Root open={open} onOpenChange={(v) => (v ? onOpenChange(true) : requestClose())}>
      <DialogP.Portal>
        <DialogP.Overlay className="qm-sheet-overlay qm-overlay-anim" />
        <DialogP.Content ref={panel} className="qm-sheet" data-size={size} aria-describedby={undefined}
          onOpenAutoFocus={(e) => { e.preventDefault(); panel.current?.querySelector<HTMLElement>("[data-sheet-close]")?.focus(); }}>
          <div className="flex justify-center pt-2 md:hidden" onPointerDown={onGripDown} onPointerMove={onGripMove} onPointerUp={onGripUp} onPointerCancel={onGripUp}>
            <span className="h-1.5 w-10 rounded-full bg-line-strong" aria-hidden />
          </div>
          <header className="flex items-start gap-3 border-b border-line px-4 pb-3 pt-2 md:px-5 md:pt-4">
            {icon && <span className="grid size-9 shrink-0 place-items-center rounded-md bg-canvas text-fg-2"><Icon name={icon} size={18} /></span>}
            <div className="min-w-0 flex-1"><DialogP.Title className="truncate text-[15.5px] font-semibold text-fg">{title}</DialogP.Title>{sub && <p className="truncate text-[12.5px] text-fg-3">{sub}</p>}</div>
            {dirty && <span className="mt-1 rounded-full bg-warning-soft px-2 py-0.5 text-[11px] font-semibold text-warning">{t("sheet.unsaved")}</span>}
            <IconButton data-sheet-close icon="x" label={t("common.close")} onClick={requestClose} tip={false} />
          </header>
          {confirmDiscard && (
            <div role="alertdialog" aria-label={t("sheet.discard.q")} className="flex flex-wrap items-center gap-2 border-b border-warning/40 bg-warning-soft px-4 py-2.5 text-[13px]">
              <span className="flex-1 text-fg">{t("sheet.discard.q")}</span>
              <Button size="sm" onClick={() => setConfirmDiscard(false)} autoFocus>{t("sheet.keep")}</Button>
              <Button size="sm" variant="danger" onClick={() => { setConfirmDiscard(false); onOpenChange(false); }}>{t("sheet.discard")}</Button>
            </div>
          )}
          <div ref={(el) => { scroller.current = el; setScrollEl(el); }} className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 md:px-5">{children}</div>
          {footer && <footer className="flex flex-wrap justify-end gap-2 border-t border-line bg-raised px-4 py-3 md:px-5">{footer}</footer>}
        </DialogP.Content>
      </DialogP.Portal>
    </DialogP.Root>
  );
}

/* ------------------------------------------------------------------ toasts */
export interface ToastItem { id: number; kind: "success" | "error" | "info" | "warning"; title: string; sub?: string; action?: { label: string; run: () => void }; persistent?: boolean }
const ToastCtx = createContext<(t: Omit<ToastItem, "id">) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  const push = useCallback((x: Omit<ToastItem, "id">) => {
    const id = ++seq.current;
    setItems((xs) => [...xs.slice(-3), { ...x, id }]);
    if (!x.persistent) setTimeout(() => dismiss(id), x.kind === "error" ? 8000 : 4000);
  }, [dismiss]);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed inset-x-3 bottom-3 z-[95] flex flex-col items-center gap-2 md:inset-x-auto md:end-5 md:items-end" aria-live="polite" role="status">
        {items.map((x) => (
          <div key={x.id} role={x.kind === "error" ? "alert" : undefined}
            className="qm-pop pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-md border border-line bg-raised px-3.5 py-3 shadow-raised">
            <span className={clsx("mt-0.5", { "text-success": x.kind === "success", "text-danger": x.kind === "error", "text-info": x.kind === "info", "text-warning": x.kind === "warning" })}>
              <Icon name={x.kind === "success" ? "check" : x.kind === "info" ? "info" : "alert"} size={17} />
            </span>
            <div className="min-w-0 flex-1"><b className="block text-[13px] font-semibold text-fg">{x.title}</b>{x.sub && <p className="text-[12.5px] text-fg-2">{x.sub}</p>}</div>
            {x.action && <button type="button" className="text-[12.5px] font-semibold text-accent-fg" onClick={() => { x.action!.run(); dismiss(x.id); }}>{x.action.label}</button>}
            <button type="button" aria-label={t("common.close")} className="text-fg-3 hover:text-fg" onClick={() => dismiss(x.id)}><Icon name="x" size={15} /></button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
