import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useDashboard } from "../lib/session";
import { useLiveTraffic } from "../lib/queries";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { usePrefs, setPref } from "../lib/prefs";
import { t, tf, num, iso } from "../lib/i18n";
import { fmtBps, devIcon, allDevices } from "../lib/format";
import { Badge, Button, Card, Empty, Loading, Stat } from "../components/ui/primitives";
import { Segmented, Combobox } from "../components/ui/forms";
import { RankList } from "../components/ui/charts";
import { Icon } from "../components/ui/Icon";

type Tab = "live" | "apps" | "websites" | "timeline" | "domains";
const WINDOWS: [number, string][] = [[1, "1h"], [6, "6h"], [24, "24h"], [72, "3d"], [168, "7d"], [336, "14d"]];
interface Hist { total_queries: number; top_domains?: { domain: string; hits: number; status?: string }[]; activity?: { bucket_minute: string; count: number }[]; recent?: { device_id?: number; domain: string; bucket_minute: string; count: number; status?: string }[];
  analytics?: { top_apps?: { name: string; category?: string; queries: number; percentage: number; color?: string }[]; top_websites?: { domain: string; queries: number; percentage: number }[]; timeline?: { slot: string; label: string; queries: number }[] } }

/** "Who is using the line right now, and for what?" */
export default function Insights() {
  const d = useDashboard();
  const prefs = usePrefs();
  const [tab, setTab] = useState<Tab>("live");
  const [device, setDevice] = useState("all");
  const win = WINDOWS.some(([w]) => w === prefs.insightsWindow) ? prefs.insightsWindow : 24;
  // History: cached 60 s per (device, window). Changing the selection aborts the old request.
  const hist = useQuery({ queryKey: ["hist", device, win], enabled: tab !== "live", staleTime: 60_000,
    queryFn: ({ signal }) => api.get<Hist>(`/api/history/${encodeURIComponent(device)}?window=${win}&limit=200`, signal) });
  const devOpts = useMemo(() => [{ value: "all", label: t("ins.all") }, ...(d ? allDevices(d.users).map((x) => ({ value: String(x.id), label: x.name || x.mac, sub: x.owner })) : [])], [d]);
  return (
    <div className="flex flex-col gap-[var(--gap)]">
      <div className="flex flex-wrap items-end gap-3">
        <Combobox className="w-full sm:w-72" label={t("ins.device")} value={device} onChange={setDevice} options={devOpts} />
        {tab !== "live" && <div className="flex flex-col gap-1.5"><span className="text-[12.5px] font-medium text-fg-2">{t("ins.window")}</span>
          <Segmented label={t("ins.window")} value={String(win)} onChange={(v) => setPref("insightsWindow", +v)} options={WINDOWS.map(([v, l]) => ({ value: String(v), label: l }))} /></div>}
        {device !== "all" && <Button size="sm" variant="ghost" icon="reset" onClick={() => setDevice("all")}>{t("f.clearall")}</Button>}
      </div>
      <Segmented label={t("ins.view")} value={tab} onChange={setTab} options={(["live", "apps", "websites", "timeline", "domains"] as Tab[]).map((k) => ({ value: k, label: t("ins.tab." + k) }))} />
      {tab === "live" ? <LiveView device={device} onPick={(id) => { setDevice(id); setTab("apps"); }} />
        : hist.isError ? <Empty icon="alert" title={t("ins.fail")} sub={(hist.error as Error).message} action={<Button size="sm" onClick={() => hist.refetch()}>{t("conn.retry")}</Button>} />
        : !hist.data ? <Loading what={t("ins.loading")} h={260} />
        : !hist.data.total_queries ? <Empty icon="insights" title={t("ins.empty")} sub={t("ins.empty.sub")} action={win < 336 ? <Button size="sm" onClick={() => setPref("insightsWindow", 336)}>{t("ins.widen")}</Button> : undefined} />
        : <HistView tab={tab} h={hist.data} device={device} fetching={hist.isFetching} />}
    </div>
  );
}

