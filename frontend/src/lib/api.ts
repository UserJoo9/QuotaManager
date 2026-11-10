import { t } from "./i18n";
// Same contract as before: cookie session + X-QM-CSRF on every request.
export class ApiError extends Error {
  constructor(message: string, public status: number, public detail?: unknown) { super(message); }
}
let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

// Turn server/transport failures into plain-language causes with a recovery hint.
export function explain(status: number, raw: string): string {
  if (/current password incorrect/i.test(raw)) return t("err.pw.bad");
  if (/invalid code/i.test(raw)) return t("err.code.bad");
  if (status === 0) return t("err.net");
  if (status === 403) return t("err.forbidden");
  if (status === 409) return raw || t("err.conflict");
  if (status === 422) return t("err.invalid") + (raw ? ` (${raw})` : "");
  if (status === 429) return t("err.slow");
  if (status === 503) return raw || t("err.busy");
  if (status >= 500) return t("err.server") + (raw ? ` (${raw})` : "");
  return raw || t("err.http").replace("{n}", String(status));
}
function detailText(d: unknown): string {
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? String((x as { msg: unknown }).msg) : JSON.stringify(x))).join("; ");
  return d ? JSON.stringify(d) : "";
}
async function req<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { method, signal, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-QM-CSRF": "1" }, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) { if ((e as Error).name === "AbortError") throw e; throw new ApiError(explain(0, ""), 0); }
  if (res.status === 401 && path !== "/api/login") { onUnauthorized(); throw new ApiError(t("err.session"), 401); }
  let data: unknown = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && typeof data === "object" ? (data as { detail?: unknown }).detail : undefined;
    throw new ApiError(explain(res.status, detailText(d)), res.status, d);
  }
  return data as T;
}
export const api = {
  get: <T = unknown>(p: string, signal?: AbortSignal) => req<T>("GET", p, undefined, signal),
  post: <T = unknown>(p: string, b?: unknown) => req<T>("POST", p, b),
  patch: <T = unknown>(p: string, b?: unknown) => req<T>("PATCH", p, b),
  del: <T = unknown>(p: string) => req<T>("DELETE", p),
};
