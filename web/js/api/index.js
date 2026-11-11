// API layer — domain-meaningful server-facing operations.
// Selects endpoints, validates/maps request/response DTOs, translates server errors.
// Depends on transport (wire mechanics) and model/api (DTO mapping).
// Does NOT know about rendering, DOM, or components.

import { http } from "../transport/http.js";
import {
  mapDashboardResponse,
  mapLoginRequest,
  mapLoginResponse,
  mapBundleRequest,
  mapRechargeRequest,
  mapPatchUserRequest,
  mapPatchDeviceRequest,
  mapTopUpCommand,
  mapDailyPlanRequest,
  mapNetworkRequest,
  mapGuestRequest,
  mapMacListRequest,
  mapStaticLeaseRequest,
  mapFirewallRequest,
  mapDnsRuleRequest,
  mapDnsImportRequest,
} from "../model/api/index.js";

const ENDPOINTS = {
  me: "/api/me",
  login: "/api/login",
  logout: "/api/logout",
  dashboard: "/api/dashboard",
  bundle: "/api/bundle",
  resetMonth: "/api/reset-month",
  recharges: "/api/bundle/recharges",
  user: (id) => `/api/users/${id}`,
  topUpUser: (id) => `/api/users/${id}/topup`,
  topUpDevice: (id) => `/api/devices/${id}/topup`,
  kickUser: (id) => `/api/users/${id}?blacklist=false`,
  blacklistUser: (id) => `/api/users/${id}?blacklist=true`,
  approveGuest: (id) => `/api/guest/user/${id}/approve`,
  rejectGuest: (id) => `/api/guest/user/${id}/reject`,
  makeRegular: (id) => `/api/users/${id}/make-regular`,
  createUser: "/api/users",
  createDevice: "/api/devices",
  kickDevice: (id) => `/api/devices/${id}?blacklist=false`,
  blacklistDevice: (id) => `/api/devices/${id}?blacklist=true`,
  dnsUser: (id) => `/api/users/${id}/dns`,
  dnsDevice: (id) => `/api/devices/${id}/dns`,
  network: "/api/network",
  vpnShare: "/api/network/vpn-share",
  randomMacs: "/api/network/random-macs",
  staticLeases: "/api/network/static-leases",
  staticLease: (mac) => `/api/network/static-leases/${encodeURIComponent(mac)}`,
  macLists: "/api/mac-lists",
  guest: "/api/guest",
  wanConfig: "/api/wan",
  wanTest: "/api/wan/test",
  wanApply: "/api/wan",
  firewall: "/api/firewall",
  firewallRevert: "/api/firewall/revert",
  firewallBan: "/api/firewall/ban",
  firewallUnban: "/api/firewall/unban",
  firewallRemoveHttps: "/api/security/remove-https",
  firewallEnforceHttps: "/api/security/enforce-https",
  dnsStatus: "/api/dns/status",
  dnsMaster: "/api/dns/master",
  dnsTier: "/api/dns/tier",
  dnsPreset: (id) => `/api/dns/presets/${id}`,
  dnsRules: "/api/dns/rules",
  dnsRule: (id) => `/api/dns/rules/${id}`,
  dnsImport: "/api/dns/import",
  dnsQuick: "/api/dns/rules/quick",
  dnsAckAttack: "/api/security/ack-attack",
  vpnLoadAll: "/api/vpn/load-all",
  vpnAddNode: "/api/vpn/nodes",
  vpnNode: (id) => `/api/vpn/nodes/${id}`,
  vpnConnect: (id) => `/api/vpn/connect/${id}`,
  vpnDisconnect: "/api/vpn/disconnect",
  vpnPing: (id) => `/api/vpn/ping/${id}`,
  vpnSetInsecure: "/api/vpn/settings/insecure",
  vpnRoute: (type, id) => `/api/vpn/route/${type}/${id}`,
  vpnRouteAll: "/api/vpn/route/all",
  vpnLogs: "/api/vpn/logs",
  vpnSettings: "/api/vpn/settings",
  password: "/api/password",
  totpEnroll: "/api/totp/enroll",
  totpEnable: "/api/totp/enable",
  totpDisable: "/api/totp/disable",
  totpGet: "/api/totp",
  updatesCheck: "/api/updates/check",
  updatesInstall: "/api/updates/install",
  updatesPrefs: "/api/updates",
  logs: "/api/logs?limit=600",
  setup: "/api/setup",
  setupComplete: "/api/setup/complete",
  dailyPlanPreview: (userId) => `/api/users/${userId}/daily-plan/preview`,
};

