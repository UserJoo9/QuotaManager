// API models — request/response DTOs at the process boundary.
// These mirror the server's JSON contract and are translated to/from domain models.

/**
 * @typedef {Object} ApiDashboardResponse
 * @property {Object[]} users
 * @property {Object} bundle
 * @property {Object|null} vpn
 * @property {Object|null} vpn_share
 * @property {boolean} gateway_blocked_desired
 * @property {boolean} gateway_blocked_programmed
 * @property {boolean} unmanaged_block
 * @property {Object[]} rogue
 * @property {Object|null} security
 * @property {number} version
 */

/**
 * @typedef {Object} ApiLoginRequest
 * @property {string} password
 * @property {string|null} code
 */

/**
 * @typedef {Object} ApiLoginResponse
 * @property {boolean} ok
 * @property {boolean} authed
 * @property {boolean|null} totp
 */

/**
 * @typedef {Object} ApiBundleRequest
 * @property {number} total_gb
 * @property {number} reset_day
 * @property {string} period_type
 */

/**
 * @typedef {Object} ApiRechargeRequest
 * @property {number} gb
 * @property {number} expires_at
 * @property {boolean} recurring
 * @property {string} target_type
 * @property {number|null} target_id
 * @property {string} comment
 */

/**
 * @typedef {Object} ApiPatchUserRequest
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
 * @typedef {Object} ApiPatchDeviceRequest
 * @property {string|null} name
 * @property {number|null} user_id
 * @property {boolean|null} blocked
 * @property {number|null} limit_down_mbps
 * @property {number|null} limit_up_mbps
 * @property {string|null} dns_server
 * @property {number|null} daily_share_gb
 */

/**
 * @typedef {Object} ApiTopUpRequest
 * @property {number} add_gb
 */

/**
 * @typedef {Object} ApiDailyPlanRequest
 * @property {string} mode
 * @property {number} daily_gb
 * @property {string} carry
 * @property {string} split
 * @property {Object<string, number>} daily_heavy
 */

/**
 * @typedef {Object} ApiNetworkRequest
 * @property {boolean} enabled
 * @property {number} total_down_mbps
 * @property {number} total_up_mbps
 * @property {number} lan_rate_mbps
 * @property {number} vpn_pct
 * @property {number} vpn_down_mbps
 * @property {number} vpn_up_mbps
 * @property {boolean} decline_random_macs
 * @property {boolean} decline_random_macs_existing
 */

/**
 * @typedef {Object} ApiGuestRequest
 * @property {boolean} enabled
 * @property {boolean} stop_new
 * @property {number} quota_gb
 * @property {number} speed_limit_mbps
 * @property {number} limit
 */

/**
 * @typedef {Object} ApiMacListRequest
 * @property {string[]} allow
 * @property {string[]} deny
 */

/**
 * @typedef {Object} ApiStaticLeaseRequest
 * @property {string} mac
 * @property {string} ip
 * @property {string|null} ip6
 * @property {string|null} hostname
 */

/**
 * @typedef {Object} ApiFirewallRequest
 * @property {boolean} enabled
 * @property {string[]} services
 * @property {Object[]} port_forwards
 * @property {Object[]} rules
 * @property {string[]} allow_cidrs
 * @property {string[]} deny_cidrs
 * @property {string} dmz
 * @property {Object} syn_flood
 * @property {Object} brute_force
 * @property {Object} scan_detect
 * @property {boolean} geo_block
 * @property {boolean} wan_confirmed
 */

/**
 * @typedef {Object} ApiDnsRuleRequest
 * @property {string} domain
 * @property {string} action
 * @property {string|null} target_ip
 * @property {string} scope
 * @property {number|null} scope_id
 */

/**
 * @typedef {Object} ApiDnsImportRequest
 * @property {string} text
 * @property {string} format
 * @property {string} scope
 * @property {number|null} scope_id
 * @property {string} action
 */

/** @param {any} a @param {any} b @returns {any} - first non-null/undefined value */
function coalesce(a, b) { return a !== null && a !== undefined ? a : b; }

/** @param {Object|null|undefined} daily @returns {Object|null} */
function mapDailyPlan(daily) {
  if (!daily) return null;
  return {
    active: !!daily.active,
    mode: daily.mode || "off",
    gb: daily.gb || 0,
    carry: daily.carry || "next_day",
    split: daily.split || "open",
    self_edit: !!daily.self_edit,
    heavy: daily.heavy || {},
  };
}

