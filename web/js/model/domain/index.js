// Domain models — core business entities and value objects.
// Pure data definitions with no dependencies on API, persistence, or UI.

/**
 * @typedef {Object} User
 * @property {number} id
 * @property {string} name
 * @property {number} used_gb
 * @property {number} allowance_gb
 * @property {number} percent
 * @property {boolean} blocked
 * @property {boolean} guest
 * @property {boolean} protected
 * @property {boolean} exempt_quota
 * @property {number|null} history_days
 * @property {Device[]} devices
 * @property {DailyPlan|null} daily
 */

/**
 * @typedef {Object} Device
 * @property {number} id
 * @property {string} name
 * @property {string} mac
 * @property {string|null} ip
 * @property {string|null} ip6
 * @property {string|null} vendor
 * @property {number} device_used_gb
 * @property {number} lan_used_gb
 * @property {number} device_up_gb
 * @property {number} device_down_gb
 * @property {number} lan_up_gb
 * @property {number} lan_down_gb
 * @property {boolean} connected
 * @property {boolean} blocked
 * @property {boolean} gateway
 * @property {number|null} daily_share_gb
 * @property {number|null} daily_cap_gb
 * @property {number|null} daily_used_gb
 */

/**
 * @typedef {Object} Bundle
 * @property {number} base_total_gb
 * @property {number} total_gb
 * @property {number} used_gb
 * @property {number} remaining_gb
 * @property {number|null} days_left
 * @property {string} period_type
 * @property {string|null} period_start
 * @property {string|null} period_end
 * @property {number} reset_day
 * @property {Recharge[]} recharges
 */

/**
 * @typedef {Object} Recharge
 * @property {number} id
 * @property {string} target_type
 * @property {number} target_id
 * @property {number} gb
 * @property {number} expires_at
 * @property {boolean} recurring
 * @property {string} comment
 * @property {string} created_at
 */

/**
 * @typedef {Object} DailyPlan
 * @property {boolean} active
 * @property {string} mode
 * @property {number} gb
 * @property {string} carry
 * @property {string} split
 * @property {boolean} self_edit
 * @property {Object<string, number>} heavy
 */

/**
 * @typedef {Object} VpnNode
 * @property {number} id
 * @property {string} name
 * @property {string} protocol
 * @property {string} server
 * @property {number} server_port
 * @property {string} uuid
 * @property {string|null} sni
 * @property {string|null} flow
 * @property {string|null} pbk
 * @property {string|null} sid
 * @property {string} transport
 * @property {string|null} path
 */

/**
 * @typedef {Object} VpnStatus
 * @property {string} state
 * @property {number|null} active_node_id
 * @property {string|null} active_node_name
 * @property {string|null} protocol
 * @property {string|null} interface
 * @property {number} speed_down_bps
 * @property {number} speed_up_bps
 * @property {number} total_down_bytes
 * @property {number} total_up_bytes
 * @property {number} active_connections
 * @property {number} uptime_seconds
 * @property {string|null} last_error
 */

/**
 * @typedef {Object} VpnRoute
 * @property {number} id
 * @property {string} name
 * @property {string|null} mac
 * @property {boolean} route_vpn
 */

/**
 * @typedef {Object} NetworkConfig
 * @property {boolean} enabled
 * @property {number} total_down_mbps
 * @property {number} total_up_mbps
 * @property {number} lan_rate_mbps
 * @property {number} vpn_pct
 * @property {number} vpn_down_mbps
 * @property {number} vpn_up_mbps
 * @property {boolean} decline_random_macs
 * @property {Object|null} vpn_share
 */

/**
 * @typedef {Object} GuestConfig
 * @property {boolean} enabled
 * @property {boolean} stop_new
 * @property {number} quota_gb
 * @property {number} speed_limit_mbps
 * @property {number} limit
 */

/**
 * @typedef {Object} MacEntry
 * @property {string} mac
 * @property {string|null} name
 * @property {string|null} owner
 */

/**
 * @typedef {Object} StaticLease
 * @property {string} mac
 * @property {string} ip
 * @property {string|null} ip6
 * @property {string|null} hostname
 * @property {string|null} device_name
 */

/**
 * @typedef {Object} SecurityStatus
 * @property {boolean} enabled
 * @property {string} mode
 * @property {Object|null} tls
 * @property {number} failed_logins_1h
 * @property {number} waf_blocks_1h
 * @property {boolean} syn_flood_enabled
 * @property {boolean} brute_force_enabled
 * @property {boolean} scan_detect_enabled
 * @property {boolean} geo_block
 */

/**
 * @typedef {Object} DnsRule
 * @property {number} id
 * @property {string} domain
 * @property {string} action
 * @property {string|null} target_ip
 * @property {string|null} source
 * @property {string} scope
 * @property {number|null} scope_id
 */

