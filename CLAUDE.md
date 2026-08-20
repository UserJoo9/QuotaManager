# CLAUDE SYSTEM MAP (EXISTING SYSTEM AUDIT)

_5-agent deep audit run **2026-08-19** (Reverse Engineer, Conflict & Regression
Analyst, Refactoring Architect, Performance & Log Auditor, Gatekeeper) at commit
`621b200`, working tree clean — read-only phase, **zero code modified**. This
map is the audited CURRENT TRUTH of the shipped v0.2.1 codebase. Corrections to
the previous map are inline-marked `[AUDIT]`. Audit findings carry the
`[AUDIT 2026-08-19]` tag; operational knowledge below it is verified-accurate.

Gateway that splits a metered internet bundle (e.g. Egypt 140 GB/month) fairly
across USERS — a person's allowance covers all of their devices (phone + tablet
+ laptop share one slice). When a user exceeds their allowance, every device
they own is cut at once; a per-device override can exempt a single device.
Admin dashboard: obsidian-glass web UI (fixed left sidebar, midnight obsidian
base + electric-cobalt accents, dark glassmorphism cards, 2-column masonry
user cards, subtle particle canvas). Deployment target: **Linux on an old
laptop** (Kali/Debian) — the kernel owns the network path. Docker is a
**fully wired, supported secondary install path** (see [EXISTING_ARCHITECTURE]).

---

## [CURRENT_TECH_STACK]

**Audited 2026-08-19 — pinned vs installed vs latest (2026):**

| Package | requirements-linux.txt | venv (tested) | latest 2026 | Risk |
|---|---|---|---|---|
| fastapi | `==0.141.1` | 0.141.1 | 0.141.1 | none |
| uvicorn[standard] | `==0.52.1` | 0.52.1 | 0.52.3 | LOW (patch) |
| aiosqlite | `==0.22.1` | 0.22.1 | 0.22.1 | none |
| PyYAML | `==6.0.3` | 6.0.3 | 6.0.3 | none |
| pytest | `==9.1.1` | 9.1.1 | 9.1.1 | none (NOTE: was "8.x" — stale) |
| httpx | `==0.28.1` | 0.28.1 | 0.28.1 | MED — see starlette row |
| pyflakes | `>=3.2,<4` | 3.4.0 | 3.4.x | none |
| **starlette** | **NOT pinned (transitive)** | **1.4.1** | **1.6.0** | **HIGH — drifts** |
| pydantic (transitive) | — | 2.13.4 | 2.13.4 | none |

**`[AUDIT 2026-08-19]` THE dependency hazard**: fastapi 0.141.1 declares
`starlette>=0.46.0` with **no upper bound**. A fresh `pip install -r
requirements-linux.txt` resolves **starlette 1.6.0** while the venv/test matrix
was built against **1.4.1** — the tested combination is not reproducible from
the manifest. Also, starlette 1.4.1's TestClient already raises
`StarletteDeprecationWarning: Using 'httpx' with 'starlette.testclient' is
deprecated; install 'httpx2'`. The whole API test layer (~30 TestClient sites
in test_api.py, plus test_web_ui/test_run_wiring/test_packaging) depends on it;
starlette dropping the httpx fallback breaks the suite. Fix is additive: pin
`starlette` and add `httpx2` to test deps. httpx is **test-only** — runtime
uses stdlib `urllib` (updater.py:93, dns_rules.py:241, tun2socks.py).

- Runtime: **Python 3.11** (venv; 3.10+ supported). Docker image builds on
  **python:3.12-slim-bookworm** — both ≥3.10, fine.
- Auth = stdlib `hashlib.pbkdf2_hmac` — **600k iterations** new hashes
  (`salt$iters$dk`), legacy 200k verified + auto-rehashed on login. Hardened
  **2026-08-19**: policy-enforced passwords (`core/passwords.py`: ≥12 chars,
  4 classes, ~250-entry common-password list — weak passwords 400'd on
  change/setup); progressive login backoff (`_LoginLimiter`, api/app.py:113) —
  4 instant 401s, then 429 + escalating Retry-After (1s/2s/4s...) with a
  per-account + per-IP ladder and a 50-fail/5-min **global circuit breaker**
  (lockout 30 min); throttled attempts are rejected, never PBKDF2-processed;
  generic "invalid credentials" for unknown-account AND wrong-password (no
  enumeration); per-install 16-byte salt; **TOTP 2FA** (`quota/totp.py`, RFC
  6238 stdlib, opt-in `/api/totp*`, code NEVER sufficient without the
  password); session token rotated on logout AND password change (stolen
  cookies die); optional **TLS** (`web.tls_certfile/keyfile`) forces
  `secure_cookies`; the factory-default password is flagged
  (`admin_password_default`) and **blocks Strong WAN activation** until
  changed; PPPoE password is **never shipped** to the client (`pppoe_password`
  masked + `pppoe_has_password`). Old 10/300 s per-IP flat limit replaced.
- **Linux only**: `dnsmasq` (DHCP + DNS), `nftables` (client-subnet NAT +
  accounting + hard drop), `tc` (speed shaping). Deps: `requirements-linux.txt`.
- No applicable 2026 CVEs on the pinned set; CVE-2026-48710 (BadHost) does NOT
  apply (starlette 1.4.1 ≥ 1.0.1).

---

## [CURRENT_SYSTEM_FLOW]