/** @param {Object} d @returns {Object} */
function mapDevice(d) {
  return {
    id: d.id,
    name: d.name || "",
    mac: d.mac || "",
    ip: d.ip ?? null,
    ip6: d.ip6 ?? null,
    vendor: d.vendor ?? null,
    device_used_gb: d.device_used_gb || 0,
    lan_used_gb: d.lan_used_gb || 0,
    device_up_gb: d.device_up_gb || 0,
    device_down_gb: d.device_down_gb || 0,
    lan_up_gb: d.lan_up_gb || 0,
    lan_down_gb: d.lan_down_gb || 0,
    connected: !!d.connected,
    blocked: !!d.blocked,
    gateway: !!d.gateway,
    daily_share_gb: d.daily_share_gb ?? null,
    daily_cap_gb: d.daily_cap_gb ?? null,
    daily_used_gb: d.daily_used_gb ?? null,
  };
}

/** @param {Object} u @returns {Object} */
function mapUser(u) {
  return {
    id: u.id,
    name: u.name || "",
    used_gb: u.used_gb || 0,
    allowance_gb: u.allowance_gb || 0,
    percent: u.percent || 0,
    blocked: !!u.blocked,
    guest: !!u.guest,
    protected: !!u.protected,
    exempt_quota: !!u.exempt_quota,
    history_days: u.history_days ?? null,
    devices: (u.devices || []).map(mapDevice),
    daily: u.daily ? mapDailyPlan(u.daily) : null,
  };
}

/** @param {Object} b @returns {Object} */
function mapBundle(b) {
  return {
    base_total_gb: coalesce(b.base_total_gb, b.total_gb) || 0,
    total_gb: b.total_gb || 0,
    used_gb: b.used_gb || 0,
    remaining_gb: b.remaining_gb || 0,
    days_left: b.days_left ?? null,
    period_type: b.period_type || "renew_day",
    period_start: b.period_start ?? null,
    period_end: b.period_end ?? null,
    reset_day: b.reset_day || 0,
    recharges: (b.recharges || []).map((r) => ({
      id: r.id,
      target_type: r.target_type || "all",
      target_id: r.target_id || 0,
      gb: r.gb || 0,
      expires_at: r.expires_at || 0,
      recurring: !!r.recurring,
      comment: r.comment || "",
      created_at: r.created_at || "",
    })),
  };
}

/**
 * Map the server dashboard response into domain/read models.
 * @param {Object} raw - raw API dashboard response
 * @returns {Object} mapped dashboard model
 */
export function mapDashboardResponse(raw) {
  const b = raw.bundle || {};
  return {
    users: (raw.users || []).map(mapUser),
    bundle: mapBundle(b),
    vpn: raw.vpn ?? null,
    vpn_share: raw.vpn_share ?? null,
    gateway_blocked_desired: !!raw.gateway_blocked_desired,
    gateway_blocked_programmed: !!raw.gateway_blocked_programmed,
    unmanaged_block: raw.unmanaged_block !== false,
    rogue: raw.rogue || [],
    security: raw.security ?? null,
    version: raw.version ?? null,
  };
}

/** @param {LoginCommand} cmd @returns {ApiLoginRequest} */
export function mapLoginRequest(cmd) {
  return { password: cmd.password, code: cmd.code };
}

/** @param {Object} raw @returns {ApiLoginResponse} */
export function mapLoginResponse(raw) {
  return { authed: !!raw.authed, totp: raw.totp ?? null };
}

/** @param {UpdateBundleCommand} cmd @returns {ApiBundleRequest} */
export function mapBundleRequest(cmd) {
  return {
    total_gb: cmd.total_gb,
    reset_day: cmd.reset_day,
    period_type: cmd.period_type,
  };
}

/** @param {AddRechargeCommand} cmd @returns {ApiRechargeRequest} */
export function mapRechargeRequest(cmd) {
  return {
    gb: cmd.gb,
    expires_at: cmd.expires_at,
    recurring: cmd.recurring,
    target_type: cmd.target_type,
    target_id: cmd.target_id,
    comment: cmd.comment || "",
  };
}

/** @param {PatchUserCommand} cmd @returns {ApiPatchUserRequest} */
export function mapPatchUserRequest(cmd) {
  const req = {};
  if (cmd.name !== undefined) req.name = cmd.name;
  if (cmd.blocked !== undefined) req.blocked = cmd.blocked;
  if (cmd.history_days !== undefined) req.history_days = cmd.history_days;
  if (cmd.exempt_quota !== undefined) req.exempt_quota = cmd.exempt_quota;
  if (cmd.vpn_bypass !== undefined) req.vpn_bypass = cmd.vpn_bypass;
  if (cmd.dns_server !== undefined) req.dns_server = cmd.dns_server;
  if (cmd.limit_down_mbps !== undefined) req.limit_down_mbps = cmd.limit_down_mbps;
  if (cmd.limit_up_mbps !== undefined) req.limit_up_mbps = cmd.limit_up_mbps;
  if (cmd.quota_mode !== undefined) req.quota_mode = cmd.quota_mode;
  if (cmd.fixed_gb !== undefined) req.fixed_gb = cmd.fixed_gb;
  if (cmd.daily_self_edit !== undefined) req.daily_self_edit = cmd.daily_self_edit;
  return req;
}

