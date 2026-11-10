import { useMemo, useState } from "react";
import clsx from "clsx";
import { useDashboard, useSession } from "../lib/session";
import { useDailyUsage, useLiveTraffic, speedHistory } from "../lib/queries";
import { t, tf, num, iso, fmtDate, fmtTime } from "../lib/i18n";
import { fmtBps, gb, userStatus } from "../lib/format";
import { usePrefs, setPref } from "../lib/prefs";
import { actions, useAction } from "../lib/actions";
import { api } from "../lib/api";
import { Card, Badge, Meter, Button, Empty, Loading, Term, Stat } from "../components/ui/primitives";
import { Segmented } from "../components/ui/forms";
import { LineChart, BarChart, RankList } from "../components/ui/charts";
import { Icon } from "../components/ui/Icon";
import { TopUpDialog } from "../components/domain/TopUp";
import { PersonSheet } from "../components/domain/PersonSheet";
import { PageActions } from "../shell/App";
import type { Dashboard } from "../lib/types";

const DAY = 86400000;
function pace(b: Dashboard["bundle"]) {
  const start = b.period_start ? new Date(b.period_start + "T00:00:00").getTime() : null;
  const end = b.period_end ? new Date(b.period_end + "T00:00:00").getTime() : null;
  const elapsed = start ? Math.max(1, Math.ceil((Date.now() - start) / DAY)) : null;
  const total = start && end ? Math.max(1, Math.round((end - start) / DAY)) : null;
  const avg = elapsed ? b.used_gb / elapsed : null;
  const forecast = avg != null && total ? avg * total : null;
  return { elapsed, avg, forecast, end };
}

/** "Is my internet working, who is using it, and how much is left?" */
export default function Overview() {
  const d = useDashboard();
  const [topup, setTopup] = useState(false);
  const [person, setPerson] = useState<number | null>(null);
  if (!d) return <Loading what={t("boot.reading")} h={320} />;
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <PageActions><Button size="sm" variant="primary" icon="plus" onClick={() => setTopup(true)}>{t("tu.title")}</Button></PageActions>
      <Answers d={d} />
      <QuotaCard d={d} onTopup={() => setTopup(true)} />
      <TrafficCard />
      <TopPeople d={d} onOpen={setPerson} />
      <Attention d={d} onOpen={setPerson} />
      <TopUpDialog open={topup} onOpenChange={setTopup} />
      <PersonSheet userId={person} onClose={() => setPerson(null)} />
    </div>
  );
}

function Answers({ d }: { d: Dashboard }) {
  const devices = d.users.flatMap((u) => u.devices || []);
  const online = devices.filter((x) => x.connected).length;
  const vpnOn = d.vpn?.state === "connected";
  const sec = d.security || {}, attack = (sec.failed_logins_1h || 0) + (sec.waf_blocks_1h || 0);
  const item = (icon: string, label: React.ReactNode, value: React.ReactNode, sub: React.ReactNode, tone: "success" | "danger" | "warning" | "neutral", href?: string) => (
    <a href={href} className={clsx("flex min-w-0 items-start gap-3 rounded-lg border border-line bg-surface p-3.5", href && "hover:border-line-strong")}>
      <span className={clsx("grid size-9 shrink-0 place-items-center rounded-md bg-canvas", { "text-success": tone === "success", "text-danger": tone === "danger", "text-warning": tone === "warning", "text-fg-3": tone === "neutral" })}><Icon name={icon} size={18} /></span>
      <div className="min-w-0"><div className="text-[12px] text-fg-3">{label}</div><div className="break-words text-[15px] font-semibold leading-snug text-fg">{value}</div><div className="text-[12px] leading-snug text-fg-3">{sub}</div></div>
    </a>
  );
  return (
    <section className="col-span-12 grid grid-cols-2 gap-[var(--gap)] lg:grid-cols-4" aria-label={t("ov.answers")}>
      {item("internet", t("ov.tile.internet"), d.internet === false ? t("ov.tile.offline") : d.internet ? t("ov.tile.online") : t("ov.internet.unknown"),
        d.internet === false ? t("feed.down.sub") : d.internet ? t("ov.internet.ok") : t("ov.internet.checking"), d.internet === false ? "danger" : d.internet ? "success" : "neutral", "#/internet")}
      {item("network", t("ov.tile.devices"), `${num(online, 0)} ${t("ov.of")} ${num(devices.length, 0)}`, `${num(d.blocked_count || 0, 0)} ${t("ov.tile.blocked")}`, online ? "success" : "neutral", "#/people")}
      {item("vpn", t("ov.tile.vpn"), vpnOn ? t("vpn.connected") : t("ov.tile.vpn_off"), vpnOn ? d.vpn?.active_node_name || "" : t("ov.vpn.direct"), vpnOn ? "success" : "neutral", "#/vpn")}
      {item("security", t("ov.tile.security"), attack > 5 ? t("ov.tile.sec_attack") : t("ov.tile.sec_ok"), tf("ov.sec.sub", { a: num(sec.failed_logins_1h || 0, 0), b: num(sec.waf_blocks_1h || 0, 0) }), attack > 5 ? "warning" : "success", "#/security")}
    </section>
  );
}

