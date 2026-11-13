// Compatibility layer — delegates to the new api/transport layer.
// Preserves the exact same interface for existing callers (pages, main.js).
// Migration path: pages can keep importing from here while being gradually moved to hooks.

import {
  dashboardApi, bundleApi, userApi, deviceApi,
  networkApi, securityApi, dnsApi, vpnApi, systemApi,
} from "../api/index.js";

function nice(m) {
  if (/current password incorrect/i.test(m)) return "current password incorrect";
  if (/invalid code/i.test(m)) return "invalid code";
  const h = /^HTTP (\d{3})$/.exec(m); if (h) return `HTTP ${h[1]}`;
  return m;
}

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

async function req(method, path, body) {
  try {
    if (method === "GET") return await httpGet(path);
    if (method === "POST") return await httpPost(path, body);
    if (method === "PATCH") return await httpPatch(path, body);
    if (method === "DELETE") return await httpDel(path);
    throw new Error(`Unknown method: ${method}`);
  } catch (e) {
    if (e.message === "unauthorized") { onUnauthorized(); throw e; }
    throw new Error(nice(e.message));
  }
}

async function httpGet(path) {
  const { http } = await import("../transport/http.js");
  return await http.get(path);
}

async function httpPost(path, body) {
  const { http } = await import("../transport/http.js");
  return await http.post(path, body);
}

async function httpPatch(path, body) {
  const { http } = await import("../transport/http.js");
  return await http.patch(path, body);
}

async function httpDel(path) {
  const { http } = await import("../transport/http.js");
  return await http.del(path);
}

export const api = {
  get: (p) => req("GET", p),
  post: (p, b) => req("POST", p, b),
  patch: (p, b) => req("PATCH", p, b),
  del: (p) => req("DELETE", p),
};

// Re-export new API modules for gradual migration
export { dashboardApi, bundleApi, userApi, deviceApi, networkApi, securityApi, dnsApi, vpnApi, systemApi };
