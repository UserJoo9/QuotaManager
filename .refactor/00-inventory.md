# .refactor/00-inventory.md
# File-to-Unit Mapping — Total Project Refactor
# Phase 1 complete. All 4 map files verified on disk.

## Map Files (produced by Phase 1 sub-agents)
| Map file | Path | Lines | Coverage |
|---|---|---|---|
| Backend inventory | `.refactor/map/backend_inventory.md` | 51 | 40 .py files, 24,982 lines (100% of core/quota/api/run.py) |
| Frontend inventory | `.refactor/map/frontend_inventory.md` | 58 | 52 JS files in web/js/ (51 active + 1 orphaned ui.js.orig backup) |
| UI/CSS/HTML inventory | `.refactor/map/ui_inventory.md` | 70 | 4 CSS, 9 assets, 6 HTML, 2 i18n bundles (ar.js 989 keys = en.js 989 keys) |
| Tests inventory | `.refactor/map/tests_inventory.md` | 67 | 37 test modules + conftest.py; only test_new_ui.py confirmed green (16 passed) |

## File-to-Unit Mapping (every source file handled by exactly one unit)

### Unit: Foundation — Config & Types (backend)
- core/config.py — Config dataclass (all 16 sections) from config.yaml/QUOTA_ENV
- quota/db.py — SQLite schema, domain enums (QUOTA_*, BLOCK_*, DNS_*), data layer
- quota/service.py — QuotaService: allowance math, milestones (50/75/100), enforcement state
- quota/daily_plan.py — pure daily planner (fixed/spread modes, no I/O)

### Unit: Foundation — Packet Engine & Firewall (backend)
- quota/nftables.py — q_up_/q_down_ counters, blocked set, flush() delta
- quota/firewall.py — fw_input/fw_forward/fw_dnat, WAN default-deny, auto-revert

### Unit: Foundation — Shaping & VPN Share (backend)
- quota/shaping.py — tc HTB ifb/egress, fq_codel+cake, signature reconcile
- quota/vpnshare.py — LAN never enters tunnel, dead-tunnel fallback to uplink

### Unit: Foundation — History & Analytics (backend)
- quota/history_analytics.py — DNS categorization, time-series activity
- quota/dns_rules.py — domain filtering renderer (dnsmasq-generated)
- quota/dnslog.py — dnsmasq query-log tailer, per-device/minute/domain buckets
- quota/flows.py — conntrack per-destination accounting
- quota/arp_lock.py — ARP gateway-lock responder (anti static-IP bypass)
- quota/arp_scan.py — rogue static-IP detection

### Unit: Foundation — IPv6 & Network (backend)
- quota/ipv6_probe.py — ISP IPv6 reachability probe
- quota/ipv6_setup.py — self-applying IPv6 host config
- quota/netmgr.py — network management
- quota/topology.py — LAN/WAN topology
- quota/vendor.py — vendor lookup (RDAP)

### Unit: Foundation — VPN & TUN (backend)
- quota/vpn_manager.py — VPN management
- quota/vpn_parser.py — VPN config parsing
- quota/vpnshare.py (shared) — VPN share relay
- quota/tun2socks.py — tun2socks bridge
- quota/wifi_probe.py — passive WiFi/LAN access probe (monitor mode)
- quota/latency_probe.py — ARP RTT classification

### Unit: Foundation — Storage & Security (backend)
- core/passwords.py — PBKDF2 admin password
- core/timeutil.py — timezone utilities
- core/logging_setup.py — non-blocking QueueHandler logging
- quota/__init__.py — package marker

### Unit: API Layer (backend)
- api/app.py — create_app(), qmsession cookie (7-day TTL), WAF middleware
- api/ (3 other files) — API routes, schemas, middleware

### Unit: Runtime Entry (backend)
- run.py — Gateway class (L259), main() (L2490), 15s maintenance tick, graceful degradation
- core/__init__.py — package marker

