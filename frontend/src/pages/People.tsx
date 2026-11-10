import { useDeferredValue, useMemo, useState } from "react";
import clsx from "clsx";
import { api } from "../lib/api";
import { useDashboard } from "../lib/session";
import { useLiveTraffic } from "../lib/queries";
import { usePrefs, setPref } from "../lib/prefs";
import { actions, useAction } from "../lib/actions";
import { t, tf, num, iso, mask } from "../lib/i18n";
import { gb, userStatus, deviceStatus, devIcon, fmtBps } from "../lib/format";
import type { User } from "../lib/types";
import { Badge, Button, Card, Empty, Loading, Meter, IconButton, Term } from "../components/ui/primitives";
import { Segmented, Select, SwitchRow, controlClass } from "../components/ui/forms";
import { Checkbox } from "../components/ui/checkbox";
import { ConfirmDialog, useToast } from "../components/ui/overlays";
import { MoreMenu } from "../components/ui/menu";
import { Icon } from "../components/ui/Icon";
import { PersonSheet, DeviceSheet } from "../components/domain/PersonSheet";
import { AddPersonDialog, AddDeviceDialog } from "../components/domain/AddDialogs";
import { PageActions } from "../shell/App";

type Filter = "all" | "online" | "blocked" | "guest";
type Sort = "default" | "usage" | "percent" | "name";
const is: Record<Filter, (u: User) => boolean> = {
  all: () => true, online: (u) => !u.blocked && (u.devices || []).some((x) => x.connected),
  blocked: (u) => u.blocked && !u.guest, guest: (u) => u.guest,
};

