import { useEffect, useMemo, useState, type ReactNode } from "react";
import clsx from "clsx";
import { useDashboard } from "../../lib/session";
import { actions, useAction } from "../../lib/actions";
import { t, tf, num, iso, mask } from "../../lib/i18n";
import { gb, userStatus, deviceStatus, devIcon, isIPv4, isIPv6, type Status } from "../../lib/format";
import type { User, Device } from "../../lib/types";
import { Sheet, ConfirmDialog, useToast } from "../ui/overlays";
import { Badge, Banner, Button, Meter, KV, IconButton, Term } from "../ui/primitives";
import { NumberField, TextField, Segmented, SwitchRow, Combobox } from "../ui/forms";
import { Icon } from "../ui/Icon";
import { DailySection, DeviceShare } from "./DailyPlan";

/* ---------------------------------------------------------------- shared bits */
function Section({ title, sub, children, className }: { title: ReactNode; sub?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={clsx("border-b border-line py-4 first:pt-0 last:border-0", className)}><h3 className="text-[13.5px] font-semibold text-fg">{title}</h3>{sub && <p className="mt-0.5 text-[12.5px] text-fg-3">{sub}</p>}<div className="mt-3 flex flex-col gap-3">{children}</div></section>;
}
function Split({ wan, lan, up, down }: { wan: number; lan: number; up?: number; down?: number }) {
  return (
    <div className="grid grid-cols-2 gap-2">
      <div className="rounded-md border border-line bg-canvas p-2.5"><div className="text-[12px] text-fg-3"><Term k="wan" /></div><b className="text-[15px] text-fg tabular-nums">{gb(wan)}</b>{up != null && <div className="text-[11.5px] text-fg-3 tabular-nums" dir="ltr">↑ {num(up)} · ↓ {num(down)}</div>}</div>
      <div className="rounded-md border border-line bg-canvas p-2.5"><div className="text-[12px] text-fg-3"><Term k="lan" /></div><b className="text-[15px] text-fg tabular-nums">{gb(lan)}</b><div className="text-[11.5px] text-fg-3">{t("ov.net.split.short")}</div></div>
    </div>
  );
}
function StatusLine({ st }: { st: Status }) {
  return <div className="flex flex-wrap items-center gap-2"><Badge tone={st.tone}>{st.text}</Badge>{st.reason && <span className="text-[12.5px] text-fg-3">{st.reason}</span>}</div>;
}
function Copy({ value, label }: { value: string; label: string }) {
  const toast = useToast();
  return <IconButton icon="copy" size={14} className="size-7" label={`${t("cp.copy")} ${label}`} onClick={async () => {
    try { await navigator.clipboard.writeText(value); toast({ kind: "success", title: tf("cp.done", { what: label }) }); } catch { toast({ kind: "error", title: t("cp.fail") }); }
  }} />;
}
/** Settings both people and devices share. Values are strings/numbers in a local draft. */
interface Draft { name: string; down: number | null; up: number | null; mode: "auto" | "fixed"; fixed: number | null; exempt: boolean; vpn: boolean; dns: string; hist: number | null }
const same = (a: Draft, b: Draft) => JSON.stringify(a) === JSON.stringify(b);
function useDraft(server: Draft, key: string) {
  const [draft, setDraft] = useState<Draft>(server);
  const [base, setBase] = useState<Draft>(server);
  const dirty = !same(draft, base);
  // A live update only replaces the form when nothing has been changed by the admin.
  useEffect(() => { if (!dirty) { setDraft(server); setBase(server); } }, [JSON.stringify(server), key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setDraft(server); setBase(server); }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));
  return { draft, set, dirty, reset: () => setDraft(base), commit: () => setBase(draft) };
}
const dnsError = (s: string) => (s.trim() && !isIPv4(s) && !isIPv6(s) ? t("dr.dns.bad") : null);