function QuotaCard({ d, onTopup }: { d: Dashboard; onTopup: () => void }) {
  const b = d.bundle, pct = b.total_gb > 0 ? (b.used_gb / b.total_gb) * 100 : 0;
  const { elapsed, avg, forecast, end } = pace(b);
  const over = b.total_gb > 0 && b.used_gb >= b.total_gb;
  const warn = !over && forecast != null && forecast > b.total_gb;
  const runsOutIn = avg && avg > 0 ? Math.floor(Math.max(0, b.remaining_gb) / avg) : null;
  const target = b.days_left > 0 ? Math.max(0, b.remaining_gb) / b.days_left : null;
  const resetText = b.days_left >= 0 ? tf("ov.resets.on", { n: num(b.days_left, 0), date: end ? fmtDate(end, { weekday: "short", month: "short", day: "numeric" }) : "" })
    : b.period_type === "manual" ? t("ov.period.manual") : `${t("ov.since")} ${iso(b.period_start || "—")}`;
  return (
    <Card className="col-span-12 lg:col-span-5" title={<Term k="quota">{t("side.bundle")}</Term>} sub={resetText}
      actions={over ? <Badge tone="danger">{t("ov.st.over")}</Badge> : warn ? <Badge tone="warning">{t("ov.st.pace")}</Badge> : <Badge tone="success">{t("ov.st.ok")}</Badge>}>
      <div className="flex items-baseline gap-2">
        <span className="text-[34px] font-semibold leading-none tracking-tight text-fg tabular-nums">{num(Math.max(0, b.remaining_gb))}</span>
        <span className="text-[14px] text-fg-3">{t("common.gb")} {t("ov.left")}</span>
      </div>
      <Meter value={b.used_gb} max={b.total_gb || 1} className="mt-3" label={t("side.bundle")} />
      <div className="mt-1.5 flex justify-between text-[12px] text-fg-3 tabular-nums"><span>{tf("ov.used.of", { u: num(b.used_gb), t: num(b.total_gb) })}</span><span>{num(pct, 0)}%</span></div>
      <div className="mt-4 grid grid-cols-3 gap-3 border-t border-line pt-4">
        <Stat label={t("ov.daysleft")} value={b.days_left >= 0 ? num(b.days_left, 0) : "–"} />
        <Stat label={t("ov.avg")} value={avg != null ? num(avg) : "–"} unit={avg != null ? t("common.gb") : undefined} />
        <Stat label={t("ov.forecast.used")} value={forecast != null ? num(forecast) : "–"} unit={forecast != null ? t("common.gb") : undefined} />
      </div>
      {(over || (warn && elapsed && elapsed >= 3)) && (
        <div className="mt-4 rounded-md border border-line bg-canvas p-3 text-[13px] text-fg-2">
          <p>{over ? t("ov.tip.over") : tf("ov.tip.pace", { d: num(runsOutIn, 0), l: num(Math.max(0, b.days_left - (runsOutIn || 0)), 0), t: num(target), a: num(avg) })}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" variant="primary" icon="plus" onClick={onTopup}>{t("tu.title")}</Button>
            {!over && <a href="#/network" className="inline-flex h-8 items-center rounded-md border border-line-strong px-3 text-[12.5px] font-semibold text-fg hover:bg-raised">{t("ov.tip.limits")}</a>}
          </div>
        </div>
      )}
      {!over && !warn && forecast != null && <p className="mt-4 flex items-center gap-2 text-[12.5px] text-fg-3"><Icon name="check" size={14} className="text-success" />{tf("ov.tip.ok", { x: num(Math.max(0, b.total_gb - forecast)) })}</p>}
    </Card>
  );
}

