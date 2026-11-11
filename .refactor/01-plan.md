# .refactor/01-plan.md
# Unit Decomposition & Execution Order — Total Project Refactor
# Phase 2: derived from Phase 1 inventory (.refactor/00-inventory.md, 4 map files)

## Unit Ordering Rule
Foundations first, leaves before parents, top-level pages and composition last. Units with disjoint file allowlists can run in parallel.

## Unit List (dependency-ordered)

| # | Unit | Files (~count) | Lenses | Deps | Parallel with |
|---|------|---------------|--------|------|---------------|
| U1 | Backend Foundation: config + db + service | core/config.py, quota/db.py, quota/service.py, quota/daily_plan.py (~4 files, ~6,600 lines) | A, B | none | U2 |
| U2 | Backend Foundation: packet engine + firewall | quota/nftables.py, quota/firewall.py (~2 files, ~2,500 lines) | A, B | none | U1 |
| U3 | Backend Foundation: shaping + VPN share | quota/shaping.py, quota/vpnshare.py (~2 files, ~1,600 lines) | A, B | none | U1, U2 |
| U4 | Backend Foundation: history + analytics + DNS + ARP | quota/history_analytics.py, quota/dns_rules.py, quota/dnslog.py, quota/flows.py, quota/arp_lock.py, quota/arp_scan.py (~6 files, ~6,000 lines) | A, B | U2 (firewall concepts) | U5 |
| U5 | Backend Foundation: IPv6 + network + VPN/TUN + wifi/latency | quota/ipv6_probe.py, quota/ipv6_setup.py, quota/netmgr.py, quota/topology.py, quota/vendor.py, quota/vpn_manager.py, quota/vpn_parser.py, quota/tun2socks.py, quota/wifi_probe.py, quota/latency_probe.py (~10 files, ~7,000 lines) | A, B | U2 | U6 |
| U6 | Backend Foundation: storage + security + __init__ | core/passwords.py, core/timeutil.py, core/logging_setup.py, quota/__init__.py (~4 files) | A | none | U1-U5 |
| U7 | Backend API Layer | api/app.py + 3 other api files (~4 files, ~5,000 lines) | A, B, D | U1-U6 | none |
| U8 | Backend Runtime Entry | run.py, core/__init__.py (~2 files, ~2,600 lines) | A, B, D | U7 | none |
| U9 | Frontend Layered Architecture: config → transport → model | web/js/config/constants.js, web/js/transport/http.js, web/js/model/{domain,application,api}/index.js (~4 files) | A, D | none | U10 |
| U10 | Frontend Layered Architecture: port → api → services | web/js/port/index.js, web/js/api/index.js, web/js/services/index.js (~3 files) | A, D | U9 | U11 |
| U11 | Frontend Layered Architecture: state/stores → hooks → ui → shared → app | web/js/{state/stores,hooks,ui,shared,app}/index.js (~5 files) | A, D | U10 | U9 |
| U12 | Frontend Pages (migrate remaining to hooks) | web/js/pages/{overview,people,quota,network,internet,security,securityDns,insights,vpn,system,share,internetWan,shareLive}.js (~14 files) | A, B, C | U11 | none |
| U13 | Frontend Components | web/js/components/{chart,dailyPlan,dialog,drawer,icons,peopleDialogs,toast,ui,userDrawer,welcome}.js + core/* (~11 files) | A, B, C | U11 | none |
| U14 | UI/CSS/HTML Design System | web/css/{tokens,base,layout,components}.css, web/assets/{pub,share}.css, web/*.html, web/js/i18n/{ar,en}.js (~11 files) | C | U12, U13 | none |
| U15 | Tests & Safety Net | tests/ (~37 modules + conftest) + frontend behavioral/E2E stubs | G0-G7 | U1-U14 | none |

## Total Coverage Check
- Backend: 40 .py files across U1-U8 (100% of core/quota/api/run.py)
- Frontend: 52 JS files across U9-U13 (100% of web/js/)
- UI: CSS/HTML/i18n across U14 (100% of web/css, web/assets, web/*.html, web/js/i18n)
- Tests: U15 (100% of tests/)

## Wave Plan (max 3-4 parallel workers per wave, disjoint file allowlists)
- Wave A: U1, U2, U3 (backend foundations, parallel)
- Wave B: U4, U5, U6 (backend foundations continued, parallel)
- Wave C: U7, U8 (backend API + runtime, serial after foundations)
- Wave D: U9, U10, U11 (frontend layered architecture, parallel)
- Wave E: U12, U13 (frontend pages + components, parallel)
- Wave F: U14 (UI design system, serial after pages)
- Wave G: U15 (tests & safety net, serial after all)

## Estimated Effort
- Each unit: ~10 files or fewer, ~800 changed lines or fewer (per Master Prompt)
- Units U1-U8 are mostly analysis/verification (logic already pure, well-structured)
- Units U9-U14 are the main frontend refactor work (already largely done on main; verify + harden)
- Unit U15 is the safety net (characterization tests + E2E stubs)

## Open Risks
- Full test suite unverified (working venv required; uvicorn missing in default env)
- Dead CSS candidates unverified (need coverage tool)
- No frontend behavioral/E2E tests exist yet
