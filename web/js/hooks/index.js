// Hooks layer — presentation adapters (vanilla JS facade/controller pattern).
// The only UI-facing gateway to services and shared state.
// Obtains dependencies from context, subscribes to narrow store selectors,
// exposes render-ready data, and translates user intent into service calls.
// May bind framework lifecycle and reactivity, but must NOT own business rules,
// call raw API/transport code, duplicate store state, or become a general utility.

import { createAuthService } from "../services/index.js";
import { createBundleService } from "../services/index.js";
import { createUserService } from "../services/index.js";
import { createDeviceService } from "../services/index.js";
import { createNetworkService } from "../services/index.js";
import { createSecurityService } from "../services/index.js";
import { createDnsService } from "../services/index.js";
import { createVpnService } from "../services/index.js";
import { createSystemService } from "../services/index.js";
import { createDailyPlanService } from "../services/index.js";

// ---- Hook factory ----
// Creates all hooks with shared dependencies injected at the composition root.
export function createHooks({ api, store, prefs, live, toast }) {
  const auth = createAuthService(api, store, toast);
  const bundle = createBundleService(api, store, toast);
  const users = createUserService(api, store, toast);
  const devices = createDeviceService(api, store, toast);
  const network = createNetworkService(api, store, toast);
  const security = createSecurityService(api, store, toast);
  const dns = createDnsService(api, store, toast);
  const vpn = createVpnService(api, store, toast);
  const system = createSystemService(api, store, toast);
  const dailyPlan = createDailyPlanService(api, store, toast);

  return {
    // Auth hooks
    useAuth: () => ({
      login: auth.login,
      logout: auth.logout,
      checkAuth: auth.checkAuth,
      isAuthed: () => store.getState().authed,
    }),

    // Bundle hooks
    useBundle: () => ({
      updateBundle: bundle.updateBundle,
      addGb: bundle.addGb,
      resetMonth: bundle.resetMonth,
      createRecharge: bundle.createRecharge,
      deleteRecharge: bundle.deleteRecharge,
      getUsagePercent: bundle.getUsagePercent,
      getBundle: () => {
        const s = store.getState();
        return s.dashboard ? s.dashboard.bundle : null;
      },
    }),

    // User hooks
    useUsers: () => ({
      patchUser: users.patchUser,
      topUpUser: users.topUpUser,
      blockUser: users.blockUser,
      kickUser: users.kickUser,
      blacklistUser: users.blacklistUser,
      approveGuest: users.approveGuest,
      makeRegular: users.makeRegular,
      rejectGuest: users.rejectGuest,
      isBlocked: users.isBlocked,
      isOnline: users.isOnline,
      getUsers: () => {
        const s = store.getState();
        return s.dashboard ? s.dashboard.users : [];
      },
      getUserById: (id) => {
        const s = store.getState();
        if (!s.dashboard) return null;
        return s.dashboard.users.find((u) => u.id === id) || null;
      },
    }),

    // Device hooks
    useDevices: () => ({
      patchDevice: devices.patchDevice,
      topUpDevice: devices.topUpDevice,
      blockDevice: devices.blockDevice,
      kickDevice: devices.kickDevice,
      blacklistDevice: devices.blacklistDevice,
      getDeviceById: (deviceId) => {
        const s = store.getState();
        if (!s.dashboard) return null;
        for (const u of s.dashboard.users) {
          const d = u.devices.find((d) => d.id === deviceId);
          if (d) return { ...d, owner: u.name };
        }
        return null;
      },
    }),

    // Network hooks
    useNetwork: () => ({
      updateNetwork: network.updateNetwork,
      toggleVpnShare: network.toggleVpnShare,
      setRandomMacs: network.setRandomMacs,
      addStaticLease: network.addStaticLease,
      deleteStaticLease: network.deleteStaticLease,
      addToMacList: network.addToMacList,
      removeFromMacList: network.removeFromMacList,
      updateGuest: network.updateGuest,
      getNetwork: network.getNetwork,
      getStaticLeases: network.getStaticLeases,
      getMacLists: network.getMacLists,
      getGuest: network.getGuest,
    }),

    // Security hooks
    useSecurity: () => ({
      updateFirewall: security.updateFirewall,
      revertFirewall: security.revertFirewall,
      banIp: security.banIp,
      unbanIp: security.unbanIp,
      removeHttps: security.removeHttps,
      enforceHttps: security.enforceHttps,
    }),

    // DNS hooks
    useDns: () => ({
      setDnsMaster: dns.setDnsMaster,
      setDnsTier: dns.setDnsTier,
      toggleDnsPreset: dns.toggleDnsPreset,
      addDnsRule: dns.addDnsRule,
      deleteDnsRule: dns.deleteDnsRule,
      importDnsRules: dns.importDnsRules,
      quickDnsRule: dns.quickDnsRule,
      ackAttack: dns.ackAttack,
      getHistory: dns.getHistory,
      getHistoryLive: dns.getHistoryLive,
    }),

    // VPN hooks
    useVpn: () => ({
      loadAll: vpn.loadAll,
      addNode: vpn.addNode,
      editNode: vpn.editNode,
      deleteNode: vpn.deleteNode,
      connect: vpn.connect,
      disconnect: vpn.disconnect,
      ping: vpn.ping,
      setInsecure: vpn.setInsecure,
      route: vpn.route,
      routeAll: vpn.routeAll,
      wanRenew: vpn.wanRenew,
      wanRenewConfig: vpn.wanRenewConfig,
      ipv6Probe: vpn.ipv6Probe,
      tailscaleInstall: vpn.tailscaleInstall,
      tailscaleConnect: vpn.tailscaleConnect,
      tailscaleDisconnect: vpn.tailscaleDisconnect,
      loadLogs: vpn.loadLogs,
      loadSettings: vpn.loadSettings,
      getIPv6Status: vpn.getIPv6Status,
      getTailscale: vpn.getTailscale,
    }),

    // System hooks
    useSystem: () => ({
      changePassword: system.changePassword,
      enrollTotp: system.enrollTotp,
      enableTotp: system.enableTotp,
      disableTotp: system.disableTotp,
      checkUpdates: system.checkUpdates,
      installUpdates: system.installUpdates,
      toggleAutoInstall: system.toggleAutoInstall,
      fetchLogs: system.fetchLogs,
      exportLogs: system.exportLogs,
    }),

    // Daily plan hooks
    useDailyPlan: () => ({
      saveDailyPlan: dailyPlan.saveDailyPlan,
    }),

    // Share hooks
    useShare: () => ({
      clear: share.clear,
      deleteBatch: share.deleteBatch,
      deleteFile: share.deleteFile,
      deleteText: share.deleteText,
      addText: share.addText,
      initFolder: share.initFolder,
      finalizeFolder: share.finalizeFolder,
      list: share.list,
    }),

    // Preference hooks
    usePrefs: () => ({
      getPrefs: () => prefs.getState(),
      setPref: (key, value) => prefs.setPref(key, value),
      getTheme: () => prefs.getState().theme,
      getLang: () => prefs.getState().lang,
      getAccent: () => prefs.getState().accent,
      getTone: () => prefs.getState().tone,
      getParticles: () => prefs.getState().particles,
      getFx: () => prefs.getState().fx,
      getDensity: () => prefs.getState().density,
      getRail: () => prefs.getState().rail,
    }),

    // Live hooks
    useLive: () => ({
      isConnected: () => live.isConnected(),
      getReconnectAttempts: () => live.getReconnectAttempts(),
    }),

    // Dashboard data hooks
    useDashboard: () => ({
      getState: () => store.getState(),
      getDashboard: () => {
        const s = store.getState();
        return s.dashboard;
      },
      getUsers: () => {
        const s = store.getState();
        return s.dashboard ? s.dashboard.users : [];
      },
      subscribe: (fn) => store.subscribe(fn),
    }),
  };
}