/**
 * Dashboard API
 */
export const dashboardApi = {
  getDashboard: async () => {
    const raw = await http.get(ENDPOINTS.dashboard);
    return mapDashboardResponse(raw);
  },
  login: async (password, code) => {
    const raw = await http.post(ENDPOINTS.login, mapLoginRequest({ password, code }));
    return mapLoginResponse(raw);
  },
  logout: async () => {
    await http.post(ENDPOINTS.logout);
  },
  me: async () => {
    return await http.get(ENDPOINTS.me);
  },
};

/**
 * Bundle API
 */
export const bundleApi = {
  updateBundle: async (totalGb, resetDay, periodType) => {
    await http.post(ENDPOINTS.bundle, mapBundleRequest({ total_gb: totalGb, reset_day: resetDay, period_type: periodType }));
  },
  addGb: async (gb) => {
    await http.post(ENDPOINTS.bundle, mapTopUpCommand(gb));
  },
  resetMonth: async () => {
    await http.post(ENDPOINTS.resetMonth);
  },
  createRecharge: async (gb, expiresAt, recurring, targetType, targetId, comment) => {
    await http.post(ENDPOINTS.recharges, mapRechargeRequest({ gb, expires_at: expiresAt, recurring, target_type: targetType, target_id: targetId, comment }));
  },
  deleteRecharge: async (rechargeId) => {
    await http.del(`${ENDPOINTS.recharges}/${rechargeId}`);
  },
  toggleDaily: async (enabled) => {
    await http.post(ENDPOINTS.dailyPlanPreview(0), { enabled }); // Uses daily endpoint
  },
  toggleFreeMode: async (enabled) => {
    await http.post("/api/free-mode", { enabled });
  },
};

/**
 * User API
 */
export const userApi = {
  patchUser: async (userId, fields) => {
    await http.patch(ENDPOINTS.user(userId), mapPatchUserRequest(fields));
  },
  blockUser: async (userId, blocked) => {
    await http.patch(ENDPOINTS.user(userId), { block: blocked });
  },
  topUpUser: async (userId, gb) => {
    await http.post(ENDPOINTS.topUpUser(userId), { extra_gb: gb });
  },
  kickUser: async (userId) => {
    await http.del(ENDPOINTS.kickUser(userId));
  },
  blacklistUser: async (userId) => {
    await http.del(ENDPOINTS.blacklistUser(userId));
  },
  approveGuest: async (userId) => {
    await http.post(ENDPOINTS.approveGuest(userId));
  },
  makeRegular: async (userId) => {
    await http.post(ENDPOINTS.makeRegular(userId));
  },
  rejectGuest: async (userId) => {
    await http.post(ENDPOINTS.rejectGuest(userId));
  },
  dnsUser: async (userId, dnsServer) => {
    await http.patch(ENDPOINTS.dnsUser(userId), { dns_server: dnsServer });
  },
  previewDailyPlan: async (userId, body) => {
    return await http.post(ENDPOINTS.dailyPlanPreview(userId), body);
  },
};

/**
 * Device API
 */
export const deviceApi = {
  patchDevice: async (deviceId, fields) => {
    await http.patch(`/api/devices/${deviceId}`, mapPatchDeviceRequest(fields));
  },
  blockDevice: async (deviceId, blocked) => {
    await http.patch(`/api/devices/${deviceId}`, { block: blocked });
  },
  topUpDevice: async (deviceId, gb) => {
    await http.post(ENDPOINTS.topUpDevice(deviceId), { extra_gb: gb });
  },
  kickDevice: async (deviceId) => {
    await http.del(ENDPOINTS.kickDevice(deviceId));
  },
  blacklistDevice: async (deviceId) => {
    await http.del(ENDPOINTS.blacklistDevice(deviceId));
  },
  dnsDevice: async (deviceId, dnsServer) => {
    await http.patch(ENDPOINTS.dnsDevice(deviceId), { dns_server: dnsServer });
  },
};

/**
 * Network API
 */
