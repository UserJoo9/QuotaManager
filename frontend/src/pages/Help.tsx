import { useState } from "react";
import { t } from "../lib/i18n";
import { TextField } from "../components/ui/forms";
import { Card } from "../components/ui/primitives";

const TERMS = ["wan", "lan", "quota", "period", "dhcp", "dns", "mac", "ip", "subnet", "vpn", "shaping", "guest", "rogue", "daily"];
export default function Help() {
  const [q, setQ] = useState("");
  const list = TERMS.filter((k) => (t("term." + k) + " " + t("term." + k + ".d")).toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <Card title={t("help.title")} sub={t("help.sub")} className="max-w-4xl">
      <TextField label={t("help.search")} value={q} onChange={setQ} type="search" />
      <dl className="mt-4 divide-y divide-line">
        {list.map((k) => <div key={k} className="py-3"><dt className="font-semibold text-fg">{t("term." + k)}</dt><dd className="mt-0.5 text-[13px] text-fg-2">{t("term." + k + ".d")}</dd></div>)}
        {!list.length && <p className="py-6 text-center text-[13px] text-fg-3">{t("f.nomatch")} <button type="button" className="font-semibold text-accent-fg" onClick={() => setQ("")}>{t("f.clear")}</button></p>}
      </dl>
      <p className="mt-3 text-[12.5px] text-fg-3">{t("help.more")}</p>
    </Card>
  );
}