/** "Who has access, and how much did they use?" */
export default function People() {
  const d = useDashboard();
  const prefs = usePrefs();
  const live = useLiveTraffic("all");
  const toast = useToast();
  const [q, setQ] = useState("");
  const query = useDeferredValue(q); // typing stays instant even with many devices
  const filter = (prefs.peopleFilter as Filter) in is ? (prefs.peopleFilter as Filter) : "all";
  const sort = (["default", "usage", "percent", "name"].includes(prefs.peopleSort) ? prefs.peopleSort : "default") as Sort;
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [person, setPerson] = useState<number | null>(null), [device, setDevice] = useState<{ u: number; d: number } | null>(null);
  const [add, setAdd] = useState<null | "person" | "device">(null);
  const [bulk, setBulk] = useState<null | "block" | "unblock">(null);
  const { run, pending } = useAction();

  const all = useMemo(() => (d?.users || []).map((u, i) => [u, i] as const).sort((a, b) => (+b[0].protected - +a[0].protected) || a[1] - b[1]).map((x) => x[0]), [d]);
  const rows = useMemo(() => {
    const s = query.trim().toLowerCase();
    const list = all.filter((u) => is[filter](u) && (!s || u.name.toLowerCase().includes(s) || (u.devices || []).some((x) => (x.name || "").toLowerCase().includes(s) || x.mac.toLowerCase().includes(s) || (x.ip || "").includes(s))));
    if (sort === "usage") list.sort((a, b) => b.used_gb - a.used_gb);
    else if (sort === "percent") list.sort((a, b) => (b.allowance_gb > 0 ? b.percent : -1) - (a.allowance_gb > 0 ? a.percent : -1));
    else if (sort === "name") list.sort((a, b) => a.name.localeCompare(b.name));
    return list;
  }, [all, filter, sort, query]);
  if (!d) return <Loading what={t("boot.reading")} h={320} />;
  const liveBy = new Map((live.data?.devices || []).map((x) => [x.id, x]));
  const filtersOn = filter !== "all" || sort !== "default" || q.trim() !== "";
  const clear = () => { setQ(""); setPref("peopleFilter", "all"); setPref("peopleSort", "default"); };
  const selectable = rows.filter((u) => !u.protected);
  const selUsers = all.filter((u) => sel.has(u.id));
  const allSel = selectable.length > 0 && selectable.every((u) => sel.has(u.id));
  const toggleSel = (id: number) => setSel((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const toggleOpen = (id: number) => setOpen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const doBulk = async (block: boolean) => {
    let ok = 0; const fail: string[] = [];
    for (const u of selUsers) { try { await actions.blockUser(u.id, block); ok++; } catch { fail.push(u.name); } }
    await run("bulk-refresh", async () => {}, tf(block ? "pp.bulk.blocked" : "pp.bulk.unblocked", { n: num(ok, 0) }), fail.length ? tf("pp.bulk.partial", { names: fail.join(", ") }) : undefined);
    if (fail.length) toast({ kind: "warning", title: t("pp.bulk.partial.t"), sub: fail.join(", "), persistent: true });
    setSel(new Set());
    if (ok === 0 && fail.length) throw new Error(t("act.failed"));
  };
  const affectedDevs = selUsers.flatMap((u) => u.devices || []);
  return (
    <div className="flex flex-col gap-[var(--gap)]">
      <PageActions>
        <Button size="sm" icon="plus" onClick={() => setAdd("device")}>{t("add.dev")}</Button>
        <Button size="sm" variant="primary" icon="userplus" onClick={() => setAdd("person")}>{t("add.user")}</Button>
      </PageActions>
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative min-w-52 flex-1 sm:max-w-80">
          <span className="qm-sr">{t("pp.search")}</span>
          <Icon name="search" size={16} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-fg-3" />
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("pp.search")} className={clsx(controlClass, "ps-9")} />
        </label>
        <Segmented label={t("pp.filter")} value={filter} onChange={(v) => setPref("peopleFilter", v)}
          options={(["all", "online", "blocked", "guest"] as Filter[]).map((f) => ({ value: f, label: t("pp.f." + f), count: all.filter(is[f]).length }))} />
        <div className="w-44"><Select label={t("pp.sort")} labelHidden value={sort} onChange={(v) => setPref("peopleSort", v)}
          options={(["default", "usage", "percent", "name"] as Sort[]).map((s) => ({ value: s, label: t("pp.sort." + s) }))} /></div>
        {filtersOn && <Button size="sm" variant="ghost" icon="reset" onClick={clear}>{t("f.clearall")}</Button>}
      </div>

      {sel.size > 0 && (
        <div className="sticky top-16 z-20 flex flex-wrap items-center gap-2 rounded-md border border-accent/40 bg-accent-soft px-3 py-2" role="region" aria-label={t("pp.bulk")}>
          <b className="text-[13px] text-fg">{tf("pp.sel.n", { n: num(sel.size, 0) })}</b>
          <span className="flex-1" />
          <Button size="sm" variant="danger" icon="ban" onClick={() => setBulk("block")}>{t("pp.bulk.block")}</Button>
          <Button size="sm" icon="check" onClick={() => setBulk("unblock")}>{t("pp.bulk.unblock")}</Button>
          <Button size="sm" variant="ghost" onClick={() => setSel(new Set())}>{t("pp.sel.clear")}</Button>
        </div>
      )}

      <Card className="!p-0" title={undefined}>
        <div className="hidden grid-cols-[28px_28px_minmax(0,1.4fr)_minmax(0,1.3fr)_120px_120px_40px] items-center gap-3 border-b border-line px-4 py-2 text-[12px] font-semibold text-fg-3 lg:grid" aria-hidden="false">
          <span><Checkbox checked={allSel ? true : sel.size ? "indeterminate" : false} onChange={(v) => setSel(v ? new Set(selectable.map((u) => u.id)) : new Set())} label={t("pp.sel.all")} /></span>
          <span /><span>{t("pp.col.user")}</span><span>{t("pp.col.usage")} (<Term k="wan">WAN</Term>)</span><span>{t("pp.col.status")}</span><span className="text-end">{t("pp.col.access")}</span><span />
        </div>
        {!rows.length ? (
          filtersOn ? <Empty icon="filter" title={t("pp.empty")} sub={t("pp.empty.sub")} action={<Button size="sm" icon="reset" onClick={clear}>{t("f.clearall")}</Button>} />
            : <Empty icon="people" title={t("pp.none")} sub={t("pp.none.sub")} action={<Button size="sm" variant="primary" icon="userplus" onClick={() => setAdd("person")}>{t("add.user")}</Button>} />
        ) : (
          <ul className="divide-y divide-line">
            {rows.map((u) => {
              const st = userStatus(u), devs = [...(u.devices || [])].sort((a, b) => +b.gateway - +a.gateway), isOpen = open.has(u.id), on = devs.filter((x) => x.connected).length;
              return (
                <li key={u.id} className={clsx(u.blocked && "bg-canvas/60")}>
                  <div className="grid grid-cols-[28px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 px-4 py-3 lg:grid-cols-[28px_28px_minmax(0,1.4fr)_minmax(0,1.3fr)_120px_120px_40px]">
                    <span className="lg:order-none">{!u.protected ? <Checkbox checked={sel.has(u.id)} onChange={() => toggleSel(u.id)} label={tf("pp.sel.one", { name: u.name })} /> : <Icon name="lock" size={15} className="text-fg-3" label={t("common.protected")} />}</span>
                    <IconButton icon="chev" label={tf(isOpen ? "pp.hide.devs" : "pp.show.devs", { name: u.name })} aria-expanded={isOpen} onClick={() => toggleOpen(u.id)} size={16}
                      className={clsx("hidden size-7 lg:inline-grid", isOpen ? "rotate-90" : "rtl:rotate-180")} tip={false} />
                    <button type="button" onClick={() => setPerson(u.id)} className="min-w-0 text-start">
                      <span className="flex items-center gap-2"><b className={clsx("truncate text-[13.5px]", u.blocked ? "text-fg-2" : "text-fg")}>{u.name}</b>{u.protected && <Badge icon={false}>{t("common.protected")}</Badge>}{u.guest && <Badge tone="info" icon={false}>{t("common.guest")}</Badge>}</span>
                      <span className="block text-[12px] text-fg-3">{tf("pp.devs.on", { n: num(devs.length, 0), o: num(on, 0) })}</span>
                    </button>
                    <div className="col-span-3 min-w-0 lg:col-span-1">
                      <div className="flex items-baseline justify-between gap-2 text-[12.5px] tabular-nums"><b className="text-fg">{gb(u.used_gb)}</b>
                        <span className="text-fg-3">{u.allowance_gb > 0 ? tf("pp.of.pct", { a: num(u.allowance_gb), p: num(u.percent, 0) }) : t("pp.unlimited")}</span></div>
                      <Meter value={u.allowance_gb > 0 ? u.percent : 0} thin className="mt-1" label={u.name} />
                      <div className="mt-1 flex flex-wrap gap-x-3 text-[11.5px] text-fg-3 tabular-nums"><span>LAN {gb(u.lan_used_gb || 0)}</span>
                        {u.daily?.active && <span className={clsx(u.daily.blocked && "text-warning")}>{t("dq.today")}: {num(u.daily.used_gb)} {t("ov.of")} {num(u.daily.cap_gb)} {t("common.gb")}</span>}</div>
                    </div>
                    <span className="col-start-3 row-start-1 lg:col-auto lg:row-auto"><Badge tone={st.tone} title={st.reason}>{st.text}</Badge>{st.reason && <span className="qm-sr">{st.reason}</span>}</span>
                    <span className="hidden text-end lg:block">
                      {u.blocked ? <Button size="sm" onClick={() => run("u" + u.id, () => actions.blockUser(u.id, false), tf("pp.unblocked", { name: u.name }))}>{t("pp.unblock")}</Button>
                        : <Button size="sm" variant="ghost" className="text-danger" onClick={() => { setSel(new Set([u.id])); setBulk("block"); }}>{t("pp.block")}</Button>}
                    </span>
                    <span className="hidden lg:block"><MoreMenu label={tf("pp.more", { name: u.name })} items={[
                      { label: t("pp.manage"), icon: "edit", onSelect: () => setPerson(u.id) },
                      { label: isOpen ? t("pp.hide") : t("pp.show"), icon: "chev", onSelect: () => toggleOpen(u.id) },
                      u.blocked ? { label: t("pp.unblock"), icon: "check", onSelect: () => run("u" + u.id, () => actions.blockUser(u.id, false), tf("pp.unblocked", { name: u.name })) } : null,
                    ]} /></span>
                    <div className="col-span-3 flex gap-2 lg:hidden">
                      <Button size="sm" onClick={() => setPerson(u.id)} icon="edit">{t("pp.manage")}</Button>
                      <Button size="sm" variant="ghost" aria-expanded={isOpen} onClick={() => toggleOpen(u.id)}>{isOpen ? t("pp.hide") : tf("pp.show.n", { n: num(devs.length, 0) })}</Button>
                    </div>
                  </div>
                  {isOpen && (
                    <ul className="border-t border-line bg-canvas/50 ps-4 lg:ps-[84px]">
                      {devs.map((x) => { const ds = deviceStatus(x), lv = liveBy.get(x.id); return (
                        <li key={x.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line py-2.5 pe-4 last:border-0">
                          <button type="button" onClick={() => setDevice({ u: u.id, d: x.id })} className="flex min-w-0 flex-1 items-center gap-2.5 text-start">
                            <Icon name={devIcon(x)} size={16} className="text-fg-3" />
                            <span className="min-w-0"><b className="block truncate text-[13px] text-fg">{x.name || mask(x.mac)}</b><span className="qm-ltr font-mono text-[11.5px] text-fg-3">{mask(x.mac)}{x.ip ? ` · ${x.ip}` : ""}</span></span>
                          </button>
                          <span className="w-24 text-end text-[12.5px] font-semibold tabular-nums text-fg">{gb(x.device_used_gb)}</span>
                          <span className="w-36 text-end text-[11.5px] text-fg-3 tabular-nums" dir="ltr">{x.connected ? `↓ ${fmtBps(lv?.down_bps || 0)} ↑ ${fmtBps(lv?.up_bps || 0)}` : "—"}</span>
                          <Badge tone={ds.tone}>{ds.text}</Badge>
                        </li>
                      ); })}
                      {!devs.length && <li className="py-3 text-[12.5px] text-fg-3">{t("pp.nodevices")}</li>}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      {((d.rogue || []).length > 0 || d.unmanaged_block === false) && (
        <Card title={<Term k="rogue">{t("pp.rogue")}</Term>} sub={t("pp.rogue.d")} actions={<Badge tone="warning">{num(d.rogue.length, 0)}</Badge>}>
          <SwitchRow title={t("pp.rogue.sw")} desc={t("pp.rogue.sw.d")} checked={d.unmanaged_block !== false} pending={pending === "unmanaged-policy"} disabled={pending !== null}
            onChange={(enabled) => { void run("unmanaged-policy", () => api.post("/api/unmanaged-policy", { enabled }), t("act.done")); }} />
          <ul className="divide-y divide-line">
            {d.rogue.map((r, i) => (
              <li key={(r.mac || "") + i} className="flex flex-wrap items-center gap-3 py-2.5">
                <Icon name="alert" size={16} className="text-warning" />
                <span className="min-w-0 flex-1"><b className="qm-ltr block font-mono text-[13px] text-fg">{r.ip ? iso(r.ip) : "—"}</b><span className="qm-ltr font-mono text-[11.5px] text-fg-3">{[mask(r.mac || ""), r.vendor && r.vendor !== "Unknown" ? r.vendor : ""].filter(Boolean).join(" · ")}</span></span>
                <Badge tone={r.blocked ? "danger" : r.allowed ? "success" : "warning"}>{t(r.blocked ? "pp.rogue.cut" : r.allowed ? "pp.rogue.allowed" : "pp.rogue.open")}</Badge>
                <Button size="sm" variant="primary" disabled={pending !== null} onClick={() => { void run("adopt-" + r.mac, () => api.post("/api/rogue/adopt", { mac: r.mac, ip: r.ip }), t("pp.rogue.adopted")); }}>{t("pp.rogue.manage")}</Button>
                {!r.allowed && <Button size="sm" disabled={pending !== null} onClick={() => { void run("allow-" + r.mac, () => api.post("/api/rogue/allow", { mac: r.mac }), t("pp.rogue.allowed.done")); }}>{t("pp.rogue.allow")}</Button>}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <ConfirmDialog open={!!bulk} onOpenChange={(v) => { if (!v) { setBulk(null); if (sel.size === 1 && bulk === "block") setSel(new Set()); } }}
        title={tf(bulk === "block" ? "cf.bulk.block.t" : "cf.bulk.unblock.t", { n: num(selUsers.length, 0) })} danger={bulk === "block"}
        confirmLabel={tf(bulk === "block" ? "pp.bulk.block.cta" : "pp.bulk.unblock.cta", { n: num(selUsers.length, 0) })}
        consequences={{
          changes: t(bulk === "block" ? "cf.block.changes" : "cf.unblock.changes"),
          affected: <>{tf("cf.bulk.affected", { p: num(selUsers.length, 0), d: num(affectedDevs.length, 0), o: num(affectedDevs.filter((x) => x.connected).length, 0) })}
            <ul className="mt-1 max-h-32 list-disc overflow-y-auto ps-5 text-[12.5px] text-fg-2">{selUsers.map((u) => <li key={u.id}>{u.name} · {tf("pp.devs.n", { n: num((u.devices || []).length, 0) })}</li>)}</ul></>,
          undo: t(bulk === "block" ? "cf.block.undo" : "cf.unblock.undo"),
        }}
        onConfirm={() => doBulk(bulk === "block")} />
      <PersonSheet userId={person} onClose={() => setPerson(null)} />
      {device && <DeviceSheet userId={device.u} deviceId={device.d} onClose={() => setDevice(null)} />}
      <AddPersonDialog open={add === "person"} onOpenChange={(v) => setAdd(v ? "person" : null)} />
      <AddDeviceDialog open={add === "device"} onOpenChange={(v) => setAdd(v ? "device" : null)} users={all} />
    </div>
  );
}