export const networkApi = {
  updateNetwork: async (config) => {
    await http.post(ENDPOINTS.network, mapNetworkRequest(config));
  },
  toggleVpnShare: async (enabled) => {
    await http.post(ENDPOINTS.vpnShare, { vpn_share: enabled });
  },
  setRandomMacs: async (decline, applyExisting) => {
    await http.post(ENDPOINTS.randomMacs, { decline_random_macs: decline, decline_random_macs_existing: applyExisting });
  },
  addStaticLease: async (mac, ip, ip6, hostname) => {
    await http.post(ENDPOINTS.staticLeases, mapStaticLeaseRequest({ mac, ip, ip6, hostname }));
  },
  deleteStaticLease: async (mac) => {
    await http.del(ENDPOINTS.staticLease(mac));
  },
  addToMacList: async (list, mac) => {
    const body = list === "allow" ? { allow: [mac], deny: [] } : { allow: [], deny: [mac] };
    await http.post(ENDPOINTS.macLists, body);
  },
  removeFromMacList: async (list, mac) => {
    const body = list === "allow" ? { allow: [], deny: [mac] } : { allow: [mac], deny: [] };
    await http.post(ENDPOINTS.macLists, body);
  },
  updateGuest: async (config) => {
    await http.post(ENDPOINTS.guest, mapGuestRequest(config));
  },
  getNetwork: async () => {
    return await http.get(ENDPOINTS.network);
  },
  getStaticLeases: async () => {
    return await http.get(ENDPOINTS.staticLeases);
  },
  getMacLists: async () => {
    return await http.get(ENDPOINTS.macLists);
  },
  getGuest: async () => {
    return await http.get(ENDPOINTS.guest);
  },
  getWanConfig: async () => {
    return await http.get(ENDPOINTS.wanConfig);
  },
  testWan: async (user, pass) => {
    return await http.post(ENDPOINTS.wanTest, { pppoe_user: user, pppoe_password: pass });
  },
  applyWan: async (config) => {
    await http.post(ENDPOINTS.wanApply, config);
  },
};

/**
 * Security API
 */
export const securityApi = {
  updateFirewall: async (config) => {
    await http.post(ENDPOINTS.firewall, mapFirewallRequest(config));
  },
  revertFirewall: async () => {
    await http.post(ENDPOINTS.firewallRevert);
  },
  banIp: async (ip, seconds, reason) => {
    await http.post(ENDPOINTS.firewallBan, { ip, seconds, reason });
  },
  unbanIp: async (ip) => {
    await http.post(ENDPOINTS.firewallUnban, { ip });
  },
  removeHttps: async () => {
    await http.post(ENDPOINTS.firewallRemoveHttps);
  },
  enforceHttps: async () => {
    await http.post(ENDPOINTS.firewallEnforceHttps);
  },
};

/**
 * DNS API
 */
export const dnsApi = {
  getDnsStatus: async () => {
    return await http.get(ENDPOINTS.dnsStatus);
  },
  setDnsMaster: async (enabled) => {
    await http.post(ENDPOINTS.dnsMaster, { enabled });
  },
  setDnsTier: async (tierId) => {
    await http.post(ENDPOINTS.dnsTier, { tier: tierId });
  },
  toggleDnsPreset: async (presetId, enabled) => {
    if (enabled) {
      await http.post(`${ENDPOINTS.dnsPreset(presetId)}/enable`, { scope: "global" });
    } else {
      await http.post(`${ENDPOINTS.dnsPreset(presetId)}/disable`, { scope: "global" });
    }
  },
  addDnsRule: async (domain, action, targetIp, scope, scopeId) => {
    await http.post(ENDPOINTS.dnsRules, mapDnsRuleRequest({ domain, action, target_ip: targetIp, scope, scope_id: scopeId }));
  },
  deleteDnsRule: async (ruleId) => {
    await http.del(ENDPOINTS.dnsRule(ruleId));
  },
  importDnsRules: async (text, format, scope, scopeId, action) => {
    return await http.post(ENDPOINTS.dnsImport, mapDnsImportRequest({ text, format, scope, scope_id: scopeId, action }));
  },
  quickDnsRule: async (domain, action, scope, deviceId) => {
    await http.post(ENDPOINTS.dnsQuick, { domain, action, scope, device_id: scope === "device" ? deviceId : null });
  },
  ackAttack: async () => {
    await http.post(ENDPOINTS.dnsAckAttack);
  },
  getHistory: async (device, window, limit) => {
    return await http.get(`/api/history/${encodeURIComponent(device)}?window=${window}&limit=${limit}`);
  },
  getHistoryLive: async (deviceId) => {
    return await http.get(`/api/history/live?device_id=${encodeURIComponent(deviceId)}`);
  },
};

