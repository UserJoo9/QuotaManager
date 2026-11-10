import { Component, useState, type ReactNode, type ErrorInfo } from "react";
import { t } from "../../lib/i18n";
import { Illustration } from "./Illustration";
import { Button } from "./primitives";

/** Full-area message for a page that cannot be shown: what happened, why, what to do. */
export function ErrorView({ art = "crash", title, children, actions, details }: { art?: string; title: ReactNode; children?: ReactNode; actions?: ReactNode; details?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div role="alert" className="mx-auto flex max-w-lg flex-col items-center gap-2 px-4 py-12 text-center">
      <Illustration name={art} size={150} className="mb-2" />
      <h2 className="text-[18px] font-semibold text-fg">{title}</h2>
      {children && <div className="text-[13.5px] leading-relaxed text-fg-2">{children}</div>}
      {actions && <div className="mt-3 flex flex-wrap justify-center gap-2">{actions}</div>}
      {details && (
        <details className="mt-4 w-full text-start text-[12px]">
          <summary className="cursor-pointer text-fg-3">{t("err.details")}</summary>
          <pre className="qm-ltr mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-line bg-canvas p-2 font-mono text-[11.5px] text-fg-2">{details}</pre>
          <Button size="sm" variant="ghost" icon="copy" className="mt-1" onClick={async () => { try { await navigator.clipboard.writeText(details); setCopied(true); } catch { /* ignore */ } }}>{copied ? t("cp.done.short") : t("err.copy")}</Button>
        </details>
      )}
    </div>
  );
}

/** After an update the old hashed chunks are gone: a tab opened before it must reload, not "crash". */
export const isChunkError = (e: unknown) => /Loading chunk|dynamically imported module|Importing a module script failed|error loading dynamically/i.test(String((e as Error)?.message || e));

interface State { error: Error | null }
/** Keeps a broken page from taking the whole dashboard (sidebar, banners, sheets) down with it. */
export class PageErrorBoundary extends Component<{ children: ReactNode; onHome?: () => void }, State> {
  state: State = { error: null };
  static getDerivedStateFromError(error: Error): State { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error("page error", error, info.componentStack); }
  render() {
    const e = this.state.error;
    if (!e) return this.props.children;
    if (isChunkError(e)) return (
      <ErrorView art="update" title={t("err.update.t")} actions={<Button variant="primary" icon="reset" onClick={() => location.reload()}>{t("err.update.cta")}</Button>}>
        {t("err.update.d")}
      </ErrorView>
    );
    return (
      <ErrorView title={t("err.page.t")} details={`${e.name}: ${e.message}\n${(e.stack || "").split("\n").slice(1, 6).join("\n")}`}
        actions={<><Button variant="primary" icon="reset" onClick={() => this.setState({ error: null })}>{t("err.retry")}</Button>
          <Button onClick={() => { location.hash = "#/overview"; this.setState({ error: null }); }}>{t("err.home")}</Button></>}>
        {t("err.page.d")}
      </ErrorView>
    );
  }
}

export function NotFound() {
  return (
    <ErrorView art="compass" title={t("err.404.t")} actions={<Button variant="primary" onClick={() => { location.hash = "#/overview"; }}>{t("err.home")}</Button>}>
      {t("err.404.d")}
    </ErrorView>
  );
}
