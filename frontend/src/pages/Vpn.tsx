import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useAction } from "../lib/actions";
import { vpnApi, nodeParams, type VpnNode, type VpnStatus, type RouteItem } from "../lib/vpn";
import { t, tf, num, mask } from "../lib/i18n";
import { fmtBytes } from "../lib/format";
import { Badge, Banner, Button, Card, Empty, IconButton, Loading, Stat, Term } from "../components/ui/primitives";
import { NumberField, Segmented, Select, Switch, SwitchRow, TextField, Field, controlClass } from "../components/ui/forms";
import { ConfirmDialog, Dialog } from "../components/ui/overlays";
import { Icon } from "../components/ui/Icon";
import { PageActions } from "../shell/App";

/** "Is the tunnel up, which server, and who goes through it?" */
export default function Vpn() {
  // Status + logs every 3 s only while this page is open and visible.
  const status = useQuery({ queryKey: ["vpn", "status"], queryFn: vpnApi.status, refetchInterval: 3000, refetchIntervalInBackground: false });
  const nodes = useQuery({ queryKey: ["vpn", "nodes"], queryFn: vpnApi.nodes, staleTime: 15_000 });
  const [add, setAdd] = useState(false);
  if (status.isError && !status.data) return <Empty icon="alert" title={t("vpn.fail")} sub={(status.error as Error).message} action={<Button size="sm" onClick={() => status.refetch()}>{t("conn.retry")}</Button>} />;
  if (!status.data || !nodes.data) return <Loading what={t("vpn.loading")} h={260} />;
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <PageActions><Button size="sm" variant="primary" icon="plus" onClick={() => setAdd(true)}>{t("vpn.add")}</Button></PageActions>
      <Hero st={status.data} nodes={nodes.data} onAdd={() => setAdd(true)} />
      <Routing />
      <Nodes st={status.data} nodes={nodes.data} onAdd={() => setAdd(true)} />
      <Logs />
      {add && <AddNode onClose={() => setAdd(false)} />}
    </div>
  );
}
function useVpnRefresh() {
  const qc = useQueryClient();
  return () => Promise.all([qc.invalidateQueries({ queryKey: ["vpn"] })]);
}

