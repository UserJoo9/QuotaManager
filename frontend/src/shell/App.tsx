import { Suspense, lazy, createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import * as DialogP from "@radix-ui/react-dialog";
import clsx from "clsx";
import { Sidebar } from "./Sidebar";
import { PAGES, prefetchLikely, routeById, type RouteId } from "./routes";
import { useSession, useDashboard } from "../lib/session";
import { usePrefs, setPref, effTheme } from "../lib/prefs";
import { t, tf, num, fmtTime, setLang, loadLang, privacyHidden, setPrivacyHidden } from "../lib/i18n";
import { api } from "../lib/api";
import { Button, IconButton, Banner, Loading, Spinner } from "../components/ui/primitives";
import { TextField } from "../components/ui/forms";
import { Welcome } from "./Welcome";
import { passkeyBlocker, signInWithPasskey, isCancel } from "../lib/passkey";
import { PageErrorBoundary, NotFound, ErrorView } from "../components/ui/ErrorView";


// Device-to-device transfers: engine + overlay load after first paint (own chunk).
const TransferOverlay = lazy(() => import("./TransferOverlay"));
const hashId = () => location.hash.replace(/^#\/?/, "");
const routeOf = (): RouteId => (routeById(hashId()) ? hashId() : "overview") as RouteId;
/** A typed or old bookmarked address that is not a page: say so instead of silently showing Overview. */
const isMissing = () => hashId() !== "" && !routeById(hashId());

/* Pages put their primary action into the title bar through this slot. */
const SlotCtx = createContext<HTMLElement | null>(null);
export function PageActions({ children }: { children: ReactNode }) {
  const el = useContext(SlotCtx);
  return el ? createPortal(children, el) : null;
}

export default function App() {
  const { authed } = useSession();
  const prefs = usePrefs();
  const [, force] = useState(0);
  useEffect(() => { let gone = false; loadLang(prefs.lang).then(() => { if (gone) return; setLang(prefs.lang); force((x) => x + 1); }).catch(() => {}); return () => { gone = true; }; }, [prefs.lang]);
  if (authed === null) return <div className="grid min-h-dvh place-items-center text-fg-3" role="status"><span className="flex items-center gap-2"><Spinner />{t("boot.checking")}</span></div>;
  if (authed === "unreachable") return <Unreachable />;
  if (!authed) return <Login />;
  return <Shell />;
}

/** The box did not answer at all (off, rebooting after an update, or this device is on another network). */
function Unreachable() {
  const { retryBoot } = useSession();
  const [n, setN] = useState(10);
  useEffect(() => { const i = setInterval(() => setN((x) => { if (x <= 1) { retryBoot(); return 10; } return x - 1; }), 1000); return () => clearInterval(i); }, [retryBoot]);
  return (
    <main className="grid min-h-dvh place-items-center bg-frame p-4">
      <ErrorView art="wifioff" title={t("boot.down.t")} actions={<Button variant="primary" icon="reset" onClick={retryBoot}>{t("conn.retry")}</Button>}>
        <p>{t("boot.down.d")}</p>
        <ul className="mt-3 list-disc ps-5 text-start text-[12.5px] text-fg-3"><li>{t("boot.down.1")}</li><li>{t("boot.down.2")}</li><li>{t("boot.down.3")}</li></ul>
        <p className="mt-3 text-[12px] text-fg-3" aria-live="polite">{tf("boot.down.auto", { n })}</p>
      </ErrorView>
    </main>
  );
}

function Login() {
  const { signedIn } = useSession();
  const [step, setStep] = useState<"password" | "totp">("password");
  const [pw, setPw] = useState(""), [code, setCode] = useState(""), [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false);
  // Passkey button only where the browser + address can actually do it (HTTPS name, built-in authenticator).
  const [pkOk, setPkOk] = useState(false), [pkBusy, setPkBusy] = useState(false);
  useEffect(() => { passkeyBlocker().then((b) => setPkOk(b === null)).catch(() => {}); }, []);
  const withPasskey = async () => {
    setPkBusy(true); setErr(null);
    try { await signInWithPasskey(); setPw(""); signedIn(); }
    catch (ex) { if (!isCancel(ex)) setErr(/invalid credentials|401/i.test((ex as Error).message) ? t("pk.login.unknown") : (ex as Error).message); setPkBusy(false); }
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); if (busy) return; setBusy(true); setErr(null);
    try {
      const res = await api.post<{ ok?: boolean; totp?: boolean }>("/api/login", { password: pw, code: step === "totp" ? code.trim() || undefined : undefined });
      if (res && res.ok === false && res.totp) { setStep("totp"); setBusy(false); return; }
      setPw(""); signedIn();
    } catch (ex) {
      const m = (ex as Error).message;
      setErr(/invalid credentials|unauthorized|401|password/i.test(m) ? t(step === "totp" ? "login.wrongcode" : "login.wrong") : m);
      setBusy(false);
    }
  };
  return (
    <main className="grid min-h-dvh place-items-center bg-frame p-4">
      <form onSubmit={submit} className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-line bg-surface p-7">
        <img src="/logo.svg" alt="" className="size-10" />
        <div><h1 className="text-[20px] font-semibold text-fg">{t("login.title")}</h1><p className="text-[13px] text-fg-3">{t("login.sub")}</p></div>
        {step === "password"
          ? <TextField label={t("login.password")} type="password" autoComplete="current-password" value={pw} onChange={setPw} autoFocus required />
          : <TextField label={t("login.code")} hint={t("login.code.hint")} inputMode="numeric" autoComplete="one-time-code" value={code} onChange={setCode} autoFocus mono maxLength={8} />}
        {err && <p role="alert" className="text-[13px] text-danger">{err}</p>}
        <Button type="submit" variant="primary" pending={busy} pendingText={t("login.wait")}>{step === "totp" ? t("login.verify") : t("login.submit")}</Button>
        {pkOk && step === "password" && <>
          <div className="flex items-center gap-3 text-[12px] text-fg-3" aria-hidden><span className="h-px flex-1 bg-line" />{t("pk.or")}<span className="h-px flex-1 bg-line" /></div>
          <Button icon="lock" onClick={withPasskey} pending={pkBusy} pendingText={t("pk.login.wait")}>{t("pk.login")}</Button>
        </>}
        <p className="text-[12px] leading-relaxed text-fg-3">{t("login.trust")}</p>
      </form>
    </main>
  );
}

function Shell() {
  const { live, lastUpdated, logout, connected } = useSession();
  const dash = useDashboard();
  const prefs = usePrefs();
  const [route, setRoute] = useState<RouteId>(routeOf);
  const [missing, setMissing] = useState(isMissing);
  const [navOpen, setNavOpen] = useState(false);
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const main = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const on = () => { setRoute(routeOf()); setMissing(isMissing()); setNavOpen(false); main.current?.focus({ preventScroll: true }); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on); return () => window.removeEventListener("hashchange", on);
  }, []);
  useEffect(() => { prefetchLikely(route); document.title = `${t("nav." + route)} · QuotaManager`; }, [route, prefs.lang]);
  const Page = PAGES[route];
  const rail = prefs.rail;
  const dark = effTheme() === "dark";
  const sub = route === "people" && dash ? `${num(dash.users.filter((u) => !u.protected).length, 0)} ${t("sub.people")}` : t("sub." + route);
  const sidebar = (mobile: boolean) => <Sidebar route={route} dash={dash} live={live} rail={!mobile && rail} mobile={mobile} onNavigate={() => setNavOpen(false)} onLogout={logout} />;
  return (
    <SlotCtx.Provider value={slot}>
      <a href="#main" className="qm-sr focus:not-sr-only focus:fixed focus:start-3 focus:top-3 focus:z-[100] focus:rounded-md focus:bg-raised focus:px-3 focus:py-2">{t("a11y.skip")}</a>
      <div className="flex min-h-dvh">
        <div className="sticky top-0 hidden h-dvh shrink-0 md:block">{sidebar(false)}</div>
        <DialogP.Root open={navOpen} onOpenChange={setNavOpen}>
          <DialogP.Portal>
            <DialogP.Overlay className="qm-sheet-overlay qm-overlay-anim md:hidden" />
            <DialogP.Content aria-describedby={undefined} className="qm-overlay-anim fixed inset-y-0 start-0 z-[61] h-dvh shadow-raised outline-none md:hidden">
              <DialogP.Title className="qm-sr">{t("common.menu")}</DialogP.Title>
              {sidebar(true)}
            </DialogP.Content>
          </DialogP.Portal>
        </DialogP.Root>
        <main ref={main} id="main" tabIndex={-1} className="flex min-w-0 flex-1 flex-col outline-none">
          <header className="sticky top-0 z-30 border-b border-line bg-canvas">
            <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 md:px-6">
              <IconButton icon="menu" label={t("common.menu")} className="md:hidden" onClick={() => setNavOpen(true)} />
              <div className="min-w-0 flex-1">
                <h1 className="truncate text-[17px] font-semibold text-fg md:text-[19px]">{missing ? t("err.404.short") : t("nav." + route)}</h1>
                {sub && !missing && <p className="truncate text-[12.5px] text-fg-3 max-sm:hidden">{sub}</p>}
              </div>
              <div ref={setSlot} className="flex items-center gap-2 empty:hidden max-sm:order-last max-sm:w-full max-sm:justify-end" />
              <div className="flex items-center gap-0.5">
                <div role="group" aria-label={t("common.language")} className="me-1 flex rounded-md border border-line p-0.5">
                  {(["ar", "en"] as const).map((l) => (
                    <button key={l} type="button" aria-pressed={prefs.lang === l} onClick={() => setPref("lang", l)} lang={l}
                      className={clsx("h-7 min-w-8 rounded-[5px] px-2 text-[12px] font-semibold max-md:h-9 max-md:min-w-10", prefs.lang === l ? "bg-raised text-fg" : "text-fg-3 hover:text-fg")}>{l === "ar" ? "ع" : "EN"}</button>
                  ))}
                </div>
                <PrivacyToggle />
                <IconButton icon={dark ? "sun" : "moon"} label={dark ? t("theme.light") : t("theme.dark")} onClick={() => setPref("theme", dark ? "light" : "dark")} />
              </div>
            </div>
          </header>
          <ConnectionBanner live={live} lastUpdated={lastUpdated} connected={connected} />
          <div key={route} className="qm-route-enter flex-1 px-4 pb-16 pt-4 md:px-6 md:pt-5">
            <Suspense fallback={<Loading what={t("boot.page")} h={260} />}>
              <PageErrorBoundary key={route + String(missing)}>{missing ? <NotFound /> : <Page />}</PageErrorBoundary>
            </Suspense>
          </div>
        </main>
      </div>
      <Welcome />
      {/* incoming transfers must pop up on every page, from sign-in on */}
      <Suspense fallback={null}><TransferOverlay /></Suspense>
    </SlotCtx.Provider>
  );
}