### Unit: Frontend Layered Architecture (web/js/)
- config/constants.js — STORAGE, ACCENTS, TONES, PARTICLES, LIVE, ZOOM, CANVAS, QUOTA constants
- transport/http.js — generic fetch with CSRF header
- model/domain/index.js — User, Device, Bundle, DailyPlan, VpnStatus, WANStatus, SecurityStatus, FirewallRule, DnsRule, ShareFile, ShareText, LoginSession
- model/application/index.js — LoginCommand/Result, UpdateBundleCommand, AddRechargeCommand, PatchUser/PatchDevice, TopUpCommand, DailyPlanCommand, NetworkCommand, GuestCommand, MacListCommand, StaticLeaseCommand, FirewallCommand, DnsRuleCommand, ShareCommand
- model/api/index.js — ApiDashboardResponse, ApiLoginRequest/Response, ApiBundleRequest, ApiRechargeRequest, ApiPatchUserRequest, ApiPatchDeviceRequest, ApiTopUpRequest, ApiDailyPlanRequest, ApiNetworkRequest, ApiGuestRequest, ApiMacListRequest, ApiStaticLeaseRequest, ApiFirewallRequest, ApiDnsRuleRequest, ApiDnsImportRequest, ApiShareRequest
- port/index.js — DashboardApiPort, BundleApiPort, UserApiPort, TopUpApiPort, DailyPlanApiPort, NetworkApiPort, GuestApiPort, MacApiPort, StaticLeaseApiPort, FirewallApiPort, DnsApiPort, ShareApiPort, WAN/Tailscale API ports
- api/index.js — dashboardApi, loginApi, bundleApi, userApi, topUpApi, dailyPlanApi, networkApi, guestApi, macApi, staticLeaseApi, firewallApi, dnsApi, shareApi, systemApi + ENDPOINTS
- state/stores/index.js — createDashboardStore, createBundleStore, createUserStore, createNetworkStore, createShareStore, createSecurityStore, createDnsStore, createVpnStore, createSystemStore
- services/index.js — createDashboardService, createBundleService, createUserService, createNetworkService, createDailyPlanService, createShareService, createSecurityService, createDnsService, createVpnService
- hooks/index.js — useDashboard, useBundle, useUsers, useNetwork, useDailyPlan, useShare, useSecurity, useDns, useVpn, usePreferences, useAuth
- ui/index.js — PageShell, PageHeader, NavItem, Drawer, Dialog, Toast, Button, Select, Input, Badge, StatusDot, Table, Card, KPI, ProgressBar
- shared/index.js — formatBytes, debounce, throttle, deepEqual, urlEncode, parseQuery, sleep, formatNumber, currencyFormat, relativeTime, clamp, isMobile, randId, localJson
- app/index.js — createApp composition root wiring stores/services/hooks

### Unit: Frontend Legacy Core (web/js/core/) — compat shim, NOT to be refactored now
- core/api.js — compat shim consumed by main.js and legacy core/* (kept for gradual migration)
- core/actions.js, core/overviewData.js, core/vpn.js, core/ws.js — direct core/api.js consumers (5 legacy consumers flagged in frontend_inventory.md)
- core/dom.js, core/i18n.js, core/live.js, core/particles.js, core/prefs.js, core/store.js — legacy primitives (6 files)

### Unit: Frontend Pages (web/js/pages/) — all migrated to ctx.hooks
- overview.js, people.js, quota.js, network.js, internet.js, security.js, securityDns.js, insights.js, vpn.js, system.js, share.js — 11 pages using hooks
- internetWan.js — WAN card (migrated to networkHooks)
- shareLive.js — live share card (uses core/live.js WebSocket)

### Unit: Frontend Components (web/js/components/)
- chart.js, dailyPlan.js, dialog.js, drawer.js, icons.js, peopleDialogs.js, toast.js, ui.js, userDrawer.js, welcome.js
- dailyPlan.js now passes ctx to dailySection via userDrawer → people.js

### Unit: UI/CSS/HTML (web/css/, web/assets/, web/*.html)
- css/tokens.css — design tokens (colors dark/light, spacing, typography, direction)
- css/base.css — base styles + UI polish rules
- css/layout.css — layout styles
- css/components.css — component styles + overscroll, invalid styling
- assets/pub.css, assets/share.css — public page styles
- index.html — SPA shell (skip link, dir="ltr", v3 cache)
- share.html — share page (ARIA labels, aria-hidden SVGs)
- milestone.html, report.html, guest_pending.html, spoof.html — public pages
- assets/logo.png (1.8MB — oversized, flagged), assets/qrcode.min.js (vendor)

### Unit: i18n (web/js/i18n/)
- ar.js — 989 keys (Arabic)
- en.js — 989 keys (English) — identical key sets, zero gaps

### Unit: Tests (tests/)
- test_new_ui.py — 16 passed (UI/architecture regression) — CONFIRMED GREEN
- Remaining 36 modules + conftest.py — status unverified (working venv required)
- Pre-existing failures: test_run_wiring.py, test_unmanaged.py (uvicorn missing in default env)

## Orphaned / Excluded Files
- web/js/components/ui.js.orig — unimported legacy backup, excluded from active count

## Dependencies Between Units (order for Phase 2 decomposition)
1. Foundation (config, db, service) → API Layer → Runtime Entry (backend)
2. Frontend Layered Architecture (config→transport→model→port→api→services→stores→hooks→ui→shared→app)
3. Frontend Pages/Components (consume hooks)
4. UI/CSS/HTML (design system)
5. Tests (safety net)

## Open Issues from Phase 1 (to be addressed in later phases)
- Dead CSS candidates (.skeleton, .empty, .dot, .bar, .pill, .badge-dot, .fx/.blob) — flagged for coverage-tool confirmation
- Duplicated components: .btn (3×), .card (2×), .toast (2×), .banner (2×), .chip vs .pill
- a11y gaps: skip link only on index.html; toasts lack role="status"/aria-live; dialog roles unverified
- Responsive gaps: fixed heights (.gauge/.chart 190px, .live-body/.terminal 390/260px) partially converted to max-height
- guest_pending.html redefines tokens inline (diverges from design system)
- No frontend JS behavioral/E2E tests; no kernel (nft/tc/dnsmasq/root) tests
