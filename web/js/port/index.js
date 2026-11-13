// Port layer — behavioral contracts (inbound/outbound capabilities).
// Services depend on these contracts, not on concrete implementations.
// Vanilla JS — no type annotations; contracts documented via JSDoc.

/**
 * Inbound port: Dashboard API operations.
 * Exposed by the API layer; consumed by services.
 */
export const DashboardApiPort = {
  /** Fetch the full dashboard snapshot. @returns {Promise<object>} */
  getDashboard: null,
  /** Authenticate with password and optional TOTP code. @param {string} password @param {string|null} code @returns {Promise<{authed:boolean, totp:boolean|null}>} */
  login: null,
  /** Log out the current session. @returns {Promise<void>} */
  logout: null,
  /** Check authentication status. @returns {Promise<{authenticated:boolean}>} */
  me: null,
};
/**
 * Inbound port: Bundle/quota operations.
 */
export const BundleApiPort = {
  /** Update the bundle configuration. @param {number} totalGb @param {number} resetDay @param {string} periodType @returns {Promise<void>} */
  updateBundle: null,
  /** Add GB to the bundle. @param {number} gb @returns {Promise<void>} */
  addGb: null,
  /** Reset the month. @returns {Promise<void>} */
  resetMonth: null,
  /** Create a recharge pack. @returns {Promise<void>} */
  createRecharge: null,
  /** Delete a recharge pack. @param {number} rechargeId @returns {Promise<void>} */
  deleteRecharge: null,
  /** Toggle daily plan master switch. @param {boolean} enabled @returns {Promise<void>} */
  toggleDaily: null,
  /** Toggle free mode. @param {boolean} enabled @returns {Promise<void>} */
  toggleFreeMode: null,
};

/**
 * Inbound port: User operations.
 */
export const UserApiPort = {
  /** Patch a user's settings. @param {number} userId @param {object} fields @returns {Promise<void>} */
  patchUser: null,
  /** Top up a user's quota. @param {number} userId @param {number} gb @returns {Promise<void>} */
  topUpUser: null,
  /** Kick a user (disconnect all devices). @param {number} userId @returns {Promise<void>} */
  kickUser: null,
  /** Blacklist a user. @param {number} userId @returns {Promise<void>} */
  blacklistUser: null,
  /** Approve a guest user. @param {number} userId @returns {Promise<void>} */
  approveGuest: null,
  /** Make a guest a regular user. @param {number} userId @returns {Promise<void>} */
  makeRegular: null,
  /** Reject a guest user. @param {number} userId @returns {Promise<void>} */
  rejectGuest: null,
};

/**
 * Inbound port: Device operations.
 */
export const DeviceApiPort = {
  /** Patch a device's settings. @param {number} deviceId @param {object} fields @returns {Promise<void>} */
  patchDevice: null,
  /** Top up a device's quota share. @param {number} deviceId @param {number} gb @returns {Promise<void>} */
  topUpDevice: null,
  /** Block/unblock a device. @param {number} deviceId @param {boolean} blocked @returns {Promise<void>} */
  blockDevice: null,
  /** Kick a device. @param {number} deviceId @returns {Promise<void>} */
  kickDevice: null,
  /** Blacklist a device. @param {number} deviceId @returns {Promise<void>} */
  blacklistDevice: null,
};

/**
 * Inbound port: Network operations.
 */
export const NetworkApiPort = {
  /** Update network shaping config. @param {object} config @returns {Promise<void>} */
  updateNetwork: null,
  /** Toggle VPN sharing. @param {boolean} enabled @returns {Promise<void>} */
  toggleVpnShare: null,
  /** Set random MAC decline policy. @param {boolean} decline @param {boolean} applyExisting @returns {Promise<void>} */
  setRandomMacs: null,
  /** Add a static lease. @param {string} mac @param {string} ip @param {string|null} ip6 @param {string|null} hostname @returns {Promise<void>} */
  addStaticLease: null,
  /** Delete a static lease. @param {string} mac @returns {Promise<void>} */
  deleteStaticLease: null,
  /** Add to MAC allow/deny list. @param {string} list @param {string} mac @returns {Promise<void>} */
  addToMacList: null,
  /** Remove from MAC allow/deny list. @param {string} list @param {string} mac @returns {Promise<void>} */
  removeFromMacList: null,
  /** Update guest config. @param {object} config @returns {Promise<void>} */
  updateGuest: null,
};

/**
 * Inbound port: Security operations.
 */
export const SecurityApiPort = {
  /** Update firewall config. @param {object} config @returns {Promise<void>} */
  updateFirewall: null,
  /** Revert firewall to last good. @returns {Promise<void>} */
  revertFirewall: null,
  /** Ban an IP. @param {string} ip @param {number} seconds @param {string} reason @returns {Promise<void>} */
  banIp: null,
  /** Unban an IP. @param {string} ip @returns {Promise<void>} */
  unbanIp: null,
  /** Remove HTTPS enforcement. @returns {Promise<void>} */
  removeHttps: null,
  /** Enforce HTTPS. @returns {Promise<void>} */
  enforceHttps: null,
};

/**
 * Inbound port: DNS operations.
 */