function Hero({ st, nodes, onAdd }: { st: VpnStatus; nodes: VpnNode[]; onAdd: () => void }) {
  const { run, pending } = useAction(); const refresh = useVpnRefresh();
  const [confirmOff, setConfirmOff] = useState(false);
  const state = st.state || "disconnected", on = state === "connected";
  const label = ({ connected: t("vpn.connected"), connecting: t("vpn.connecting"), error: t("vpn.error") } as Record<string, string>)[state] || t("vpn.disconnected");
  const up = st.uptime_seconds || 0;
  const target = st.active_node_id ?? nodes[0]?.id;
  return (
    <Card className="col-span-12">
      <div className="flex flex-wrap items-center gap-4">
        <span className={clsx("grid size-11 place-items-center rounded-full", on ? "bg-success-soft text-success" : state === "error" ? "bg-danger-soft text-danger" : "bg-canvas text-fg-3")}><Icon name="vpn" size={20} /></span>
        <div className="min-w-0 flex-1">
          <h2 className="text-[18px] font-semibold text-fg">{label}</h2>
          <p className="truncate text-[12.5px] text-fg-3">{st.active_node_name ? `${st.active_node_name} · ${(st.protocol || "vless").toUpperCase()} · ${st.interface || ""}` : t("vpn.nonode")}</p>
        </div>
        {on || state === "connecting"
          ? <Button variant="danger" onClick={() => setConfirmOff(true)}>{t("vpn.disconnect")}</Button>
          : target != null ? <Button variant="primary" pending={pending === "hero"} pendingText={t("vpn.connecting")} onClick={() => run("hero", async () => { await vpnApi.connect(Number(target)); await refresh(); }, t("vpn.connect.sent"), t("vpn.connect.verify"))}>{t("vpn.connect")}</Button>
          : <Button variant="primary" icon="plus" onClick={onAdd}>{t("vpn.add")}</Button>}
      </div>
      {st.last_error && <Banner tone="danger" className="mt-3">{st.last_error}</Banner>}
      <div className="mt-4 grid grid-cols-2 gap-4 border-t border-line pt-4 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label={t("vpn.down")} value={fmtBytes((st.speed_down_bps || 0) / 8) + t("unit.ps")} />
        <Stat label={t("vpn.up")} value={fmtBytes((st.speed_up_bps || 0) / 8) + t("unit.ps")} />
        <Stat label={t("vpn.sess_down")} value={fmtBytes(st.total_down_bytes || 0)} />
        <Stat label={t("vpn.sess_up")} value={fmtBytes(st.total_up_bytes || 0)} />
        <Stat label={t("vpn.conns")} value={num(st.active_connections || 0, 0)} />
        <Stat label={t("vpn.uptime")} value={on && up ? `${Math.floor(up / 3600)}h ${Math.floor((up % 3600) / 60)}m` : "—"} />
      </div>
      <ConfirmDialog open={confirmOff} onOpenChange={setConfirmOff} title={t("vpn.off.t")} confirmLabel={t("vpn.disconnect")}
        consequences={{ changes: t("vpn.off.changes"), affected: t("vpn.off.who"), undo: t("vpn.off.undo") }}
        onConfirm={async () => { if (!(await run("off", async () => { await vpnApi.disconnect(); await refresh(); }, t("vpn.off.done")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

function Nodes({ st, nodes, onAdd }: { st: VpnStatus; nodes: VpnNode[]; onAdd: () => void }) {
  const { run, pending } = useAction(); const refresh = useVpnRefresh();
  const settings = useQuery({ queryKey: ["vpn", "settings"], queryFn: vpnApi.settings, staleTime: 60_000 });
  const [edit, setEdit] = useState<VpnNode | null>(null), [del, setDel] = useState<VpnNode | null>(null), [insecure, setInsecure] = useState(false);
  const [pinged, setPinged] = useState<Set<number>>(new Set());
  const activeId = st.active_node_id != null ? Number(st.active_node_id) : null, connected = st.state === "connected";
  return (
    <Card className="col-span-12 lg:col-span-5" title={t("vpn.nodes")} sub={t("vpn.nodes.d")} actions={<Button size="sm" variant="primary" icon="plus" onClick={onAdd}>{t("vpn.add")}</Button>}>
      {!nodes.length ? <Empty icon="vpn" title={t("vpn.nonodes")} sub={t("vpn.nonodes.sub")} action={<Button size="sm" icon="plus" onClick={onAdd}>{t("vpn.add")}</Button>} /> : (
        <ul className="divide-y divide-line">{nodes.map((n) => {
          const isActive = activeId === Number(n.id), ms = n.ping_ms ?? 0;
          return (
            <li key={n.id} className={clsx("flex flex-wrap items-center gap-2 py-2.5", isActive && "rounded-md bg-accent-soft/40 px-2")}>
              <span className={clsx("size-2 shrink-0 rounded-full", isActive && connected ? "bg-success" : "bg-line-strong")} aria-hidden />
              <span className="min-w-0 flex-1"><b className="block truncate text-[13px] text-fg">{n.name}</b>
                <span className="flex items-center gap-1.5 text-[11.5px] text-fg-3"><Badge icon={false}>{(n.protocol || "vless").toUpperCase()}</Badge>
                  {ms > 0 ? <Badge tone={ms < 150 ? "success" : "warning"} icon={false}>{Math.round(ms)} ms</Badge> : ms < 0 && pinged.has(n.id) ? <Badge tone="danger">{t("vpn.timeout")}</Badge> : null}</span></span>
              {isActive && connected ? <Badge tone="success">{t("vpn.connected")}</Badge>
                : <Button size="sm" variant="primary" pending={pending === "n" + n.id} onClick={() => run("n" + n.id, async () => { await vpnApi.connect(n.id); await refresh(); }, t("vpn.connect.sent"), t("vpn.connect.verify"))}>{t("vpn.connect")}</Button>}
              <Button size="sm" icon="bolt" pending={pending === "p" + n.id} onClick={() => run("p" + n.id, async () => { await vpnApi.ping(n.id); setPinged((s) => new Set(s).add(n.id)); await refresh(); }, t("vpn.pinged"))}>{t("vpn.ping")}</Button>
              <IconButton icon="edit" label={`${t("vpn.edit")} ${n.name}`} onClick={() => setEdit(n)} />
              <IconButton icon="x" tone="danger" label={`${t("vpn.delete")} ${n.name}`} onClick={() => setDel(n)} />
            </li>
          );
        })}</ul>
      )}
      <div className="mt-2 border-t border-line">
        <SwitchRow title={t("vpn.insecure")} desc={t("vpn.insecure.d")} checked={!!settings.data?.allow_insecure} pending={pending === "ins"}
          onChange={(v) => (v ? setInsecure(true) : run("ins", async () => { await vpnApi.setInsecure(false); await settings.refetch(); }, t("vpn.insecure.off")))} />
      </div>
      {edit && <EditNode node={edit} onClose={() => setEdit(null)} />}
      <ConfirmDialog open={!!del} onOpenChange={(v) => !v && setDel(null)} title={tf("vpn.del.t", { n: del?.name || "" })} confirmLabel={t("vpn.delete")}
        consequences={{ changes: activeId === Number(del?.id) && connected ? t("vpn.del.active") : t("vpn.del.changes"), affected: del?.name, undo: t("vpn.del.undo") }}
        onConfirm={async () => { if (del && !(await run("del", async () => { await vpnApi.deleteNode(del.id); await refresh(); }, t("dr.done")))) throw new Error(t("act.failed")); }} />
      <ConfirmDialog open={insecure} onOpenChange={setInsecure} title={t("vpn.insecure")} confirmLabel={t("vpn.insecure.cta")}
        consequences={{ changes: t("vpn.insecure.changes"), affected: t("vpn.off.who"), undo: t("vpn.insecure.undo") }}
        onConfirm={async () => { if (!(await run("ins", async () => { await vpnApi.setInsecure(true); await settings.refetch(); }, t("vpn.insecure.on")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

function Routing() {
  const q = useQuery({ queryKey: ["vpn", "routing"], queryFn: vpnApi.routing, staleTime: 15_000 });
  const { run, pending } = useAction(); const qc = useQueryClient();
  const [tab, setTab] = useState<"users" | "devices">("users"), [query, setQuery] = useState(""), [bulk, setBulk] = useState<null | boolean>(null);
  const list = useMemo(() => { const s = query.trim().toLowerCase(); return (q.data?.[tab] || []).filter((x) => !s || (x.name || "").toLowerCase().includes(s) || (x.mac || "").toLowerCase().includes(s)); }, [q.data, tab, query]);
  const reload = () => qc.invalidateQueries({ queryKey: ["vpn", "routing"] });
  const set = (x: RouteItem, v: boolean) => run("r" + x.id, async () => { await vpnApi.route(tab === "users" ? "user" : "device", x.id, v); await reload(); }, tf(v ? "vpn.via.n" : "vpn.direct.n", { name: x.name }));
  const total = (q.data?.users.length || 0) + (q.data?.devices.length || 0);
  return (
    <Card className="col-span-12 lg:col-span-7" title={t("vpn.routing")} sub={t("vpn.routing.d")}
      actions={<><Button size="sm" onClick={() => setBulk(true)}>{t("vpn.route_all")}</Button><Button size="sm" onClick={() => setBulk(false)}>{t("vpn.direct_all")}</Button></>}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented size="sm" label={t("vpn.routing")} value={tab} onChange={setTab} options={[{ value: "users", label: t("vpn.tab.users"), count: q.data?.users.length }, { value: "devices", label: t("vpn.tab.devices"), count: q.data?.devices.length }]} />
        <label className="relative min-w-40 flex-1"><span className="qm-sr">{t("vpn.search")}</span><Icon name="search" size={16} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-fg-3" />
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("vpn.search")} className={clsx(controlClass, "ps-9")} /></label>
        {query && <Button size="sm" variant="ghost" icon="reset" onClick={() => setQuery("")}>{t("f.clearall")}</Button>}
      </div>
      {!q.data ? <Loading what={t("vpn.loading")} h={180} /> : !list.length ? <Empty icon="filter" title={t("pp.empty")} action={query ? <Button size="sm" onClick={() => setQuery("")}>{t("f.clearall")}</Button> : undefined} /> : (
        <ul className="max-h-96 divide-y divide-line overflow-y-auto">{list.map((x) => (
          <li key={x.id} className="flex items-center gap-3 py-2">
            <span className="min-w-0 flex-1"><b className="block truncate text-[13px] text-fg">{x.name}</b>{x.mac && <span className="qm-ltr font-mono text-[11.5px] text-fg-3">{mask(x.mac)}</span>}</span>
            <Badge tone={x.route_vpn ? "success" : "neutral"} icon={false}>{x.route_vpn ? t("vpn.via_vpn") : t("vpn.direct")}</Badge>
            <Switch checked={x.route_vpn} pending={pending === "r" + x.id} onChange={(v) => set(x, v)} label={tf("vpn.route.n", { name: x.name })} />
          </li>))}</ul>
      )}
      <ConfirmDialog open={bulk !== null} onOpenChange={(v) => !v && setBulk(null)} title={bulk ? t("vpn.route_all") : t("vpn.direct_all")} confirmLabel={bulk ? t("vpn.route_all") : t("vpn.direct_all")} danger={false}
        consequences={{ changes: bulk ? t("vpn.all.on") : t("vpn.all.off"), affected: tf("vpn.all.who", { n: num(total, 0) }), undo: t("vpn.all.undo") }}
        onConfirm={async () => { const v = !!bulk; if (!(await run("all", async () => {
          await Promise.all([...(q.data?.users || []).map((x) => vpnApi.route("user", x.id, v)), ...(q.data?.devices || []).map((x) => vpnApi.route("device", x.id, v))]); await reload();
        }, t("dr.done")))) throw new Error(t("act.failed")); }} />
    </Card>
  );
}

const strip = (s: string) => (s || "").replace(/\u001b\[[0-9;]*m/g, ""); // eslint-disable-line no-control-regex
const level = (s: string) => (/FATAL|panic|ERROR|failed|failure/i.test(s) ? "e" : /WARN/i.test(s) ? "w" : /INFO/i.test(s) ? "i" : "");
function Logs() {
  const q = useQuery({ queryKey: ["vpn", "logs"], queryFn: vpnApi.logs, refetchInterval: 3000, refetchIntervalInBackground: false });
  const [f, setF] = useState<"all" | "errors" | "warn">("all");
  const box = useRef<HTMLDivElement | null>(null), near = useRef(true);
  const logs = q.data?.logs || [];
  const shown = logs.filter((l) => { const k = level(strip(l.text)); return f === "all" || (f === "errors" ? k === "e" : k === "e" || k === "w"); });
  // stay at the bottom only when the reader already was there
  useEffect(() => { const el = box.current; if (el && near.current) el.scrollTop = el.scrollHeight; }, [shown.length]);
  return (
    <Card className="col-span-12" title={t("vpn.logs")} sub={t("vpn.logs.d")}
      actions={<><Segmented size="sm" label={t("vpn.logs")} value={f} onChange={(v) => { setF(v); near.current = true; }} options={[{ value: "all", label: t("vpn.f.all") }, { value: "errors", label: t("vpn.f.errors") }, { value: "warn", label: t("vpn.f.warn") }]} />
        <Button size="sm" icon="reset" onClick={() => q.refetch()}>{t("vpn.refresh")}</Button></>}>
      <div ref={box} role="log" tabIndex={0} aria-label={t("vpn.logs")} onScroll={(e) => { const el = e.currentTarget; near.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}
        className="qm-ltr h-72 overflow-y-auto rounded-md border border-line bg-canvas p-3 font-mono text-[12px] leading-relaxed text-fg-2">
        {shown.length ? shown.map((l, i) => { const s = strip(l.text), k = level(s); return (
          <div key={i}>{l.time && <span className="text-fg-3">[{l.time}] </span>}<span className={clsx(k === "e" && "text-danger", k === "w" && "text-warning", k === "i" && "text-info")}>{s}</span></div>); })
          : <span className="text-fg-3">{logs.length ? t("vpn.nomatch") : t("vpn.nologs")}</span>}
      </div>
    </Card>
  );
}

function AddNode({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState(""), [raw, setRaw] = useState(""), [tried, setTried] = useState(false);
  const { run, pending } = useAction(); const refresh = useVpnRefresh();
  const links = raw.split("\n").filter((x) => /^(vless|vmess|trojan|ss|hysteria2?|tuic):\/\//i.test(x.trim()) || x.trim().startsWith("{"));
  const err = tried && !links.length ? t("vpn.link.bad") : null;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("vpn.add_title")} sub={t("vpn.add_d")} busy={!!pending}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" pending={!!pending} pendingText={t("vpn.verifying")}
        onClick={async () => { setTried(true); if (!links.length) return; if (await run("add", async () => { await vpnApi.addNode(name.trim(), raw.trim()); await refresh(); }, t("vpn.saved"))) onClose(); }}>{t("vpn.save_verify")}</Button></>}>
      <div className="flex flex-col gap-4">
        <TextField label={t("vpn.name")} value={name} onChange={setName} placeholder={t("vpn.name_ph")} optional />
        <Field label={<Term k="vpn">{t("vpn.link")}</Term>} hint={t("vpn.link.h")} error={err}>
          {({ id, describedBy, invalid }) => <textarea id={id} aria-describedby={describedBy} aria-invalid={invalid} rows={5} dir="ltr" spellCheck={false} value={raw} onChange={(e) => setRaw(e.target.value)}
            placeholder={"vless://…\nvmess://…\ntrojan://…\nss://…"} className={clsx(controlClass, "h-auto py-2 font-mono text-[12.5px]")} />}
        </Field>
      </div>
    </Dialog>
  );
}
function EditNode({ node, onClose }: { node: VpnNode; onClose: () => void }) {
  const p0 = nodeParams(node);
  const [p, setP] = useState(p0), [port, setPort] = useState<number | null>(p0.server_port);
  const { run, pending } = useAction(); const refresh = useVpnRefresh();
  const set = (k: keyof typeof p) => (v: string) => setP((x) => ({ ...x, [k]: v }));
  const bad = !p.server.trim() || !p.uuid.trim() || !port;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("vpn.edit_title")} sub={node.name} wide busy={!!pending}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={bad} pending={!!pending} pendingText={t("act.saving")} onClick={async () => {
        if (await run("edit", async () => { await vpnApi.editNode(node.id, { name: p.name.trim() || null, server: p.server.trim(), server_port: port, uuid: p.uuid.trim(), sni: p.sni.trim() || null, flow: p.flow.trim() || null,
          pbk: p.pbk.trim() || null, sid: p.sid.trim() || null, transport: p.transport, path: p.path.trim() || null }); await refresh(); }, t("vpn.saved"))) onClose(); }}>{t("vpn.save")}</Button></>}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-[3fr_1fr]">
        <TextField label={t("vpn.name")} value={p.name} onChange={set("name")} className="sm:col-span-2" />
        <TextField label={t("vpn.server")} value={p.server} onChange={set("server")} mono error={!p.server.trim() ? t("f.required") : null} />
        <NumberField label={t("vpn.port")} value={port} onValue={setPort} spec={{ min: 1, max: 65535, integer: true }} />
        <TextField label={t("vpn.uuid")} value={p.uuid} onChange={set("uuid")} mono className="sm:col-span-2" error={!p.uuid.trim() ? t("f.required") : null} />
        <TextField label={t("vpn.sni")} value={p.sni} onChange={set("sni")} mono optional />
        <TextField label={t("vpn.flow")} value={p.flow} onChange={set("flow")} mono optional />
        <TextField label={t("vpn.pbk")} value={p.pbk} onChange={set("pbk")} mono optional />
        <TextField label={t("vpn.sid")} value={p.sid} onChange={set("sid")} mono optional />
        <Select label={t("vpn.transport")} value={p.transport} onChange={set("transport")} options={["TCP", "WS", "gRPC"].map((v) => ({ value: v, label: v }))} />
        <TextField label={t("vpn.path")} value={p.path} onChange={set("path")} mono optional />
      </div>
    </Dialog>
  );
}
