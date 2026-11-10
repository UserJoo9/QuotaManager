import { useState } from "react";
import { actions, useAction } from "../../lib/actions";
import { t, tf } from "../../lib/i18n";
import { MAC_RE, normMac } from "../../lib/format";
import type { User } from "../../lib/types";
import { Dialog } from "../ui/overlays";
import { Button } from "../ui/primitives";
import { NumberField, TextField, Segmented, SwitchRow, Combobox } from "../ui/forms";

function QuotaPick({ mode, setMode, gb, setGb }: { mode: "auto" | "fixed"; setMode: (m: "auto" | "fixed") => void; gb: number | null; setGb: (v: number | null) => void }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[12.5px] font-medium text-fg-2">{t("dr.quota")}</span>
      <Segmented label={t("dr.quota")} value={mode} onChange={setMode} options={[{ value: "auto", label: t("dr.quota.auto") }, { value: "fixed", label: t("dr.quota.fixed") }]} />
      <p className="text-[12px] text-fg-3">{t("dr.quota." + mode + ".d")}</p>
      {mode === "fixed" && <NumberField label={t("dr.quota.gb")} value={gb} onValue={setGb} unit={t("common.gb")} spec={{ min: 0, max: 100000, decimals: 2 }} stepper />}
    </div>
  );
}
export function AddPersonDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [name, setName] = useState(""), [mode, setMode] = useState<"auto" | "fixed">("auto"), [gb, setGb] = useState<number | null>(10);
  const [down, setDown] = useState<number | null>(0), [up, setUp] = useState<number | null>(0), [exempt, setExempt] = useState(false), [vpn, setVpn] = useState(false);
  const [tried, setTried] = useState(false);
  const { run, pending } = useAction();
  const submit = async () => {
    setTried(true); if (!name.trim()) return;
    const body: Record<string, unknown> = { name: name.trim(), quota_mode: mode, limit_down_mbps: down || 0, limit_up_mbps: up || 0, exempt_quota: exempt, vpn_bypass: vpn };
    if (mode === "fixed") body.fixed_gb = gb || 0;
    if (await run("add", () => actions.createUser(body), tf("add.person.done", { name: name.trim() }), t("add.person.next"))) { setName(""); setTried(false); onOpenChange(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("add.user")} sub={t("add.user.sub")} busy={!!pending}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="primary" onClick={submit} pending={!!pending} pendingText={t("act.saving")}>{t("add.person.cta")}</Button></>}>
      <div className="flex flex-col gap-4">
        <TextField label={t("add.user.name")} value={name} onChange={setName} placeholder={t("add.user.name.ph")} error={tried && !name.trim() ? t("f.required") : null} autoFocus />
        <QuotaPick mode={mode} setMode={setMode} gb={gb} setGb={setGb} />
        <div className="grid grid-cols-2 gap-3">
          <NumberField label={t("dr.limit.down")} value={down} onValue={setDown} unit={t("unit.mbps")} spec={{ min: 0, max: 100000, decimals: 1, allowEmpty: true }} />
          <NumberField label={t("dr.limit.up")} value={up} onValue={setUp} unit={t("unit.mbps")} spec={{ min: 0, max: 100000, decimals: 1, allowEmpty: true }} />
        </div>
        <p className="-mt-2 text-[12px] text-fg-3">{t("dr.limit.hint")}</p>
        <div className="divide-y divide-line">
          <SwitchRow title={t("dr.unlimited")} desc={t("dr.unlimited.sub")} checked={exempt} onChange={setExempt} />
          <SwitchRow title={t("dr.vpnbypass")} desc={t("dr.vpnbypass.sub")} checked={vpn} onChange={setVpn} />
        </div>
      </div>
    </Dialog>
  );
}
export function AddDeviceDialog({ open, onOpenChange, users }: { open: boolean; onOpenChange: (v: boolean) => void; users: User[] }) {
  const [mac, setMac] = useState(""), [name, setName] = useState(""), [owner, setOwner] = useState("new"), [uname, setUname] = useState("");
  const [mode, setMode] = useState<"auto" | "fixed">("auto"), [gb, setGb] = useState<number | null>(10), [tried, setTried] = useState(false);
  const { run, pending } = useAction();
  const m = normMac(mac), macErr = tried || mac.length >= 17 ? (MAC_RE.test(m) ? null : t("n.mac.bad")) : null;
  const submit = async () => {
    setTried(true); if (!MAC_RE.test(m)) return;
    const body: Record<string, unknown> = { mac: m, name: name.trim(), quota_mode: mode };
    if (owner !== "new") body.user_id = +owner; else if (uname.trim()) body.user_name = uname.trim();
    if (mode === "fixed" && owner === "new") body.fixed_gb = gb || 0;
    if (await run("add", () => actions.createDevice(body), tf("add.dev.done", { name: name.trim() || m }))) { setMac(""); setName(""); setTried(false); onOpenChange(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("add.dev")} sub={t("add.dev.sub")} busy={!!pending}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="primary" onClick={submit} pending={!!pending} pendingText={t("act.saving")}>{t("add.dev.cta")}</Button></>}>
      <div className="flex flex-col gap-4">
        <TextField label={t("dr.mac")} hint={t("add.mac.hint")} value={mac} onChange={setMac} placeholder="aa:bb:cc:dd:ee:ff" mono error={macErr} autoFocus />
        <TextField label={t("add.dev.name")} value={name} onChange={setName} optional />
        <Combobox label={t("add.dev.owner")} value={owner} onChange={setOwner}
          options={[{ value: "new", label: t("add.dev.newuser") }, ...users.filter((u) => !u.protected).map((u) => ({ value: String(u.id), label: u.name }))]} />
        {owner === "new" && <>
          <TextField label={t("add.user.name")} value={uname} onChange={setUname} placeholder={t("add.dev.uname.ph")} optional />
          <QuotaPick mode={mode} setMode={setMode} gb={gb} setGb={setGb} />
        </>}
      </div>
    </Dialog>
  );
}
