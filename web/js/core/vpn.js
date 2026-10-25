// VPN data layer: one cache + the exact endpoints the classic dashboard uses.
import { api } from "./api.js";
export const vpn = { status: null, nodes: [], routing: { users: [], devices: [] }, settings: { allow_insecure: false }, logs: [], loaded: false, loading: false };

export const loadStatus = async () => { vpn.status = await api.get("/api/vpn/status"); };
export const loadNodes = async () => { vpn.nodes = await api.get("/api/vpn/nodes"); };
export const loadRouting = async () => { vpn.routing = await api.get("/api/vpn/routing"); };
export const loadSettings = async () => { vpn.settings = await api.get("/api/vpn/settings"); };
export const loadLogs = async () => { vpn.logs = (await api.get("/api/vpn/logs?limit=300")).logs || []; };

export async function loadAll() {
  if (vpn.loading) return;
  vpn.loading = true;
  try { await Promise.allSettled([loadStatus(), loadNodes(), loadRouting(), loadSettings(), loadLogs()]); vpn.loaded = true; }
  finally { vpn.loading = false; }
}
export const vpnApi = {
  connect: (node_id) => api.post("/api/vpn/connect", { node_id }),
  disconnect: () => api.post("/api/vpn/disconnect"),
  ping: (id) => api.post(`/api/vpn/nodes/${id}/ping`),
  addNode: (name, raw) => api.post("/api/vpn/nodes", { name: name || null, raw }),
  editNode: (id, payload) => api.patch(`/api/vpn/nodes/${id}`, payload),
  deleteNode: (id) => api.del(`/api/vpn/nodes/${id}`),
  route: (target_type, target_id, route_vpn) => api.post("/api/vpn/routing", { target_type, target_id, route_vpn }),
  setInsecure: (allow_insecure) => api.post("/api/vpn/settings", { allow_insecure }),
};

// Same extraction the classic UI does: structured config first, then the raw link.
export function nodeParams(node) {
  let cfg = {}; try { cfg = JSON.parse(node.config_json || "{}"); } catch { /* bad json */ }
  const tls = cfg.tls || {}, reality = tls.reality || {}, tr = cfg.transport || {};
  let host = "", port = 443, uuid = "", q = {};
  if (node.raw_uri && node.raw_uri.includes("://")) {
    try {
      const rest = node.raw_uri.split("://")[1], at = rest.indexOf("@");
      if (at !== -1) {
        uuid = decodeURIComponent(rest.substring(0, at));
        const hp = rest.substring(at + 1), qi = hp.indexOf("?"), hi = hp.indexOf("#");
        const hostPort = hp.substring(0, qi !== -1 ? qi : (hi !== -1 ? hi : undefined));
        if (hostPort.includes(":")) { host = hostPort.split(":")[0]; port = parseInt(hostPort.split(":")[1], 10) || 443; } else host = hostPort;
      }
      if (node.raw_uri.includes("?")) q = Object.fromEntries(new URLSearchParams(node.raw_uri.split("?")[1].split("#")[0]).entries());
    } catch { /* ignore */ }
  }
  const type = String(tr.type || q.type || "tcp").toLowerCase();
  return {
    name: node.name || "", server: cfg.server || host, server_port: cfg.server_port || port,
    uuid: cfg.uuid || cfg.password || uuid, sni: tls.server_name || q.sni || "", flow: cfg.flow || q.flow || "",
    pbk: reality.public_key || q.pbk || "", sid: reality.short_id || q.sid || "",
    transport: type === "ws" ? "WS" : type === "grpc" ? "gRPC" : "TCP",
    path: tr.path || tr.service_name || q.path || "",
  };
}
