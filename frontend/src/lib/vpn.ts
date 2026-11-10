// VPN data layer: the exact endpoints the gateway exposes (tests/test_new_ui.py checks for drift).
import { api } from "./api";

export interface VpnStatus { state?: string; active_node_id?: number | string | null; active_node_name?: string; protocol?: string; interface?: string; last_error?: string; uptime_seconds?: number; speed_down_bps?: number; speed_up_bps?: number; total_down_bytes?: number; total_up_bytes?: number; active_connections?: number }
export interface VpnNode { id: number; name: string; protocol?: string; ping_ms?: number; raw_uri?: string; config_json?: string }
export interface RouteItem { id: number; name: string; mac?: string; route_vpn: boolean }
export interface VpnRouting { users: RouteItem[]; devices: RouteItem[] }

export const vpnApi = {
  status: () => api.get<VpnStatus>("/api/vpn/status"),
  nodes: () => api.get<VpnNode[]>("/api/vpn/nodes"),
  routing: () => api.get<VpnRouting>("/api/vpn/routing"),
  settings: () => api.get<{ allow_insecure: boolean }>("/api/vpn/settings"),
  logs: () => api.get<{ logs?: { time?: string; text: string }[] }>("/api/vpn/logs?limit=300"),
  connect: (node_id: number) => api.post("/api/vpn/connect", { node_id }),
  disconnect: () => api.post("/api/vpn/disconnect"),
  ping: (id: number) => api.post(`/api/vpn/nodes/${id}/ping`),
  addNode: (name: string, raw: string) => api.post("/api/vpn/nodes", { name: name || null, raw }),
  editNode: (id: number, payload: object) => api.patch(`/api/vpn/nodes/${id}`, payload),
  deleteNode: (id: number) => api.del(`/api/vpn/nodes/${id}`),
  route: (target_type: string, target_id: number, route_vpn: boolean) => api.post("/api/vpn/routing", { target_type, target_id, route_vpn }),
  setInsecure: (allow_insecure: boolean) => api.post("/api/vpn/settings", { allow_insecure }),
};

/** Structured config first, then the raw link (same extraction as before). */
export function nodeParams(node: VpnNode) {
  let cfg: Record<string, any> = {}; // eslint-disable-line @typescript-eslint/no-explicit-any
  try { cfg = JSON.parse(node.config_json || "{}"); } catch { /* bad json */ }
  const tls = cfg.tls || {}, reality = tls.reality || {}, tr = cfg.transport || {};
  let host = "", port = 443, uuid = "", q: Record<string, string> = {};
  if (node.raw_uri && node.raw_uri.includes("://")) {
    try {
      const rest = node.raw_uri.split("://")[1], at = rest.indexOf("@");
      if (at !== -1) {
        uuid = decodeURIComponent(rest.substring(0, at));
        const hp = rest.substring(at + 1), qi = hp.indexOf("?"), hi = hp.indexOf("#");
        const hostPort = hp.substring(0, qi !== -1 ? qi : hi !== -1 ? hi : undefined);
        if (hostPort.includes(":")) { host = hostPort.split(":")[0]; port = parseInt(hostPort.split(":")[1], 10) || 443; } else host = hostPort;
      }
      if (node.raw_uri.includes("?")) q = Object.fromEntries(new URLSearchParams(node.raw_uri.split("?")[1].split("#")[0]).entries());
    } catch { /* ignore */ }
  }
  const type = String(tr.type || q.type || "tcp").toLowerCase();
  return {
    name: node.name || "", server: String(cfg.server || host), server_port: Number(cfg.server_port || port),
    uuid: String(cfg.uuid || cfg.password || uuid), sni: String(tls.server_name || q.sni || ""), flow: String(cfg.flow || q.flow || ""),
    pbk: String(reality.public_key || q.pbk || ""), sid: String(reality.short_id || q.sid || ""),
    transport: type === "ws" ? "WS" : type === "grpc" ? "gRPC" : "TCP", path: String(tr.path || tr.service_name || q.path || ""),
  };
}
