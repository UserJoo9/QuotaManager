import { t } from "./i18n.js";
// Same contract as the legacy UI: cookie session + X-QM-CSRF on every request.
// Known English server messages are shown in the UI language.
function nice(m) {
  if (/current password incorrect/i.test(m)) return t("err.pw.bad");
  if (/invalid code/i.test(m)) return t("err.code.bad");
  const h = /^HTTP (\d{3})$/.exec(m); if (h) return t("err.http").replace("{n}", h[1]);
  return m;
}
let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

async function req(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json", "X-QM-CSRF": "1" }, credentials: "same-origin" };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401 && path !== "/api/login") { onUnauthorized(); throw new Error("unauthorized"); }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const msg = (data && data.detail) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? nice(msg) : JSON.stringify(msg));
  }
  return data;
}
export const api = {
  get: (p) => req("GET", p),
  post: (p, b) => req("POST", p, b),
  patch: (p, b) => req("PATCH", p, b),
  del: (p) => req("DELETE", p),
};
