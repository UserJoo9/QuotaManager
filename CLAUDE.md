# CLAUDE.md — working guide for QuotaManager

Read this BEFORE changing anything. It tells you what owns what, where a change
belongs, and which mistakes already cost a real outage. Deep technical detail lives in
`Structure_README.md`; the old v0.2.1 audit is archived in
`docs/legacy-system-audit-v0.2.1.md` (historical, partly stale).

## 1. What this is

A Linux gateway (old laptop, Kali/Debian) that splits ONE metered internet bundle
(e.g. 140 GB/month) fairly across **users**; a user's allowance covers all their
devices. Over quota → all the user's devices are cut together. The kernel owns the
network path; Python only configures it and does the accounting.

- **Kernel**: nftables (`inet quota_gateway` counters/blocks, `inet quota_nat` NAT,
  `inet quota_firewall`), `tc` (HTB + ifb speed shaping), policy routing (VPN share).
- **dnsmasq**: DHCP (+ DHCPv6/RA), DNS forwarder, domain filtering, query log.
- **Python (FastAPI + asyncio, single process `run.py`)**: one maintenance tick every
  15 s, REST API + `/ws`, SQLite (aiosqlite).
- **Web UI**: static files in `web/` (vanilla JS, no build step).
- **sing-box** child process for the built-in VPN (Clash API on 127.0.0.1:9090).

## 2. Golden rules (each one is a past incident)

