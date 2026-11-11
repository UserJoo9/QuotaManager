// Application models — commands, queries, use-case results, and read models.
// These model the inputs and outputs of application operations, not wire format.

/**
 * @typedef {Object} LoginCommand
 * @property {string} password
 * @property {string|null} code
 */

/**
 * @typedef {Object} LoginResult
 * @property {boolean} authed
 * @property {boolean|null} totp
 */

/**
 * @typedef {Object} UpdateBundleCommand
 * @property {number} total_gb
 * @property {number} reset_day
 * @property {string} period_type
 */

/**
 * @typedef {Object} AddRechargeCommand
 * @property {number} gb
 * @property {number} expires_at
 * @property {boolean} recurring
 * @property {string} target_type
 * @property {number|null} target_id
 * @property {string} comment
 */

/**
 * @typedef {Object} PatchUserCommand
 * @property {number} id
 * @property {string|null} name
 * @property {boolean|null} blocked
 * @property {number|null} history_days
 * @property {boolean|null} exempt_quota
 * @property {boolean|null} vpn_bypass
 * @property {string|null} dns_server
 * @property {number|null} limit_down_mbps
 * @property {number|null} limit_up_mbps
 * @property {string|null} quota_mode
 * @property {number|null} fixed_gb
 * @property {boolean|null} daily_self_edit
 */

/**
 * @typedef {Object} PatchDeviceCommand
 * @property {number} id
 * @property {string|null} name
 * @property {number|null} user_id
 * @property {boolean|null} blocked
 * @property {number|null} limit_down_mbps
 * @property {number|null} limit_up_mbps
 * @property {string|null} dns_server
 * @property {number|null} daily_share_gb
 */

/**
 * @typedef {Object} TopUpCommand
 * @property {number} id
 * @property {boolean} isUser
 * @property {number} gb
 */

/**
 * @typedef {Object} DailyPlanCommand
 * @property {number} userId
 * @property {string} mode
 * @property {number} gb
 * @property {string} carry
 * @property {string} split
 * @property {Object<string, number>} heavy
 */

/**
 * @typedef {Object} NetworkConfigCommand
 * @property {boolean} enabled
 * @property {number} total_down_mbps
 * @property {number} total_up_mbps
 * @property {number} lan_rate_mbps
 * @property {number} vpn_pct
 * @property {number} vpn_down_mbps
 * @property {number} vpn_up_mbps
 * @property {boolean} decline_random_macs
 */

/**
 * @typedef {Object} VpnConnectCommand
 * @property {number} nodeId
 */

/**
 * @typedef {Object} VpnRouteCommand
 * @property {string} type
 * @property {number} id
 * @property {boolean} route_vpn
 */

/**
 * @typedef {Object} FirewallRuleCommand
 * @property {string} name
 * @property {string} protocol
 * @property {number} source_port
 * @property {number} target_port
 * @property {string} target_ip
 */

/**
 * @typedef {Object} DnsRuleCommand
 * @property {string} domain
 * @property {string} action
 * @property {string|null} target_ip
 * @property {string} scope
 * @property {number|null} scope_id
 */

/**
 * @typedef {Object} DashboardView
 * @property {User[]} users
 * @property {Bundle} bundle
 * @property {LiveSnapshot} live
 * @property {SecurityStatus} security
 */

export function createLoginCommand(password, code) {
  return { password, code: code || null };
}

export function createUpdateBundleCommand(total_gb, reset_day, period_type) {
  return { total_gb, reset_day, period_type };
}

export function createAddRechargeCommand(gb, expires_at, recurring, target_type, target_id, comment) {
  return { gb, expires_at, recurring, target_type, target_id, comment };
}

export function createPatchUserCommand(id, fields) {
  return { id, ...fields };
}

export function createPatchDeviceCommand(id, fields) {
  return { id, ...fields };
}

export function createTopUpCommand(id, isUser, gb) {
  return { id, isUser, gb };
}

export function createDailyPlanCommand(userId, mode, gb, carry, split, heavy) {
  return { userId, mode, gb, carry, split, heavy };
}

export function createNetworkConfigCommand(cfg) {
  return {
    enabled: cfg.enabled,
    total_down_mbps: cfg.total_down_mbps,
    total_up_mbps: cfg.total_up_mbps,
    lan_rate_mbps: cfg.lan_rate_mbps,
    vpn_pct: cfg.vpn_pct,
    vpn_down_mbps: cfg.vpn_down_mbps,
    vpn_up_mbps: cfg.vpn_up_mbps,
    decline_random_macs: cfg.decline_random_macs,
  };
}

export function createVpnConnectCommand(nodeId) {
  return { nodeId };
}

export function createVpnRouteCommand(type, id, routeVpn) {
  return { type, id, route_vpn: routeVpn };
}

export function createFirewallRuleCommand(name, protocol, sourcePort, targetPort, targetIp) {
  return { name, protocol, source_port: sourcePort, target_port: targetPort, target_ip: targetIp };
}

export function createDnsRuleCommand(domain, action, targetIp, scope, scopeId) {
  return { domain, action, target_ip: targetIp, scope, scope_id: scopeId };
}