The router keeps WiFi + NAT, **DHCP disabled**. Clients join the router's WiFi
but their default gateway + DNS is the gateway box, so every byte crosses it.
The box puts clients on their **own subnet** (`192.168.2.0/24`, gateway = the
box's real address) and masquerades them out the uplink — deterministic, no
proxy_arp (the kernel's proxy_arp refuses same-subnet targets, which would
silently let downloads bypass the box).

1. `scripts/setup_gateway_kali.sh`: `ip_forward=1` + IPv6 off (sysctl); a
   static uplink IP **and** a client-subnet alias on the auto-detected wired
   NIC (Ethernet + carrier, skips WiFi/VPN; verifies the addresses landed);
   dnsmasq (`dhcp-authoritative` so devices migrate off stale router leases
   fast; dual upstreams = router + 8.8.8.8; lease length from `LEASE_HOURS`);
   a masquerade NAT table for the client subnet; systemd drop-ins (dnsmasq
   waits for `network-online.target`; nftables `ExecStop` scoped to
   `quota_nat` only so it never wipes the live app table); a systemd unit for
   the app. **The box's uplink address must be fixed** (script sets it static,
   default `192.168.1.110`; on a VM also reserve it on the router for the
   machine's MAC — a drifting/clashing address is an access outage).
2. `run.py --config config.yaml` starts; every client byte crosses the box.
   Lifecycle (verified `[AUDIT 2026-08-19]`): `main()` loads config →
   `setup_logging` → `Gateway(cfg)` → `asyncio.run(_serve())`. `_serve` calls
   `gateway.startup()` FIRST (DB connect → `_apply_topology_override` →
   TopologyManager → `_seed_bundle_from_cfg` → `ensure_period` → engine start →
   shaper start → dns_manager → vpn/tun2socks + initial `_sync_vpn_share` →
   firewall build (config-gated, `load_config` → `load_geo` → initial `apply`) →
   optional ArpLock/ArpScanner/DnslogTailer/WifiProbe → create the
   `_maintenance_task`), **then** `create_app(...)` (WAN topology manager +
   engine's `_client_net` must exist), then uvicorn. Shutdown cancels the
   maintenance task, then per-subsystem shutdown (engine → firewall watchdog
   cancel → shaper → arp_lock → dnslog → wifi_probe → DB close).
3. **nftables engine** (`quota/nftables.py`): one named counter pair per device
   (`q_up_<ip>` / `q_down_<ip>`, dots→underscores) in the `forward` chain + a
   `blocked` set. The kernel counts at line rate; the app only reconciles and
   reads `nft -j list counters` on a 15 s tick. **Local (LAN) traffic never
   counts**: counter + `@blocked` drop rules exclude both local subnets
   (`engine.client_subnet` / `engine.uplink_subnet`), so a quota-blocked device
   keeps LAN access while its internet is cut. The `blocked` set is rebuilt
   only when membership changes (`_last_blocked_ips` cache) — a same-set
   re-flush every tick would open a short unblock window. App table
   (`inet quota_gateway`) is separate from the setup-owned NAT table
   (`inet quota_nat`). **Restart-safe accounting**: `flush table` keeps named
   counter totals, so `start()` best-effort runs `nft reset counters` and
   `_add_device()` re-seeds the delta baseline from surviving counters —
   otherwise the first drain re-adds the whole old total to `usage_daily`.
4. Every ~15 s the maintenance loop (`_maintenance_tick`, run.py:1103-1264)
   runs 8 sequential job groups (each per-step try/except): 1) `ensure_period`
   (skips when `reset_day=0`) → 1b) `_sync_dnsmasq_leases` (parse lease file,
   prune expired, refuse-list sync) → 1b2) `_collect_interfaces` (`ip -j neigh`
   → `devices.source_interface` NIC tag; text fallback; FAILED rows skipped;
   last-known NIC kept on disconnect) → 1b3) `_wifi_probe_tick` → 1b4)
   `_maybe_latency_tick` (30 s cadence) → 1c) rogue scan (60 s cadence) → 1d)
   `_dns_history_tick` → 1e) events prune (hourly, 30-day) → 1f)
   `_wan_ip_renew_tick` → 1g) `updater.maybe_check()` → 2) **counter drain**:
   `asyncio.to_thread(self.engine.flush)` → per-IP `add_usage` + GATEWAY_MAC
   box usage → 3) `service.evaluate_blocks()` → 4) `service.snapshot_state()`
   → `asyncio.to_thread(engine.update_state, …)` + `set_gateway_blocked` →
   `holder.swap(EngineSnapshot(...))` → 5) `_sync_shaping` → 6) `_sync_dns_rules`
   → 6b) `_sync_refuse_fragment` → 7) `_sync_vpn_share` → 8) `firewall.reconcile`
   (signature-gated re-apply + scan-watch/fw_* counter drain). A tick > 1.0 s
   logs a warning; exceptions are swallowed per job.
   **Router-side WiFi/LAN label** (`quota/latency_probe.py`, ON by default, ANY
   hardware): the box raw-ARPs every leased client and times the replies —
   wired answers in well under a millisecond, WiFi pays airtime (≥1 ms), so the
   FASTEST sample decides `WiFi`/`LAN`; raw AF_PACKET backend with a `ping`
   parse fallback; interleaved send/drain rounds (power-save devices wake and
   still get sampled); consecutive-sweep streak guard (`min_consistent`)
   prevents flapping, a device that stops replying keeps its label. The
   responder set drives the **device-card LED** (`connected` = answered the
   latest sweep, fresh ≤3×interval; lease-based fallback when the probe isn't
   running) — a leased-but-silent device goes grey, since dnsmasq keeps the
   lease for LEASE_HOURS. When the box HAS a monitor-capable card, the passive
   probe (`quota/wifi_probe.py`, airmon-ng + airodump-ng on a dedicated thread,
   OFF by default) takes precedence and adds the exact SSID. The manual
   per-device pin (`POST /api/devices/{id}/access`) always wins the display.
   Every 60 s a **rogue LAN scan** (`quota/arp_scan.py`) raw-ARP-probes both
   subnets; active hosts NOT in the lease file surface in `rogue` (+ `warning`
   event).
5. A blocked device: IP added to the `blocked` set → kernel **drops** its
   forward-chain packets — hard internet cut at line rate. Admin toggles work
   the same way.
