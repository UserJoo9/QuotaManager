import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useDashboard, useSession } from "../lib/session";
import { api } from "../lib/api";
import { useAction } from "../lib/actions";
import { t, tf, num, iso, privacyHidden } from "../lib/i18n";
import { Badge, Banner, Button, Card, KV, Loading, Meter, Term } from "../components/ui/primitives";
import { NumberField, SwitchRow, TextField, Switch } from "../components/ui/forms";
import { ConfirmDialog, useToast } from "../components/ui/overlays";
import { Icon } from "../components/ui/Icon";

interface Wan { topology?: string; configured?: string; pending?: string; ppp0?: string; ppp_ip?: string; ppp_peer?: string; renew_enabled?: boolean; renew_minutes?: number; renew_last?: string; source?: string; public_ip6?: string; restart_scheduled?: boolean; internet?: boolean; pppoe_user?: string; pppoe_has_password?: boolean; wan_if?: string }
interface V6 { wan_ipv6_active?: boolean; lan_prefix?: string; lan_gateway?: string; lan_mode?: string; wan_ipv6_address?: string; direct_devices?: number; last_probe_time?: number; last_probe_details?: string }
interface Ts { state: string; message?: string; install_command?: string; install_log?: string[]; install_step?: number; auth_url?: string; url?: string; ip?: string; url_name?: string; version?: string; peers?: number; peers_online?: number; peer_list?: { name?: string; os?: string; online?: boolean; path?: string }[]; health?: string[]; wan_exposed?: boolean }

/** "Is the line up, how does the box reach the internet, and can I reach the box from outside?" */
export default function Internet() {
  const d = useDashboard();
  if (!d) return <Loading what={t("boot.reading")} h={300} />;
  const w = ((d as unknown as { wan?: Wan }).wan) || {};
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Status w={w} up={d.internet} />
      <AutoRenew w={w} />
      <Ipv6 />
      <Tailscale />
      <WanMode w={w} />
    </div>
  );
}

