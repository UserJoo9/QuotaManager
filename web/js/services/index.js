// Services layer — frontend business logic and workflows.
// Services own application decisions: validate intents, coordinate API operations,
// apply domain rules, and update state through narrow store contracts.
// Framework-agnostic: no components, hooks, contexts, DOM, or framework lifecycle APIs.
// Never import hooks or UI.

import {
  DashboardApiPort,
  BundleApiPort,
  UserApiPort,
  DeviceApiPort,
  NetworkApiPort,
  SecurityApiPort,
  DnsApiPort,
  VpnApiPort,
  SystemApiPort,
  DashboardStorePort,
  PrefsStorePort,
  ToastPort,
} from "../port/index.js";
import {
  createUser, createDevice, createBundle, createVpnStatus,
  isUserBlocked, isUserOnline, bundleUsagePercent,
} from "../model/domain/index.js";
import {
  createLoginCommand, createUpdateBundleCommand, createAddRechargeCommand,
  createPatchUserCommand, createPatchDeviceCommand, createTopUpCommand,
  createDailyPlanCommand, createNetworkConfigCommand, createVpnConnectCommand,
  createVpnRouteCommand, createFirewallRuleCommand, createDnsRuleCommand,
} from "../model/application/index.js";

// ---- Auth service ----
export function createAuthService(api, store, toast) {
  return {
    login: async (password, code) => {
      const cmd = createLoginCommand(password, code);
      const result = await api.dashboard.login(cmd.password, cmd.code);
      store.setAuthed(result.authed);
      toast.show(result.authed ? "Login successful" : "Login failed", result.authed ? "ok" : "bad");
      return result.authed;
    },
    logout: async () => {
      await api.dashboard.logout();
      store.setAuthed(false);
      store.setDashboard(null);
      toast.show("Logged out", "ok");
    },
    checkAuth: async () => {
      const result = await api.dashboard.me();
      store.setAuthed(result.authenticated);
      return result.authenticated;
    },
  };
}

// ---- Bundle/quota service ----
export function createBundleService(api, store, toast) {
  return {
    updateBundle: async (totalGb, resetDay, periodType) => {
      const cmd = createUpdateBundleCommand(totalGb, resetDay, periodType);
      await api.bundle.updateBundle(cmd.total_gb, cmd.reset_day, cmd.period_type);
      toast.show("Bundle saved", "ok");
    },
    addGb: async (gb) => {
      await api.bundle.addGb(gb);
      toast.show(`Added ${gb} GB`, "ok");
    },
    resetMonth: async () => {
      await api.bundle.resetMonth();
      toast.show("Month reset", "ok");
    },
    createRecharge: async (gb, expiresAt, recurring, targetType, targetId, comment) => {
      const cmd = createAddRechargeCommand(gb, expiresAt, recurring, targetType, targetId, comment);
      await api.bundle.createRecharge(cmd.gb, cmd.expires_at, cmd.recurring, cmd.target_type, cmd.target_id, cmd.comment);
      toast.show("Recharge created", "ok");
    },
    deleteRecharge: async (rechargeId) => {
      await api.bundle.deleteRecharge(rechargeId);
      toast.show("Recharge deleted", "ok");
    },
    toggleDaily: async (enabled) => {
      await api.bundle.toggleDaily(enabled);
      toast.show(enabled ? "Daily plan enabled" : "Daily plan disabled", "ok");
    },
    toggleFreeMode: async (enabled) => {
      await api.bundle.toggleFreeMode(enabled);
      toast.show(enabled ? "Free mode enabled" : "Free mode disabled", "ok");
    },
    getUsagePercent: () => {
      const state = store.getState();
      const bundle = state.dashboard ? state.dashboard.bundle : null;
      return bundle ? bundleUsagePercent(bundle) : 0;
    },
  };
}