6. **Speed shaping** (`quota/shaping.py`, Linux only) is a second kernel-side
   stack that never touches nftables: `TcShaper` reconciles an **HTB +
   fq_codel** tree on the single NIC. Uploads (client→internet) are redirected
   at NIC **ingress** into `ifb0` and shaped by `ip src`; downloads are shaped
   at NIC **egress** by `ip dst`. Per-device leaves under per-user classes
   (capped at the user's aggregate) under a download aggregate under a root
   capped at the **real line speed** — effective cap `min(dev, user)`; the
   default class is capped at the direction total (NOT a pass-through). Tree
   rebuilt only on a signature change of (enabled, totals, aqm, sorted caps);
   `totals` of 0.0 are intentionally excluded from the signature. **LAN
   pass-through**: client↔uplink-subnet and client↔box traffic rides a
   **prio-1 class `1:99`** at the LAN link rate (`shaping.lan_rate_mbps`,
   default 1000; NEVER the WAN cap); priorities are deliberately non-zero (tc
   treats `prio 0` as "no priority").
6b. **Kernel firewall** (`quota/firewall.py`, the separate `inet quota_firewall`
   table) is a third kernel-side stack layered **BEFORE** the quota engine:
   `fw_input`/`fw_forward` (`type filter hook input/forward priority -100`,
   engine is 0 — firewall-denied traffic never reaches the quota counters, and
   quota cuts still apply afterward) + `fw_dnat` (WAN mode, `nat prerouting
   priority -100`). Sets: `fw_bans` (`interval, timeout` — auto-expiring CIDR
   bans), `fw_scan_watch` (`dynamic, timeout 60s, counter` — port-scan
   detector), `fw_allow`/`fw_deny` (deny > allow). **Posture is derived from
   the deployment topology at render time, never stored**: LAN = permissive-out
   (explicit blocklist + bans + custom rules + box SYN-flood guard); WAN adds a
   **default-deny for NEW inbound on ppp0** (input + forward) — the ppp0 drops
   are placed BEFORE the SYN flood guard so external SYNs never hit the
   rate-limit first — and the dashboard port is NEVER exposed on ppp0 without
   the explicit `wan_confirmed` opt-in. Port-forwards + DMZ are WAN-only (API
   409 in LAN mode). **Safe
   apply**: `sanitize` refuses lockout configs (an unconditional input deny of
   the client subnet = dashboard lockout; an unconditional forward deny = whole
   household cut; a deny_cidr overlapping the box/probe/client subnet is
   dropped), snapshots the ruleset + config (`data/firewall_snapshots/` +
   `firewall_last_good` DB setting = the config that was GOOD BEFORE the apply),
   programs, then a **watchdog task** re-verifies management reachability after
   `watchdog_seconds` (default 45) and **auto-reverts to last-good**. Bans
   (manual, login brute-force past `brute_force.threshold`, port-scan past
   `scan_detect.syn_threshold`) land in `@fw_bans` with kernel timeouts +
   DB events; counter-driven drops are **aggregate per rule** (no per-source IP
   except app-initiated bans) and surface in the Firewall log tab via named
   counters drained each tick. Every named counter (`fw_deny_drop` etc.) is
   DECLARED via `add counter` before any rule references it (real nft errors
   "No such file or directory" otherwise — same contract as the engine's
   `_add_counter`). No nft `log` statements (dmesg-flood avoided).
   The WAN-transition pre-apply (`run.py:_firewall_wan_preapply`) programs the
   TARGET posture before `netmgr.apply` brings ppp0 up — no exposure window.
   Degrades gracefully: no nft/root => `available=False`, every kernel op no-op.
7. FastAPI + uvicorn serves the dashboard + REST API + `/ws` push (5 s
   snapshots). **`[AUDIT 2026-08-19]` fanout is correct**: one shared
   `_push_loop` builds ONE `_dashboard_payload()` per 5 s for ALL sockets
   (`app.py:1714-1724`); WS handshake is HMAC cookie-auth (4401 on bad token),
   10 s ping keepalive; client polls `/api/dashboard` every 10 s as a fallback.
   **API-surface + anti-replay hygiene (2026-08-19)**: FastAPI auto-docs
   (`/api/docs` Swagger + `/api/openapi.json`) are OFF by default
   (`web.docs_enabled: false` — the schema is a structured endpoint map a
   WAN-facing attacker mines in seconds); every DATA response (`/api/*`,
   `/report`, `/milestone`, `/`) is stamped `Cache-Control: no-store` so the
   browser never persists MACs/usage/history/log-tails to cache or disk on a
   shared machine (`Pragma: no-cache` + `Expires: 0`; `/assets/*` stays
   cacheable); `X-Robots-Tag: noindex, nofollow` + `web/robots.txt` keep
   crawlers out. The `_dashboard_payload` deliberately ships NO raw events
   list or log tail (both served on demand: `/api/logs`; the report page
   contract still carries them — pinned by test_report_gated_by_source_ip).
8. **ARP gateway-lock** (`engine.gateway_arp_lock`, OFF in config.yaml but ON
   in the setup-generated config): a device that sets a static IP + the ROUTER
   as its gateway bypasses the box at L2 entirely. The lock: a raw-socket
   responder (`quota/arp_lock.py`) claims the router's IP on the CLIENT subnet,
   an `arp`-family nftables rule drops the router's competing replies, and a
   `forward` deny drops any client-subnet source NOT in the `known_ips` set (=
   leased DHCP IPs). Self-sustaining (dropped traffic re-ARPs and is
   re-answered). Uplink-subnet hosts keep the real router; a static ARP entry
   or an uplink-subnet static IP still evades capture (surfaced as a rogue).
   `known_ips` rebuilt only on membership change.
9. **Strong (WAN) mode** (`engine.topology=wan`, optional, OFF by default): the
   box dials the PPPoE line itself (`quota-wan-ppp.service` runs
   `pppd call quota-wan nodetach` — pppd must NOT daemonize or systemd
   kill-loops it; public IP lands on `ppp0`; creds in `/etc/ppp/{chap,pap}-
   secrets`, chmod 600) and the router is demoted to a pure bridge/AP. **The
   dashboard WAN tab applies the switch LIVE** (`quota/netmgr.py`
   `TopologyManager`): collects PPPoE creds, rewrites config.yaml + the DB
   setting TOGETHER (`topology_source=dashboard` + `topology` — never one
   without the other, the v18 revert bug), runs `scripts/topology.sh` (creds
   via the ENVIRONMENT never argv), schedules a detached self-restart; applier
   failure rolls config + DB back; "Revert to LAN" restores from `dhcp.lan_*`
   + `engine.lan_gateway_arp_lock` snapshot keys. Under wan the box KEEPS the
   uplink IP as a secondary router-admin alias, so the uplink subnet stays
   LOCAL (router-admin traffic never consumes quota). ARP gateway-lock forced
   off; rogue scanner probes only the client subnet; `quota/topology.py`
   `detect_ppp` reports ppp0 state into `wan_status` — **judged by its
   negotiated IPv4, never by operstate**. WAN tab also has throwaway **Test
   PPPoE** (`scripts/test_pppoe.sh`, unit ppp200) + **Restart PPPoE /
   auto-renew** (interval clamped to 5-min floor, default 15; 409 while ppp0
   is down).
10. **Per-device browsing history** (`quota/dnslog.py`, ON by default): the
    setup script installs an **app-owned dnsmasq fragment**
    (`/etc/dnsmasq.d/quota-dnslog.conf` — `log-queries=extra` + `log-async=20`
    + `log-facility=/var/log/quota-dnsmasq.log`) and **enables `conf-dir=` in
    `/etc/dnsmasq.conf`** (dnsmasq otherwise silently ignores every fragment —
    the live-box empty-History-tab bug) + a logrotate snippet (copytruncate,
    5M, rotate 3). A dedicated tailer thread (`DnslogTailer`) polls every
    0.5 s, strips `\x00` sparse holes, caps the partial-line buffer at 1 MB,
    and pushes parsed `(minute, ip, domain)` events onto a **bounded queue —
    overflow drops lines, never blocks DNS or the event loop**. Parser accepts
    both the bare shape and the verbose `1 192.168.2.186/16773 query[A] …`
    shape (dnsmasq ≥2.90). Each tick drains into `dns_history`, persists the
    read cursor (`dnslog_state`), and prunes **per user** at their
    `history_days` (NULL = global `history.retention_days`).

**Quota model (per user)**: the monthly allowance lives on a **user**
(`users` table; `devices.user_id`), not a device. Auto users equally share the
bundle remainder after fixed users take their GB off the top; a user's usage =
Σ their devices' usage. The cut is **resolved** at render/enforcement time
(`service.resolve_device_state`), never written to device rows. Precedence:
**user admin > device admin > user quota (unless per-device `bypass`) > ok**;
mac_lists add **deny > allow** above it all. New DHCP devices auto-create their
own user (one device ⇒ one user) in the **DISABLED onboarding lock**
(`users.quota_mode="disabled"`, 0 GB): claims NO share, always quota-blocked
until the admin assigns shared/fixed. **STOP NEW CONNECTIONS** and
**Decline random MACs** refuse at the DHCP level instead — the MAC is written
to a persisted refuse list (`stop_new_refused_macs` / `decline_random_refused_macs`,
each gate's own) and to the app-owned dnsmasq fragment (`dhcp.ignore_file`,
one `dhcp-host=<mac>,ignore` line each; `dnsmasq --test` gate + restart), then
run.py returns WITHOUT registering a device; the just-issued lease stays
kernel-cut via `snapshot_state`'s row-less pass until it expires. Fragment
unwritable → graceful fallback to the legacy registered + admin-blocked path.
`decline_random` requires BOTH the locally-administered bit AND an empty vendor
lookup (`vendor_for(mac) == ""`) — legacy products with registered local OUIs
(3COM 02:c0:8c, DEC aa:00:00, Olivetti 02:aa:3c) are never classified random.
Guest mode unchanged (mints + admin-block at the cap; lowering the cap cuts the
NEWEST over-cap guests immediately, oldest stay).

**Bundle source**: `config.yaml` is the default source of truth for
`bundle.total_gb` / `bundle.reset_day` / `bundle.period_type`, re-applied on
every startup (`_seed_bundle_from_cfg`). Once the admin edits via the dashboard
(`POST /api/bundle`), `bundle_source=dashboard` and config.yaml stops
overriding.

**Bundle type (`bundle.period_type`)**: `renew_day` (default) resets on
`reset_day` (0 = never auto-reset); `end_of_month` is the ISP's month-end bill —
the configured day drives the reset too, day 0 falls back to the calendar end —
all via `Bundle.effective_reset_day` (day range 0-31).

**Period math (fixed 2026-08-17)**: `timeutil.period_bounds` returns the period
**containing now** — before this month's reset day the current period began
last month on the reset day. `ensure_period` rolls when the recorded
`period_end` has passed, never by comparing `period_start` against the grid — a
mid-month reset-day change re-anchors `period_end` without rolling or zeroing
usage.

**Electric-cut fallback (optional)**: the router keeps a small non-overlapping
DHCP pool (gateway = router) on the uplink subnet while dnsmasq serves only the
client subnet — devices fall back to direct internet during a gateway outage.
Trade-off: fallback-leased devices are not counted/controlled while the gateway
is down.

**Packaging + releases**: the `.deb` is built **only** by GitHub Actions —
`.github/workflows/release.yml` renders `packaging/DEBIAN/control` from
`quota/version.py` (single source of truth; a `v*` tag must match or the
workflow fails loudly), stages into `/opt/quota-manager`, runs
`dpkg-deb --build --root-owner-group`, uploads to GitHub Releases. `postinst`
builds the venv + runs `setup_gateway_kali.sh` with `QUOTA_NO_APT=1` +
enables/starts `quota-gateway`; `prerm` stops/disables; both idempotent,
preserving `/etc/quota-gateway/config.yaml` + `/var/lib/quota-gateway/quota.db`.
**`apt-repo.yml`** turns each Release into a **signed apt repo** (`workflow_run`
on `release` + dispatch backfill): imports the private key from the
`APT_REPO_GPG_KEY` secret, signs `Packages`/`Release`, pushes to `gh-pages`
hosted at https://UserJoo9.github.io/QuotaManager/. Public key at
`quota-manager.gpg`; `tests/test_packaging.py` pins the whole contract.
**`[AUDIT 2026-08-19]` ALSO present**: `ci.yml` (pytest + node + docker-build-test)
and `docker-publish.yml` — two workflows the old map didn't list.

**`[AUDIT 2026-08-19]` Docker install path IS wired, not orphan**: `Dockerfile`
(python:3.12-slim), `docker-compose.yml`, `DOCKER_DEPLOYMENT.md`,
`.dockerignore`, `.env.example`, `scripts/docker-entrypoint.sh` (full parallel
gateway bootstrapper: NAT + dnsmasq + dnslog config), `scripts/docker-systemctl-shim.sh`,
`.github/workflows/docker-publish.yml`. No Python code imports Docker (only a
`core/config.py:455` docstring). Docker lacks WAN/PPPoE mode support. `data/`
mount (`./data:/var/lib/quota-gateway:rw`) in compose; `data/` is gitignored +
empty on dev.

---

## [EXISTING_ARCHITECTURE]

**Paradigm verdict `[AUDIT 2026-08-19]`: "layered façade with god objects" — a
Composition-Root Monolith.** One process, three fat god classes + one god
method + one god file. Directional discipline `api → quota → core` is nominally
respected and the kernel hot path has **no Python at all** (kernel counts +
drops), but behavior concentrates in:

| Object | Size | Evidence |
|---|---|---|
| `run.py` `Gateway` | 1,910 lines, ~40 methods | `__init__` ~180 attr/lock lines (254-431); `_maintenance_tick` (1103-1264) = 15+ jobs |
| `quota/db.py` `Database` | 1,418 lines | schema + 15 inline ALTER migrations + backfill + seed + 70+ CRUD/settings/events methods |
| `api/app.py` `create_app` closure | 2,244 lines, 64 routes + 1 WS | `_dashboard_payload` (483-624), `_device_view` (393-402), ~20 injected callbacks (255-274) |
| `quota/service.py` `QuotaService` | 999 lines, 50+ methods | quota math AND shaping/VPN/WAN/guest/mac-list settings (754-908) |
| `quota/firewall.py` `FirewallManager` | 1,310 lines | separate `inet quota_firewall` table + safe-apply/watchdog + bans/scan/geo |
| `web/assets/app.js` | 2,622 lines | 102+ functions, 71+ listeners, 17 `innerHTML` rebuilds, ~30 panels |

**Actual coupling violations:**
1. `api/app.py` imports `quota.dns_rules` (line 40) and runs renderer logic —
   `normalize_pattern` (67, 811, 1211, 1263), `compile_source_text` (1283),
   `fetch_preset` (1326). Presentation layer does provisioning work.
2. `quota/db.py` imports the type hub: `from quota.engine import GATEWAY_MAC`
   (line 35) — a bottom-up storage→types dependency.
3. `quota/service.py` persists shaping totals (754-777), the VPN switch
   (788-811), and WAN PPPoE renew schedule (830-874) — network-provisioning
   settings live in the "quota domain service".
4. `run.py` is composition root AND business orchestrator — the entire product
   cadence is one method.
5. Hand-rolled DI: `create_app` takes ~20 optional callables, wired explicitly
   in run.py:1811-1828; every new kernel toggle added a callback by copy-paste.

**Structure map (verified `[AUDIT 2026-08-19]`):**
```
QuotaManager/
├── CLAUDE.md                 <- this file (SYSTEM MAP / audit)
├── README.md / README_AR.md  # end-user docs
├── Structure_README.md       # developer docs (drifts from code — treat as aspirational)
├── LICENSE / CHANGELOG.md
├── quota-manager.gpg         # armored PUBLIC key for the signed apt repo
├── quota-manager-secret.asc  # [AUDIT] UNENCRYPTED PRIVATE PGP key, gitignored, on dev box only
├── config.yaml               # Linux gateway settings (dnsmasq + nftables + gates)
├── run.py                    # Composition root + orchestrator (Gateway, _maintenance_tick)
├── requirements-linux.txt    # pinned runtime + test deps (starlette NOT pinned — see TECH_STACK)
├── Dockerfile / docker-compose.yml / DOCKER_DEPLOYMENT.md / .env.example / .dockerignore
├── .github/workflows/        # release.yml, apt-repo.yml, ci.yml, docker-publish.yml
├── packaging/DEBIAN/         # control.template, postinst, prerm
├── core/                     # clean foundation, zero upward deps
│   ├── config.py (575)       # config.yaml -> typed Config dataclasses (incl. FirewallConfig + WebConfig + WafConfig)
│   ├── logging_setup.py (118)# QueueHandler -> writer thread -> rotating file (5MB x3)
│   ├── passwords.py (139)    # admin password policy: ≥12 chars, 4 classes, ~250 common-password list
│   └── timeutil.py (95)      # month-boundary math (zoneinfo)
├── quota/                    # kernel + domain + provisioning subsystems
│   ├── engine.py (111)       # THE TYPE HUB: EngineSnapshot/SnapshotHolder + GATEWAY_MAC
│   ├── db.py (1418)          # god storage file: schema + migrations + CRUD + settings + events + seeding
│   ├── service.py (999)      # quota math + block fan-out + settings grab-bag
│   ├── nftables.py (892)     # kernel counters + blocked set + ARP-lock denies + gw_allowed
│   ├── shaping.py (648)      # TcShaper (HTB + fq_codel, single-NIC two-tree, LAN pass-through)
│   ├── firewall.py (1310)    # separate inet quota_firewall table: safe-apply watchdog + bans/scan/geo
│   ├── vpnshare.py           # VpnShareManager (policy routing via ip rule table 200)
│   ├── tun2socks.py          # auto-provisioner (pinned v2.7.0 + sha256, SOCKS probe)
│   ├── totp.py               # opt-in TOTP 2FA (RFC 6238, stdlib: hmac + base32)
│   ├── arp_scan.py (301)     # rogue static-IP detection
│   ├── arp_lock.py (157)     # ARP gateway-lock responder (raw socket thread)
│   ├── latency_probe.py (189)# ARP-RTT WiFi/LAN classification (ON by default)
│   ├── wifi_probe.py (262)   # passive monitor-mode SSID labels (OFF by default)
│   ├── dnslog.py             # DNS history tailer + dns_history persistence
│   ├── dns_rules.py          # DnsRuleManager (host filtering -> dnsmasq conf)
│   ├── topology.py           # detect_ppp / restart_pppoe / check_internet
│   ├── updater.py            # GitHub self-update (version compare + .deb install)
│   ├── netmgr.py             # TopologyManager (live LAN/WAN switch + rollback)
│   ├── vendor.py + oui.txt   # MAC OUI -> manufacturer (53.5k prefixes)
│   └── version.py            # single source of truth for release version
├── api/
│   ├── app.py (2244)         # FastAPI factory: 64 REST + /ws + static mount + milestone + report (incl. /api/firewall CRUD/ban/revert/geo/log) + WAF/CSRF/security-headers middleware + auth hardening (login limiter, TOTP, session rotation, default-pw WAN gate)
│   ├── waf.py (180)          # embedded request-level WAF: SQLi/XSS/cmdi/path signatures, scanner UA, per-endpoint rate limits, auto-ban state (mode auto = strict WAN / log LAN)
│   └── schemas.py            # pydantic request models (24 + firewall + totp models)
├── web/
│   ├── index.html / milestone.html / report.html
│   └── assets/ styles.css + app.js (2622, no schema check on payload)
├── scripts/
│   ├── setup_gateway_kali.sh # sysctl, NAT, dnsmasq, dnslog fragment, systemd, Docker-ish? no
│   ├── topology.sh           # runtime LAN/WAN applier (env-fed)
│   ├── test_pppoe.sh         # throwaway dial (ppp200)
│   ├── update_oui.py         # regenerate oui.txt from IEEE
│   ├── replay_nft_startup.sh # debug reproduction of startup nft sequence
│   ├── docker-entrypoint.sh  # Docker bootstrapper (NAT + dnsmasq + dnslog config)
│   └── docker-systemctl-shim.sh
├── docs/                     # [AUDIT] screenshots only (favicon, dashboard.png) — NOT docs
├── data/                     # [AUDIT] gitignored, empty on dev; db_path default "data/quota.db" (config.py:420)
└── logs/                     # [AUDIT] gitignored runtime artifact (quota.log, rotating)
```
Tests: 24 files (test_run_wiring.py 122 KB, test_api.py 103 KB, test_quota_service.py
73 KB are the giants pinning the god-object seams); 644 passed (608 at v0.2.1
+36 firewall: tests/test_firewall.py — fake-nft gate over the 9 acceptance
sections; +31 security: tests/test_security.py — auth core / WAN gate / TOTP /
CSRF / WAF / SSRF / limiter units / docs-off / no-store / payload minimization).

**`[AUDIT 2026-08-19]` verified orphans / decoupled:**
- `core/config.py:311` `preset_cache_dir` (DnsFilterConfig + config.yaml:227) —
  **documented but dead**: `dns_rules.fetch_preset` (245) fetches fresh each
  time, no disk cache anywhere.
- `quota-manager-secret.asc` — unencrypted PGP **private** key on the dev box
  (gitignored; the CI path uses the `APT_REPO_GPG_KEY` secret). Residual
  exposure, not a repo breach.
- `docs/` and `logs/` — not runtime or packaging payload.
- `scripts/replay_nft_startup.sh` + `update_oui.py` — dev/diagnostic tools,
  not wired into CI/release.
- `Structure_README.md` drifts from the code (aspirational dev docs; only
  referenced from a comment in dns_rules.py:474).

**Proposed bounded-context mapping (for the future refactor):** Quota domain
(service.py quota half + timeutil + vendor); Persistence (db.py storage half);
Network kernel layer (nftables, shaping, arp_*, latency/wifi probe, engine
types); Provisioning/topology (netmgr, topology, dns_rules, dnslog, scripts);
Connectivity/relay (vpnshare, tun2socks); App/infra (api, web, updater, core).
Straddlers to split: `quota/service.py`, `quota/db.py`, `api/app.py`, and
`run.py` (which straddles every context).

---

## [LEGACY_DEBT_AND_RISKS]

_Inventory the refactor phase should address before breaking changes land.
Items from the 2026-08-08/10 audits marked ✔ are fixed. Items marked
`[AUDIT 2026-08-19]` are NEW findings from this audit._

**Dependencies `[AUDIT 2026-08-19]` (the only real dependency hazards):**
- **starlette UNPINNED** — fresh installs resolve 1.6.0 vs tested 1.4.1 (see
  [CURRENT_TECH_STACK]). Pin it.
- **TestClient httpx→httpx2 deprecation** — starlette 1.4.1 warns today; the
  whole API test layer (~30+ sites) breaks when the httpx fallback is dropped.
  Add `httpx2` to test deps.
- Docker inherits the same starlette drift (no extra pinning).

**Dead code (verified `[AUDIT 2026-08-19]`):**
- Test-only, zero production callers: `quota/db.py` `set_lease` (:972),
  `get_usage` (:1038).
- **CORRECTION to old map**: `get_device_by_ip` (:669) and `get_ip_for_mac`
  (:952) are **now PRODUCTION** (run.py:1022/1184, app.py:567/629) — the old
  "test-only" claim is stale. `add_topup_user` (:838) is production
  (service.py:925).
- Confirmed removed ✔: `/api/usage`, `/api/usage/{id}`, `/api/events`,
  `add_topup`, `has_bundle`, `is_blocked`, `is_admin_blocked`,
  `get_usage_series`.
- `preset_cache_dir` config key: **dead** (see Orphans above).

**Known open defects (root-cause located, NOT fixed):**
- **Per-device block can silently not cut a lease-less device** — kernel
  `@blocked` is keyed by IP from lease rows; a lease-less device gets `ip=""`
  → never blocked. Only cover is the ARP-lock `known_ips` deny (OFF by
  default, forced OFF in WAN). Matches "per-device block not working".
- `/report` is default-ON for the whole client subnet (rogue static-IP device
  reads full household usage + log tail with no session; gate is sound —
  documented "trusted LAN" assumption).
- **`[AUDIT 2026-08-19]` db.py migrations swallow ALL exceptions** —
  `connect()` (364-515) runs a dozen `try: ALTER except Exception: pass`
  blocks. Correct for "column already exists", but a migration that raises for
  a non-duplicate reason silently continues on a half-migrated DB. Re-run
  safety is tested; failure visibility is nil.

**Security `[AUDIT 2026-08-19]` (prioritized):**
- **HIGH — default admin credential**: `api/app.py:181` —
  `os.environ.get("QUOTA_ADMIN_PASSWORD", "admin")`. If the env var is unset
  the admin password is literally `admin` (and persisted). On a WAN-facing box
  this is the single most exploitable finding. Fix: force a random password +
  print once on first boot, or refuse to run with the default.
- **MEDIUM — updater .deb install has NO checksum/signature verification**:
  `quota/updater.py:335-363` downloads from GitHub Releases and runs
  `apt-get install` (transient systemd unit) on the raw file — unlike
  tun2socks (pinned v2.7.0 + sha256), there is no digest check.
- **MEDIUM — session cookie has no `secure=True`** (app.py:1647-1648; httponly
  + samesite=lax only). Fine on LAN HTTP; a hijack vector behind
  HTTPS-terminated WAN.
- **MEDIUM — unencrypted PGP private key on dev disk** (`quota-manager-secret.asc`).
  Delete or encrypt locally; CI uses the GitHub secret.
- Verified fixed/OK: `/api/milestone/notify` IP-ownership gate ✔; PBKDF2 600k +
  legacy rehash ✔; login rate limit ✔; CVE-2026-48710 not applicable ✔;
  tun2socks pinned download ✔.

**Simplicity debt (the violations to address — ranked `[AUDIT 2026-08-19]`):**
1. **CRITICAL — 3 sources of truth for bundle & topology** (config.yaml + DB
   row + ownership flag), written in lockstep by config dataclass +
   `bundle_config`/`settings` tables + `netmgr.render_config` (255-329). Every
   new knob touches 4+ files. (Old map: "2 sources"; the flag makes 3.)
2. **CRITICAL — topology state written by THREE writers** (`netmgr.render_config`,
   `scripts/topology.sh`, `scripts/setup_gateway_kali.sh` — near-identical
   dnsmasq heredocs at setup:423-468 vs topology.sh:164-208). The bug class
   behind the v18 revert + v19 creds-wipe.
3. **HIGH — two on/off switches for shaping** (config `shaping.enabled`
   decides whether TcShaper is even BUILT, run.py:145-154; DB `shaping_enabled`
   is the runtime master, run.py:1281). A config-only box builds no shaper; a
   DB-only toggle silently does nothing.
4. **HIGH — WS wire format unversioned + user aggregates duplicated per
   device**: `_device_view` (app.py:337-402) embeds allowance/used/percent/
   mode on every device (~30 keys/device) while `user_views` nests full device
   views (462, 486) — N+1 serialization. app.js (2,334 lines) consumes it blind
   with full `innerHTML` rebuilds (17 sites); a key rename = silent UI break,
   not a compile error. Also no delta projection / schema versioning.
5. **HIGH — `quota/db.py` does everything** (schema + migrations + CRUD +
   settings + events + seeding + backfill) and **untyped dicts cross
   service/API boundaries**: `get_period_usage_by_user` etc. return bare dicts
   (service.py:291, 348; app.py:420, 430) — a dict-key change is invisible to
   type checkers.
6. **HIGH — `run.py:_maintenance_tick` god method** (1103-1264) + the
   `_sync_*` family (shaping 1266, dns_rules 1358, refuse_fragment 1403,
   vpn_share 1474) with the `_shaping_lock`/`_dns_lock`/`_vpn_lock` trio
   (340-347).
7. **MEDIUM — `QuotaService` settings grab-bag** (quota math + guest + shaping
   + VPN + WAN-renew + mac-lists in one 999-line class).
8. **MEDIUM — hand-rolled DI** in `create_app` (~20 callbacks, wired by
   copy-paste at run.py:1811-1828).

**Performance (audited `[AUDIT 2026-08-19]`):**
- **Tick cost**: steady state 2-4 subprocesses/tick (`nft -j list counters`,
   `ip neigh`, cache-gated no-op checks); first boot / state change: 70-115+
   `tc` + ~80 `nft`. Heavy work is off-loop via `asyncio.to_thread` (verified
   at all sites ✔). Remaining ON-LOOP costs: ~30+ unbatched DB statements/
   commits per tick (add_usage per device + set_setting + set_device_state +
   evaluate/snapshot reads); `_sync_vpn_share`'s `ip`/`ss` subprocesses and
   `_sync_dns_rules`'s dnsmasq restart are NOT to_thread'd (rare in steady
   state).
- **WS**: fanout-correct (one payload/5 s shared), but payload + DOM grow
   linearly with device count; no schema versioning.
- **DB growth**: events pruned hourly (30-day) ✔; dns_history pruned per-user
   hourly ✔; **`[AUDIT 2026-08-19]` NO `VACUUM`/`wal_checkpoint` maintenance
   anywhere** — the DB file + WAL grow monotonically (prunes don't shrink the
   file).
- **Timing telemetry**: exactly ONE measurement — the >1.0 s tick warning
   (run.py:1085-1093). No per-substep timing, no event-loop lag, no queue-depth
   telemetry. Cannot attribute which subsystem is slow.

**Logging `[AUDIT 2026-08-19]`:**
- Non-blocking queue logger (QueueHandler → 5000-record drop-on-full queue →
  1 writer thread → 5 MB × 3 rotation; console at INFO/ERROR, file at DEBUG) —
  architecture is sound. **Format is plain-text, unstructured**
  (`%(asctime)s %(levelname)s %(name)s: %(message)s`): no JSON, no correlation
  IDs, no device/IP in machine-parseable form, no per-request/per-tick fields.
  The `events` DB table is the only structured trace. A live-box diagnosis
  ("gate does nothing on the box") requires manually cross-referencing plain
  timestamps. Log-call distribution: run.py 50, nftables 12, dns_rules 10,
  vpnshare 9, netmgr 7, dnslog 6, shaping/arp_scan/arp_lock/tun2socks 4 each;
  **core/config.py, core/timeutil.py, quota/engine.py, quota/topology.py log
  zero** (pure logic). `docs/` has no documented log formats.

**Top break points for the pending breaking change (blast radius order
`[AUDIT 2026-08-19]`):**
1. **`quota/engine.py` `EngineSnapshot` (type hub)** — consumed by
   run.py:1173-1241, app.py:328/416-554, nftables.py:63, arp_scan.py. A field
   rename ripples through the kernel engine, orchestrator, API serializers,
   and ~10 test files at once.
2. **`api/app.py` `_dashboard_payload` + `_device_view` wire format** — ~30
   keys/device, user aggregates duplicated, NO schema versioning; app.js
   consumes it blind. A rename = silent UI break.
3. **`quota/db.py` dataclasses ↔ `SCHEMA` ↔ inline migrations ↔ plain-dict
   query results** — an entity change means editing the dataclass (67-204),
   `SCHEMA` (206-354), a try/except ALTER (383-511), and every SQL column list;
   dict-key changes are invisible to type checkers.
4. **`run.py:_maintenance_tick` + the `_sync_*` family** — the only place the
   15 s cadence is defined; every subsystem lifecycle lands here.
5. **config.yaml ↔ core/config.py ↔ DB settings ↔ ownership flags** — every
   bundle/topology knob is a transaction across config + run.py seeding +
   netmgr.render_config + api/schemas + JS.

**Top test weight asymmetry (refactor constraint):** test_run_wiring.py
(122 KB), test_api.py (103 KB), test_quota_service.py (73 KB) pin the current
god-object seams with fakes — a refactor must keep these green or they become
the regression net that blocks the breaking change.

---

## [KNOWN LIMITS] (honest)
- Counting is approximate ("≈" in the UI) — counters read every ~15 s, so the
  live split lags and bytes are attributed to the device that owned an IP at
  drain time. No throttling — exceeded devices are hard-blocked (kernel drop),
  never throttled.
- **Root required**: nftables + dnsmasq (udp/53 + udp/67) + tc all need root;
  the systemd service and postinst run as root, so only a manual foreground
  run needs `sudo`.
- Subsystems degrade gracefully: no `nft`/root => no counting (DB usage still
  shown); no dnsmasq => no DHCP/DNS; no `tc`/`ifb` => no shaping. Service is
  `Restart=always` + systemd.
- **Electric-cut fallback is a liveness trade-off**: devices holding a router
  fallback lease during a gateway outage are not counted/controlled.
- **IPv4 only**: IPv6 RAs come straight from the router and never cross the
  gateway; the ROUTER's IPv6/RA must be disabled too.
- **Static-IP bypassers are denied, not magically fixed**: the ARP gateway-lock
  cuts router-gateway static-IP devices, but a static ARP entry or an
  uplink-subnet static IP still evades it (surfaced as rogue). Router MAC
  filtering / client isolation / Strong (WAN) mode are the durable complements.
- **Strong (WAN) mode needs the router hands-on**: applying WAN while the
  router isn't actually bridged cuts internet until it is. A PPPoE outage takes
  internet down until ppp0 redials.
- **Speed shaping needs real line rates**: set the Network-tab totals to the
  real line down/up. `tc` rates are approximate; the single-NIC egress tree
  shares bandwidth between uplink traffic and client downloads.
- **The ARP-RTT WiFi/LAN label is statistical, not measured**: a fast 5G device
  can read LAN (lower `threshold_ms`), a loaded 2.4 GHz network can spike both
  classes, ICMP-blocking clients are unclassified without root. The streak
  guard kills flapping; the label only drives the display chip — enforcement
  never depends on it.
- **The box's own internet is metered by default** (`engine.count_gateway`,
  default ON): box traffic is charged to the protected Gateway user (fixed
  1.0 GB), silently deducted from every auto-share bundle. A Gateway block cuts
  the box's own internet only.
