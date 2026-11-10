import { useEffect } from "react";
import { useLive, acceptIncoming, declineIncoming, cancelXfer, dismissDone, setNotifier, startLiveShare } from "../lib/liveTransfer";
import { t, tf } from "../lib/i18n";
import { fmtBytes } from "../lib/format";
import { Dialog, useToast } from "../components/ui/overlays";
import { Banner, Button, Meter } from "../components/ui/primitives";

/** Incoming request / progress for device-to-device transfers, on every page. */
export default function TransferOverlay() {
  const s = useLive();
  const toast = useToast();
  useEffect(() => { startLiveShare(); }, []);
  useEffect(() => { setNotifier((n) => toast({ kind: n.kind, title: n.key ? tf(n.key, n.vars || {}) : n.vars?.text || "" })); }, [toast]);
  const inc = s.incoming, x = s.xfer;
  if (inc) return (
    <Dialog open onOpenChange={(v) => !v && declineIncoming()} title={t("lt.incoming")} sub={`"${inc.sender_name}" (${inc.sender_ip})`}
      footer={<><Button onClick={declineIncoming}>{t("lt.decline")}</Button><Button variant="primary" onClick={acceptIncoming} autoFocus>{t("lt.accept")}</Button></>}>
      <p className="break-all text-[13.5px] font-semibold text-fg">{inc.file_name}</p>
      <p className="text-[12.5px] text-fg-3">{fmtBytes(inc.file_size)}</p>
    </Dialog>
  );
  if (!x) return null;
  const pct = x.size > 0 ? Math.min(100, Math.round((x.loaded / x.size) * 100)) : 0;
  const el = (Date.now() - x.start) / 1000, speed = el > 0.1 && x.loaded ? `${fmtBytes(x.loaded / el)}/s` : "";
  const title = x.phase === "done" ? t("lt.complete") : x.phase === "waiting" ? t("lt.waiting") : x.role === "receiver" ? tf("lt.receiving", { n: x.peerName || "" }) : t("lt.sending");
  return (
    <Dialog open onOpenChange={() => {}} title={title} busy
      footer={x.phase === "done" ? <Button variant="primary" onClick={dismissDone}>{t("lt.done")}</Button> : <Button onClick={cancelXfer}>{t("common.cancel")}</Button>}>
      <p className="break-all text-[13.5px] font-semibold text-fg">{x.name}</p>
      <div className="my-3" role="status" aria-live="polite">
        <Meter value={pct} neutral={x.phase !== "done"} label={x.name} />
        <div className="mt-1 flex justify-between text-[12px] text-fg-3 tabular-nums"><span>{pct}% ({fmtBytes(x.loaded)} / {fmtBytes(x.size)})</span><span>{speed}</span></div>
      </div>
      {x.phase !== "done" && <Banner tone="info">{t("lt.keepopen")}</Banner>}
    </Dialog>
  );
}
