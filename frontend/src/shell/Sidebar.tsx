import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { SortProps } from "./SortableNav";
import clsx from "clsx";
import { Icon } from "../components/ui/Icon";
import { IconButton, Meter, Tip } from "../components/ui/primitives";
import { useToast } from "../components/ui/overlays";
import { t, tf, num, getLang } from "../lib/i18n";
import { usePrefs, setPref } from "../lib/prefs";
import { GROUPS, mergeOrder, prefetchRoute, routeById, type RouteId } from "./routes";
import type { Dashboard } from "../lib/types";
import type { LiveState } from "../lib/live";

type DndMod = typeof import("./SortableNav");
let dndMod: DndMod | null = null;
let dndLoading: Promise<DndMod> | null = null;
const loadDnd = () => (dndLoading ||= import("./SortableNav").then((m) => (dndMod = m)).catch((error) => { dndLoading = null; throw error; }));

interface Props { route: RouteId; dash?: Dashboard; live: LiveState; onNavigate?: () => void; rail?: boolean; onLogout: () => void; mobile?: boolean }

export function Sidebar({ route, dash, live, onNavigate, rail, onLogout, mobile }: Props) {
  const prefs = usePrefs();
  const navId = useId();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [loadingEditor, setLoadingEditor] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const [dnd, setDnd] = useState<DndMod | null>(dndMod);
  const toggleEditing = async () => {
    if (editing) { setEditing(false); return; }
    setLoadingEditor(true);
    try { setDnd(await loadDnd()); setEditing(true); }
    catch { toast({ kind: "error", title: t("nav.edit.failed") }); }
    finally { setLoadingEditor(false); }
  };
  const order = useMemo(() => mergeOrder(prefs.navOrder), [prefs.navOrder]);
  useLayoutEffect(() => {
    const area = scroll.current;
    const active = area?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!area || !active || !area.clientHeight) return;
    const bounds = area.getBoundingClientRect(), item = active.getBoundingClientRect();
    if (item.top < bounds.top + 8) area.scrollTop -= bounds.top + 8 - item.top;
    else if (item.bottom > bounds.bottom - 8) area.scrollTop += item.bottom - bounds.bottom + 8;
  }, [route, rail, prefs.lang, order]);
  const label = (id: string) => t("nav." + id);

  const counts: Partial<Record<RouteId, { n: number; tone: "neutral" | "warning" }>> = {};
  if (dash) {
    const people = dash.users.filter((u) => !u.protected).length;
    if (people) counts.people = { n: people, tone: "neutral" };
    const attention = dash.users.filter((u) => u.guest && u.blocked).length + (dash.rogue || []).length + (dash.security?.default_password ? 1 : 0) + (dash.internet === false ? 1 : 0);
    if (attention) counts.overview = { n: attention, tone: "warning" };
  }
  // Optimistic: the new order shows at once; if this browser cannot store it, say so (it still applies for this visit).
  const save = (next: Record<string, string[]>) => {
    if (!setPref("navOrder", { v: 1, groups: next })) toast({ kind: "warning", title: t("nav.order.nosave") });
  };
  const b = dash?.bundle, pct = b && b.total_gb > 0 ? (b.used_gb / b.total_gb) * 100 : 0;
  const liveTxt = live === "live" ? t("conn.live") : live === "polling" ? t("conn.polling") : live === "offline" ? t("conn.offline") : t("conn.reconnecting");
  return (
    <aside className={clsx("qm-sidebar flex h-full min-h-0 flex-col gap-2 bg-frame py-3", rail ? "w-[72px] items-center px-2" : "w-[248px] px-3", !mobile && "border-e border-line")} aria-label={t("nav.aria")}>
      <div className={clsx("flex shrink-0 items-center gap-2.5 px-1.5", rail && "flex-col px-0")}>
        <img src="/logo.svg" alt="" className="size-7" />
        {!rail && <div className="min-w-0 flex-1 leading-tight"><div className="truncate text-[14.5px] font-semibold text-fg" dir="ltr">QuotaManager</div><div className="truncate text-[11.5px] text-fg-3">{t("nav.gateway")}{dash?.version ? ` · v${dash.version}` : ""}</div></div>}
        {mobile ? <IconButton icon="x" label={t("common.close")} onClick={onNavigate} className="size-8" /> : <IconButton icon="panel" label={rail ? t("common.expand") : t("common.collapse")} aria-expanded={!rail} onClick={() => { setEditing(false); setPref("rail", !rail); }} className={clsx(rail ? "rotate-180 rtl:rotate-0" : "rtl:rotate-180", "size-8")} size={16} />}
      </div>

      {!rail && <div className="flex min-h-8 shrink-0 items-center justify-between gap-2 px-1.5">
        <span className="text-[11.5px] text-fg-3">{editing ? t("nav.edit.hint") : t("nav.browse")}</span>
        <button type="button" aria-pressed={editing} aria-busy={loadingEditor || undefined} disabled={loadingEditor} onClick={toggleEditing}
          className="inline-flex min-h-8 shrink-0 items-center gap-1.5 rounded-md px-2 text-[11.5px] font-semibold text-fg-2 hover:bg-raised hover:text-fg">
          <Icon name={editing ? "check" : "edit"} size={13} />{t(editing ? "nav.done" : "nav.customize")}
        </button>
      </div>}
      {editing && !rail && <button type="button" onClick={() => { setPref("navOrder", null); toast({ kind: "success", title: t("nav.order.reset.done") }); }}
        className="self-start rounded-md px-2 py-1 text-[11.5px] text-accent-fg hover:bg-raised">{t("nav.order.reset")}</button>}

      <div ref={scroll} className="qm-sidebar-scroll min-h-0 w-full flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-0.5">
        <nav className="flex flex-col gap-3" aria-label={t("nav.aria")}>
          {GROUPS.map((g) => (
            <div key={g.id} role="group" aria-labelledby={rail ? undefined : `${navId}-${g.id}`} aria-label={rail ? t(g.label) : undefined} className={clsx(rail && "w-full border-t border-line pt-3 first:border-0 first:pt-0")}>
              {!rail && <div id={`${navId}-${g.id}`} className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-[.08em] text-fg-3 rtl:text-[12px] rtl:normal-case rtl:tracking-normal">{t(g.label)}</div>}
              <ul className="flex flex-col gap-0.5">
                {(() => {
                  const ids = order[g.id] || [];
                  const row = (id: string, sp?: SortProps) => (
                    <NavItem key={id} id={id as RouteId} active={route === id} rail={!!rail} count={counts[id as RouteId]} sp={sp}
                      onNavigate={onNavigate} sortable={editing && !rail && ids.length > 1} />
                  );
                  return dnd && editing && !rail && ids.length > 1
                    ? <dnd.SortableGroup ids={ids} labelOf={label} onReorder={(next) => save({ ...order, [g.id]: next })}>{(id, sp) => row(id, sp)}</dnd.SortableGroup>
                    : ids.map((id) => row(id));
                })()}
              </ul>
            </div>
          ))}
        </nav>

      </div>

      {b && (
        <a href="#/quota" onClick={onNavigate} aria-label={rail ? `${t("side.bundle")}: ${num(pct, 0)}%` : undefined}
          className={clsx("qm-sidebar-bundle block w-full shrink-0 rounded-md border border-line bg-surface hover:border-line-strong", rail ? "px-1 py-2 text-center" : "px-2.5 py-2")}>
          {rail ? <b className="text-[11px] text-fg tabular-nums">{num(pct, 0)}%</b> :
            <div className="flex items-baseline justify-between text-[12px] text-fg-2"><span>{t("side.bundle")}</span><b className="text-[13px] text-fg tabular-nums">{num(pct, 0)}%</b></div>}
          <Meter value={pct} className="my-1.5" label={t("side.bundle")} />
          {!rail && <div className="text-[12px] text-fg-3">{num(Math.max(0, b.remaining_gb))} {t("common.gb")} {t("side.left")}{b.days_left >= 0 ? ` \u00b7 ${tf("side.resets", { n: num(b.days_left, 0) })}` : ""}</div>}
        </a>
      )}

      <div className={clsx("flex shrink-0 items-center gap-2 border-t border-line px-1.5 pt-2", rail && "flex-col")}>
        <span className="grid size-7 place-items-center rounded-full border border-line bg-surface text-[12px] font-semibold text-fg-2" aria-hidden>A</span>
        {!rail && <div className="min-w-0 flex-1 leading-tight"><b className="block text-[13px] text-fg">{t("nav.admin")}</b>
          <span className="flex items-center gap-1.5 text-[11.5px] text-fg-3"><span className={clsx("size-1.5 rounded-full", live === "live" ? "bg-success" : live === "offline" ? "bg-danger" : "bg-warning")} aria-hidden />{liveTxt}</span></div>}
        <IconButton icon="logout" label={t("common.logout")} onClick={onLogout} size={16} className="size-8" />
      </div>
    </aside>
  );
}