export const DnsApiPort = {
  /** Get DNS status. @returns {Promise<object>} */
  getDnsStatus: null,
  /** Update DNS master switch. @param {boolean} enabled @returns {Promise<void>} */
  setDnsMaster: null,
  /** Update DNS tier. @param {string} tierId @returns {Promise<void>} */
  setDnsTier: null,
  /** Update a DNS preset. @param {string} presetId @param {boolean} enabled @returns {Promise<void>} */
  toggleDnsPreset: null,
  /** Add a DNS rule. @returns {Promise<void>} */
  addDnsRule: null,
  /** Delete a DNS rule. @param {number} ruleId @returns {Promise<void>} */
  deleteDnsRule: null,
  /** Import DNS rules from text. @returns {Promise<object>} */
  importDnsRules: null,
  /** Quick block/allow a domain. @returns {Promise<void>} */
  quickDnsRule: null,
  /** Acknowledge attack alert. @returns {Promise<void>} */
  ackAttack: null,
};

/**
 * Inbound port: VPN operations.
 */
export const VpnApiPort = {
  /** Load all VPN data. @returns {Promise<void>} */
  loadAll: null,
  /** Add a VPN node. @param {string} name @param {string} link @returns {Promise<void>} */
  addNode: null,
  /** Edit a VPN node. @param {number} nodeId @param {object} fields @returns {Promise<void>} */
  editNode: null,
  /** Delete a VPN node. @param {number} nodeId @returns {Promise<void>} */
  deleteNode: null,
  /** Connect to a node. @param {number} nodeId @returns {Promise<void>} */
  connect: null,
  /** Disconnect VPN. @returns {Promise<void>} */
  disconnect: null,
  /** Ping a node. @param {number} nodeId @returns {Promise<void>} */
  ping: null,
  /** Set insecure mode. @param {boolean} enabled @returns {Promise<void>} */
  setInsecure: null,
  /** Route a user/device via VPN or direct. @param {string} type @param {number} id @param {boolean} viaVpn @returns {Promise<void>} */
  route: null,
  /** Route all via VPN or direct. @param {boolean} viaVpn @returns {Promise<void>} */
  routeAll: null,
  /** Renew WAN connection. @returns {Promise<void>} */
  wanRenew: null,
  /** Save WAN renew config. @param {boolean} enabled @param {number} minutes @returns {Promise<void>} */
  wanRenewConfig: null,
  /** Probe IPv6. @returns {Promise<void>} */
  ipv6Probe: null,
  /** Install Tailscale. @returns {Promise<void>} */
  tailscaleInstall: null,
  /** Connect Tailscale. @param {boolean} logout @returns {Promise<void>} */
  tailscaleConnect: null,
  /** Disconnect Tailscale. @param {boolean} logout @returns {Promise<void>} */
  tailscaleDisconnect: null,
  /** Load VPN logs. @returns {Promise<void>} */
  loadLogs: null,
  /** Load VPN settings. @returns {Promise<void>} */
  loadSettings: null,
};

/**
 * Inbound port: System operations.
 */
export const SystemApiPort = {
  /** Change password. @param {string} current @param {string} newPassword @returns {Promise<void>} */
  changePassword: null,
  /** Enroll TOTP. @returns {Promise<object>} */
  enrollTotp: null,
  /** Enable TOTP with code. @param {string} code @returns {Promise<void>} */
  enableTotp: null,
  /** Disable TOTP. @returns {Promise<void>} */
  disableTotp: null,
  /** Check for updates. @returns {Promise<object>} */
  checkUpdates: null,
  /** Install updates. @returns {Promise<void>} */
  installUpdates: null,
  /** Toggle auto-install. @param {boolean} enabled @returns {Promise<void>} */
  toggleAutoInstall: null,
  /** Fetch logs. @returns {Promise<object>} */
  fetchLogs: null,
  /** Export logs. @returns {void} */
  exportLogs: null,
};

/**
 * Inbound port: Share operations.
 */
export const ShareApiPort = {
  /** Clear all shares. @returns {Promise<void>} */
  clear: null,
  /** Delete batch of files. @param {number[]} ids @returns {Promise<void>} */
  deleteBatch: null,
  /** Delete a shared file. @param {number} id @returns {Promise<void>} */
  deleteFile: null,
  /** Delete a shared text. @param {number} id @returns {Promise<void>} */
  deleteText: null,
  /** Add a shared text. @param {string} text @returns {Promise<void>} */
  addText: null,
  /** Init a folder upload. @param {string} name @param {number} ttl @returns {Promise<{folder_id:number}>} */
  initFolder: null,
  /** Finalize a folder upload. @param {number} folderId @returns {Promise<void>} */
  finalizeFolder: null,
  /** List all shares. @returns {Promise<object>} */
  list: null,
  /** Get network config. @returns {Promise<object>} */
  getNetwork: null,
  /** Get static leases. @returns {Promise<object[]>} */
  getStaticLeases: null,
  /** Get MAC lists. @returns {Promise<object>} */
  getMacLists: null,
  /** Get guest config. @returns {Promise<object>} */
  getGuest: null,
};

/**
 * Outbound port: Store contract for services to read/write state.
 * Services depend on this, not on a concrete store implementation.
 */
export const DashboardStorePort = {
  getState: null,
  setDashboard: null,
  setAuthed: null,
  setLive: null,
  subscribe: null,
};

/**
 * Outbound port: Preference contract.
 */
export const PrefsStorePort = {
  getPrefs: null,
  setPref: null,
  applyPrefs: null,
};

/**
 * Outbound port: Toast notification contract.
 */
export const ToastPort = {
  show: null,
};