function LiveView({ device, onPick }: { device: string; onPick: (id: string) => void }) {
  const live = useLiveTraffic(device, 3000);
  const d = live.data;
  if (!d) return <Loading what={t("ov.loading.live")} h={220} />;
  const top = d.top_consumer, devs = d.devices || [];
  const sites = (x: (typeof devs)[number]) => {
    const dest = (x.top_destinations || []).slice(0, 4);
    if (dest.length) return dest.map((s) => <Badge key={s.domain} icon={false} className="qm-ltr">{s.domain} · {fmtBps(Math.max(s.up_bps || 0, s.down_bps || 0))}</Badge>);
    const rs = (x.recent_sites?.length ? x.recent_sites.map((s) => s.domain) : x.recent_domains || []).slice(0, 4);
    return rs.length ? rs.map((s) => <Badge key={s} icon={false} className="qm-ltr">{s}</Badge>) : [<span key="n" className="text-[12px] text-fg-3">{t("ins.live.nosites")}</span>];
  };
  return (
    <Card>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4"><Stat label={t("ins.live.down")} value={fmtBps(d.total_down_bps)} /><Stat label={t("ins.live.up")} value={fmtBps(d.total_up_bps)} /></div>
      {top && !top.is_idle && top.total_bps > 0 ? (
        <div className="mt-4 rounded-md border border-line bg-canvas p-3">
          <div className="text-[12px] text-fg-3">{t("ins.live.top")}</div>
          <div className="mt-1 flex flex-wrap items-center gap-3"><Icon name={devIcon(top)} size={18} className="text-fg-3" /><b className="text-[16px] text-fg">{top.name}</b>
            <span className="text-[12.5px] text-fg-3">{top.user_name} {top.ip ? <>· {iso(top.ip)}</> : null}</span>
            <span className="ms-auto text-[12.5px] font-semibold tabular-nums text-fg" dir="ltr">↓ {fmtBps(top.down_bps)} ↑ {fmtBps(top.up_bps)}</span><Badge tone="accent" icon={false}>{num(top.share_pct, 0)}%</Badge></div>
          <div className="mt-2 flex flex-wrap gap-1.5">{sites(top)}</div>
        </div>
      ) : <Empty icon="network" title={t("ins.live.quiet")} sub={t("ins.live.quiet.d")} />}
      <RankList rows={devs.map((x, i) => ({ key: x.id, label: <>{`#${i + 1} `}{x.name}</>, sub: x.user_name, value: x.is_idle ? t("ins.idle") : `${num(x.share_pct, 0)}% · ↓ ${fmtBps(x.down_bps)} ↑ ${fmtBps(x.up_bps)}`, pct: x.share_pct || 0,
        onClick: () => onPick(String(x.id)), extra: <div className="mt-1.5 flex flex-wrap gap-1.5">{sites(x)}</div> }))} />
      <p className="mt-2 text-[12px] text-fg-3">{t("ins.live.note")}</p>
    </Card>
  );
}

