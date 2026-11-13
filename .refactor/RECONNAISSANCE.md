# Phase 1: Full Project Reconnaissance
# Quota Manager — Strategic Evolution & Policy Engine

## Repository Structure (302 files, ~5.6MB)

### Backend (Python)
- **api/** (4 files, 240KB): `app.py` (207KB — FastAPI app), `schemas.py`, `waf.py`
- **core/** (5 files, 49KB): `config.py` (32KB), `logging_setup.py`, `passwords.py`, `timeutil.py`
- **quota/** (32 files, 2.4MB): `service.py` (86KB), `db.py` (102KB), `firewall.py` (63KB), `nftables.py` (57KB), `dns_rules.py` (33KB), `shaping.py` (50KB), `flows.py` (19KB), `history_analytics.py` (17KB), `arp_lock.py`, `arp_scan.py`, `daily_plan.py`, `dnslog.py`, `engine.py`, `ipv6_probe.py`, `ipv6_setup.py`, `latency_probe.py`, `local_share.py`, `netmgr.py`, `oui.txt` (1.6MB), `service.py`, `startup_health.py`, `tailscale.py`, `topology.py`, `totp.py`, `tun2socks.py`, `updater.py`, `vendor.py`, `version.py`, `vpn_manager.py`, `vpn_parser.py`, `vpnshare.py`, `wifi_probe.py`
- **run.py** (135KB): Entry point, Gateway class, maintenance loop

### Frontend (web/)
- **web/js/pages/** (14 files): overview, people, quota, network, internet, internetWan, security, securityDns, insights, vpn, system, share, shareLive, systemTabs
- **web/js/components/** (12 files): chart, dailyPlan, dialog, drawer, icons, peopleDialogs, toast, ui, userDrawer, welcome, ui.js.orig
- **web/js/core/** (10 files): actions, api, dom, i18n, live, overviewData, particles, prefs, store, vpn, ws
- **web/js/state/stores/**: index.js
- **web/js/hooks/**: index.js
- **web/css/** (4 files): tokens.css, base.css, layout.css, components.css
- **web/assets/**: pub.css, share.css, detector.js, i18n files, qrcode.min.js, pub-theme.js
- **web/guest_pending.html**, **web/index.html**, etc.

### Tests
- 46 test files, 891KB total
- test_api.py (127KB — largest), test_firewall.py (36KB), test_dns_rules.py (14KB), test_config.py (13KB)

### Config & Packaging
- **config.yaml** (19KB): Main configuration
- **requirements-linux.txt**: Dependencies
- **packaging/**: Debian, Arch, RPM, generic install scripts
- **.github/workflows/**: CI, release, apt-repo, packaging-smoke

---

## Key Architecture Insights

### Current Enforcement Stack (from code analysis)
1. **DNS Filtering** (`quota/dns_rules.py`): dnsmasq-based, `address=/domain/target` for block/redirect, `server=upstream$tag` for per-client DNS
2. **Firewall** (`quota/firewall.py`): nftables `quota_firewall` table, 3 chains (input/forward/dnat), bans with kernel timeout
3. **Packet Engine** (`quota/nftables.py`): Accounting + hard block via `quota_gateway` table
4. **Traffic Shaping** (`quota/shaping.py`): tc (traffic control) for speed limits
5. **ARP Lock** (`quota/arp_lock.py`): Prevents ARP spoofing
6. **VPN/Tailscale** (`quota/tailscale.py`, `quota/vpn_manager.py`): Tunnel integration

### Current Policy Model
- **DNS rules**: Global, per-user, or per-device scope via DHCP tags
- **Firewall rules**: CIDR-based allow/deny, bans with expiry
- **Quota**: Per-user GB limits, auto/fixed modes, milestone tracking
- **No unified policy engine exists** — rules are scattered across modules

### Frontend State Model
- **Stores** (`web/js/state/stores/index.js`): Dashboard state, auth, prefs, live
- **Hooks** (`web/js/hooks/index.js`): Presentation adapters for auth, bundle, users, devices, network, security, dns, vpn, system, dailyPlan, share
- **Services** (`web/js/services/index.js`): Business logic factories
- **API** (`web/js/api/index.js`): Endpoint map + DTO translation
- **Pages** (14 files): Route-level components

### Current Blocking Capabilities
| Mechanism | Scope | Granularity | Limitation |
|-----------|-------|-------------|------------|
| DNS block | Global/User/Device | Domain-level | Bypassed by DoH/DoT, hardcoded IPs |
| nftables ban | Global | IP/CIDR | Per-IP, not per-app |
| nftables allow/deny | Global | CIDR | No app awareness |
| tc shaping | Global/User/Device | Bandwidth | Rate limit only, no blocking |
| Quota block | User/Device | GB limit | All-or-nothing per user |

### Missing Capabilities (from prompt analysis)
- No application-aware blocking (TikTok vs domain)
- No feature-level control (YouTube Shorts vs regular)
- No content category classification
- No unified policy engine with priority/conflict resolution
- No policy simulation
- No enforcement adapter abstraction
- No capability detection
- No audit logging for policy changes
- No time-based policy scheduling
- No policy templates
- No bulk operations
- No policy import/export