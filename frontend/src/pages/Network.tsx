import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useDashboard } from "../lib/session";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { t, tf, num, mask } from "../lib/i18n";
import { MAC_RE, normMac, isIPv4, isIPv6, allDevices } from "../lib/format";
import { Badge, Button, Card, Empty, Loading, Term, IconButton, Banner, KV } from "../components/ui/primitives";
import { NumberField, SwitchRow, TextField, Combobox } from "../components/ui/forms";
import { SliderField } from "../components/ui/slider";
import { ConfirmDialog, Dialog } from "../components/ui/overlays";
import { Icon } from "../components/ui/Icon";

interface NetCfg { enabled: boolean; total_down_mbps: number; total_up_mbps: number; lan_rate_mbps: number; vpn_pct: number; vpn_down_mbps: number; vpn_up_mbps: number; vpn_share?: { enabled?: boolean; interface?: string; status?: { state?: string; interface?: string } }; decline_random_macs: boolean }
interface Guest { enabled?: boolean; stop_new?: boolean; quota_gb?: number; speed_limit_mbps?: number; limit?: number }
interface Lease { mac: string; ip: string; ip6?: string; hostname?: string; device_name?: string }
const useNet = () => useQuery({ queryKey: ["network"], staleTime: 30_000, queryFn: async () => {
  const [cfg, leases, macs, guest] = await Promise.all([api.get<NetCfg>("/api/network"), api.get<Lease[]>("/api/network/static-leases"), api.get<{ allow?: string[]; deny?: string[] }>("/api/mac-lists"), api.get<Guest>("/api/guest")]);
  return { cfg, leases, macs: { allow: macs.allow || [], deny: macs.deny || [] }, guest };
} });

/** "How is traffic shaped and routed, and who may join?" */
export default function Network() {
  const net = useNet();
  if (net.isError) return <Empty icon="alert" title={t("n.loadfail")} sub={(net.error as Error).message} action={<Button size="sm" onClick={() => net.refetch()}>{t("conn.retry")}</Button>} />;
  if (!net.data) return <Loading what={t("n.loading")} h={320} />;
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Shaping cfg={net.data.cfg} />
      <Routing cfg={net.data.cfg} />
      <GuestCard g={net.data.guest} />
      <MacList kind="allow" list={net.data.macs.allow} />
      <MacList kind="deny" list={net.data.macs.deny} />
      <Leases leases={net.data.leases} />
    </div>
  );
}
function useNetAction() {
  const qc = useQueryClient(); const a = useAction();
  return { ...a, run: (k: string, fn: () => Promise<unknown>, ok?: string, sub?: string) => a.run(k, async () => { await fn(); await qc.invalidateQueries({ queryKey: ["network"] }); }, ok, sub) };
}