function HistView({ tab, h, device, fetching }: { tab: Tab; h: Hist; device: string; fetching: boolean }) {
  const d = useDashboard();
  const { run, pending } = useAction();
  const an = h.analytics || {};
  const topApp = an.top_apps?.[0], topWeb = an.top_websites?.[0];
  const quick = (domain: string, action: "block" | "allow", scope: "device" | "global") =>
    run(`${action}${scope}${domain}`, () => api.post("/api/dns/rules/quick", { domain, action, scope, device_id: scope === "device" ? +device : null }), tf(`ins.${action}.done`, { d: domain }));
  const nameOf = (id?: number) => { const x = d ? allDevices(d.users).find((y) => y.id === id) : null; return x ? x.name || x.mac : id ? "#" + id : ""; };
  const tone = (s?: string) => (s === "blocked" ? "danger" : s === "allowed" ? "success" : s === "redirected" ? "info" : "neutral") as "danger" | "success" | "info" | "neutral";
  const tiles = (
    <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Stat label={t("ins.queries")} value={num(h.total_queries, 0)} />
      <Stat label={t("ins.topapp")} value={<span className="text-[15px]">{topApp ? `${topApp.name} (${num(topApp.percentage, 0)}%)` : "—"}</span>} />
      <Stat label={t("ins.topweb")} value={<span className="qm-ltr text-[15px]">{topWeb ? `${topWeb.domain} (${num(topWeb.percentage, 0)}%)` : "—"}</span>} />
      <Stat label={t("ins.domains")} value={num((h.top_domains || []).length, 0)} />
    </div>
  );
  const note = <p className="mt-3 text-[12px] text-fg-3">{t("ins.dns.note")}{fetching ? ` · ${t("ins.refreshing")}` : ""}</p>;
  if (tab === "apps") return <Card>{tiles}{an.top_apps?.length ? <RankList rows={an.top_apps.map((a, i) => ({ key: a.name, label: a.name, sub: a.category, value: `${num(a.queries, 0)} · ${num(a.percentage, 0)}%`, pct: a.percentage, color: `var(--chart-${(i % 6) + 1})` }))} /> : <Empty title={t("ins.apps.none")} />}{note}</Card>;
  if (tab === "websites") return <Card>{tiles}{an.top_websites?.length ? <RankList rows={an.top_websites.map((w) => ({ key: w.domain, label: <span className="qm-ltr">{w.domain}</span>, value: `${num(w.queries, 0)} · ${num(w.percentage, 0)}%`, pct: w.percentage }))} /> : <Empty title={t("ins.empty")} />}{note}</Card>;
  if (tab === "timeline") {
    const tl = an.timeline || [], mx = Math.max(1, ...tl.map((x) => x.queries));
    return <Card title={t("ins.timeline.t")} sub={t("ins.timeline.d")}>{tl.length ? (
      <div className="flex h-56 items-end gap-1 border-b border-line" dir="ltr" role="list">
        {tl.map((x) => <div key={x.slot} role="listitem" aria-label={`${x.label}: ${x.queries}`} title={`${x.slot}: ${num(x.queries, 0)}`} className="flex h-full flex-1 flex-col justify-end">
          <span className="rounded-t-[3px] bg-chart-1" style={{ height: `${Math.max(2, (x.queries / mx) * 100)}%` }} /><span className="mt-1 truncate text-center text-[10px] text-fg-3">{x.label}</span></div>)}
      </div>) : <Empty title={t("ins.empty")} />}{note}</Card>;
  }
  const total = h.total_queries || 0;
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Card className="col-span-12" title={t("ins.top")} sub={t("ins.top.d")}>
        <ul className="divide-y divide-line">{(h.top_domains || []).map((r) => (
          <li key={r.domain} className="flex flex-wrap items-center gap-2 py-2">
            <b className="qm-ltr min-w-0 flex-1 truncate text-[13px] text-fg">{r.domain}</b><Badge tone={tone(r.status)}>{t("ins.st." + (r.status || "none"))}</Badge>
            <span className="w-28 text-end text-[12px] text-fg-3 tabular-nums">{num(r.hits, 0)} · {total ? num((r.hits / total) * 100, 1) : "0"}%</span>
            {device !== "all" && <Button size="sm" variant="ghost" pending={pending === `blockdevice${r.domain}`} onClick={() => quick(r.domain, "block", "device")}>{t("ins.block.dev")}</Button>}
            <Button size="sm" variant="ghost" pending={pending === `blockglobal${r.domain}`} onClick={() => quick(r.domain, "block", "global")}>{t("ins.block.all")}</Button>
            <Button size="sm" variant="ghost" pending={pending === `allowglobal${r.domain}`} onClick={() => quick(r.domain, "allow", "global")}>{t("ins.allow")}</Button>
          </li>
        ))}</ul>
      </Card>
      <Card className="col-span-12" title={t("ins.recent")} sub={t("ins.recent.d")}>
        <ul className="max-h-96 divide-y divide-line overflow-y-auto">{(h.recent || []).map((r, i) => (
          <li key={i} className="flex items-center gap-3 py-2"><span className="qm-ltr min-w-0 flex-1 truncate text-[13px] text-fg">{r.device_id ? <span className="text-fg-3">[{nameOf(r.device_id)}] </span> : null}{r.domain}</span>
            <span className="qm-ltr font-mono text-[11.5px] text-fg-3">{r.bucket_minute} × {r.count}</span><Badge tone={tone(r.status)}>{t("ins.st." + (r.status || "none"))}</Badge></li>
        ))}</ul>
      </Card>
    </div>
  );
}