// ---- User service ----
export function createUserService(api, store, toast) {
  return {
    patchUser: async (userId, fields) => {
      const cmd = createPatchUserCommand(userId, fields);
      await api.user.patchUser(cmd.id, {
        name: cmd.name,
        blocked: cmd.blocked,
        history_days: cmd.history_days,
        exempt_quota: cmd.exempt_quota,
        vpn_bypass: cmd.vpn_bypass,
        dns_server: cmd.dns_server,
        limit_down_mbps: cmd.limit_down_mbps,
        limit_up_mbps: cmd.limit_up_mbps,
        quota_mode: cmd.quota_mode,
        fixed_gb: cmd.fixed_gb,
        daily_self_edit: cmd.daily_self_edit,
      });
      toast.show("User saved", "ok");
    },
    blockUser: async (userId, blocked) => {
      await api.user.blockUser(userId, blocked);
      toast.show(blocked ? "User blocked" : "User unblocked", "ok");
    },
    topUpUser: async (userId, gb) => {
      await api.user.topUpUser(userId, gb);
      toast.show(`Topped up ${gb} GB`, "ok");
    },
    kickUser: async (userId) => {
      await api.user.kickUser(userId);
      toast.show("User kicked", "ok");
    },
    blacklistUser: async (userId) => {
      await api.user.blacklistUser(userId);
      toast.show("User blacklisted", "ok");
    },
    approveGuest: async (userId) => {
      await api.user.approveGuest(userId);
      toast.show("Guest approved", "ok");
    },
    makeRegular: async (userId) => {
      await api.user.makeRegular(userId);
      toast.show("Made regular user", "ok");
    },
    rejectGuest: async (userId) => {
      await api.user.rejectGuest(userId);
      toast.show("Guest rejected", "ok");
    },
    setDns: async (userId, dnsServer) => {
      await api.user.dnsUser(userId, dnsServer);
      toast.show("DNS updated", "ok");
    },
    isBlocked: (userId) => {
      const state = store.getState();
      const user = state.dashboard ? state.dashboard.users.find((u) => u.id === userId) : null;
      return user ? isUserBlocked(user) : false;
    },
    isOnline: (userId) => {
      const state = store.getState();
      const user = state.dashboard ? state.dashboard.users.find((u) => u.id === userId) : null;
      return user ? isUserOnline(user) : false;
    },
  };
}

// ---- Device service ----
export function createDeviceService(api, store, toast) {
  return {
    patchDevice: async (deviceId, fields) => {
      const cmd = createPatchDeviceCommand(deviceId, fields);
      await api.device.patchDevice(cmd.id, {
        name: cmd.name,
        user_id: cmd.user_id,
        blocked: cmd.blocked,
        limit_down_mbps: cmd.limit_down_mbps,
        limit_up_mbps: cmd.limit_up_mbps,
        dns_server: cmd.dns_server,
        daily_share_gb: cmd.daily_share_gb,
      });
      toast.show("Device saved", "ok");
    },
    blockDevice: async (deviceId, blocked) => {
      await api.device.blockDevice(deviceId, blocked);
      toast.show(blocked ? "Device blocked" : "Device unblocked", "ok");
    },
    topUpDevice: async (deviceId, gb) => {
      await api.device.topUpDevice(deviceId, gb);
      toast.show(`Topped up ${gb} GB`, "ok");
    },
    kickDevice: async (deviceId) => {
      await api.device.kickDevice(deviceId);
      toast.show("Device kicked", "ok");
    },
    blacklistDevice: async (deviceId) => {
      await api.device.blacklistDevice(deviceId);
      toast.show("Device blacklisted", "ok");
    },
    setDns: async (deviceId, dnsServer) => {
      await api.device.dnsDevice(deviceId, dnsServer);
      toast.show("DNS updated", "ok");
    },
  };
}

// ---- Network service ----
export function createNetworkService(api, store, toast) {
  return {
    updateNetwork: async (config) => {
      const cmd = createNetworkConfigCommand(config);
      await api.network.updateNetwork({
        enabled: cmd.enabled,
        total_down_mbps: cmd.total_down_mbps,
        total_up_mbps: cmd.total_up_mbps,
        lan_rate_mbps: cmd.lan_rate_mbps,
        vpn_pct: cmd.vpn_pct,
        vpn_down_mbps: cmd.vpn_down_mbps,
        vpn_up_mbps: cmd.vpn_up_mbps,
        decline_random_macs: cmd.decline_random_macs,
      });
      toast.show("Network saved", "ok");
    },
    toggleVpnShare: async (enabled) => {
      await api.network.toggleVpnShare(enabled);
      toast.show(enabled ? "VPN share enabled" : "VPN share disabled", "ok");
    },
    setRandomMacs: async (decline, applyExisting) => {
      await api.network.setRandomMacs(decline, applyExisting);
      toast.show("Random MAC policy updated", "ok");
    },
    addStaticLease: async (mac, ip, ip6, hostname) => {
      await api.network.addStaticLease(mac, ip, ip6, hostname);
      toast.show("Static lease added", "ok");
    },
    deleteStaticLease: async (mac) => {
      await api.network.deleteStaticLease(mac);
      toast.show("Static lease deleted", "ok");
    },
    addToMacList: async (list, mac) => {
      await api.network.addToMacList(list, mac);
      toast.show(`Added to ${list} list`, "ok");
    },
    removeFromMacList: async (list, mac) => {
      await api.network.removeFromMacList(list, mac);
      toast.show(`Removed from ${list} list`, "ok");
    },
    updateGuest: async (config) => {
      await api.network.updateGuest(config);
      toast.show("Guest config saved", "ok");
    },
    getNetwork: async () => {
      return await api.network.getNetwork();
    },
    getStaticLeases: async () => {
      return await api.network.getStaticLeases();
    },
    getMacLists: async () => {
      return await api.network.getMacLists();
    },
    getGuest: async () => {
      return await api.network.getGuest();
    },
    getWanConfig: async () => {
      return await api.network.getWanConfig();
    },
    testWan: async (user, pass) => {
      return await api.network.testWan(user, pass);
    },
    applyWan: async (config) => {
      await api.network.applyWan(config);
      toast.show("WAN config applied", "ok");
    },
  };
}