1. **Nothing blocking on the event loop.** Subprocess/`nft`/`tc`/file IO goes through
   `asyncio.to_thread` (or the engine's sync API called from a thread). The tick warns at >1 s.
2. **Kernel state is cache-gated and idempotent.** Setters (`set_ipv6_wan`,
   `_sync_blocked*`, shaping `update_state`) compare to a cache and do nothing when
   unchanged; a failed program clears the cache so the next tick retries. Never
   re-flush a set every tick (opens a brief unblock window).
3. **One bad value must not kill the engine.** `NftablesEngine._run` flips
   `available=False` on an nft error ⇒ NO counting/blocking for everyone. Validate
   everything that goes into nft (MAC regex, IP parse) BEFORE programming it.
4. **Never trust identifiers from outside.** dnsmasq's lease file has DHCPv6 lines
   (`expiry iaid ipv6 …`) — field 2 is not a MAC. Accept only `aa:bb:cc:dd:ee:ff` + IPv4.
5. **dnsmasq config is fragile.** `dnsmasq --test` validates files one by one; option
   conflicts (`bind-dynamic` + `bind-interfaces`) only fail at start. App-owned fragments
   must be minimal; after a restart verify dnsmasq is active (`rollback_if_dnsmasq_down`).
6. **The box's own resolver must stay sane.** dnsmasq binds only the LAN NIC, so
   `127.0.0.1:53`/`[::1]:53` and `[ULA]:53` are NOT answered. Don't point anything on the
   box at them (sing-box's generated DNS uses `direct-dns` = system resolver).
7. **The VPN server's hostname must resolve BEFORE the TUN exists** (`pin_server_hostnames`).
   sing-box `auto_route` captures the box's own traffic, including that lookup.
8. **Setup must not be a manual step on update.** Anything the host needs (sysctl, dnsmasq
   fragment, NAT rules, addresses) is applied by the app at start (`_startup_self_heal`,
   `quota/ipv6_setup.py`, `quota/startup_health.py`) AND written identically by
   `scripts/setup_gateway_kali.sh` so fresh installs and updates converge.
9. **Every kernel feature is IPv4 AND IPv6.** A new nft rule/counter/set/tc filter needs
   its `ip6` twin (or an explicit, documented "IPv4 only" in `Structure_README.md`).
10. **Presence ≠ lease.** A green LED needs REACHABLE neighbor / ARP reply / recent traffic.
    Share % is meaningless on an idle line (`LIVE_IDLE_NET_BPS`). Live speed = bytes ÷ the
    real flush window (`EngineSnapshot.interval_s`).
11. **Tests: every outage gets a regression test** and stays. Don't delete or loosen one to
    make a change pass — fix the change.
12. **Admin settings must survive updates and setup re-runs.** Runtime state lives in the DB
    (`/var/lib/quota-gateway/quota.db`, outside the project folder): firewall config/geo,
    DNS rules/presets, VPN nodes/active node/auto-connect, WAN/topology, shaping, guest...
    Never move such state into the project folder or `config.yaml`. `config.yaml` is only
    seed/defaults; the setup script backs it up (`config.yaml.prev`) and carries over every
    section it does not write (`firewall`, `dns_filter`, `vpn_share`, `network`, ...).
13. **Never let the browser run stale UI.** `/assets/*` are served `Cache-Control: no-cache`
    and `index.html` references `js/main.js?v=N` (JS/CSS/fonts under `/js/`, `/css/`, `/fonts/` are `no-cache` too) — bump N when you change them
    (the page itself is `no-store`). A fixed `?v=` + no cache header made an updated server
    keep serving the old UI from the browser cache.
14. **Secrets**: `quota-manager-secret.asc` (PGP *private* key) is git-ignored — never copy it
    to a server, never commit it. `data/`, `logs/` hold the admin hash and usage.

## 3. Runtime picture

```
LAN clients ─ DHCP/DNS(v4+v6 RA) ─► dnsmasq ──► query log ─► DnslogTailer ─► dns_history
     │ traffic
     ▼
 nftables forward chain:  [arp-lock deny] → jump v6gate (IPv6 only) → blocked drops
                          → per-device named counters (q_up_/q_down_<addr>)
     │                      (counters read every tick by NftablesEngine.flush)
     ▼
 tc: ifb redirect (v4 prio, v6 prio 7) → HTB class per device/user → fq_codel
     ▼
 NAT: quota_nat masquerade (v4) + NAT66 (ULA prefix) ─► uplink (router, or ppp0 in WAN mode)
     └─ optional: policy route client subnet → quota-vpn TUN (sing-box) = "VPN share"
```

### The maintenance tick (`Gateway._maintenance_tick`, run.py, every 15 s)

1. `ensure_period` (month roll) · 1b `_sync_dnsmasq_leases` (+ `_purge_phantom_devices`) ·
   1c rogue scan (60 s) · 1d DNS history drain · 1e prune events · 1f WAN renew ·
   1h update check.
2. Drain engine counters → `usage_daily` (per device, IPv4+IPv6 via `ip_to_mac_all`).
3. `service.evaluate_blocks` → block states from usage vs allowances.
4. Push `ip_to_mac`/`blocked` into the engine + swap the `EngineSnapshot` (what the
   dashboard/WS reads). Also syncs the IPv6 gate: `engine.set_ipv6_wan(_ipv6_wan_wanted())`.
5. `_sync_shaping` (tc) · 6 `_sync_dns_rules` (+ 6b DHCP-refusal fragment) ·
   7 `_sync_vpn_share` · 8 firewall reconcile · latency/ARP/neighbor collection.

Startup: `startup_core` (DB, fast, ≤20 ms) → `startup_subsystems` (engine, shaping, VPN,
firewall, dnslog, probes — concurrent) → `_startup_self_heal` (NAT, NICs, ip_forward,
nftables.conf, IPv6 host config).

## 4. Module map (what owns what)

| Path | Owns | Does NOT |
|---|---|---|
| `run.py` | `Gateway`: wiring, tick, neighbor/lease collection, VPN/WAN glue, `create_app(...)` wiring | business rules (→ `service.py`), kernel syntax (→ engine modules) |
| `api/app.py` | all REST routes + `/ws` + static mount, auth/session/WAF glue | quota math, nft/tc commands |
| `api/schemas.py` | pydantic request models/validators | logic |
| `api/waf.py` | request filtering/ban middleware | |
| `core/config.py` | `config.yaml` → typed dataclasses (defaults live HERE) | |
| `core/passwords.py`, `quota/totp.py` | password policy/PBKDF2, TOTP | |
| `quota/db.py` | SQLite schema, migrations (`ALTER TABLE … ADD COLUMN` guarded), all queries | policy |
| `quota/service.py` | quota math, allowances, block fan-out/bypass precedence, top-ups, recharge, guest/shaping/VPN settings | kernel |
| `quota/engine.py` | `EngineSnapshot`, `SnapshotHolder`, `GATEWAY_MAC`, counters types | |
| `quota/nftables.py` | `NftablesEngine`: table/chains/sets/counters, blocked sets (v4/v6/MAC), `v6gate`, `in6guard`, flush/drain | tc, dnsmasq |
| `quota/shaping.py` | `TcShaper`: HTB/ifb tree, per-device & per-user caps, v4+v6 filters | |
| `quota/firewall.py` | `quota_firewall` table: posture, rules, bans, port-forward, geo (**IPv4 only**) | |
| `quota/arp_lock.py`, `arp_scan.py`, `latency_probe.py`, `wifi_probe.py` | rogue detection, ARP gateway-lock, WiFi/LAN + LED probe | |
| `quota/dns_rules.py` | domain rules/presets/per-client DNS → generated dnsmasq conf (`render_tags`, `[ip6]` in dhcp-host) | |
| `quota/flows.py` | conntrack per-destination byte deltas, IP→name resolution (dnsmasq → reverse DNS → RDAP owner → IP; feeds Live traffic `top_destinations`) | guess app names |
| `quota/dnslog.py`, `history_analytics.py` | query-log parser; analytics, **Live traffic** (share %, idle, speed) | |
| `quota/vpn_manager.py` | sing-box supervisor, Clash API, node state | config syntax (→ `vpn_parser`) |
| `quota/vpn_parser.py` | link parsing, **sing-box config generator**, `pin_server_hostnames` | process control |
| `quota/vpnshare.py` | policy routing of the client subnet into the TUN (`ip rule`/table 200) | |
| `quota/tun2socks.py` | bridge for userspace VPN clients | |
| `quota/netmgr.py`, `topology.py` | WAN tab: LAN/WAN(PPPoE) switching, public-IP renew | |
| `quota/startup_health.py` | idempotent self-heal: NAT (+NAT66, ip6 DNS redirect), ULA address, ip_forward, nftables.conf | |
| `quota/ipv6_probe.py` | ISP IPv6 probe, hysteresis | |
| `quota/ipv6_setup.py` | app-owned IPv6 host config: dnsmasq RA fragment, sysctl, box DNS fix, rollback, kill-switch | |
| `quota/local_share.py` | LAN file sharing (`/api/share`) | |
| `quota/updater.py` | GitHub self-update | |
| `quota/tailscale.py` | built-in Tailscale remote access (status / install / connect / disconnect) | |
| `scripts/setup_gateway_kali.sh` | first install: packages, sysctl, dnsmasq, NAT, systemd unit (`CONF_DIR=/etc/quota-gateway`) | day-2 changes — those belong in the app |
| `scripts/topology.sh` | runtime LAN/WAN applier invoked by the panel | |
| `web/index.html`, `web/js/` (`main.js`, `pages/*`, `core/*`, `i18n/{en,ar}.js`), `web/css/` | the dashboard (ES modules, no build; one page file per tab) | |
| `packaging/DEBIAN/` | `.deb` build | |
| `tests/` | one `test_<module>.py` per module (+ `test_ci`, `test_packaging`, `test_web_ui`) | |

### Data (`quota/db.py`)
Tables: `users`, `devices`, `leases`, `static_leases`, `usage_daily`, `settings`,
`bundle_config`, `bundle_recharges`, `mac_lists`, `events`, `dns_history`, `dns_presets`,
`domain_rules`, `guest_vouchers`, `vpn_nodes`, `vpn_routing`. `settings` is a key/value
store for dashboard-owned state (a dashboard-set value then wins over `config.yaml`, e.g.
`bundle_source`, `topology_source`). The box itself is a protected user/device under
`GATEWAY_MAC = 00:00:00:00:00:00`.

### Config
`config.yaml` → `core/config.py` (`Config` → `DhcpConfig`, `EngineConfig`, `ShapingConfig`,
`VpnShareConfig`, `DnsFilterConfig`, `FirewallConfig`, `WebConfig`, …). Add a key = add the
dataclass field with a default there, then the commented line in `config.yaml`, then
`Structure_README.md → Configuration`. The systemd service runs with
`--config /etc/quota-gateway/config.yaml` (generated by the setup script); the repo's
`config.yaml` is the template/dev file — check which one a server really uses.

## 5. "I want to change X — where?" playbook

| Task | Touch (in order) |
|---|---|
| New REST endpoint | `api/schemas.py` (model) → `api/app.py` (route, auth, `service`/`db` call) → `tests/test_api.py` → `Structure_README.md → REST API` |
| New setting that must persist | `db` settings key via `service.get_/set_*` → route → UI → default in `core/config.py` if it also has a yaml twin |
| New DB column/table | `quota/db.py` (schema + guarded `ALTER`) → model/dataclass → tests. Never edit an existing migration |
| New per-device kernel rule/counter | `nftables.py` (`_add_device`/sync, **v4 + v6**, LAN-excluded) → `tests/test_nftables.py` fake-nft → shaping if speed-related → engine fakes in `tests/test_run_wiring.py` |
| New value pushed to the engine each tick | `run.py` tick step 4 + `EngineSnapshot` (`engine.py`) + consumers (`history_analytics`, `api/app.py`) |
| New background probe/subsystem | module in `quota/` (start/stop, injected `run`) → start in `Gateway.startup_subsystems`, stop in `shutdown`, tick hook if periodic → config dataclass → tests |
| Host-level requirement (sysctl, dnsmasq option, NAT rule, address) | `startup_health.py` / `ipv6_setup.py` (idempotent, never raises) → hooked in `_startup_self_heal` → ALSO the setup script → test the "second run is a no-op" case |
| IPv6 behaviour | `ipv6_probe.py` (state), `nftables.py` (`v6gate`, `in6guard`, counters, `blocked6`), `shaping.py` (prio 7/5), `run.py` (`_collect_neigh_active`, `_ipv6_wan_wanted`, `_tracked_ipv6`, cap `IPV6_MAX_PER_MAC`), `ipv6_setup.py` (host), UI `renderWan`/device card |
| VPN behaviour | config text → `vpn_parser.generate_sing_box_config`; process/state → `vpn_manager.py`; who goes through the tunnel → `vpnshare.py` + `run.py::_sync_vpn_share`; node editing UI → `app.js` `loadVpn*/renderVpn*` |
| DNS filtering | `dns_rules.py` (render) + `run.py::_sync_dns_rules`; never write dnsmasq files from the API layer |
| Quota/blocking rule | `service.py` (`compute_allowances`, `evaluate_blocks`) — kernel gets the result via tick step 4 |
| Dashboard tab/card | `web/js/pages/<tab>.js` (+ `core/*` helpers); every user-visible string goes through `t("key")` in BOTH `web/js/i18n/en.js` and `ar.js` (`tests/test_new_ui.py` checks parity); bump `main.js?v=N` in `web/index.html` |
| Live traffic destinations | `quota/flows.py` (sampling/labels) → `run.py` tick (`self.flows`, `EngineSnapshot.flows_by_mac`) → `history_analytics` (`top_destinations`) → `web/js/pages/insights.js` |
| Live traffic / LED | `history_analytics.get_live_traffic_analytics`; presence in `run.py::_collect_neigh_active`, `_maybe_latency_tick`, `_latency_active_ips` |
| Release | bump `quota/version.py` + CHANGELOG section; tags only for a real release (see §7) |

## 6. Testing & CI parity

CI (`.github/workflows/ci.yml`): Python **3.12**, `pyflakes api core quota tests run.py`,
`pytest -v --timeout=60`. Locally:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-linux.txt pytest-timeout
.venv/bin/pyflakes api core quota tests run.py
.venv/bin/pytest -q --timeout=60            # if a sandbox segfaults on the full run, run per file
for f in $(find web/js -name '*.js'); do node --check "$f"; done
# Dashboard (web/, served at /): ES modules, no build step. tests/test_new_ui.py guards it
# (no inline script/handlers, self-hosted fonts only, ar/en key parity, imports resolve).
bash -n scripts/setup_gateway_kali.sh scripts/topology.sh scripts/test_pppoe.sh
```

Tests never touch the real kernel: `nft`/`tc`/`ip`/`systemctl` are injected fakes
(`FakeNft`, `FakeRun`, `FakeNftNat`, …). New host-touching code must take an injectable
`run` (and path/`proc_root` arguments) so it is testable the same way. `tests/test_ci.py`
and `tests/test_packaging.py` guard repo hygiene — read their failure messages before
"fixing" them.

## 7. Docs, git, releases

- `CHANGELOG.md` — plain language for end users (Added/Changed/Fixed), no code names.
- `Structure_README.md` — all technical detail; keep the *Project structure* tree, the
  subsystem section and *Lessons learned* current when you add/rename a module.
- `README.md` + `README_AR.md` — user manual; keep BOTH in sync.
- `AGENTS.md` / `.agents/rules/documentation.md` — the rules above, authoritative.
- **Never modify/re-tag/force-push existing `v*` tags.** Tags only for a brand-new release.
- Commit only when asked. No secrets, no `data/`, no `*.deb`.

## 8. Deployment & operating

- Deploy = copy the project folder to the server (not the `.asc` private key) and
  `sudo systemctl restart quota-gateway`. Updates must self-apply; if you catch yourself
  writing "re-run the setup script", move that logic into the app (rule 8).
- Logs: `journalctl -u quota-gateway`; app log in `logs/`. Useful checks:
  `nft list table inet quota_gateway`, `nft list chain inet quota_gateway forward`,
  `systemctl is-active dnsmasq`, `ip rule`, `ip route show table 200`.
- Symptoms → first suspect: "Packet engine is off" = an nft command failed at start/sync
  (look for `nft add failed`); no DNS for everyone = dnsmasq down (config conflict);
  VPN "Connected" but nothing loads = server-name resolution/DNS loop through the TUN;
  uncounted device = it took IPv6 from the ISP router (`IPv6 bypass` tag) or has a static IP (rogue).

## 9. Known limits (keep honest)

`quota_firewall` bans/geo and the captive portal are IPv4 only; static IPv6 reservations only
bind DHCPv6 clients (Android/iOS use SLAAC); the dashboard binds IPv4; counting is read every
~15 s (approximate); IPv6 has not been validated against a real IPv6 ISP yet (the probe keeps
the gate closed → IPv4 fallback when the ISP has none).