/**
 * @typedef {Object} DnsPreset
 * @property {string} id
 * @property {boolean} enabled
 * @property {number} domain_count
 * @property {string} scope
 * @property {number|null} scope_id
 */

/**
 * @typedef {Object} LiveSnapshot
 * @property {User[]} users
 * @property {Bundle} bundle
 * @property {VpnStatus|null} vpn
 * @property {Object|null} vpn_share
 * @property {boolean} gateway_blocked_desired
 * @property {boolean} gateway_blocked_programmed
 * @property {boolean} unmanaged_block
 * @property {Object[]} rogue
 * @property {Object|null} security
 * @property {number} version
 */

/**
 * @typedef {Object} HistoryAnalytics
 * @property {number} total_queries
 * @property {Object[]} top_domains
 * @property {Object[]} top_apps
 * @property {Object[]} top_websites
 * @property {Object[]} timeline
 * @property {Object[]} activity
 * @property {Object[]} recent
 */

/**
 * @typedef {Object} LiveDevice
 * @property {number} id
 * @property {string} name
 * @property {string|null} mac
 * @property {string|null} ip
 * @property {string|null} user_name
 * @property {number} down_bps
 * @property {number} up_bps
 * @property {number} total_bps
 * @property {number} share_pct
 * @property {boolean} is_idle
 * @property {number} top_consumer_id
 */

export function createUser(partial) {
  return {
    id: partial.id ?? 0,
    name: partial.name ?? "",
    used_gb: partial.used_gb ?? 0,
    allowance_gb: partial.allowance_gb ?? 0,
    percent: partial.percent ?? 0,
    blocked: partial.blocked ?? false,
    guest: partial.guest ?? false,
    protected: partial.protected ?? false,
    exempt_quota: partial.exempt_quota ?? false,
    history_days: partial.history_days ?? null,
    devices: partial.devices ?? [],
    daily: partial.daily ?? null,
  };
}

export function createDevice(partial) {
  return {
    id: partial.id ?? 0,
    name: partial.name ?? "",
    mac: partial.mac ?? "",
    ip: partial.ip ?? null,
    ip6: partial.ip6 ?? null,
    vendor: partial.vendor ?? null,
    device_used_gb: partial.device_used_gb ?? 0,
    lan_used_gb: partial.lan_used_gb ?? 0,
    device_up_gb: partial.device_up_gb ?? 0,
    device_down_gb: partial.device_down_gb ?? 0,
    lan_up_gb: partial.lan_up_gb ?? 0,
    lan_down_gb: partial.lan_down_gb ?? 0,
    connected: partial.connected ?? false,
    blocked: partial.blocked ?? false,
    gateway: partial.gateway ?? false,
    daily_share_gb: partial.daily_share_gb ?? null,
    daily_cap_gb: partial.daily_cap_gb ?? null,
    daily_used_gb: partial.daily_used_gb ?? null,
  };
}

export function createBundle(partial) {
  return {
    base_total_gb: partial.base_total_gb ?? partial.total_gb ?? 0,
    total_gb: partial.total_gb ?? 0,
    used_gb: partial.used_gb ?? 0,
    remaining_gb: partial.remaining_gb ?? 0,
    days_left: partial.days_left ?? null,
    period_type: partial.period_type ?? "renew_day",
    period_start: partial.period_start ?? null,
    period_end: partial.period_end ?? null,
    reset_day: partial.reset_day ?? 0,
    recharges: partial.recharges ?? [],
  };
}

export function createVpnStatus(partial) {
  return {
    state: partial.state ?? "disconnected",
    active_node_id: partial.active_node_id ?? null,
    active_node_name: partial.active_node_name ?? null,
    protocol: partial.protocol ?? null,
    interface: partial.interface ?? null,
    speed_down_bps: partial.speed_down_bps ?? 0,
    speed_up_bps: partial.speed_up_bps ?? 0,
    total_down_bytes: partial.total_down_bytes ?? 0,
    total_up_bytes: partial.total_up_bytes ?? 0,
    active_connections: partial.active_connections ?? 0,
    uptime_seconds: partial.uptime_seconds ?? 0,
    last_error: partial.last_error ?? null,
  };
}

export function isUserBlocked(user) {
  return user.blocked === true;
}

export function isUserOnline(user) {
  return (user.devices || []).some((d) => d.connected === true);
}

export function bundleUsagePercent(bundle) {
  if (bundle.total_gb <= 0) return 0;
  return Math.round((bundle.used_gb / bundle.total_gb) * 100);
}

export function bundleRemainingPercent(bundle) {
  if (bundle.total_gb <= 0) return 100;
  return Math.round((bundle.remaining_gb / bundle.total_gb) * 100);
}