function Status({ w, up }: { w: Wan; up: boolean | null }) {
  const isWan = w.topology === "wan", canRenew = isWan && w.ppp0 === "up";
  const [confirm, setConfirm] = useState(false);
  const { run } = useAction();
  return (
    <Card className="col-span-12 lg:col-span-7" title={t("i.status")}
      actions={<>{up === false ? <Badge tone="danger">{t("i.offline")}</Badge> : up ? <Badge tone="success">{t("i.online")}</Badge> : <Badge>{t("i.checking")}</Badge>}<Badge tone="accent" icon={false}>{isWan ? "WAN" : "LAN"}</Badge></>}>
      <KV items={[[t("i.mode"), isWan ? t("i.mode.wan") : t("i.mode.lan")], isWan && [t("i.ppp"), w.ppp0 || "–", true], isWan && [t("i.pubip"), w.ppp_ip ? iso(w.ppp_ip) : "–", true],
        isWan && [t("i.peer"), w.ppp_peer ? iso(w.ppp_peer) : "–", true], isWan && [t("i.last"), w.renew_last ? iso(w.renew_last) : t("i.never")]]} />
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
        <div><b className="block text-[13.5px] text-fg">{t("i.renew")}</b><p className="text-[12.5px] text-fg-3">{canRenew ? t("i.renew.d") : t("i.renew.na")}</p></div>
        <Button variant="danger" icon="bolt" disabled={!canRenew} onClick={() => setConfirm(true)}>{t("i.renew.btn")}</Button>
      </div>
      <ConfirmDialog open={confirm} onOpenChange={setConfirm} title={t("i.renew")} confirmLabel={t("i.renew.btn")}
        consequences={{ changes: t("i.renew.d"), affected: t("cf.everyone"), undo: t("i.renew.undo"), connection: t("i.renew.conn") }}
        onConfirm={async () => { if (!(await run("renew", () => api.post("/api/wan/renew"), t("i.renewed")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

function AutoRenew({ w }: { w: Wan }) {
  const isWan = w.topology === "wan";
  const [on, setOn] = useState(!!w.renew_enabled), [min, setMin] = useState<number | null>(w.renew_minutes || 15);
  const dirty = on !== !!w.renew_enabled || min !== (w.renew_minutes || 15);
  useEffect(() => { if (!dirty) { setOn(!!w.renew_enabled); setMin(w.renew_minutes || 15); } }, [w.renew_enabled, w.renew_minutes]); // eslint-disable-line react-hooks/exhaustive-deps
  const { run, pending } = useAction();
  return (
    <Card className="col-span-12 lg:col-span-5" title={t("i.auto")} sub={isWan ? t("i.auto.d") : undefined}>
      {!isWan ? <p className="text-[13px] text-fg-3">{t("i.auto.na")}</p> : (
        <div className="flex flex-col gap-3">
          <SwitchRow title={t("i.auto.on")} desc={t("i.auto.on.d")} checked={on} onChange={setOn} />
          <NumberField label={t("i.auto.min")} hint={t("i.auto.min.h")} value={min} onValue={setMin} unit={t("i.min.unit")} spec={{ min: 5, max: 1440, integer: true }} stepper presets={[5, 15, 30, 60]} disabled={!on} />
          {on && min && <p className="text-[12px] text-fg-3">{tf("i.auto.next", { n: num(min, 0) })}</p>}
          <div><Button variant="primary" disabled={!dirty || !(min && min >= 5)} pending={pending === "auto"} pendingText={t("act.saving")}
            onClick={() => run("auto", () => api.post("/api/wan/renew-config", { enabled: on, minutes: min }), t("q.saved"))}>{t("i.save")}</Button></div>
        </div>
      )}
    </Card>
  );
}

function Ipv6() {
  const q = useQuery({ queryKey: ["ipv6"], staleTime: 30_000, queryFn: () => api.get<V6>("/api/network/ipv6/status") });
  const qc = useQueryClient();
  const { run, pending } = useAction();
  const v6 = q.data;
  return (
    <Card className="col-span-12 lg:col-span-6" title="IPv6" sub={t("i.v6.sub")} actions={v6 && <Badge tone={v6.wan_ipv6_active ? "success" : "neutral"}>{v6.wan_ipv6_active ? t("i.v6.active") : t("i.v6.inactive")}</Badge>}>
      {!v6 ? <Loading what={t("i.v6.loading")} h={140} /> : <>
        <KV items={[[t("i.v6.prefix"), v6.lan_prefix || "–", true], [t("i.v6.gw"), v6.lan_gateway || "–", true], [t("i.v6.mode"), v6.lan_mode || "–"], [t("i.v6.addr"), v6.wan_ipv6_address ? iso(v6.wan_ipv6_address) : "–", true],
          [t("i.v6.direct"), num(v6.direct_devices || 0, 0)], [t("i.v6.last"), v6.last_probe_time ? new Date(v6.last_probe_time * 1000).toLocaleString("en-GB") : t("i.v6.never")]]} />
        {v6.last_probe_details && <p className="mt-3 text-[12.5px] text-fg-3">{v6.last_probe_details}</p>}
        <div className="mt-4"><Button size="sm" pending={pending === "probe"} pendingText={t("i.v6.probing")}
          onClick={() => run("probe", async () => { await api.post("/api/network/ipv6/probe"); await qc.invalidateQueries({ queryKey: ["ipv6"] }); }, t("i.v6.probed"))}>{t("i.v6.probe")}</Button></div>
      </>}
    </Card>
  );
}

const TS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = { connected: "success", needs_login: "warning", starting: "warning", installing: "warning", error: "danger" };
function Tailscale() {
  // Poll faster only while something is in progress (install / waiting for sign-in).
  const q = useQuery({ queryKey: ["tailscale"], queryFn: () => api.get<Ts>("/api/tailscale"),
    refetchInterval: (qq) => (["installing", "needs_login", "starting"].includes(qq.state.data?.state || "") ? 3000 : 30000), refetchIntervalInBackground: false });
  const qc = useQueryClient(), toast = useToast();
  const { run, pending } = useAction();
  const [logout, setLogout] = useState(false);
  const ts = q.data;
  const call = (k: string, path: string, body?: object, ok?: string) => run(k, async () => { qc.setQueryData(["tailscale"], await api.post<Ts>(path, body)); }, ok);
  const copy = async (s: string) => { try { await navigator.clipboard.writeText(s); toast({ kind: "success", title: t("i.ts.copied") }); } catch { toast({ kind: "info", title: s }); } };
  let body: React.ReactNode = <Loading what={t("i.ts.loading")} h={140} />;
  if (ts) {
    const st = ts.state;
    if (st === "not_installed") body = <>
      <p className="text-[13px] text-fg-2">{t("i.ts.install.d")}</p>
      <p className="text-[12.5px] text-fg-3">{t("i.ts.trust")}</p>
      {ts.message && <Banner tone="warning">{t("i.ts.fail")}</Banner>}
      <div><Button variant="primary" pending={pending === "inst"} onClick={() => call("inst", "/api/tailscale/install")}>{t(ts.message ? "i.ts.retry" : "i.ts.install")}</Button></div>
      {ts.message && <details className="text-[12.5px]"><summary className="cursor-pointer text-fg-3">{t("i.ts.details")}</summary>
        <p className="mt-2 text-fg-3">{t("i.ts.install.manual")}</p><code className="qm-ltr block break-all font-mono text-[12px]">{ts.install_command}</code>
        <pre className="qm-ltr mt-2 max-h-36 overflow-auto rounded-md bg-canvas p-2 font-mono text-[11.5px]">{[ts.message, ...(ts.install_log || [])].join("\n")}</pre></details>}
    </>;
    else if (st === "installing") { const n = Math.min(ts.install_step || 0, 4); body = <>
      <p className="text-[13px] text-fg-2">{t("i.ts.installing")}</p>
      <Meter value={Math.max(8, n * 25)} label={t("i.ts.installing")} />
      <ol className="flex flex-col gap-1 text-[13px]">{[1, 2, 3, 4].map((i) => <li key={i} className={clsx("flex items-center gap-2", i <= n ? "text-fg" : "text-fg-3")}><Icon name={i <= n ? "check" : "dot"} size={14} />{t("i.ts.step." + i)}</li>)}</ol>
    </>; }
    else if (st === "needs_login") body = ts.auth_url ? <>
      <Banner tone="info" title={t("i.ts.login")}>{t("i.ts.login.d")}</Banner>
      <div><a className="inline-flex h-9 items-center gap-2 rounded-md bg-accent px-4 text-[13.5px] font-semibold text-accent-ink" href={ts.auth_url} target="_blank" rel="noopener noreferrer">{t("i.ts.login.btn")}<Icon name="chev" size={14} className="rtl:rotate-180" /></a></div>
    </> : <><p className="text-[13px] text-fg-3">{t("i.ts.wait")}</p><div><Button variant="primary" onClick={() => call("con", "/api/tailscale/connect")}>{t("i.ts.connect")}</Button></div></>;
    else if (st === "connected") body = <>
      <KV items={[[t("i.ts.addr"), ts.url || ts.ip, true], ts.url_name ? [t("i.ts.name"), ts.url_name, true] : null, [t("i.ts.version"), ts.version || "–", true], [t("i.ts.peers"), num(ts.peers || 0, 0)]]} />
      <p className="text-[12.5px] text-fg-3">{t("i.ts.how")}</p>
      {ts.peer_list?.length ? <ul className="divide-y divide-line rounded-md border border-line">{ts.peer_list.map((p, i) => (
        <li key={i} className="flex items-center justify-between gap-2 px-3 py-2 text-[13px]"><span className="truncate text-fg">{p.name || "?"}{p.os ? <span className="text-fg-3"> · {p.os}</span> : null}</span>
          <Badge tone={p.online ? "success" : "neutral"}>{p.online ? t("i.ts.peer.on") + (p.path ? " · " + t("i.ts.peer." + p.path) : "") : t("i.ts.peer.off")}</Badge></li>))}</ul>
        : <Banner tone="info">{t("i.ts.nodev")}</Banner>}
      {!!ts.peer_list?.length && !ts.peers_online && <Banner tone="warning">{t("i.ts.noonline")}</Banner>}
      {ts.wan_exposed && <Banner tone="warning">{t("i.ts.fw.on")} <a className="font-semibold text-accent-fg" href="#/security">{t("i.ts.fw.link")}</a></Banner>}
      {!!ts.health?.length && <details className="text-[12.5px]"><summary className="cursor-pointer text-fg-3">{t("i.ts.details")}</summary><pre className="qm-ltr mt-2 max-h-32 overflow-auto rounded-md bg-canvas p-2 font-mono text-[11.5px]">{ts.health.join("\n")}</pre></details>}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" icon="copy" onClick={() => copy(ts.url || ts.ip || "")}>{t("i.ts.copy")}</Button>
        <Button pending={pending === "dis"} onClick={() => call("dis", "/api/tailscale/disconnect", { logout: false }, t("i.ts.off"))}>{t("i.ts.disconnect")}</Button>
        <Button variant="danger" onClick={() => setLogout(true)}>{t("i.ts.logout")}</Button>
      </div>
    </>;
    else if (st === "starting") body = <p className="text-[13px] text-fg-3">{t("i.ts.starting")}</p>;
    else body = <>
      <p className="text-[13px] text-fg-3">{st === "daemon_down" ? t("i.ts.down") : t("i.ts.stopped")}</p>
      {ts.message && <Banner tone="warning">{ts.message}</Banner>}
      <div><Button variant="primary" pending={pending === "con"} onClick={() => call("con", "/api/tailscale/connect")}>{t("i.ts.connect")}</Button></div>
    </>;
  }
  return (
    <Card className="col-span-12 lg:col-span-6" title={t("i.ts")} sub={t("i.ts.d")} actions={ts && <Badge tone={TS_TONE[ts.state] || "neutral"}>{t("i.ts.st." + ts.state)}</Badge>}>
      <div className="flex flex-col gap-3">{body}</div>
      <ConfirmDialog open={logout} onOpenChange={setLogout} title={t("i.ts.logout")} confirmLabel={t("i.ts.logout")}
        consequences={{ changes: t("i.ts.logout.changes"), affected: t("i.ts.logout.who"), undo: t("i.ts.logout.undo"), connection: t("i.ts.logout.conn") }}
        onConfirm={async () => { if (!(await call("out", "/api/tailscale/disconnect", { logout: true }))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

type TestMsg = { ok: boolean | null; text: string } | null;
function verdict(r: { ok?: boolean; status?: string; detail?: string; local_ip?: string; peer_ip?: string; internet?: boolean; script_output?: string }): TestMsg {
  if (r && r.ok) return { ok: true, text: `${t("wan.t.ok")}${r.local_ip ? `  ${iso(r.local_ip)} ↔ ${iso(r.peer_ip)}` : ""}  ${t("wan.t.net")}: ${r.internet ? t("wan.yes") : t("wan.no")}${r.internet ? "" : "\n" + (r.detail || "")}` };
  const st = r?.status || "error", known = ["no-pppoe-server", "auth-failed", "concurrent-session", "link-down", "error"].includes(st) ? st : "error";
  const lead = known === "concurrent-session" ? t("wan.t.second") : `${t("wan.t.fail")} — ${r?.detail || t("wan.t.nodial")}`;
  return { ok: known === "concurrent-session" ? null : false, text: `${lead}\n→ ${t("wan.fix." + known)}${r?.script_output ? "\n" + r.script_output : ""}` };
}
/** Strong (PPPoE) mode. Can cut the internet, so applying shows the full warning + recovery path first. */
function WanMode({ w }: { w: Wan }) {
  const { refresh } = useSession();
  const toast = useToast();
  const q = useQuery({ queryKey: ["wan"], staleTime: 30_000, queryFn: () => api.get<Wan>("/api/wan") });
  const desired = (w.configured || w.topology) === "wan", live = w.topology === "wan", linkUp = (w.ppp0 || "") === "up";
  const [on, setOn] = useState(desired), [user, setUser] = useState(""), [pass, setPass] = useState(""), [wanIf, setWanIf] = useState("");
  const [dirty, setDirty] = useState(false), [msg, setMsg] = useState<TestMsg>(null), [testing, setTesting] = useState(false), [applying, setApplying] = useState(false);
  const [confirm, setConfirm] = useState<null | "wan" | "lan">(null);
  useEffect(() => { if (q.data && !dirty) { setOn((q.data.configured || q.data.topology) === "wan"); setUser(privacyHidden() ? "" : q.data.pppoe_user || ""); setWanIf(q.data.wan_if || ""); setPass(""); } }, [q.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const edit = <T,>(f: (v: T) => void) => (v: T) => { f(v); setDirty(true); };
  const test = async (auto = false) => {
    setTesting(true); setMsg({ ok: null, text: t(auto ? "wan.t.auto" : "wan.t.run") });
    try { setMsg(verdict(await api.post("/api/wan/test", { pppoe_user: user.trim(), pppoe_password: pass }))); }
    catch (e) { setMsg({ ok: false, text: `${t("wan.t.err")}: ${(e as Error).message}` }); } finally { setTesting(false); }
  };
  const [autoRan, setAutoRan] = useState(false);
  useEffect(() => { if (!autoRan && q.data && !dirty && desired && !linkUp && w.ppp0 !== undefined) { setAutoRan(true); test(true); } }, [q.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const apply = async (target: "wan" | "lan") => {
    setApplying(true);
    try {
      const body: Record<string, unknown> = { topology: target };
      if (target === "wan") { body.pppoe_user = user.trim(); body.pppoe_password = pass; body.wan_if = wanIf.trim(); }
      await api.post("/api/wan", body); setDirty(false); setPass("");
      toast({ kind: "info", title: t(target === "wan" ? "wan.applied.wan" : "wan.applied.lan"), persistent: true });
      await q.refetch(); await refresh().catch(() => {});
    } finally { setApplying(false); }
  };
  const banner = dirty ? t("wan.b.pending") : w.restart_scheduled ? t(desired ? "wan.b.rs.wan" : "wan.b.rs.lan")
    : w.pending && w.pending !== w.topology ? t("wan.b.next").replace("{p}", w.pending).replace("{t}", w.topology || "") : desired ? t(linkUp ? "wan.b.up" : "wan.b.down") : "";
  return (
    <Card className="col-span-12" title={<Term k="wan">{t("wan.title")}</Term>} sub={t("wan.sub")} actions={<><Badge tone={live ? "warning" : "success"} icon={false}>{live ? "WAN" : "LAN"}</Badge><Badge tone={linkUp ? "success" : "neutral"} icon={false}>ppp0 {w.ppp0 || "n/a"}</Badge></>}>
      {banner && <Banner tone={desired && !linkUp && !dirty ? "warning" : "info"} className="mb-3">{banner}</Banner>}
      <KV items={[[t("wan.k.source"), w.source === "dashboard" ? "dashboard" : "config.yaml"], [t("wan.k.ip"), w.ppp_ip ? iso(w.ppp_ip) : "—", true], ["IPv6", w.public_ip6 ? iso(w.public_ip6) : t("wan.k.v6none"), true]]} />
      <div className="mt-2 flex items-start justify-between gap-4 border-t border-line py-3">
        <div><b className="block text-[13.5px] text-fg">{t("wan.mode")}</b><p className="text-[12.5px] text-fg-3">{t("wan.mode.d")}</p></div>
        <Switch checked={on} onChange={edit(setOn)} label={t("wan.mode")} />
      </div>
      {on && (
        <div className="flex flex-col gap-3">
          <p className="text-[12.5px] text-fg-3">{t("wan.creds.d")}</p>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <TextField label={t("wan.user")} value={user} onChange={edit(setUser)} placeholder="user@isp" mono autoComplete="username" />
            <TextField label={t("wan.pass")} type="password" value={pass} onChange={edit(setPass)} autoComplete="new-password" placeholder={q.data?.pppoe_has_password ? t("wan.pass.stored") : t("wan.pass.enter")} />
          </div>
          <TextField label={t("wan.if")} hint={t("wan.if.h")} value={wanIf} onChange={edit(setWanIf)} placeholder="eth1" mono className="max-w-xs" />
          <div><Button onClick={() => test()} pending={testing} pendingText={t("wan.t.running")}>{t("wan.t.btn")}</Button></div>
          {msg && <pre className={clsx("whitespace-pre-wrap rounded-md border p-3 font-mono text-[12px]", msg.ok ? "border-success/40 bg-success-soft" : msg.ok === false ? "border-danger/40 bg-danger-soft" : "border-line bg-canvas")} role="status">{msg.text}</pre>}
          <p className="text-[12.5px] text-fg-3">{t("wan.t.h")}</p>
        </div>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="primary" pending={applying} pendingText={t("wan.applying")} disabled={!dirty && on === desired && (!on || linkUp)} onClick={() => setConfirm(on ? "wan" : "lan")}>{t("wan.apply")}</Button>
        {on && (desired || live) && <Button disabled={applying} onClick={() => setConfirm("lan")}>{t("wan.revert")}</Button>}
        {dirty && <Button variant="ghost" onClick={() => { setDirty(false); setOn(desired); q.refetch(); }}>{t("wan.cancel")}</Button>}
      </div>
      <details className="mt-4 border-t border-line pt-3 text-[13px]"><summary className="cursor-pointer font-semibold text-fg">{t("wan.guide")}</summary>
        <ol className="mt-2 list-decimal space-y-1 ps-5 text-fg-2"><li>{t("wan.g1")}</li><li>{t("wan.g2")}</li><li>{t("wan.g3")}</li></ol><p className="mt-2 text-fg-3">{t("wan.g.rev")}</p></details>
      <ConfirmDialog open={!!confirm} onOpenChange={(v) => !v && setConfirm(null)} title={t(confirm === "wan" ? "wan.cf.wan.t" : "wan.cf.lan.t")} confirmLabel={t(confirm === "wan" ? "wan.cf.wan.cta" : "wan.cf.lan.cta")}
        consequences={{ changes: t(confirm === "wan" ? "wan.warn.wan.c" : "wan.warn.lan.c"), affected: t("cf.everyone"), undo: t("wan.g.rev"), connection: t("wan.cf.conn") }}
        onConfirm={() => apply(confirm || "lan")} />
    </Card>
  );
}