// ---- Security service ----
export function createSecurityService(api, store, toast) {
  return {
    updateFirewall: async (config) => {
      await api.security.updateFirewall(config);
      toast.show("Firewall applied", "ok");
    },
    revertFirewall: async () => {
      await api.security.revertFirewall();
      toast.show("Firewall reverted", "ok");
    },
    banIp: async (ip, seconds, reason) => {
      await api.security.banIp(ip, seconds, reason);
      toast.show(`IP ${ip} banned`, "ok");
    },
    unbanIp: async (ip) => {
      await api.security.unbanIp(ip);
      toast.show(`IP ${ip} unbanned`, "ok");
    },
    removeHttps: async () => {
      await api.security.removeHttps();
      toast.show("HTTPS removed", "ok");
    },
    enforceHttps: async () => {
      await api.security.enforceHttps();
      toast.show("HTTPS enforced", "ok");
    },
  };
}

// ---- DNS service ----
export function createDnsService(api, store, toast) {
  return {
    setDnsMaster: async (enabled) => {
      await api.dns.setDnsMaster(enabled);
      toast.show(enabled ? "DNS shield enabled" : "DNS shield disabled", "ok");
    },
    setDnsTier: async (tierId) => {
      await api.dns.setDnsTier(tierId);
      toast.show("DNS tier updated", "ok");
    },
    toggleDnsPreset: async (presetId, enabled) => {
      await api.dns.toggleDnsPreset(presetId, enabled);
      toast.show(`Preset ${presetId} ${enabled ? "enabled" : "disabled"}`, "ok");
    },
    addDnsRule: async (domain, action, targetIp, scope, scopeId) => {
      const cmd = createDnsRuleCommand(domain, action, targetIp, scope, scopeId);
      await api.dns.addDnsRule(cmd.domain, cmd.action, cmd.target_ip, cmd.scope, cmd.scope_id);
      toast.show("DNS rule added", "ok");
    },
    deleteDnsRule: async (ruleId) => {
      await api.dns.deleteDnsRule(ruleId);
      toast.show("DNS rule deleted", "ok");
    },
    importDnsRules: async (text, format, scope, scopeId, action) => {
      const result = await api.dns.importDnsRules(text, format, scope, scopeId, action);
      toast.show(`Imported ${result.created} rules`, "ok");
      return result;
    },
    quickDnsRule: async (domain, action, scope, deviceId) => {
      await api.dns.quickDnsRule(domain, action, scope, deviceId);
      toast.show(`Domain ${action}ed`, "ok");
    },
    ackAttack: async () => {
      await api.dns.ackAttack();
      toast.show("Attack acknowledged", "ok");
    },
    getHistory: async (device, window, limit) => {
      return await api.dns.getHistory(device, window, limit);
    },
    getHistoryLive: async (deviceId) => {
      return await api.dns.getHistoryLive(deviceId);
    },
  };
}