function TrafficCard() {
  const prefs = usePrefs();
  const [tab, setTab] = useState<"speed" | "days" | "devices">("speed");
  const { lastUpdated } = useSession();
  const live = useLiveTraffic("all");
  const daily = useDailyUsage(tab === "days");
  const lan = prefs.netView === "lan";
  const pts = speedHistory().map((p) => ({ t: p.t, down: lan ? p.ldown : p.down, up: lan ? p.lup : p.up }));
  const bps = (x: { down_bps: number; up_bps: number; lan_down_bps?: number; lan_up_bps?: number }) => lan ? (x.lan_down_bps || 0) + (x.lan_up_bps || 0) : x.down_bps + x.up_bps;
  const devs = useMemo(() => (live.data?.devices || []).filter((x) => bps(x) > 0).sort((a, c) => bps(c) - bps(a)).slice(0, 6), [live.data, lan]); // eslint-disable-line react-hooks/exhaustive-deps
  const series = [{ key: "down", label: t("ov.down"), color: "var(--chart-1)" }, { key: "up", label: t("ov.up"), color: "var(--chart-2)", dashed: true }];
  let body: React.ReactNode;
  if (tab === "speed") {
    if (!live.data && pts.length < 2) body = <Loading what={t("ov.loading.live")} h={200} />;
    else {
      const peak = Math.max(0, ...pts.map((p) => p.down));
      body = <>
        <LineChart data={pts} series={series} fmt={fmtBps} xLabel={(p) => fmtTime(p.t)} xStart={tf("ov.chart.agoN", { n: num(Math.max(1, Math.round((pts.length * 5) / 60)), 0) })} xEnd={t("ov.chart.now")}
          summary={tf("ov.chart.sum.speed", { down: fmtBps(pts[pts.length - 1]?.down || 0), up: fmtBps(pts[pts.length - 1]?.up || 0), peak: fmtBps(peak) })} />
        {pts.length < 3 && <p className="mt-2 text-[12px] text-fg-3">{t("ov.chart.collecting")}</p>}
      </>;
    }
  } else if (tab === "days") {
    if (!daily.data) body = daily.isError ? <Empty icon="alert" title={t("act.failed")} sub={(daily.error as Error).message} action={<Button size="sm" onClick={() => daily.refetch()}>{t("conn.retry")}</Button>} /> : <Loading what={t("ov.loading.days")} h={200} />;
    else {
      const dd = daily.data.map((x) => ({ key: x.date, label: fmtDate(x.date), value: lan ? x.lan : x.gb }));
      const total = dd.reduce((a, x) => a + x.value, 0), peak = dd.reduce((a, x) => (x.value > a.value ? x : a), dd[0]);
      body = <>
        <div className="mb-3 grid grid-cols-3 gap-3"><Stat label={t("ov.chart.total30")} value={num(total)} unit={t("common.gb")} /><Stat label={t("ov.chart.avg")} value={num(total / dd.length)} unit={t("common.gb")} /><Stat label={t("ov.chart.peak")} value={num(peak.value)} unit={t("common.gb")} sub={peak.label} /></div>
        <BarChart data={dd} fmt={(v) => `${num(v, v < 10 ? 1 : 0)} ${t("common.gb")}`} label={t("ov.chart.hover")} summary={tf("ov.chart.sum.days", { total: num(total), peak: num(peak.value), day: peak.label })} />
      </>;
    }
  } else {
    body = !live.data ? <Loading what={t("ov.loading.live")} h={200} /> : !devs.length ? <Empty icon="network" title={lan ? t("ov.net.lan.idle") : t("ov.chart.idle")} sub={t("ov.idle.sub")} />
      : <RankList rows={devs.map((x) => ({ key: x.id, label: x.name || x.mac, sub: x.user_name, value: `↓ ${fmtBps(lan ? x.lan_down_bps || 0 : x.down_bps)} · ↑ ${fmtBps(lan ? x.lan_up_bps || 0 : x.up_bps)}`, pct: (bps(x) / bps(devs[0])) * 100 }))} />;
  }
  return (
    <Card className="col-span-12 lg:col-span-7" title={t("ov.live")}
      sub={<>{lastUpdated ? tf("ov.updated", { t: fmtTime(lastUpdated) }) : ""}{lan ? ` · ${t("ov.net.lan.note")}` : ""}</>}
      actions={<>
        <Segmented size="sm" label={t("ov.net")} value={prefs.netView} onChange={(v) => setPref("netView", v)} options={[{ value: "wan", label: "WAN" }, { value: "lan", label: "LAN" }]} />
        <Segmented size="sm" label={t("ov.chart.view")} value={tab} onChange={setTab} options={[{ value: "speed", label: t("ov.chart.speed") }, { value: "days", label: t("ov.chart.days") }, { value: "devices", label: t("ov.chart.devices") }]} />
      </>}>
      <p className="-mt-1 mb-3 text-[12px] text-fg-3"><Term k="wan" /> · <Term k="lan" /></p>
      {body}
    </Card>
  );
}