function NavItem({ id, active, rail, count, onNavigate, sortable, sp }: {
  id: RouteId; active: boolean; rail: boolean; count?: { n: number; tone: "neutral" | "warning" }; onNavigate?: () => void;
  sortable: boolean; sp?: SortProps;
}) {
  const r = routeById(id)!;
  const name = t("nav." + id);
  const link = (
    <a href={"#/" + id} aria-label={rail ? name : undefined} aria-current={active ? "page" : undefined} onClick={onNavigate}
      onPointerEnter={() => prefetchRoute(id)} onFocus={() => prefetchRoute(id)} onTouchStart={() => prefetchRoute(id)}
      className={clsx("relative flex h-9 min-w-0 flex-1 items-center gap-2.5 rounded-md text-[13px] transition-colors max-md:h-11",
        rail ? "w-11 flex-none justify-center" : "px-2.5",
        active ? "bg-raised font-semibold text-fg before:absolute before:inset-y-2 before:start-0 before:w-[3px] before:rounded-full before:bg-accent" : "font-medium text-fg-2 hover:bg-raised/70 hover:text-fg")}>
      <Icon name={r.icon} size={17} className={active ? "text-fg" : ""} />
      {!rail && <span className="min-w-0 flex-1 truncate">{name}</span>}
      {count && (rail
        ? <span className={clsx("absolute end-1.5 top-1.5 size-2 rounded-full", count.tone === "warning" ? "bg-warning" : "bg-fg-3")} aria-label={tf("nav.count", { n: count.n })} />
        : <span className={clsx("min-w-5 rounded-full px-1.5 text-center text-[11px] font-semibold tabular-nums", count.tone === "warning" ? "bg-warning-soft text-warning" : "bg-canvas text-fg-3")} aria-label={tf("nav.count", { n: count.n })}>{num(count.n, 0)}</span>)}
    </a>
  );
  return (
    <li ref={sp?.setNodeRef} style={sp?.style} className={clsx("group/nav relative flex shrink-0 items-center", sp?.isDragging && "z-10 rounded-md bg-surface shadow-raised", sp?.isOver && !sp?.isDragging && "qm-drop-line")}>
      {sortable && !rail && (
        // The six-dot handle is the only drag source: clicking the row still navigates.
        <button type="button" {...(sp?.handle || {})} aria-label={`${t("nav.reorder")}: ${name}`} aria-roledescription={t("nav.handle")}
          className={clsx("grid h-9 w-6 shrink-0 cursor-grab touch-none place-items-center rounded-md text-fg-3 hover:bg-raised hover:text-fg active:cursor-grabbing max-md:h-11")}>
          <Icon name="grip" size={14} />
        </button>
      )}
      {rail ? <Tip label={name} side={getLang() === "ar" ? "left" : "right"}>{link}</Tip> : link}
    </li>
  );
}