/**
 * VPN API
 */
export const vpnApi = {
  loadAll: async () => {
    await http.get(ENDPOINTS.vpnLoadAll);
  },
  addNode: async (name, link) => {
    await http.post(ENDPOINTS.vpnAddNode, { name, raw: link });
  },
  editNode: async (nodeId, fields) => {
    await http.post(ENDPOINTS.vpnNode(nodeId), fields);
  },
  deleteNode: async (nodeId) => {
    await http.del(ENDPOINTS.vpnNode(nodeId));
  },
  connect: async (nodeId) => {
    await http.post(ENDPOINTS.vpnConnect(nodeId));
  },
  disconnect: async () => {
    await http.post(ENDPOINTS.vpnDisconnect, { logout: false });
  },
  ping: async (nodeId) => {
    await http.post(ENDPOINTS.vpnPing(nodeId));
  },
  setInsecure: async (enabled) => {
    await http.post(ENDPOINTS.vpnSetInsecure, { allow_insecure: enabled });
  },
  route: async (type, id, viaVpn) => {
    await http.post(ENDPOINTS.vpnRoute(type, id), { route_vpn: viaVpn });
  },
  routeAll: async (viaVpn) => {
    await http.post(ENDPOINTS.vpnRouteAll, { route_vpn: viaVpn });
  },
  wanRenew: async () => {
    await http.post("/api/wan/renew");
  },
  wanRenewConfig: async (enabled, minutes) => {
    await http.post("/api/wan/renew-config", { enabled, minutes });
  },
  ipv6Probe: async () => {
    await http.post("/api/network/ipv6/probe");
  },
  tailscaleInstall: async () => {
    await http.post("/api/tailscale/install");
  },
  tailscaleConnect: async (logout) => {
    await http.post("/api/tailscale/connect", { logout });
  },
  tailscaleDisconnect: async (logout) => {
    await http.post("/api/tailscale/disconnect", { logout });
  },
  loadLogs: async () => {
    return await http.get(ENDPOINTS.vpnLogs);
  },
  loadSettings: async () => {
    return await http.get(ENDPOINTS.vpnSettings);
  },
  getIPv6Status: async () => {
    return await http.get("/api/network/ipv6/status");
  },
  getTailscale: async () => {
    return await http.get("/api/tailscale");
  },
};

/**
 * System API
 */
export const shareApi = {
  clear: async () => {
    await http.post("/api/share/clear");
  },
  deleteBatch: async (ids) => {
    await http.post("/api/share/delete-batch", { ids });
  },
  deleteFile: async (id) => {
    await http.del(`/api/share/files/${id}`);
  },
  deleteText: async (id) => {
    await http.del(`/api/share/texts/${id}`);
  },
  addText: async (text) => {
    await http.post("/api/share/text", { text });
  },
  initFolder: async (name, ttl) => {
    return await http.post("/api/share/folder/init", { folder_name: name, ttl });
  },
  finalizeFolder: async (folderId) => {
    await http.post("/api/share/folder/finalize", { folder_id: folderId });
  },
  list: async () => {
    return await http.get("/api/share/list");
  },
};

/**
 * System API
 */
export const systemApi = {
  changePassword: async (current, newPassword) => {
    await http.post(ENDPOINTS.password, { current, new: newPassword });
  },
  enrollTotp: async () => {
    return await http.post(ENDPOINTS.totpEnroll);
  },
  enableTotp: async (code) => {
    await http.post(ENDPOINTS.totpEnable, { code });
  },
  disableTotp: async () => {
    await http.post(ENDPOINTS.totpDisable);
  },
  checkUpdates: async () => {
    return await http.get(ENDPOINTS.updatesCheck);
  },
  installUpdates: async () => {
    return await http.post(ENDPOINTS.updatesInstall);
  },
  toggleAutoInstall: async (enabled) => {
    await http.post(ENDPOINTS.updatesPrefs, { auto_install: enabled });
  },
  fetchLogs: async () => {
    return await http.get(ENDPOINTS.logs);
  },
  getTotp: async () => {
    return await http.get(ENDPOINTS.totpGet);
  },
  getSetup: async () => {
    return await http.get(ENDPOINTS.setup);
  },
  completeSetup: async (body) => {
    return await http.post(ENDPOINTS.setupComplete, body);
  },
  exportLogs: () => {
    // Export handled in UI layer
  },
};