function LimitsAndRules({ draft, set, kind, withQuota }: { draft: Draft; set: <K extends keyof Draft>(k: K, v: Draft[K]) => void; kind: "user" | "device"; withQuota: boolean }) {
  return (
    <>
      <Section title={t("dr.limits")} sub={t("dr.limit.hint")}>
        <div className="grid grid-cols-2 gap-3">
          <NumberField label={t("dr.limit.down")} value={draft.down} onValue={(v) => set("down", v)} unit={t("unit.mbps")} spec={{ min: 0, max: 100000, decimals: 1, allowEmpty: true }} placeholder="0" />
          <NumberField label={t("dr.limit.up")} value={draft.up} onValue={(v) => set("up", v)} unit={t("unit.mbps")} spec={{ min: 0, max: 100000, decimals: 1, allowEmpty: true }} placeholder="0" />
        </div>
      </Section>
      {withQuota && (
        <Section title={t("dr.quota")} sub={t("dr.quota.sub")}>
          <Segmented label={t("dr.quota")} value={draft.mode} onChange={(v) => set("mode", v)} options={[{ value: "auto", label: t("dr.quota.auto") }, { value: "fixed", label: t("dr.quota.fixed") }]} />
          <p className="text-[12px] text-fg-3">{t("dr.quota." + draft.mode + ".d")}</p>
          {draft.mode === "fixed" && <NumberField label={t("dr.quota.gb")} value={draft.fixed} onValue={(v) => set("fixed", v)} unit={t("common.gb")} spec={{ min: 0, max: 100000, decimals: 2 }} stepper />}
        </Section>
      )}
      <Section title={t("dr.rules")}>
        <div className="-my-3 divide-y divide-line">
          <SwitchRow title={t("dr.unlimited")} desc={t(kind === "user" ? "dr.unlimited.sub" : "dr.bypass.sub")} checked={draft.exempt} onChange={(v) => set("exempt", v)} />
          <SwitchRow title={t("dr.vpnbypass")} desc={t("dr.vpnbypass.sub")} checked={draft.vpn} onChange={(v) => set("vpn", v)} />
        </div>
        <TextField label={<Term k="dns">{t("dr.dns")}</Term>} hint={t("dr.dns.hint")} value={draft.dns} onChange={(v) => set("dns", v)} placeholder="1.1.1.1" mono error={dnsError(draft.dns)} optional />
      </Section>
    </>
  );
}
async function saveDraft(kind: "user" | "device", id: number, before: Draft, d: Draft, withQuota: boolean) {
  const patch = kind === "user" ? actions.patchUser : actions.patchDevice;
  const body: Record<string, unknown> = {};
  if (d.name.trim() !== before.name) body.name = d.name.trim();
  if (d.down !== before.down) body.limit_down_mbps = d.down ?? 0;
  if (d.up !== before.up) body.limit_up_mbps = d.up ?? 0;
  if (withQuota && (d.mode !== before.mode || d.fixed !== before.fixed)) { body.quota_mode = d.mode; if (d.mode === "fixed") body.fixed_gb = d.fixed ?? 0; }
  if (d.exempt !== before.exempt) body[kind === "user" ? "exempt_quota" : "bypass"] = d.exempt;
  if (d.vpn !== before.vpn) body.vpn_bypass = d.vpn;
  if (kind === "user" && d.hist !== before.hist && d.hist != null) body.history_days = d.hist;
  if (Object.keys(body).length) await patch(id, body);
  if (d.dns.trim() !== before.dns) await (kind === "user" ? actions.dnsUser : actions.dnsDevice)(id, d.dns.trim());
}
function TopUpRow({ onAdd, pending }: { onAdd: (v: number) => Promise<boolean>; pending: boolean }) {
  const [v, setV] = useState<number | null>(null), [arm, setArm] = useState(false);
  return (
    <Section title={t("dr.topup")} sub={t("dr.topup.hint")}>
      <NumberField label={t("dr.topup.amount")} value={v} onValue={(x) => { setV(x); setArm(false); }} unit={t("common.gb")} spec={{ min: 0.01, max: 100000, decimals: 2, allowEmpty: true }} presets={[1, 5, 10]} />
      <div className="flex flex-wrap items-center gap-2">
        {!arm ? <Button size="sm" icon="plus" disabled={!(v && v > 0)} onClick={() => setArm(true)}>{t("dr.topup.add")}</Button>
          : <><Button size="sm" variant="primary" pending={pending} pendingText={t("act.saving")} onClick={async () => { if (v && (await onAdd(v))) { setV(null); setArm(false); } }}>{tf("dr.topup.cta", { n: num(v, 2) })}</Button><Button size="sm" variant="ghost" onClick={() => setArm(false)}>{t("common.cancel")}</Button></>}
      </div>
    </Section>
  );
}