- **/milestone is public and /report is source-IP-gated, not session-gated**:
  /report (any client-subnet source or `report.allowed_ips`) shows full
  household usage + events + log tail with no admin login. Both assume a
  trusted LAN — keep the dashboard port LAN-only.
- **LAN mode needs a fixed uplink address on the box**: router DHCP reservation
  or a static address (setup sets `192.168.1.110` and verifies it). Not an
  issue in WAN mode.
- **Docker is not WAN-capable** and carries no extra version pinning (shares
  the starlette drift).
- **`[AUDIT 2026-08-19]` default admin password is `admin`** when
  `QUOTA_ADMIN_PASSWORD` is unset — change it on any non-ephemeral deployment.
  Mitigated 2026-08-19: the `admin_password_default` marker blocks Strong WAN
  activation and drives the dashboard security banner until it's changed.

---

## [VERSION HISTORY] (headlines + gotchas; full detail in CHANGELOG.md)
- **v0.3.0 (2026-08-19)** — security hardening pass (password policy, login
  limiter, session rotation, TOTP 2FA, embedded WAF, CSRF guard, security
  headers, default-password WAN gate, PPPoE masking, SSRF allowlists), firewall
  rule-ordering fix, notification center, firewall/port-forward modal forms,
  one-click HTTPS with rollback, HTTPS endpoint bug fixes, API anti-replay
  hygiene (`Cache-Control: no-store`, `X-Robots-Tag`, docs off by default),
  README simplification. Suite 640 pass. Built on the audit at commit `621b200`.