function Shaping({ cfg }: { cfg: NetCfg }) {
  const d = useDashboard();
  const { run, pending } = useNetAction();
  const init = { total_down_mbps: cfg.total_down_mbps, total_up_mbps: cfg.total_up_mbps, lan_rate_mbps: cfg.lan_rate_mbps, vpn_pct: cfg.vpn_pct, vpn_down_mbps: cfg.vpn_down_mbps, vpn_up_mbps: cfg.vpn_up_mbps };
  const [f, setF] = useState(init), [base, setBase] = useState(JSON.stringify(init));
  const [awaiting, setAwaiting] = useState(false);
  const dirty = JSON.stringify(f) !== base;
  useEffect(() => { if (!dirty) { setF(init); setBase(JSON.stringify(init)); } }, [JSON.stringify(init)]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k: keyof typeof f) => (v: number | null) => setF((x) => ({ ...x, [k]: v ?? 0 }));
  const sp = d?.shaping, applying = !!(sp && sp.available && !sp.applied);
  // "Applied" is shown only after the gateway itself reports the new tree is in place.
  useEffect(() => { if (awaiting && sp && sp.applied) setAwaiting(false); }, [awaiting, sp]);
  const status = !cfg.enabled ? <Badge>{t("n.st.off")}</Badge> : applying || awaiting ? <Badge tone="warning" icon="reset">{t("n.st.applying")}</Badge> : sp?.available === false ? <Badge tone="danger">{t("n.st.unavail")}</Badge> : <Badge tone="success">{t("n.st.applied")}</Badge>;
  const unit = t("unit.mbps");
  return (
    <Card className="col-span-12 lg:col-span-7" title={<Term k="shaping">{t("n.shape")}</Term>} sub={t("n.shape.d")} actions={status}>
      <SwitchRow title={t("n.enabled")} desc={cfg.enabled ? t("n.enabled.on") : t("n.enabled.off")} checked={cfg.enabled} pending={pending === "en"}
        onChange={(v) => { setAwaiting(v); run("en", () => api.post("/api/network", { enabled: v }), v ? t("n.enabled.sent") : t("n.disabled.sent"), v ? t("n.verify") : undefined); }} />
      <div className="mt-2 grid grid-cols-1 gap-4 border-t border-line pt-4 sm:grid-cols-2">
        <NumberField label={t("n.down")} hint={t("n.line.hint")} value={f.total_down_mbps} onValue={set("total_down_mbps")} unit={unit} spec={{ min: 0, max: 100000, decimals: 1 }} />
        <NumberField label={t("n.up")} value={f.total_up_mbps} onValue={set("total_up_mbps")} unit={unit} spec={{ min: 0, max: 100000, decimals: 1 }} />
        <NumberField label={t("n.lan")} hint={t("n.lan.h")} value={f.lan_rate_mbps} onValue={set("lan_rate_mbps")} unit={unit} spec={{ min: 0, max: 100000, integer: true }} presets={[100, 1000, 2500]} />
      </div>
      <details className="group mt-4 rounded-md border border-line">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-[13px] font-semibold text-fg"><Icon name="chev" size={15} className="transition-transform group-open:rotate-90 rtl:rotate-180 rtl:group-open:rotate-90" />{t("n.vpn.adv")}<span className="font-normal text-fg-3">· {t("n.vpn.adv.d")}</span></summary>
        <div className="flex flex-col gap-4 border-t border-line p-3">
          <SliderField label={t("n.vpnpct")} hint={t("n.vpnpct.h")} value={f.vpn_pct} onValue={set("vpn_pct")} min={0} max={100} step={5} unit="%" marks={[0, 50, 100]} zeroLabel={t("n.vpnpct.auto")} />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <NumberField label={t("n.vpndown")} value={f.vpn_down_mbps} onValue={set("vpn_down_mbps")} unit={unit} spec={{ min: 0, max: 100000, decimals: 1 }} hint={t("n.zero.auto")} />
            <NumberField label={t("n.vpnup")} value={f.vpn_up_mbps} onValue={set("vpn_up_mbps")} unit={unit} spec={{ min: 0, max: 100000, decimals: 1 }} hint={t("n.zero.auto")} />
          </div>
        </div>
      </details>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={!dirty} pending={pending === "save"} pendingText={t("act.applying")} onClick={async () => { setAwaiting(true); if (await run("save", () => api.post("/api/network", f), t("n.saved.sent"), t("n.verify"))) setBase(JSON.stringify(f)); else setAwaiting(false); }}>{t("n.apply")}</Button>
        {dirty && <Button variant="ghost" onClick={() => setF(JSON.parse(base))}>{t("sheet.revert")}</Button>}
        {dirty && <span className="text-[12px] text-fg-3">{t("n.apply.note")}</span>}
      </div>
    </Card>
  );
}