/* ---------------------------------------------------------------- person */
/** Mounted only while a person is selected, so the form starts from their real values. */
export function PersonSheet({ userId, onClose }: { userId: number | null; onClose: () => void }) {
  const d = useDashboard();
  const exists = userId != null && !!d?.users.some((x) => x.id === userId);
  // the person was removed elsewhere: close instead of showing stale data
  useEffect(() => { if (userId != null && d && !exists) onClose(); }, [userId, d, exists, onClose]);
  return exists ? <PersonSheetBody key={userId} userId={userId} onClose={onClose} /> : null;
}
function PersonSheetBody({ userId, onClose }: { userId: number; onClose: () => void }) {
  const d = useDashboard();
  const u = d?.users.find((x) => x.id === userId);
  const [device, setDevice] = useState<number | null>(null);
  const [dailyDirty, setDailyDirty] = useState(false);
  const server: Draft = useMemo(() => u ? { name: u.name || "", down: u.limit_down_mbps || 0, up: u.limit_up_mbps || 0, mode: u.quota_mode === "fixed" ? "fixed" : "auto", fixed: u.fixed_gb ?? 0, exempt: !!u.exempt_quota, vpn: !!u.vpn_bypass, dns: u.dns_server || "", hist: u.history_days } : ({} as Draft), [u]);
  const f = useDraft(server, String(userId));
  const { run, pending } = useAction();
  const [confirm, setConfirm] = useState<null | "block" | "kick" | "blacklist" | "reject">(null);
  if (!u || !d) return null;
  const st = userStatus(u), devices = u.devices || [];
  const invalid = !!dnsError(f.draft.dns) || !f.draft.name.trim();
  const save = () => run("save", async () => { await saveDraft("user", u.id, server, f.draft, true); f.commit(); }, t("dr.saved"), t("dr.saved.sub"));
  const gw = d.gateway;
  return (
    <>
      <Sheet open onOpenChange={(v) => !v && onClose()} title={u.name} sub={u.protected ? t("common.protected") : u.guest ? t("common.guest") : t("dr.person_sub")} icon="people" dirty={f.dirty || dailyDirty} size="full"
        footer={<>
          {f.dirty && <span className="me-auto self-center text-[12px] text-fg-3">{t("sheet.pending")}</span>}
          <Button onClick={f.reset} disabled={!f.dirty}>{t("sheet.revert")}</Button>
          <Button variant="primary" onClick={save} disabled={!f.dirty || invalid} pending={pending === "save"} pendingText={t("act.saving")}>{t("dr.save.person")}</Button>
        </>}>
        <Section title={t("dr.thisperiod")}>
          <div className="flex items-baseline gap-2"><span className="text-[26px] font-semibold text-fg tabular-nums">{num(u.used_gb)}</span><span className="text-[13px] text-fg-3">{u.allowance_gb > 0 ? tf("dr.of.allow", { n: num(u.allowance_gb) }) : `${t("common.gb")} · ${t("pp.unlimited")}`}</span></div>
          {u.allowance_gb > 0 && <Meter value={u.percent} label={u.name} />}
          <StatusLine st={st} />
          <Split wan={u.used_gb} lan={u.lan_used_gb} />
        </Section>
        <Section title={t("pp.access")} sub={u.blocked ? t("dr.access.off") : t("dr.access.on")}>
          {u.protected && gw?.blocked_desired && gw.blocked_programmed === false && <Banner tone="warning">{t("dr.gw.warn")}</Banner>}
          {u.protected && u.blocked && !(gw?.blocked_desired && gw.blocked_programmed === false) && <Banner tone="info">{t("dr.gw.cut")}</Banner>}
          <div className="flex flex-wrap gap-2">
            {u.blocked ? <Button variant="primary" icon="check" pending={pending === "unblock"} onClick={() => run("unblock", () => actions.blockUser(u.id, false), tf("pp.unblocked", { name: u.name }))}>{t("pp.unblock")}</Button>
              : <Button variant="danger" icon="ban" onClick={() => setConfirm("block")}>{t("pp.block")}</Button>}
            {u.guest && u.blocked && <Button variant="primary" icon="check" pending={pending === "approve"} onClick={() => run("approve", () => actions.approveGuest(u.id), tf("feed.guest.ok", { name: u.name }))}>{t("dr.approve")}</Button>}
            {u.guest && <Button pending={pending === "regular"} onClick={() => run("regular", () => actions.makeRegular(u.id), t("dr.regular.done"))}>{t("dr.regular")}</Button>}
            {u.guest && <Button variant="danger" onClick={() => setConfirm("reject")}>{t("dr.reject")}</Button>}
          </div>
        </Section>
        {!u.protected && <Section title={t("dr.details")}>
          <TextField label={t("dr.name")} value={f.draft.name} onChange={(v) => f.set("name", v)} error={!f.draft.name.trim() ? t("f.required") : null} />
          <NumberField label={t("dr.hist")} hint={t("dr.hist.hint")} value={f.draft.hist} onValue={(v) => f.set("hist", v)} unit={t("common.days")} spec={{ min: 0, max: 365, integer: true, allowEmpty: true }} placeholder={t("dr.hist.ph")} stepper presets={[7, 30, 90]} />
        </Section>}
        <LimitsAndRules draft={f.draft} set={f.set} kind="user" withQuota />
        <Section title={t("dr.devices")} sub={tf("pp.devs.on", { n: num(devices.length, 0), o: num(devices.filter((x) => x.connected).length, 0) })}>
          <ul className="divide-y divide-line rounded-md border border-line">
            {devices.map((x) => { const ds = deviceStatus(x); return (
              <li key={x.id}><button type="button" onClick={() => setDevice(x.id)} className="flex w-full items-center gap-3 px-3 py-2.5 text-start hover:bg-canvas">
                <Icon name={devIcon(x)} size={17} className="text-fg-3" />
                <span className="min-w-0 flex-1"><b className="block truncate text-[13px] text-fg">{x.name || mask(x.mac)}</b><span className="qm-ltr font-mono text-[11.5px] text-fg-3">{mask(x.mac)}{x.ip ? ` · ${x.ip}` : ""}</span></span>
                <span className="text-[12.5px] font-semibold text-fg tabular-nums">{gb(x.device_used_gb)}</span><Badge tone={ds.tone}>{ds.text}</Badge><Icon name="chev" size={15} className="text-fg-3 rtl:rotate-180" />
              </button></li>
            ); })}
            {!devices.length && <li className="px-3 py-4 text-center text-[12.5px] text-fg-3">{t("pp.nodevices")}</li>}
          </ul>
        </Section>
        <DailySection u={u} dash={d} onDirty={setDailyDirty} />
        {!u.protected && <TopUpRow pending={pending === "topup"} onAdd={(v) => run("topup", () => actions.topupUser(u.id, v), tf("dr.topup.done.n", { n: num(v, 2), name: u.name }))} />}
        {!u.protected && (
          <Section title={t("dr.danger")} className="[&_h3]:text-danger">
            <DangerRow title={t("dr.kick")} sub={t("dr.kick.sub")} label={t("dr.kick")} onClick={() => setConfirm("kick")} />
            <DangerRow title={t("dr.blacklist")} sub={t("dr.bl.sub")} label={t("dr.blacklist")} onClick={() => setConfirm("blacklist")} />
          </Section>
        )}
      </Sheet>
      <PersonConfirms u={u} which={confirm} onClose={() => setConfirm(null)} onGone={onClose} />
      <DeviceSheet userId={u.id} deviceId={device} onClose={() => setDevice(null)} />
    </>
  );
}
function DangerRow({ title, sub, label, onClick }: { title: string; sub: string; label: string; onClick: () => void }) {
  return <div className="flex items-start justify-between gap-3"><div><b className="block text-[13px] text-fg">{title}</b><p className="text-[12.5px] text-fg-3">{sub}</p></div><Button size="sm" variant="danger" onClick={onClick}>{label}</Button></div>;
}
function PersonConfirms({ u, which, onClose, onGone }: { u: User; which: null | "block" | "kick" | "blacklist" | "reject"; onClose: () => void; onGone: () => void }) {
  const { run } = useAction();
  const n = (u.devices || []).length, on = (u.devices || []).filter((x) => x.connected).length;
  const affected = <>{tf("cf.person.devs", { name: u.name, n: num(n, 0), o: num(on, 0) })}<ul className="mt-1 list-disc ps-5 text-[12.5px] text-fg-2">{(u.devices || []).slice(0, 6).map((x) => <li key={x.id}>{x.name || mask(x.mac)}</li>)}</ul></>;
  const must = async (ok: Promise<boolean>) => { if (!(await ok)) throw new Error(t("act.failed")); };
  const conf = {
    block: { title: tf("cf.block.t", { name: u.name }), label: tf("pp.block.cta", { name: u.name }), c: { changes: t("cf.block.changes"), affected, undo: t("cf.block.undo"), connection: u.protected ? t("cf.block.gw") : undefined }, go: () => must(run("block", () => actions.blockUser(u.id, true), tf("pp.blocked", { name: u.name }))) },
    kick: { title: tf("cf.kick.t", { name: u.name }), label: t("dr.kick"), c: { changes: t("cf.kick.changes"), affected, undo: t("cf.kick.undo") }, go: async () => { await must(run("kick", () => actions.kickUser(u.id), t("dr.done"))); onGone(); } },
    blacklist: { title: tf("cf.bl.t", { name: u.name }), label: t("dr.blacklist"), c: { changes: t("cf.bl.changes"), affected, undo: t("cf.bl.undo") }, go: async () => { await must(run("bl", () => actions.blacklistUser(u.id), t("dr.done"))); onGone(); } },
    reject: { title: tf("cf.reject.t", { name: u.name }), label: t("dr.reject"), c: { changes: t("cf.reject.changes"), affected, undo: t("cf.reject.undo") }, go: async () => { await must(run("rej", () => actions.rejectGuest(u.id), t("dr.done"))); onGone(); } },
  };
  const c = which ? conf[which] : null;
  return <ConfirmDialog open={!!which} onOpenChange={(v) => !v && onClose()} title={c?.title} consequences={c?.c || { changes: "" }} confirmLabel={c?.label || ""} onConfirm={c ? c.go : async () => {}} />;
}

