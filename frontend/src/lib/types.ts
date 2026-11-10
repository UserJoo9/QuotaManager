// Shapes of the gateway API (only the fields the UI reads).
export interface Daily {
  mode: "off" | "fixed" | "spread"; gb: number; carry: "next_day" | "spread"; split: "open" | "equal" | "fixed";
  heavy: Record<string, number>; self_edit: boolean; active: boolean; cap_gb: number | null; used_gb: number | null;
  blocked: boolean; resets_at: string | null;
}
export interface Device {
  id: number; mac: string; name: string; vendor: string; gateway: boolean; user_id: number; user_name: string;
  ip: string; ip6: string; ipv6_direct: boolean; connected: boolean; guest: boolean; quota_mode: string; fixed_gb: number | null;
  bypass: boolean; vpn_bypass: boolean; limit_down_mbps: number; limit_up_mbps: number; dns_server: string;
  blocked: boolean; block_state: string; device_used_gb: number; device_up_gb: number; device_down_gb: number;
  lan_used_gb: number; lan_up_gb: number; lan_down_gb: number; daily_share_gb: number; daily_cap_gb?: number | null; daily_used_gb?: number | null;
}
export interface User {
  id: number; name: string; quota_mode: "auto" | "fixed"; fixed_gb: number | null; guest: boolean; protected: boolean;
  exempt_quota: boolean; vpn_bypass: boolean; limit_down_mbps: number; limit_up_mbps: number; history_days: number | null;
  dns_server: string; allowance_gb: number; used_gb: number; percent: number; lan_used_gb: number; blocked: boolean;
  block_state: string; quota_blocked: boolean; daily: Daily | null; devices: Device[];
}
export interface Recharge { id: number; gb: number; used_gb: number; remaining_gb: number; expires_at: number; recurring: boolean; target_type: "all" | "user" | "device"; target_id: number | null; comment: string }
export interface Bundle {
  base_total_gb: number; total_gb: number; used_gb: number; remaining_gb: number; reset_day: number;
  period_type: "renew_day" | "end_of_month" | "manual"; period_start: string | null; period_end: string | null; days_left: number; recharges: Recharge[];
}
export interface Rogue { mac?: string; ip?: string; vendor?: string; online?: boolean; blocked?: boolean; allowed?: boolean }
export interface Dashboard {
  bundle: Bundle; daily_enabled: boolean; free_mode: boolean; free_mode_enabled: boolean; users: User[]; devices?: Device[]; rogue: Rogue[]; unmanaged_block?: boolean;
  gateway?: { blocked_desired?: boolean; blocked_programmed?: boolean };
  vpn_share?: { enabled?: boolean; interface?: string; status?: { state?: string; interface?: string } };
  vpn?: { state?: string; active_node_name?: string };
  shaping?: { available?: boolean; applied?: boolean };
  internet: boolean | null; blocked_count: number; version?: string;
  security?: { failed_logins_1h?: number; waf_blocks_1h?: number; attack_new?: number; default_password?: boolean };
  ts?: number;
}
export interface LiveDevice { id: number; name: string; mac: string; ip?: string; user_name?: string; vendor?: string; down_bps: number; up_bps: number; total_bps: number; lan_down_bps?: number; lan_up_bps?: number; share_pct?: number; is_idle?: boolean; top_destinations?: { domain: string; up_bps?: number; down_bps?: number }[]; recent_sites?: { domain: string }[]; recent_domains?: string[] }
export interface LiveTraffic {
  total_down_bps: number; total_up_bps: number; total_lan_down_bps?: number; total_lan_up_bps?: number;
  devices: LiveDevice[]; top_consumer?: LiveDevice & { is_idle?: boolean }; history?: { t?: number; down: number; up: number; lan_down?: number; lan_up?: number }[];
}