function TopPeople({ d, onOpen }: { d: Dashboard; onOpen: (id: number) => void }) {
  const top = [...d.users].filter((u) => !u.protected).sort((a, c) => c.used_gb - a.used_gb).slice(0, 6);
  return (
    <Card className="col-span-12 lg:col-span-7" title={t("ov.topusers")} sub={t("ov.topusers.sub")} actions={<a href="#/people" className="text-[12.5px] font-semibold text-accent-fg hover:underline">{t("ov.viewall")}</a>}>
      {!top.length ? <Empty icon="people" title={t("ov.nousers")} sub={t("ov.nousers.sub")} action={<a href="#/people" className="text-[13px] font-semibold text-accent-fg">{t("add.user")}</a>} /> : (
        <ul className="divide-y divide-line">
          {top.map((u) => { const st = userStatus(u), on = (u.devices || []).filter((x) => x.connected).length; return (
            <li key={u.id}>
              <button type="button" onClick={() => onOpen(u.id)} className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 py-2.5 text-start sm:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_auto]">
                <span className="min-w-0"><b className="block truncate text-[13.5px] text-fg">{u.name}</b><span className="text-[12px] text-fg-3">{tf("pp.devs.on", { n: num((u.devices || []).length, 0), o: num(on, 0) })}</span></span>
                <span className="col-span-2 min-w-0 sm:col-span-1">
                  <span className="flex justify-between text-[12.5px] tabular-nums"><b className="text-fg">{gb(u.used_gb)}</b><span className="text-fg-3">{u.allowance_gb > 0 ? `${num(u.percent, 0)}% ${t("ov.of")} ${num(u.allowance_gb)}` : t("pp.unlimited")}</span></span>
                  <Meter value={u.allowance_gb > 0 ? u.percent : 0} thin className="mt-1" label={u.name} />
                </span>
                <span className="row-start-1 sm:row-auto"><Badge tone={st.tone} title={st.reason}>{st.text}</Badge></span>
              </button>
            </li>
          ); })}
        </ul>
      )}
    </Card>
  );
}

function Attention({ d, onOpen }: { d: Dashboard; onOpen: (id: number) => void }) {
  const { run, pending } = useAction();
  const items: { key: string; tone: "danger" | "warning" | "info"; icon: string; title: string; sub?: string; acts?: React.ReactNode }[] = [];
  if (d.internet === false) items.push({ key: "down", tone: "danger", icon: "alert", title: t("feed.down"), sub: t("feed.down.sub"), acts: <a className="text-[12.5px] font-semibold text-accent-fg" href="#/internet">{t("feed.down.cta")}</a> });
  for (const u of d.users.filter((x) => x.guest && x.blocked)) items.push({ key: "g" + u.id, tone: "info", icon: "userplus", title: tf("feed.guest.t", { name: u.name }), sub: tf("feed.guest.sub", { n: num((u.devices || []).length, 0) }),
    acts: <><Button size="sm" variant="primary" pending={pending === "ap" + u.id} onClick={() => run("ap" + u.id, () => actions.approveGuest(u.id), tf("feed.guest.ok", { name: u.name }))}>{t("dr.approve")}</Button>
      <Button size="sm" onClick={() => onOpen(u.id)}>{t("feed.review")}</Button></> });
  for (const r of d.rogue || []) items.push({ key: "r" + r.mac, tone: "warning", icon: "alert", title: t("feed.rogue"), sub: [iso(r.mac || ""), r.ip ? iso(r.ip) : "", r.vendor || ""].filter(Boolean).join(" · "), acts: <a className="text-[12.5px] font-semibold text-accent-fg" href="#/people">{t("feed.rogue.cta")}</a> });
  if (d.security?.default_password) items.push({ key: "pw", tone: "warning", icon: "lock", title: t("feed.defpw"), sub: t("feed.defpw.sub"), acts: <a className="text-[12.5px] font-semibold text-accent-fg" href="#/system">{t("feed.defpw.cta")}</a> });
  if ((d.security?.attack_new || 0) > 5) items.push({ key: "atk", tone: "danger", icon: "shield", title: t("feed.attack"), sub: `${num(d.security!.attack_new, 0)} ${t("feed.attack.sub")}`,
    acts: <Button size="sm" pending={pending === "ack"} onClick={() => run("ack", () => api.post("/api/security/ack-attack"), t("feed.ack.done"))}>{t("feed.ack")}</Button> });
  return (
    <Card className="col-span-12 lg:col-span-5" title={t("ov.attention")} actions={items.length ? <Badge tone="warning">{num(items.length, 0)}</Badge> : null}>
      {!items.length ? <Empty icon="check" title={t("feed.allclear")} sub={t("feed.allclear.sub")} /> : (
        <ul className="flex flex-col gap-2">
          {items.map((x) => (
            <li key={x.key} className="flex gap-3 rounded-md border border-line bg-canvas p-3">
              <span className={clsx("mt-0.5", { "text-danger": x.tone === "danger", "text-warning": x.tone === "warning", "text-info": x.tone === "info" })}><Icon name={x.icon} size={17} /></span>
              <div className="min-w-0 flex-1"><b className="block text-[13px] text-fg">{x.title}</b>{x.sub && <p className="break-words text-[12.5px] text-fg-3">{x.sub}</p>}{x.acts && <div className="mt-2 flex flex-wrap items-center gap-2">{x.acts}</div>}</div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