/* ---------------------------------------------------------------- device */
export function DeviceSheet({ userId, deviceId, onClose }: { userId: number; deviceId: number | null; onClose: () => void }) {
  const d = useDashboard();
  const exists = deviceId != null && !!d?.users.find((x) => x.id === userId)?.devices.some((y) => y.id === deviceId);
  useEffect(() => { if (deviceId != null && d && !exists) onClose(); }, [deviceId, d, exists, onClose]);
  return exists ? <DeviceSheetBody key={deviceId} userId={userId} deviceId={deviceId} onClose={onClose} /> : null;
}
function DeviceSheetBody({ userId, deviceId, onClose }: { userId: number; deviceId: number; onClose: () => void }) {
  const d = useDashboard();
  const users = d?.users || [];
  const u = users.find((x) => x.id === userId);
  const x: Device | undefined = u?.devices.find((y) => y.id === deviceId);
  const server: Draft = useMemo(() => x ? { name: x.name || "", down: x.limit_down_mbps || 0, up: x.limit_up_mbps || 0, mode: "auto", fixed: 0, exempt: !!x.bypass, vpn: !!x.vpn_bypass, dns: x.dns_server || "", hist: null } : ({} as Draft), [x]);
  const f = useDraft(server, String(deviceId));
  const [owner, setOwner] = useState(String(userId));
  useEffect(() => { setOwner(String(userId)); }, [userId, deviceId]);
  const { run, pending } = useAction();
  const [confirm, setConfirm] = useState<null | "block" | "kick" | "blacklist">(null);
  if (!x || !u) return null;
  const moved = owner !== String(userId);
  const dirty = f.dirty || moved;
  const ds = deviceStatus(x);
  const save = () => run("save", async () => { await saveDraft("device", x.id, server, f.draft, false); if (moved) await actions.patchDevice(x.id, { user_id: +owner }); f.commit(); }, t("dr.saved"), moved ? tf("dr.moved", { name: users.find((y) => String(y.id) === owner)?.name || "" }) : t("dr.saved.sub"));
  const must = async (ok: Promise<boolean>) => { if (!(await ok)) throw new Error(t("act.failed")); };
  const affected = tf("cf.dev.affected", { name: x.name || mask(x.mac), owner: u.name });
  return (
    <>
      <Sheet open onOpenChange={(v) => !v && onClose()} title={x.name || mask(x.mac)} sub={`${t("dr.dev.sub")} · ${u.name}`} icon={devIcon(x)} dirty={dirty} size="full"
        footer={x.gateway ? undefined : <>
          <Button onClick={() => { f.reset(); setOwner(String(userId)); }} disabled={!dirty}>{t("sheet.revert")}</Button>
          <Button variant="primary" onClick={save} disabled={!dirty || !!dnsError(f.draft.dns)} pending={pending === "save"} pendingText={t("act.saving")}>{t("dr.save.device")}</Button>
        </>}>
        <Section title={t("dr.thisperiod")}>
          <div className="flex items-baseline gap-2"><span className="text-[26px] font-semibold text-fg tabular-nums">{num(x.device_used_gb)}</span><span className="text-[13px] text-fg-3">{t("common.gb")} · {u.name}</span></div>
          {u.allowance_gb > 0 && <Meter value={(x.device_used_gb / u.allowance_gb) * 100} label={x.name} />}
          <StatusLine st={ds} />
          <Split wan={x.device_used_gb} lan={x.lan_used_gb} up={x.device_up_gb} down={x.device_down_gb} />
        </Section>
        <Section title={t("dr.addresses")}>
          <KV items={[
            [<span className="inline-flex items-center gap-1"><Term k="mac" /><Copy value={x.mac} label="MAC" /></span>, iso(x.mac), true],
            x.ip ? [<span className="inline-flex items-center gap-1"><Term k="ip" /><Copy value={x.ip} label="IP" /></span>, x.ip, true] : [t("dr.ip"), t("dr.ip.none")],
            x.ip6 ? [<span className="inline-flex items-center gap-1">{t("dr.ip6")}<Copy value={x.ip6} label="IPv6" /></span>, x.ip6, true] : null,
            x.vendor ? [t("dr.vendor"), x.vendor] : null,
          ]} />
        </Section>
        {!x.gateway && (
          <Section title={t("pp.access")} sub={x.blocked ? t("dr.access.off.dev") : t("dr.access.on.dev")}>
            <div>{x.blocked ? <Button variant="primary" icon="check" pending={pending === "unblock"} onClick={() => run("unblock", () => actions.blockDevice(x.id, false), tf("pp.unblocked", { name: x.name }))}>{t("pp.unblock.dev")}</Button>
              : <Button variant="danger" icon="ban" onClick={() => setConfirm("block")}>{t("pp.block.dev")}</Button>}</div>
          </Section>
        )}
        {!x.gateway && <Section title={t("dr.details")}>
          <TextField label={t("dr.name")} value={f.draft.name} onChange={(v) => f.set("name", v)} />
          <Combobox label={t("dr.move")} hint={t("dr.move.hint")} value={owner} onChange={setOwner}
            options={users.filter((y) => !y.protected || y.id === u.id).map((y) => ({ value: String(y.id), label: y.name, sub: tf("pp.devs.n", { n: num((y.devices || []).length, 0) }) }))} />
        </Section>}
        {!x.gateway && <LimitsAndRules draft={f.draft} set={f.set} kind="device" withQuota={false} />}
        {!x.gateway && <DeviceShare x={x} u={u} />}
        <TopUpRow pending={pending === "topup"} onAdd={(v) => run("topup", () => actions.topupDevice(x.id, v), tf("dr.topup.done.n", { n: num(v, 2), name: x.name }))} />
        {!x.gateway && (
          <Section title={t("dr.danger")} className="[&_h3]:text-danger">
            <DangerRow title={t("dr.kick")} sub={t("dr.kick.sub")} label={t("dr.kick")} onClick={() => setConfirm("kick")} />
            <DangerRow title={t("dr.blacklist")} sub={t("dr.bl.sub")} label={t("dr.blacklist")} onClick={() => setConfirm("blacklist")} />
          </Section>
        )}
      </Sheet>
      <ConfirmDialog open={confirm === "block"} onOpenChange={(v) => !v && setConfirm(null)} title={tf("cf.block.t", { name: x.name || mask(x.mac) })} confirmLabel={t("pp.block.dev")}
        consequences={{ changes: t("cf.blockdev.changes"), affected, undo: t("cf.block.undo") }} onConfirm={() => must(run("block", () => actions.blockDevice(x.id, true), tf("pp.blocked", { name: x.name })))} />
      <ConfirmDialog open={confirm === "kick"} onOpenChange={(v) => !v && setConfirm(null)} title={tf("cf.kick.t", { name: x.name || mask(x.mac) })} confirmLabel={t("dr.kick")}
        consequences={{ changes: t("cf.kickdev.changes"), affected, undo: t("cf.kick.undo") }} onConfirm={async () => { await must(run("kick", () => actions.kickDevice(x.id), t("dr.done"))); onClose(); }} />
      <ConfirmDialog open={confirm === "blacklist"} onOpenChange={(v) => !v && setConfirm(null)} title={tf("cf.bl.t", { name: x.name || mask(x.mac) })} confirmLabel={t("dr.blacklist")}
        consequences={{ changes: t("cf.bldev.changes"), affected, undo: t("cf.bl.undo") }} onConfirm={async () => { await must(run("bl", () => actions.blacklistDevice(x.id), t("dr.done"))); onClose(); }} />
    </>
  );
}
