import { useState } from "react";
import { useDashboard } from "../lib/session";
import { usePrefs, setPref } from "../lib/prefs";
import { t } from "../lib/i18n";
import { Badge, Button, Card, Meter, Banner } from "../components/ui/primitives";
import { Segmented, SwitchRow } from "../components/ui/forms";
import { useToast } from "../components/ui/overlays";
import { AccountTab, UpdatesTab, LogsTab } from "./SystemTabs";

type Tab = "appearance" | "account" | "updates" | "logs";
export default function System() {
  const [tab, setTab] = useState<Tab>("appearance");
  return (
    <div className="flex flex-col gap-[var(--gap)]">
      <Segmented label={t("sys.tabs")} value={tab} onChange={setTab} options={(["appearance", "account", "updates", "logs"] as Tab[]).map((k) => ({ value: k, label: t("sys.tab." + k) }))} />
      {tab === "appearance" ? <Appearance /> : tab === "account" ? <AccountTab /> : tab === "updates" ? <UpdatesTab /> : <LogsTab />}
    </div>
  );
}
function Row({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center justify-between gap-3 py-3"><div className="min-w-0"><b className="block text-[13.5px] text-fg">{title}</b>{desc && <p className="text-[12.5px] text-fg-3">{desc}</p>}</div>{children}</div>;
}
function Appearance() {
  const p = usePrefs();
  const d = useDashboard();
  const toast = useToast();
  // Appearance is a local preference: it applies instantly (no server round-trip involved).
  const set = <K extends Parameters<typeof setPref>[0]>(k: K, v: Parameters<typeof setPref<K>>[1]) => { if (!setPref(k, v)) toast({ kind: "warning", title: t("sys.nosave") }); };
  return (
    <div className="grid grid-cols-12 gap-[var(--gap)]">
      <Card className="col-span-12 lg:col-span-7" title={t("sys.appearance")} sub={t("sys.appearance.d")}>
        <div className="divide-y divide-line">
          <Row title={t("sys.mode")} desc={t("sys.mode.d")}><Segmented label={t("sys.mode")} value={p.theme} onChange={(v) => set("theme", v)} options={[{ value: "light", label: t("sys.theme.light") }, { value: "dark", label: t("sys.theme.dark") }, { value: "auto", label: t("sys.theme.auto") }]} /></Row>
          <Row title={t("sys.language")}><Segmented label={t("sys.language")} value={p.lang} onChange={(v) => set("lang", v)} options={[{ value: "ar", label: "العربية" }, { value: "en", label: "English" }]} /></Row>
          <Row title={t("sys.density")} desc={t("sys.density.d")}><Segmented label={t("sys.density")} value={p.density} onChange={(v) => set("density", v)} options={[{ value: "comfortable", label: t("sys.density.comfortable") }, { value: "compact", label: t("sys.density.compact") }]} /></Row>
          <Row title={t("sys.motion")} desc={t("sys.motion.d")}><Segmented label={t("sys.motion")} value={p.motion} onChange={(v) => set("motion", v)} options={[{ value: "full", label: t("sys.motion.full") }, { value: "reduced", label: t("sys.motion.reduced") }]} /></Row>
        </div>
      </Card>
      <Card className="col-span-12 lg:col-span-5" title={t("sys.sidebar")} sub={t("sys.sidebar.d")}>
        <SwitchRow title={t("sys.rail")} desc={t("sys.rail.d")} checked={p.rail} onChange={(v) => set("rail", v)} />
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
          <Button size="sm" icon="reset" disabled={!p.navOrder} onClick={() => { set("navOrder", null); toast({ kind: "success", title: t("nav.order.reset.done") }); }}>{t("nav.order.reset")}</Button>
          <span className="text-[12px] text-fg-3">{p.navOrder ? t("sys.order.custom") : t("sys.order.default")}</span>
        </div>
      </Card>
      <Card className="col-span-12" title={t("sys.preview")} sub={t("sys.pv.note")}>
        <div className="flex flex-wrap items-center gap-2" aria-hidden>
          <Button variant="primary" tabIndex={-1}>{t("sys.pv.primary")}</Button><Button tabIndex={-1}>{t("sys.pv.ghost")}</Button><Button variant="danger" tabIndex={-1}>{t("sys.pv.danger")}</Button>
          <Badge tone="success">{t("pp.st.online")}</Badge><Badge tone="warning">{t("ov.st.pace")}</Badge><Badge tone="danger">{t("pp.st.blocked")}</Badge><Badge tone="info">{t("pp.st.pending")}</Badge><Badge>{t("pp.st.offline")}</Badge>
        </div>
        <div className="mt-4 grid max-w-md gap-2"><Meter value={42} label="42%" /><Meter value={93} label="93%" /><Meter value={100} label="100%" /></div>
        <Banner tone="info" className="mt-4">{t("sys.pv.banner")}</Banner>
      </Card>
      <Card className="col-span-12" title={t("sys.about")}>
        <p className="text-[13px] text-fg-2">QuotaManager <span className="qm-ltr font-mono">{d?.version ? "v" + d.version : ""}</span></p>
        <p className="mt-1 text-[12.5px] text-fg-3">{t("sys.about.privacy")}</p>
      </Card>
    </div>
  );
}
