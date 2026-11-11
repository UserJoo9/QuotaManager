// Hooks layer — presentation adapters (vanilla JS facade/controller pattern).
// The only UI-facing gateway to services and shared state.
// Obtains dependencies from context, subscribes to narrow store selectors,
// exposes render-ready data, and translates user intent into service calls.
// May bind framework lifecycle and reactivity, but must NOT own business rules,
// call raw API/transport code, duplicate store state, or become a general utility.

import {
  createAuthService,
  createBundleService,
  createUserService,
  createDeviceService,
  createNetworkService,
  createSecurityService,
  createDnsService,
  createVpnService,
  createSystemService,
  createDailyPlanService,
} from "../services/index.js";

/**
 * Creates all presentation hooks with dependencies injected at the composition root.
 *
 * @param {Object} deps - injected dependencies
 * @param {Object} deps.api - API layer: dashboard, bundle, user, device, network,
 *   security, dns, vpn, system
 * @param {Object} deps.store - dashboard store
 * @param {Object} deps.prefs - prefs store
 * @param {Object} deps.live - live store
 * @param {Object} deps.toast - toast port, { show(message, kind) }
 * @returns {Object} hook factories: useAuth, useBundle, useUsers, useDevices,
 *   useNetwork, useSecurity, useDns, useVpn, useSystem, useDailyPlan, useShare,
 *   usePrefs, useLive, useDashboard
 */
// ---- Hook factory ----
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
      isAuthed: store.getAuthed,
    }),

    // Bundle hooks
    useBundle: () => ({
      updateBundle: bundle.updateBundle,
      addGb: bundle.addGb,
      resetMonth: bundle.resetMonth,
      createRecharge: bundle.createRecharge,
      deleteRecharge: bundle.deleteRecharge,
      getUsagePercent: bundle.getUsagePercent,
      getBundle: () => store.getBundle(),
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
      getUsers: () => store.getUsers(),
      getUserById: (id) => store.getUserById(id),
    }),

    // Device hooks
    useDevices: () => ({
      patchDevice: devices.patchDevice,
      topUpDevice: devices.topUpDevice,
      blockDevice: devices.blockDevice,
      kickDevice: devices.kickDevice,
      blacklistDevice: devices.blacklistDevice,
      getDeviceById: (deviceId) => store.getDeviceById(deviceId),
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
      getWanConfig: network.getWanConfig,
      testWan: network.testWan,
      applyWan: network.applyWan,
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
      getTotp: system.getTotp,
      getSetup: system.getSetup,
      completeSetup: system.completeSetup,
      exportLogs: system.exportLogs,
    }),

    // Daily plan hooks
    useDailyPlan: () => ({
      saveDailyPlan: dailyPlan.saveDailyPlan,
      previewDailyPlan: dailyPlan.previewDailyPlan,
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
      getState: store.getState,
      getDashboard: () => store.getDashboard(),
      getUsers: () => store.getUsers(),
      subscribe: store.subscribe,
    }),
  };
}