function PrivacyToggle() {
  const [hidden, setHidden] = useState(privacyHidden());
  return <IconButton icon={hidden ? "eyeoff" : "eye"} label={t(hidden ? "common.priv.show" : "common.priv.hide")} aria-pressed={!hidden}
    onClick={() => { setPrivacyHidden(!hidden); setHidden(!hidden); window.dispatchEvent(new Event("qm:privacy")); }} />;
}

/** Calm but persistent: live data is stale while this shows, and it says since when. */
function ConnectionBanner({ live, lastUpdated, connected }: { live: string; lastUpdated: number | null; connected: boolean }) {
  const [, tick] = useState(0);
  useEffect(() => { const i = setInterval(() => tick((x) => x + 1), 10000); return () => clearInterval(i); }, []);
  if (connected || live === "connecting") return null;
  const since = lastUpdated ? tf("conn.since", { t: fmtTime(lastUpdated) }) : "";
  return (
    <div className="px-4 pt-3 md:px-6">
      <Banner tone={live === "offline" ? "danger" : "warning"} title={live === "offline" ? t("conn.lost") : t("conn.reconnecting.t")}
        action={<Button size="sm" icon="reset" onClick={() => location.reload()}>{t("conn.retry")}</Button>}>
        {t(live === "offline" ? "conn.lost.d" : "conn.reconnecting.d")} {since}
      </Banner>
    </div>
  );
}