// ---- VPN service ----
export function createVpnService(api, store, toast) {
  return {
    loadAll: async () => {
      await api.vpn.loadAll();
    },
    addNode: async (name, link) => {
      await api.vpn.addNode(name, link);
      toast.show("VPN node added", "ok");
    },
    editNode: async (nodeId, fields) => {
      await api.vpn.editNode(nodeId, fields);
      toast.show("VPN node saved", "ok");
    },
    deleteNode: async (nodeId) => {
      await api.vpn.deleteNode(nodeId);
      toast.show("VPN node deleted", "ok");
    },
    connect: async (nodeId) => {
      const cmd = createVpnConnectCommand(nodeId);
      await api.vpn.connect(cmd.nodeId);
      toast.show("VPN connecting...", "ok");
    },
    disconnect: async () => {
      await api.vpn.disconnect();
      toast.show("VPN disconnected", "ok");
    },
    ping: async (nodeId) => {
      await api.vpn.ping(nodeId);
      toast.show("Ping sent", "ok");
    },
    setInsecure: async (enabled) => {
      await api.vpn.setInsecure(enabled);
      toast.show(enabled ? "Insecure mode enabled" : "Insecure mode disabled", "ok");
    },
    route: async (type, id, viaVpn) => {
      const cmd = createVpnRouteCommand(type, id, viaVpn);
      await api.vpn.route(cmd.type, cmd.id, cmd.route_vpn);
      toast.show(`Routed via ${viaVpn ? "VPN" : "direct"}`, "ok");
    },
    routeAll: async (viaVpn) => {
      await api.vpn.routeAll(viaVpn);
      toast.show(`All routed via ${viaVpn ? "VPN" : "direct"}`, "ok");
    },
    wanRenew: async () => {
      await api.vpn.wanRenew();
      toast.show("WAN renewed", "ok");
    },
    wanRenewConfig: async (enabled, minutes) => {
      await api.vpn.wanRenewConfig(enabled, minutes);
      toast.show("WAN renew config saved", "ok");
    },
    ipv6Probe: async () => {
      await api.vpn.ipv6Probe();
      toast.show("IPv6 probed", "ok");
    },
    tailscaleInstall: async () => {
      await api.vpn.tailscaleInstall();
      toast.show("Tailscale installing...", "ok");
    },
    tailscaleConnect: async (logout) => {
      await api.vpn.tailscaleConnect(logout);
      toast.show(logout ? "Tailscale logging out" : "Tailscale connecting...", "ok");
    },
    tailscaleDisconnect: async (logout) => {
      await api.vpn.tailscaleDisconnect(logout);
      toast.show("Tailscale disconnected", "ok");
    },
    loadLogs: async () => {
      return await api.vpn.loadLogs();
    },
    loadSettings: async () => {
      return await api.vpn.loadSettings();
    },
    getIPv6Status: async () => {
      return await api.vpn.getIPv6Status();
    },
    getTailscale: async () => {
      return await api.vpn.getTailscale();
    },
  };
}

// ---- System service ----
export function createSystemService(api, store, toast) {
  return {
    changePassword: async (current, newPassword) => {
      await api.system.changePassword(current, newPassword);
      toast.show("Password changed", "ok");
    },
    enrollTotp: async () => {
      return await api.system.enrollTotp();
    },
    enableTotp: async (code) => {
      await api.system.enableTotp(code);
      toast.show("TOTP enabled", "ok");
    },
    disableTotp: async () => {
      await api.system.disableTotp();
      toast.show("TOTP disabled", "ok");
    },
    checkUpdates: async () => {
      return await api.system.checkUpdates();
    },
    installUpdates: async () => {
      await api.system.installUpdates();
      toast.show("Updates installed", "ok");
    },
    toggleAutoInstall: async (enabled) => {
      await api.system.toggleAutoInstall(enabled);
      toast.show(enabled ? "Auto-install enabled" : "Auto-install disabled", "ok");
    },
    fetchLogs: async () => {
      return await api.system.fetchLogs();
    },
    getTotp: async () => {
      return await api.system.getTotp();
    },
    getSetup: async () => {
      return await api.system.getSetup();
    },
    completeSetup: async (body) => {
      return await api.system.completeSetup(body);
    },
    exportLogs: () => {
      // Export handled in UI layer
    },
  };
}

// ---- Daily plan service ----
export function createDailyPlanService(api, store, toast) {
  return {
    saveDailyPlan: async (userId, mode, gb, carry, split, heavy) => {
      const cmd = createDailyPlanCommand(userId, mode, gb, carry, split, heavy);
      await api.user.patchUser(userId, {
        daily_mode: cmd.mode,
        daily_gb: cmd.gb,
        daily_carry: cmd.carry,
        daily_split: cmd.split,
        daily_heavy: cmd.heavy,
      });
      toast.show("Daily plan saved", "ok");
    },
    previewDailyPlan: async (userId, body) => {
      return await api.user.previewDailyPlan(userId, body);
    },
  };
}

// ---- Share service ----
export function createShareService(api, store, toast) {
  return {
    clear: async () => {
      await api.share.clear();
      toast.show("Share cleared", "ok");
    },
    deleteBatch: async (ids) => {
      await api.share.deleteBatch(ids);
      toast.show("Files deleted", "ok");
    },
    deleteFile: async (id) => {
      await api.share.deleteFile(id);
      toast.show("File deleted", "ok");
    },
    deleteText: async (id) => {
      await api.share.deleteText(id);
      toast.show("Text deleted", "ok");
    },
    addText: async (text) => {
      await api.share.addText(text);
      toast.show("Text added", "ok");
    },
    initFolder: async (name, ttl) => {
      return await api.share.initFolder(name, ttl);
    },
    finalizeFolder: async (folderId) => {
      await api.share.finalizeFolder(folderId);
    },
    list: async () => {
      return await api.share.list();
    },
  };
}