/** @param {PatchDeviceCommand} cmd @returns {ApiPatchDeviceRequest} */
export function mapPatchDeviceRequest(cmd) {
  const req = {};
  if (cmd.name !== undefined) req.name = cmd.name;
  if (cmd.user_id !== undefined) req.user_id = cmd.user_id;
  if (cmd.blocked !== undefined) req.blocked = cmd.blocked;
  if (cmd.limit_down_mbps !== undefined) req.limit_down_mbps = cmd.limit_down_mbps;
  if (cmd.limit_up_mbps !== undefined) req.limit_up_mbps = cmd.limit_up_mbps;
  if (cmd.dns_server !== undefined) req.dns_server = cmd.dns_server;
  if (cmd.daily_share_gb !== undefined) req.daily_share_gb = cmd.daily_share_gb;
  return req;
}

/** @param {number} gb @returns {ApiTopUpRequest} */
export function mapTopUpCommand(gb) {
  return { add_gb: gb };
}

/** @param {DailyPlanCommand} cmd @returns {ApiDailyPlanRequest} */
export function mapDailyPlanRequest(cmd) {
  return {
    mode: cmd.mode,
    daily_gb: cmd.gb,
    carry: cmd.carry,
    split: cmd.split,
    daily_heavy: cmd.heavy,
  };
}

/** @param {NetworkConfigCommand} cmd @returns {ApiNetworkRequest} */
export function mapNetworkRequest(cmd) {
  return {
    enabled: cmd.enabled,
    total_down_mbps: cmd.total_down_mbps,
    total_up_mbps: cmd.total_up_mbps,
    lan_rate_mbps: cmd.lan_rate_mbps,
    vpn_pct: cmd.vpn_pct,
    vpn_down_mbps: cmd.vpn_down_mbps,
    vpn_up_mbps: cmd.vpn_up_mbps,
    decline_random_macs: cmd.decline_random_macs,
  };
}

/** @param {GuestConfig} cmd @returns {ApiGuestRequest} */
export function mapGuestRequest(cmd) {
  return {
    enabled: cmd.enabled,
    stop_new: cmd.stop_new,
    quota_gb: cmd.quota_gb,
    speed_limit_mbps: cmd.speed_limit_mbps,
    limit: cmd.limit,
  };
}

/** @param {string[]} allow @param {string[]} deny @returns {ApiMacListRequest} */
export function mapMacListRequest(allow, deny) {
  return { allow, deny };
}

/** @param {StaticLease} req @returns {ApiStaticLeaseRequest} */
export function mapStaticLeaseRequest(req) {
  return {
    mac: req.mac,
    ip: req.ip,
    ip6: req.ip6 ?? "",
    hostname: req.hostname ?? "",
  };
}

/** @param {Object} cmd @returns {ApiFirewallRequest} */
export function mapFirewallRequest(cmd) {
  return {
    enabled: cmd.enabled,
    services: cmd.services || [],
    port_forwards: cmd.port_forwards || [],
    rules: cmd.rules || [],
    allow_cidrs: cmd.allow_cidrs || [],
    deny_cidrs: cmd.deny_cidrs || [],
    dmz: cmd.dmz || "",
    syn_flood: cmd.syn_flood || { rate: 10, burst: 20 },
    brute_force: cmd.brute_force || { threshold: 10, ban_seconds: 1800 },
    scan_detect: cmd.scan_detect || { enabled: false, syn_threshold: 200, ban_seconds: 3600 },
    geo_block: !!cmd.geo_block,
    wan_confirmed: !!cmd.wan_confirmed,
  };
}

/** @param {DnsRuleCommand} cmd @returns {ApiDnsRuleRequest} */
export function mapDnsRuleRequest(cmd) {
  return {
    domain: cmd.domain,
    action: cmd.action,
    target_ip: cmd.target_ip !== null && cmd.target_ip !== undefined ? cmd.target_ip : null,
    scope: cmd.scope,
    scope_id: cmd.scope_id !== null && cmd.scope_id !== undefined ? cmd.scope_id : null,
  };
}

/** @param {Object} cmd @returns {ApiDnsImportRequest} */
export function mapDnsImportRequest(cmd) {
  return {
    text: cmd.text,
    format: cmd.format,
    scope: cmd.scope,
    scope_id: cmd.scope_id !== null && cmd.scope_id !== undefined ? cmd.scope_id : null,
    action: cmd.action,
  };
}