function Routing({ cfg }: { cfg: NetCfg }) {
  const d = useDashboard();
  const { run, pending } = useNetAction();
  const [sweep, setSweep] = useState(false);
  const vs = cfg.vpn_share || {}, live = d?.vpn_share || vs, st = live.status?.state || "";
  const vpnOn = ["on", "connected", "active"].includes(st);
  const capped = d ? allDevices(d.users).filter((x) => x.limit_down_mbps || x.limit_up_mbps) : [];
  return (
    <Card className="col-span-12 lg:col-span-5" title={t("n.routing")} sub={t("n.routing.d")}>
      <div className="divide-y divide-line">
        <SwitchRow title={t("n.share")} desc={t("n.share.d")} checked={!!vs.enabled} pending={pending === "share"} onChange={(v) => run("share", () => api.post("/api/network", { vpn_share: v }), v ? t("n.share.on") : t("n.share.off"))}>
          {st && <div className="mt-1.5"><Badge tone={vpnOn ? "success" : st === "error" ? "danger" : "neutral"}>{vpnOn ? t("n.st.tunnel.up") : st}</Badge></div>}
        </SwitchRow>
        <SwitchRow title={t("n.rand")} desc={t("n.rand.d")} checked={cfg.decline_random_macs} pending={pending === "rand"} onChange={(v) => run("rand", () => api.post("/api/network", { decline_random_macs: v }), t("n.saved"))}>
          {!cfg.decline_random_macs && <Button size="sm" variant="ghost" className="mt-2" onClick={() => setSweep(true)}>{t("n.rand.old")}</Button>}
        </SwitchRow>
      </div>
      <KV className="mt-3 border-t border-line pt-3" items={[
        [t("n.st.down"), cfg.total_down_mbps ? `${num(cfg.total_down_mbps, 0)} ${t("unit.mbps")}` : t("n.unlimited")],
        [t("n.st.up"), cfg.total_up_mbps ? `${num(cfg.total_up_mbps, 0)} ${t("unit.mbps")}` : t("n.unlimited")],
        [t("n.st.vpn"), vpnOn ? `${t("n.st.on")} · ${live.status?.interface || vs.interface || ""}` : t("n.st.off")],
        [t("n.st.caps"), num(capped.length, 0)],
      ]} />
      <ConfirmDialog open={sweep} onOpenChange={setSweep} title={t("n.rand.old")} confirmLabel={t("n.rand.old.cta")}
        consequences={{ changes: t("n.rand.old.changes"), affected: t("n.rand.old.who"), undo: t("n.rand.old.undo") }}
        onConfirm={async () => { if (!(await run("sweep", () => api.post("/api/network", { decline_random_macs: true, decline_random_macs_existing: true }), t("n.saved")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

function GuestCard({ g }: { g: Guest }) {
  const { run, pending } = useNetAction();
  const init = { quota_gb: g.quota_gb ?? 0, speed_limit_mbps: g.speed_limit_mbps ?? 0, limit: g.limit ?? 1 };
  const [f, setF] = useState(init), [base, setBase] = useState(JSON.stringify(init));
  const dirty = JSON.stringify(f) !== base;
  useEffect(() => { if (!dirty) { setF(init); setBase(JSON.stringify(init)); } }, [JSON.stringify(init)]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Card className="col-span-12" title={<Term k="guest">{t("n.guest")}</Term>} sub={t("n.guest.d")}>
      <div className="grid grid-cols-1 gap-x-8 lg:grid-cols-2">
        <div className="divide-y divide-line">
          <SwitchRow title={t("n.guestmode")} desc={t("n.guestmode.d")} checked={!!g.enabled} pending={pending === "gm"} onChange={(v) => run("gm", () => api.post("/api/guest", { enabled: v }), v ? t("n.guest.on") : t("n.guest.off"))} />
          <SwitchRow title={t("n.stopnew")} desc={t("n.stopnew.d")} checked={!!g.stop_new} pending={pending === "sn"} onChange={(v) => run("sn", () => api.post("/api/guest", { stop_new: v }), t("n.saved"))} />
        </div>
        <div className="flex flex-col gap-4 pt-3 lg:pt-0">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <NumberField label={t("n.guest.quota")} value={f.quota_gb} onValue={(v) => setF({ ...f, quota_gb: v ?? 0 })} unit={t("common.gb")} spec={{ min: 0.01, max: 100000, decimals: 2 }} presets={[1, 2, 5]} />
            <NumberField label={t("n.guest.speed")} hint={t("n.guest.speed.h")} value={f.speed_limit_mbps} onValue={(v) => setF({ ...f, speed_limit_mbps: v ?? 0 })} unit={t("unit.mbps")} spec={{ min: 0, max: 100000, decimals: 1 }} />
          </div>
          <NumberField label={t("n.guest.max")} hint={t("n.guest.max.h")} value={f.limit} onValue={(v) => setF({ ...f, limit: v ?? 1 })} unit={t("n.guest.devunit")} spec={{ min: 1, max: 10000, integer: true }} stepper className="max-w-64" />
          <div className="flex gap-2"><Button variant="primary" disabled={!dirty} pending={pending === "gs"} pendingText={t("act.saving")} onClick={async () => { if (await run("gs", () => api.post("/api/guest", f), t("n.guest.saved"), t("n.guest.saved.sub"))) setBase(JSON.stringify(f)); }}>{t("n.guest.save")}</Button>
            {dirty && <Button variant="ghost" onClick={() => setF(JSON.parse(base))}>{t("sheet.revert")}</Button>}</div>
        </div>
      </div>
    </Card>
  );
}

function MacList({ kind, list }: { kind: "allow" | "deny"; list: string[] }) {
  const d = useDashboard();
  const { run, pending } = useNetAction();
  const [suggest, setSuggest] = useState(false);
  const [v, setV] = useState(""), [err, setErr] = useState<string | null>(null), [del, setDel] = useState<string | null>(null);
  const known = new Map(d ? allDevices(d.users).map((x) => [x.mac.toLowerCase(), `${x.name || x.mac} · ${x.owner}`]) : []);
  const add = async () => {
    const m = normMac(v);
    if (!MAC_RE.test(m)) { setErr(t("n.mac.bad")); return; }
    if (list.includes(m)) { setErr(t("n.mac.dup")); return; }
    if (await run("add", () => api.post("/api/mac-lists", { [kind]: [...list, m] }), tf("n.mac.added", { m: mask(m) }))) setV("");
  };
  return (
    <Card className="col-span-12 lg:col-span-6" title={t("n." + kind)} sub={t("n." + kind + ".d")} actions={<Badge icon={false}>{num(list.length, 0)}</Badge>}>
      <form className="flex items-start gap-2" onSubmit={(e) => { e.preventDefault(); add(); }}>
        <div className="relative min-w-0 flex-1" onFocus={() => setSuggest(true)} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setSuggest(false); }}>
          <TextField label={t("n.mac.add")} labelHidden hint={!err ? t("add.mac.hint") : undefined} value={v} onChange={(x) => { setV(x); setErr(null); }} placeholder="aa:bb:cc:dd:ee:ff" mono error={err} autoComplete="off" />
          {suggest && [...known].some(([m, n]) => !list.includes(m) && `${m} ${n}`.toLowerCase().includes(v.toLowerCase())) && (
            <div className="absolute start-0 top-12 z-20 max-h-48 w-full overflow-y-auto rounded-md border border-line bg-raised p-1 shadow-raised" aria-label={t("f.choose")}>
              {[...known].filter(([m, n]) => !list.includes(m) && `${m} ${n}`.toLowerCase().includes(v.toLowerCase())).map(([m, n]) => (
                <button key={m} type="button" className="flex w-full flex-col rounded-[6px] px-2.5 py-2 text-start text-[13px] text-fg hover:bg-canvas focus:bg-canvas"
                  onClick={() => { setV(m); setErr(null); setSuggest(false); }}><span>{n}</span><span className="qm-ltr font-mono text-[12px] text-fg-3">{mask(m)}</span></button>
              ))}
            </div>
          )}
        </div>
        <Button type="submit" icon="plus" pending={pending === "add"}>{t("n.mac.add")}</Button>
      </form>
      {!list.length ? <p className="mt-3 text-[12.5px] text-fg-3">{t("n.mac.none." + kind)}</p> : (
        <ul className="mt-3 divide-y divide-line">
          {list.map((m) => (
            <li key={m} className="flex items-center gap-3 py-2">
              <Icon name={kind === "allow" ? "check" : "ban"} size={15} className={kind === "allow" ? "text-success" : "text-danger"} />
              <span className="min-w-0 flex-1"><b className="block truncate text-[13px] text-fg">{known.get(m) || t("n.mac.unknown")}</b><span className="qm-ltr font-mono text-[11.5px] text-fg-3">{mask(m)}</span></span>
              <IconButton icon="x" tone="danger" label={tf("n.mac.remove", { m: mask(m) })} onClick={() => setDel(m)} />
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog open={!!del} onOpenChange={(o) => !o && setDel(null)} title={tf("n.mac.remove", { m: mask(del || "") })} confirmLabel={t("n.mac.remove.cta")}
        consequences={{ changes: t("n.mac.remove." + kind), affected: del ? known.get(del) || mask(del) : "", undo: t("n.mac.remove.undo") }}
        onConfirm={async () => { if (!(await run("rm", () => api.post("/api/mac-lists", { [kind]: list.filter((x) => x !== del) }), t("n.saved")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

function Leases({ leases }: { leases: Lease[] }) {
  const d = useDashboard();
  const { run, pending } = useNetAction();
  const [open, setOpen] = useState(false), [del, setDel] = useState<Lease | null>(null);
  const [mac, setMac] = useState(""), [ip, setIp] = useState(""), [ip6, setIp6] = useState(""), [host, setHost] = useState(""), [tried, setTried] = useState(false);
  const devs = d ? allDevices(d.users) : [];
  const macErr = tried && !MAC_RE.test(normMac(mac)) ? t("n.mac.bad") : null, ipErr = (tried || ip) && !isIPv4(ip) ? t("n.ip.bad") : null, ip6Err = ip6 && !isIPv6(ip6) ? t("n.ip6.bad") : null;
  const save = async () => {
    setTried(true); if (!MAC_RE.test(normMac(mac)) || !isIPv4(ip) || (ip6 && !isIPv6(ip6))) return;
    if (await run("lease", () => api.post("/api/network/static-leases", { mac: normMac(mac), ip: ip.trim(), ip6: ip6.trim(), hostname: host.trim() }), tf("n.lease.saved", { ip }), t("n.lease.saved.sub"))) { setOpen(false); setMac(""); setIp(""); setIp6(""); setHost(""); setTried(false); }
  };
  return (
    <Card className="col-span-12" title={<Term k="dhcp">{t("n.leases")}</Term>} sub={t("n.leases.d")} actions={<Button size="sm" variant="primary" icon="plus" onClick={() => setOpen(true)}>{t("n.leases.new")}</Button>}>
      {!leases.length ? <Empty icon="network" title={t("n.leases.none")} sub={t("n.leases.none.sub")} /> : (
        <div className="overflow-x-auto"><table className="w-full min-w-[560px] text-[13px]">
          <thead><tr className="border-b border-line text-[12px] text-fg-3"><th className="py-2 text-start font-semibold">{t("n.lease.dev")}</th><th className="py-2 text-start font-semibold">MAC</th><th className="py-2 text-start font-semibold">IPv4 / IPv6</th><th /></tr></thead>
          <tbody className="divide-y divide-line">{leases.map((l) => (
            <tr key={l.mac}><td className="py-2.5 font-semibold text-fg">{l.device_name || l.hostname || "—"}</td><td className="qm-ltr py-2.5 font-mono text-[12px] text-fg-2">{mask(l.mac)}</td>
              <td className="qm-ltr py-2.5 font-mono text-[12px] text-fg">{l.ip}{l.ip6 && <span className="block text-fg-3">{l.ip6}</span>}</td>
              <td className="py-2.5 text-end"><IconButton icon="x" tone="danger" label={tf("n.lease.del", { ip: l.ip })} onClick={() => setDel(l)} /></td></tr>
          ))}</tbody>
        </table></div>
      )}
      <Dialog open={open} onOpenChange={setOpen} title={t("n.lease.title")} sub={t("n.lease.sub")} busy={pending === "lease"}
        footer={<><Button onClick={() => setOpen(false)}>{t("common.cancel")}</Button><Button variant="primary" onClick={save} pending={pending === "lease"} pendingText={t("act.saving")}>{t("n.lease.save")}</Button></>}>
        <div className="flex flex-col gap-4">
          <Combobox label={t("n.lease.pick")} value={devs.find((x) => x.mac === normMac(mac))?.mac || ""} onChange={(m) => { setMac(m); const x = devs.find((y) => y.mac === m); if (x?.ip && !ip) setIp(x.ip); if (x && !host) setHost(x.name); }}
            options={devs.map((x) => ({ value: x.mac, label: x.name || x.mac, sub: x.owner }))} placeholder={t("n.lease.pick.ph")} />
          <TextField label={t("n.lease.mac")} value={mac} onChange={setMac} placeholder="aa:bb:cc:dd:ee:ff" mono error={macErr} />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <TextField label={<Term k="ip">{t("n.lease.ip")}</Term>} hint={t("n.lease.ip.h")} value={ip} onChange={setIp} placeholder="192.168.2.50" mono error={ipErr} inputMode="decimal" />
            <TextField label={t("n.lease.ip6")} value={ip6} onChange={setIp6} mono error={ip6Err} optional />
          </div>
          <TextField label={t("n.lease.host")} value={host} onChange={setHost} optional />
          <Banner tone="info">{t("n.lease.note")}</Banner>
        </div>
      </Dialog>
      <ConfirmDialog open={!!del} onOpenChange={(o) => !o && setDel(null)} title={tf("n.lease.del", { ip: del?.ip || "" })} confirmLabel={t("n.lease.del.cta")}
        consequences={{ changes: t("n.lease.del.changes"), affected: del?.device_name || del?.hostname || mask(del?.mac || ""), undo: t("n.lease.del.undo") }}
        onConfirm={async () => { if (del && !(await run("dl", () => api.del(`/api/network/static-leases/${encodeURIComponent(del.mac)}`), t("dr.done")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}