- **2026-08-19 (fix)** — **HTTPS endpoint hardening** (three bugs): (1)
  `key_path.chmod(0o600)` ran before `openssl` created the key file —
  `FileNotFoundError` on first-time HTTPS enable. Moved chmod to after cert
  generation. (2) `resolve_config_path()` without arguments resolved to the
  project-root `config.yaml`, not the production path (`--config
  /etc/quota-gateway/config.yaml`). The enforce/remove endpoints updated the
  wrong file and restart loaded the unchanged config. Fixed by resolving the
  path from the running `topology_manager.config_path`. (3) `remove-https`
  used `data.get("web") or {}` — a missing `web:` key produced a throwaway
  dict; `secure_cookies: false` was never written. Fixed with `setdefault`.
  Also added `GET /api/security/tls` status endpoint.
- **2026-08-19 (fix + feature)** — **Firewall rule-ordering fix**: in
  `render_commands`, `iifname "ppp0" ct state new drop` was placed AFTER the
  SYN flood guard (`limit rate 10/second burst 20 packets accept`) in both
  `fw_input` and `fw_forward` chains — external SYNs hit the rate-limit first
  and were accepted, never reaching the drop. Fixed by moving the ppp0 drops
  BEFORE the SYN flood rules. **Notification center** (`web/index.html` +
  `app.js`): bell icon in the top-right corner with red badge, dropdown list
  fed by the same `security` payload (failed logins, WAF blocks, default
  password warning, WAN-over-HTTP warning), "Clear all" with localStorage
  persistence. **Firewall forms**: rule CRUD + port-forward CRUD now use modal
  forms (name, action, chain, IPs, protocol, ports) instead of browser
  `prompt()` dialogs; port forwards also gained an **Edit** button per row.
