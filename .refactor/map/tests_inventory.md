# Tests & Configuration Inventory — QuotaManager (refactor/total, pre-refactor baseline)

## Test files (tests/ — 37 modules + conftest.py)

| # | File | Purpose / what it tests | Baseline status |
|---|------|-------------------------|-----------------|
| 1 | test_api.py | API integration (FastAPI TestClient + temp SQLite) — login/auth, routes, enforcement | unverified |
| 2 | test_security.py | Auth core, OWASP fixes, embedded WAF, TOTP, rate limiting | unverified |
| 3 | test_guest_captive.py | Guest captive portal & search redirection | unverified |
| 4 | test_unmanaged.py | Static-IP/unmanaged hosts: auto-cut, adopt, allow | **collection error** (uvicorn missing) |
| 5 | test_local_share.py | Local Share manager + REST API (sanitize_filename, CRUD) | unverified |
| 6 | test_run_wiring.py | E2E smoke of run.py wiring: Gateway, uvicorn boot, API+WS, maintenance tick | **collection error** (uvicorn missing) |
| 7 | test_quota_service.py | Quota service pure logic (fake clock, real sqlite) — 1788 LOC | unverified |
| 8 | test_daily_service.py | Daily quota wired into service (fake clock) | unverified |
| 9 | test_daily_plan.py | Pure daily-quota planner (spread/next_day modes, carry) | unverified |
| 10 | test_free_mode.py | Free mode (out-of-quota open box, recharge closes it) | unverified |
| 11 | test_users_migration.py | v2 legacy DB migration in place (connect() ALTERs + backfill) | unverified |
| 12 | test_config.py | Config loading; Linux surface (no arp:/fallback_* fields) | unverified |
| 13 | test_nftables.py | nftables engine accounting+block via fake `nft` binary | unverified |
| 14 | test_firewall.py | Firewall module — 9 acceptance sections via fake `nft` | unverified |
| 15 | test_shaping.py | tc speed-shaping engine via fake `tc`/`ip`/`modprobe` (FakeTc) | unverified |
| 16 | test_vpnshare.py | VPN-share policy routing via fake `ip` + /sys/class/net | unverified |
| 17 | test_tun2socks.py | tun2socks auto-provisioner (download/verify/spawn, no network) | unverified |
| 18 | test_vpn.py | VPN link parsing (vless reality) + sing-box config + history analytics | unverified |
| 19 | test_arp_scan.py | Rogue-device scanner: ARP frames, NIC resolution, scan exclusions | unverified |
| 20 | test_arp_lock.py | ARP gateway-lock responder via fake AF_PACKET socket | unverified |
| 21 | test_latency_probe.py | ARP-RTT probe classifier math + ping-parse fallback | unverified |
| 22 | test_wifi_probe.py | Passive WiFi probe: airodump CSV parsing + thread snapshot | unverified |
| 23 | test_ipv6_probe.py | ISP IPv6 reachability + dual-stack state manager | unverified |
| 24 | test_ipv6_setup.py | Self-applying IPv6 host config (dnsmasq fragment, sysctl) | unverified |
| 25 | test_startup_health.py | Startup self-heal (nft/ip/sysctl faked) | unverified |
| 26 | test_netmgr.py | TopologyManager — dashboard WAN-tab LAN/WAN switch | unverified |
| 27 | test_topology.py | WAN detection: detect_ppp, check_internet, check_internet_dns, restart_pppoe | unverified |
| 28 | test_dnslog.py | dnsmasq query-log tailer + DNS-history DB layer | unverified |
| 29 | test_dns_rules.py | hosts/AdBlock-Plus parsing, wildcard norm, dnsmasq tag/rule renderer | unverified |
| 30 | test_vendor.py | Offline IEEE OUI vendor lookup (vendor_for, _display) | unverified |
| 31 | test_flows.py | Per-destination conntrack traffic (where bytes go) | unverified |
| 32 | test_distro.py | scripts/distro.sh package-manager detection + per-distro pkgs | unverified |
| 33 | test_packaging.py | .deb packaging contract (control from quota/version.py), no dpkg | unverified |
| 34 | test_ci.py | CI workflow structure (.github/workflows/ci.yml valid YAML, triggers/jobs) | unverified |
| 35 | test_new_ui.py | Dashboard guards: served, cache-safe, CSP-clean, self-contained, i18n parity | **16 passed** (CONFIRMED) |
| 36 | conftest.py | infra: aiosqlite worker threads forced daemon (no hang on exit) | n/a |

## Pre-existing failures (recorded, not regressions)
- `test_run_wiring.py` / `test_unmanaged.py`: collection error — `uvicorn` missing in default env. Full suite requires the working venv: `/home/abed/Data/03_Professional/Projects/Telegram_Bots/tanzil/.venv/bin/python`.
- Only `test_new_ui.py` (16/16) is confirmed green; every other module is unverified pending a venv run.

## Dependencies (requirements-linux.txt)
fastapi==0.141.1, uvicorn[standard]==0.52.1, aiosqlite==0.22.1, PyYAML==6.0.3, pytest==9.1.1 (dev), httpx==0.28.1 (TestClient), pyflakes>=3.2,<4 (lint), aiohttp>=3.9.0 (optional/compat).

## Configuration (config.yaml — 372 lines, 16 blocks)
bundle / db_path-log_file-log_level-timezone / dhcp / engine / shaping / vpn_share / web / waf / report / history / dns_filter / network / updates / firewall (+ latent shaping/vpn_share/firewall/waf/report/history/dns_filter/updates blocks documented as comments). Values match scripts/setup_gateway_kali.sh defaults. Overridable via `--config` or `QUOTA_CONFIG` env.

## Entry point (run.py — ~2612 lines)
`Gateway` class wires all layers in one Linux gateway process: non-blocking logging, SQLite (aiosqlite) quota service + per-period snapshot, 15 s maintenance coroutine (flush counters → re-evaluate blocks → push enforcement maps), FastAPI/uvicorn (REST + WebSocket push + static UI). Any subsystem lacking `nft`/root/dnsmasq degrades gracefully — never crashes the app. Usage: `python run.py [--config config.yaml] [--port 8080] [--debug]`.

## Test infrastructure
pytest 9.1.1 + httpx TestClient + pyflakes. Hermetic-by-default: fakes for `nft` (FakeNft), `tc`/`ip` (FakeTc/FakeIp), `ss`, tun2socks downloader, sysfs, `/proc`, `dnsmasq`/`systemctl`. tmp_path SQLite DBs; deterministic fake clock (`make_clock`). UI tests assert on real `web/` files. Heavy cross-module fixture reuse (test_quota_service, test_run_wiring, test_api).

## Test gaps (untested / thinly covered areas)
- Frontend JS behavioral tests (51 JS files, 4 CSS, 6 HTML — only `node --check` syntax + test_new_ui.py guards; no browser/E2E tests).
- Kernel integration: real nftables/tc/dnsmasq/root (all faked; no privilege tests).
- `.deb` build with dpkg-deb (contract tests only); packaging CI (release.yml/apt-repo.yml) not exercised.
- `run.py` CLI `main()`/argparse surface (Gateway class tested, not entrypoint).
- `scripts/setup_gateway_kali.sh` / `scripts/topology.sh` (only distro.sh tested).
- Live network/probes: latency raw socket needs root; IPv6 sysctl faked; GitHub update check faked (no real network).
- No test for the WAN-tab live apply restart path end-to-end (TopologyManager apply flow faked).