- **2026-08-19 (feature)** — **API-surface + anti-replay hygiene**: FastAPI
  auto-docs OFF by default (`web.docs_enabled: false` — `/api/docs` +
  `/api/openapi.json` are a structured endpoint map); every data response
  (`/api/*`, `/report`, `/milestone`, `/`) stamped `Cache-Control: no-store`
  (browser never caches MACs/usage/history/log-tails on a shared machine);
  `X-Robots-Tag: noindex` + `web/robots.txt`. The 5 s WS push + dashboard poll
  already shipped NO raw events/log tail (on-demand `/api/logs` only) — pinned
  by test_dashboard_payload_minimized; the /report contract keeps its
  logs+events (test_report_gated_by_source_ip). Suite 638→640.
- **2026-08-19 (feature)** — **Security hardening pass** (all four phases in
  one go, 27-test gate): **auth core** — `core/passwords.py` policy (≥12
  chars/4 classes/common-list), `_LoginLimiter` progressive backoff (4 free →
  429 with escalating Retry-After, per-IP + per-account + 50-fail/5-min global
  breaker, rejected attempts never PBKDF2-processed), generic
  "invalid credentials" (no account/wrong-password oracle), session rotation
  on logout + password change; **OWASP fixes** — default-password
  `admin_password_default` flag blocks Strong WAN activation, PPPoE password
  never leaves the box (`pppoe_password` masked + `pppoe_has_password`),
  SSRF allowlists in `updater.py`/`dns_rules.py`, optional TLS
  (`web.tls_certfile/keyfile`) forcing `secure_cookies`; **embedded WAF**
  (`api/waf.py` + middleware, config `waf:`): SQLi/XSS/cmdi/path signatures,
  scanner-UA fingerprints, size/header caps, per-endpoint rate limits,
  auto-ban feed, mode auto = strict WAN / log LAN, fail-closed on WAN, plus
  **CSRF custom-header guard** (`X-QM-CSRF`, browser-context only) and
  **security headers** (strict CSP on / + /api, looser on /report /milestone);
  **extra hardening** — opt-in **TOTP 2FA** (`quota/totp.py` RFC 6238 stdlib,
  `/api/totp*`, password always required, code never sufficient alone),
  dashboard `security` block (failed_logins_1h / waf_blocks_1h /
  default_password / totp_enabled / wan_http) + UI banner + 2FA modal. Suite
  608→636. Known limits: CSRF gate is bypassed when NO Origin/Referer is sent
  (raw API clients — browser-context requests always carry one); WAF is
  log-only on LAN by design (a mis-fire must not break the LAN dashboard);
  `/api/setup/complete` does NOT rotate the session (fresh install has no
  other sessions to protect).
- **2026-08-19** — 5-agent read-only deep audit (this map). No code changed.
- **2026-08-19 (feature)** — **Firewall module** (`quota/firewall.py`, 36-test
  gate): separate `inet quota_firewall` table layered before the quota engine
  (hook priority -100), LAN/WAN posture derived from topology (WAN default-deny
  NEW inbound on ppp0 + `wan_confirmed` dashboard opt-in), safe-apply with
  sanitize lockout-refusal + ruleset/config snapshots + watchdog auto-revert to
  the pre-apply last-good, kernel bans (manual / login brute-force / port-scan
  via the dynamic `fw_scan_watch` set), SYN-flood guard, allow/deny CIDR lists,
  ordered custom rules, WAN port-forwards + DMZ (API 409 in LAN), Firewall tab
  (config + bans + log) with `fw_*` counter drains each tick. Suite 571→608.
  Known limits (documented in flow 6b): counter-driven drops are aggregate per
  rule (per-source IP only for app-initiated bans); no nft `log` statements;
  port-forwards/DMZ are WAN-only; `netmgr.render_config` does NOT emit a
  `firewall:` block (the DB `firewall_config` setting is the runtime master).
- **2026-08-17** — v0.2.1 released (commit `cf0146f`, tag pushed, suite 571
  passed + pyflakes + node clean): self-update checks, disabled onboarding
  lock, DHCP-level refusals (stop-new + decline-random via dnsmasq fragment),
  end-of-month bundle type + period-math fix, guest-limit apply-to-existing,
  random-MAC vendor-OUI sweep fix, ARP-RTT WiFi/LAN labels, LED=presence,
  audit-fix batch, MAC whitelist/blacklist + phantom-device fix + exempt-quota.
  Changelog popup gotcha: the release body is immutable at tag time — keep the
  GitHub release body and main's CHANGELOG section in sync (a box stuck on the
  `raw.githubusercontent.com` fallback sees the body, not the CHANGELOG).
- **2026-08-16** — audit-fix batch: off-loop nft/tc/ppp/lease/log reads, WS
  payload built once/tick, `prune_events` hourly cap, PBKDF2 600k + rehash,
  login rate limit, milestone-notify IP gate, dead-code sweep, GATEWAY_MAC
  quota flag, source-interface NIC tags.
- **2026-08-16** — v0.2.0 released: v19-v28.4 bundle (WAN/LAN manager, VPN
  share + gw_allowed + tun2socks, DNS filtering, browsing history, Network
  overhaul + gates, LAN pass-through shaping).
- **2026-08-12** — v0.1.3 released (VPN share + DNS filtering + signed apt
  repo). **2026-08-11** — v0.1.2 released (browsing history + All-devices +
  theme). History-tab-empty root cause: dnsmasq ≥2.90 verbose log shape AND
  `conf-dir=` commented — setup now enables it.
- **2026-08-10** — deep audit (v0.1.1 baseline): lease-less block defect
  CONFIRMED open; perf audit found no timing telemetry + on-loop storms (both
  since addressed).
- **2026-08-08→08-05** — v20→v11: WAN strong mode, ARP gateway-lock + rogue
  scan, UI redesign, .deb packaging, per-user quota model, Linux pivot.