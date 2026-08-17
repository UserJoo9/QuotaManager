# CLAUDE SYSTEM MAP — Quota Manager

Gateway that splits a metered internet bundle (e.g. Egypt 140 GB/month) fairly
across USERS — a person's allowance covers all of their devices (phone + tablet
+ laptop share one slice). When a user exceeds their allowance, every device
they own is cut at once; a per-device override can exempt a single device.
Admin dashboard: obsidian-glass web UI (fixed left sidebar, midnight obsidian
base + electric-cobalt accents, dark glassmorphism cards, 2-column masonry
user cards, subtle particle canvas). Deployment target: **Linux on an old
laptop** (Kali/Debian) — the kernel owns the network path.

## [TECH_STACK]
- Python **3.11** (runtime venv; 3.10+ supported), fastapi **0.141.1** (REST +
  WebSocket + static UI), uvicorn **0.52.1** (`[standard]` includes websockets),
  aiosqlite **0.22.1** (async SQLite, WAL), PyYAML **6.0.3** (config),
  auth = stdlib `hashlib.pbkdf2_hmac` (single admin, session cookie),
  tests = pytest **8.x** + fastapi TestClient.
- **Linux only**: `dnsmasq` (DHCP + DNS), `nftables` (client-subnet NAT +
  accounting + hard drop), `tc` (speed shaping). Deps: `requirements-linux.txt`.

## [SYSTEM_FLOW]
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
4. Every ~15 s the maintenance loop: rolls the quota period if stale (skips
   when `reset_day=0`) → syncs device bindings from the dnsmasq lease file →
   drains counter deltas into `usage_daily` → re-evaluates block states →
   pushes fresh ip→mac / blocked maps into the engine + snapshot holder.
   Every ~15 s it also learns each leased device's **source NIC** from
   `ip -j neigh` (`devices.source_interface`, text `ip neigh` fallback;
   IPv4-only, FAILED rows skipped, last-known NIC kept on disconnect) — the
   dashboard maps it through `network.interface_tags` into the WiFi/LAN chip
   on the device card. Every tick it also resolves the **router-side** access
   label: the box raw-ARPs every leased client and times the replies
   (`quota/latency_probe.py`, ON by default, ANY hardware — no monitor card
   needed): wired answers in well under a millisecond, WiFi pays airtime
   (≥1 ms), so the fastest sample decides `WiFi`/`LAN`; raw AF_PACKET backend
   with a `ping` parse fallback; requests go out in **interleaved send/drain
   rounds** so a power-save device (sleeping phone / NIC-sleeping PC) that
   wakes seconds later still gets its replies sampled (a send-everything-then-
   listen sweep closes the receive window before it wakes); a consecutive-sweep
   streak guard (`min_consistent`) prevents flapping and a device that stops
   replying keeps its previous label; the sweep's responder set also drives
   the device-card LED — a leased device that stops answering (asleep/off/
   another network) goes grey, since a DHCP lease alone lags reality by up to
   `LEASE_HOURS`. When the box HAS a monitor-capable card, the
   passive probe (`quota/wifi_probe.py`, airmon-ng + airodump-ng on a
   dedicated thread, OFF by default) takes precedence and adds the exact
   SSID: a leased device heard on a known BSSID is labeled `WiFi · <SSID>`,
   heard-but-unknown `WiFi`, leased-but-never-heard past `lan_after_seconds`
   `LAN` (grace in memory, no flap; the rogue ARP scan elicits the
   sightings). The manual per-device pin (`devices.access_override`,
   `POST /api/devices/{id}/access`) always wins the display while the auto
   label keeps tracking reality.
   Every 60 s a **rogue LAN scan** (`quota/arp_scan.py`) raw-ARP-probes both
   subnets; active hosts NOT in the lease file surface in the snapshot's
   `rogue` list (+ `warning` event) — a static-IP bypasser is otherwise
   invisible.
5. A blocked device: IP added to the `blocked` set → kernel **drops** its
   forward-chain packets — hard internet cut at line rate. Admin toggles work
   the same way.
6. **Speed shaping** (`quota/shaping.py`, Linux only) is a second kernel-side
   stack that never touches nftables: `TcShaper` reconciles an **HTB +
   fq_codel** tree on the single NIC. Uploads (client→internet) are redirected
   at NIC **ingress** into `ifb0` and shaped by `ip src`; downloads are shaped
   at NIC **egress** by `ip dst`. Per-device leaves under per-user classes
   (capped at the user's aggregate) under a download aggregate under a root
   capped at the **real line speed** from the Network tab — effective cap
   `min(dev, user)`; the default class is capped at the direction total (NOT a
   pass-through), so an unlimited downloader cannot flood the modem buffer.
   Tree rebuilt only on a signature change of (enabled, totals, aqm, sorted
   caps). **LAN pass-through**: client↔uplink-subnet and client↔box traffic
   (NAS, router admin, dashboard, file shares) rides a **prio-1 class `1:99`**
   at the LAN link rate (`shaping.lan_rate_mbps`, default 1000; falls back to
   1000 Mbps, NEVER the WAN cap), covering both directions + the box's own
   addresses; priorities are deliberately non-zero (tc treats `prio 0` as "no
   priority" and assigns it AFTER real priorities). Shaping sits after
   nftables in the packet path: blocked devices are already dropped, counters
   see real pre-NAT src / post-NAT dst either way.
7. FastAPI + uvicorn serves the dashboard + REST API + `/ws` push (5 s
   snapshots); the client also polls as a fallback.
8. **ARP gateway-lock** (`engine.gateway_arp_lock`, OFF in config.yaml but ON
   in the setup-generated config): a device that sets a static IP + the ROUTER
   as its gateway bypasses the box at L2 entirely. The lock: a raw-socket
   responder (`quota/arp_lock.py`) claims the router's IP on the CLIENT subnet
   (answers client-subnet ARP requests for the router with the box's own MAC),
   an `arp`-family nftables rule drops the router's competing replies, and a
   `forward` deny drops any client-subnet source NOT in the `known_ips` set
   (= leased DHCP IPs). The bypasser's frames arrive at the box and are
   dropped — internet cut until it uses the quota gateway. Self-sustaining
   (dropped traffic re-ARPs and is re-answered). Uplink-subnet hosts keep the
   real router; a static ARP entry or an uplink-subnet static IP still evades
   capture (surfaced as a rogue; router-side MAC allowlist is the durable
   complement). `known_ips` rebuilt only on membership change.
9. **Strong (WAN) mode** (`engine.topology=wan`, optional, OFF by default): the
   box dials the PPPoE line itself (`quota-wan-ppp.service` runs
   `pppd call quota-wan nodetach` — pppd must NOT daemonize or systemd
   kill-loops it; public IP lands on `ppp0`; creds in `/etc/ppp/{chap,pap}-
   secrets`, chmod 600) and the router is demoted to a pure bridge/AP — no
   second router to bypass to. **The dashboard WAN tab applies the switch
   LIVE** (`quota/netmgr.py` `TopologyManager`): collects PPPoE creds,
   rewrites config.yaml + the DB setting TOGETHER (`topology_source=dashboard`
   + `topology` — never one without the other, the v18 revert bug), runs the
   runtime applier `scripts/topology.sh` (NIC + dnsmasq + PPPoE dial, creds via
   the ENVIRONMENT never argv), schedules a detached self-restart; applier
   failure rolls config + DB back (no restart into a half-applied state);
   "Revert to LAN" restores from `dhcp.lan_*` + `engine.lan_gateway_arp_lock`
   snapshot keys (never a guess at 192.168.1.1). Under wan the box KEEPS the
   uplink IP as a secondary router-admin alias, so `resolve_local_networks`
   treats the uplink subnet as LOCAL (router-admin traffic never consumes
   quota; not a bypass — the masquerade only covers the client subnet). ARP
   gateway-lock forced off, rogue scanner probes only the client subnet,
   `quota/topology.py` `detect_ppp` reports ppp0 state into `wan_status` —
   **judged by its negotiated IPv4, never by operstate** (PPP is carrier-less;
   sysfs says `unknown` even while pppd holds a live link). WAN tab also has a
   throwaway **Test PPPoE** dial (`scripts/test_pppoe.sh`, unit ppp200, no
   config/routing change) + **Restart PPPoE / auto-renew** for a fresh public
   IP (interval clamped to a 5-minute floor, default 15; 409 while ppp0 is
   down).
10. **Per-device browsing history** (`quota/dnslog.py`, ON by default): the
    setup script installs an **app-owned dnsmasq fragment**
    (`/etc/dnsmasq.d/quota-dnslog.conf` — `log-queries=extra` + `log-async=20`
    + `log-facility=/var/log/quota-dnsmasq.log`; both scripts only rewrite
    `quota-gateway.conf`, so it survives re-runs and WAN/LAN toggles) and
    **enables `conf-dir=` in `/etc/dnsmasq.conf`** (dnsmasq otherwise silently
    ignores every fragment — the live-box empty-History-tab bug) + a logrotate
    snippet (copytruncate, 5M, rotate 3). A dedicated tailer thread
    (`DnslogTailer`) polls every 0.5 s, strips `\x00` sparse holes, caps the
    partial-line buffer at 1 MB, and pushes parsed `(minute, ip, domain)`
    events onto a **bounded queue — overflow drops lines, never blocks DNS or
    the event loop**. Parser accepts both the bare shape and the verbose
    `1 192.168.2.186/16773 query[A] …` shape (dnsmasq ≥2.90). Each tick drains
    into a `dns_history` table (per device/minute/domain), persists the read
    cursor (`dnslog_state` — restart-resume, first start seeks to EOF), and
    prunes **per user** at their `history_days` (NULL = global
    `history.retention_days`). History tab (`GET /api/history/{device_id}`, or
    `all`/`0` for the household aggregate, auth-gated): top domains, hourly
    activity, recent queries; bandwidth reuses existing snapshot fields.
    Rotation resets the tailer cursor; missing log file is not an error;
    `history.enabled: false` stops recording.

**Quota model (per user)**: the monthly allowance lives on a **user**
(`users` table; `devices.user_id`), not a device. Auto users equally share the
bundle remainder after fixed users take their GB off the top; a user's usage =
Σ their devices' usage. When a user exceeds their allowance, every device they
own is cut together. The cut is **resolved** at render/enforcement time
(`service.resolve_device_state`), never written to device rows — a user-level
admin cut is lossless, clearing it restores all devices. Precedence:
**user admin > device admin > user quota (unless per-device `bypass`) > ok**;
per-device admin cut always wins. A `bypass` keeps one device online despite
its user's quota block. Enforcement stays per-MAC/per-IP — the engine's
`blocked` set still drops at line rate; only the *decision* is per-user. New
DHCP devices auto-create their own user (one device ⇒ one user) in the
**DISABLED onboarding lock** (`users.quota_mode="disabled"`, 0 GB): it claims
NO share of the bundle and is always quota-blocked (the admin's positive
shared/fixed assignment in the user/device modal is the only way online —
guests are unaffected; STOP NEW CONNECTIONS and Decline-random MACs refuse
new MACs at the DHCP level instead — no row at all, see the gate entries).
Legacy device-only DBs
are migrated in place by `db.connect()` (idempotent ALTERs + backfill).

**Bundle source (fixed)**: `config.yaml` is the default source of truth for
`bundle.total_gb` / `bundle.reset_day` / `bundle.period_type`, re-applied on
every startup (`run.py: _seed_bundle_from_cfg`). Once the admin edits the
bundle or recharges via the dashboard (`POST /api/bundle`), a `bundle_source`
setting is set to `dashboard` and config.yaml stops overriding it — a UI edit
survives a restart.

**Bundle type (`bundle.period_type`)**: `renew_day` (default) resets on the
configured `reset_day` (0 = never auto-reset); `end_of_month` is the ISP's
**month-end bill** — the configured day drives the reset too (many ISPs close
the month on the 25th/28th), and day 0 falls back to the calendar end (1st of
next month) — all via `Bundle.effective_reset_day` (day range 0-31). The
dashboard/Welcome bundle panel has the selector; the reset-day input stays
editable in both modes (its 0-hint text adapts).

**No-auto-reset (`reset_day=0`, renew-day type only)**: the period opens once
and never rolls by itself; the bundle grows only via "Bundle recharged"
(`service.recharge`, keeps `period_start`) and a new month starts only via
"Reset month now".

**Period math (fixed 2026-08-17)**: `timeutil.period_bounds` returns the
period **containing now** — before this month's reset day the current period
began last month on the reset day. `ensure_period` rolls when the recorded
`period_end` has passed, never by comparing `period_start` against the grid —
a mid-month reset-day change re-anchors `period_end` (via
`recompute_allowances`) without rolling or zeroing the recorded usage.

**Electric-cut fallback (optional)**: the router can keep a small
non-overlapping DHCP pool (gateway = router) on the uplink subnet
(192.168.1.x) while dnsmasq serves only the client subnet (192.168.2.x) — no
overlap by construction. Devices fall back to direct internet during a gateway
outage and re-join as leases renew (`LEASE_HOURS=1` for fast re-adoption).
Trade-off: fallback-leased devices are not counted/controlled while the
gateway is down.

**Packaging + releases**: the `.deb` is built **only** by GitHub Actions —
`.github/workflows/release.yml` renders `packaging/DEBIAN/control` from
`quota/version.py` (single source of truth; a `v*` tag must match or the
workflow fails loudly), stages the runtime payload into `/opt/quota-manager`,
runs `dpkg-deb --build --root-owner-group`, and uploads to GitHub Releases —
the release description is auto-composed from the released version's
`CHANGELOG.md` section (notes never drift). `postinst` builds the venv + runs
`setup_gateway_kali.sh` with `QUOTA_NO_APT=1` + enables/starts `quota-gateway`;
`prerm` stops/disables on remove/upgrade; both idempotent, preserving
`/etc/quota-gateway/config.yaml` + `/var/lib/quota-gateway/quota.db`. A second
workflow, **`apt-repo.yml`**, turns each Release into a **signed apt repo**
(`workflow_run` on `release` + dispatch backfill): imports the private key
from the `APT_REPO_GPG_KEY` secret, regenerates + signs `Packages`/`Release`
(`Release.gpg` + clearsigned `InRelease`), pushes to `gh-pages` (with
`.nojekyll`) hosted at https://UserJoo9.github.io/QuotaManager/ — a one-time
`deb [signed-by=…] …` source line then makes `apt-get install quota-manager`
and `apt update && apt upgrade` work. Public key committed at
`quota-manager.gpg`. `tests/test_packaging.py` pins the whole contract (no
dpkg needed).

## [ARCHITECTURE]
```
QuotaManager/
├── CLAUDE.md                 <- this file (SYSTEM MAP)
├── README.md                 # end-user docs (install, usage, troubleshooting)
├── Structure_README.md       # developer docs (architecture, config, API,
│                             #   tests, release process)
├── LICENSE                   # MIT license
├── CHANGELOG.md              # release changelog
├── quota-manager.gpg         # armored PUBLIC key for the signed apt repo
├── .github/workflows/
│   ├── release.yml           # on a v* tag: build .deb -> GitHub Releases
│   └── apt-repo.yml          # workflow_run on release + dispatch backfill:
│                             #   sign + publish the .deb to gh-pages (apt repo)
├── packaging/DEBIAN/
│   ├── control.template      # Debian control (Version rendered from version.py)
│   ├── postinst              # venv + setup_gateway_kali.sh (QUOTA_NO_APT=1) + start
│   └── prerm                 # stop + disable quota-gateway on remove/upgrade
├── config.yaml               # Linux gateway settings (dnsmasq + nftables)
├── run.py                    # Gateway wiring: engine + maintenance + uvicorn;
│                             #   source-interface collector (ip -j neigh → tag)
│                             #   + router-side WiFi/LAN label resolution
│                             #   (ARP-RTT classifier + optional monitor probe
│                             #   → devices)
├── requirements-linux.txt    # Linux deps (fastapi, uvicorn, aiosqlite, PyYAML + test deps)
├── scripts/
│   ├── setup_gateway_kali.sh # Linux: sysctl, client-subnet NAT, dnsmasq,
│   │                         #   dnslog fragment + logrotate, systemd unit,
│   │                         #   info (QUOTA_NO_APT skips apt)
│   ├── topology.sh           # runtime LAN/WAN applier (panel-invoked): NIC
│   │                         #   (nmcli/ifupdown), dnsmasq, PPPoE dial; env-fed
│   ├── test_pppoe.sh         # throwaway PPPoE dial (ppp200) — test creds with
│   │                         #   NO config/topology/routing change (WAN tab)
│   ├── update_oui.py         # regenerate quota/oui.txt from the IEEE registry
│   └── replay_nft_startup.sh # reproduce the engine's startup nft command sequence (debug)
├── core/
│   ├── config.py             # config.yaml -> typed Config dataclasses
│   ├── logging_setup.py      # QueueHandler -> writer thread -> rotating file
│   └── timeutil.py           # month-boundary math (zoneinfo)
├── quota/
│   ├── db.py                 # SQLite schema + async access (aiosqlite); users
│   │                         #   table + devices.user_id/bypass + idempotent
│   │                         #   migration (legacy devices → own user);
│   │                         #   speed caps: devices/users limit_down/up_mbps;
│   │                         #   devices.source_interface (box-NIC WiFi/LAN tag)
│   │                         #   + access_interface/access_override (router-side
│   │                         #   WiFi SSID / LAN pin, override wins the display);
│   │                         #   dns_history table + per-user history_days;
│   │                         #   mac_lists (whitelist/blacklist)
│   ├── engine.py             # shared snapshot types (Linux): EngineCounters,
│   │                         #   RogueHost, EngineSnapshot, SnapshotHolder +
│   │                         #   GATEWAY_MAC sentinel — the thread-safe handoff
│   │                         #   between the kernel-side engine and asyncio;
│   │                         #   the type hub (a field rename ripples everywhere)
│   ├── service.py            # per-user quota math (allowance on the user,
│   │                         #   usage = Σ devices), block fan-out + bypass
│   │                         #   precedence, top-up, recharge, reset_day=0,
│   │                         #   period roll; shaping + guest + gate settings
│   ├── nftables.py           # NftablesEngine (Linux): kernel counters + block
│   │                         #   + ARP gateway-lock deny rules (known_ips set);
│   │                         #   gw_allowed set: the box egress that survives a
│   │                         #   Gateway cut while VPN share relays the household
│   │                         #   (accepts above gw_blocked drops + q_gw counters)
│   ├── vpnshare.py           # VpnShareManager: "VPN share" policy routing —
│   │                         #   client subnet -> dedicated route table whose
│   │                         #   default points at the box's TUN (sing-box/
│   │                         #   xray/WireGuard), local LAN routes kept,
│   │                         #   idempotent reconcile self-heals leftovers;
│   │                         #   refuses stale/address-less tunnel pins
│   ├── tun2socks.py          # Tun2socksManager: auto-provisions the tun2socks
│   │                         #   bridge when VPN share finds no kernel TUN
│   │                         #   (v2rayN's userspace netstack) — pinned +
│   │                         #   sha256-verified one-time download, SOCKS
│   │                         #   listener auto-detect (probes the fallback!),
│   │                         #   spawn/kill child, honest per-state status
│   ├── shaping.py            # TcShaper (Linux): per-device + per-user speed
│   │                         #   caps + low-latency queues (HTB + fq_codel),
│   │                         #   single-NIC two-tree design (see SYSTEM_FLOW)
│   ├── arp_scan.py           # rogue static-IP detection: raw-socket ARP probe
│   │                         #   of both LAN subnets -> hosts not leased by DHCP
│   ├── arp_lock.py           # ARP gateway-lock responder: claims the router's
│   │                         #   IP on the client subnet so bypassers' frames
│   │                         #   arrive at the box (raw-socket thread)
│   ├── latency_probe.py      # WiFi/LAN classification by ARP round-trip time
│   │                         #   (ON by default, ANY hardware): the fastest
│   │                         #   reply sample decides; ping-parse fallback;
│   │                         #   feeds devices.access_interface WiFi/LAN
│   ├── wifi_probe.py         # router-side WiFi/LAN label probe: passive
│   │                         #   monitor-mode sniffing (airmon-ng + airodump-ng
│   │                         #   on a dedicated thread) -> per-device SSID /
│   │                         #   LAN labels, OFF by default, only with a
│   │                         #   monitor-capable card
│   ├── dnslog.py             # DNS browsing history: dnsmasq query-log parser
│   │                         #   + DnslogTailer thread (dedicated thread,
│   │                         #   bounded queue, rotation-safe) -> dns_history
│   ├── dns_rules.py          # DnsRuleManager: host-based domain filtering —
│   │                         #   block/allow/redirect per user or device, ABP
│   │                         #   blocklist presets, rendered into dnsmasq
│   │                         #   config (conf-file -> rules/*.conf)
│   ├── topology.py           # WAN-topology detection: detect_ppp() reports
│   │                         #   whether ppp0 is up + its address pair (WAN tab);
│   │                         #   restart_pppoe() = public-IP renewal (v24)
│   ├── updater.py            # GitHub self-update checks (NEW): version
│   │                         #   compare (quota/version.py), CHANGELOG.md parse
│   │                         #   (newest-first, Unreleased skipped), 24 h gate +
│   │                         #   persisted updates_state, optional auto-install
│   │                         #   of the .deb under a transient systemd unit
│   │                         #   (prerm stops quota-gateway — a child apt-get
│   │                         #   would die with the cgroup)
│   ├── netmgr.py             # TopologyManager (v19/19.1): the dashboard WAN
│   │                         #   tab's live LAN/WAN switch — config.yaml + DB
│   │                         #   written together, runs scripts/topology.sh,
│   │                         #   detached restart, applier-failure ROLLBACK;
│   │                         #   lan_* snapshot keys power Revert; test_pppoe()
│   ├── vendor.py             # MAC OUI -> manufacturer (IEEE registry, lazy)
│   ├── oui.txt               # bundled IEEE MA-L/MA-M/MA-S database (53.5k prefixes)
│   └── version.py            # single source of truth for the release version
├── api/
│   ├── app.py                # FastAPI factory: REST + /ws + static mount +
│   │                         #   /milestone (public, own-user) + /report (IP-gated)
│   │                         #   + access-label pin + SSID picker routes
│   └── schemas.py            # pydantic request models
├── web/
│   ├── index.html            # login + dashboard + modals
│   ├── milestone.html        # public milestone page (requester's OWN user only)
│   ├── report.html           # source-IP-gated household usage report (no session)
│   └── assets/
│       ├── styles.css        # obsidian-glass cobalt theme (sidebar + masonry)
│       └── app.js            # WS client, dashboard render, user-grouped
│                             #   device cards, user + device controls
└── tests/
    ├── test_vpnshare.py       # VpnShareManager vs a fake `ip` binary
    ├── test_tun2socks.py      # Tun2socksManager vs fakes (pinned download, spawn)
    ├── test_dns_rules.py      # hosts/ABP parsing, wildcard scopes, rendering
    ├── test_quota_service.py # period math, per-user allowance math, block
    │                         #   fan-out + bypass, recharge
    ├── test_api.py           # REST API integration (user CRUD, recharge,
    │                         #   reset-day-0, bundle_source ownership)
    ├── test_web_ui.py        # static UI served (tabs, Network panel)
    ├── test_shaping.py       # TcShaper vs a fake `tc` binary (commands)
    ├── test_packaging.py     # release workflow + control/postinst/prerm +
    │                         #   QUOTA_NO_APT + apt-repo contract (no dpkg)
    ├── test_vendor.py        # OUI -> vendor lookup (longest-prefix)
    ├── test_config.py        # typed config parsing (Linux settings)
    ├── test_nftables.py      # NftablesEngine vs a fake `nft` binary
    ├── test_arp_scan.py      # rogue static-IP detection (fake raw sockets)
    ├── test_arp_lock.py      # ARP gateway-lock responder (fake frames)
    ├── test_latency_probe.py # ARP-RTT classifier math + sweep wiring (fakes)
    ├── test_wifi_probe.py    # airodump CSV parse + probe snapshot + thread smoke
    ├── test_dnslog.py        # dnsmasq query-log parser + tailer + dns_history DB
    ├── test_netmgr.py        # TopologyManager WAN/LAN apply + rollback + PPPoE test
    ├── test_topology.py      # detect_ppp / check_internet probes
    ├── test_updater.py       # version math + CHANGELOG parse + GitHub check +
    │                         #   systemd-run/apt install (fakes, no network)
    ├── test_users_migration.py # legacy device-only DB → users backfill
    └── test_run_wiring.py    # run.py wiring + live boot + bundle reconcile +
                              #   dnsmasq lease sync + live-counter regression
```
Dependencies point downward only: `api -> quota/core`, `quota -> core`.
Engine ↔ asyncio communicate through thread-safe counter snapshots (no locks in
the packet hot path). On Linux the hot path has **no Python at all** — the
kernel counts and drops.

## [ORPHANS & PENDING]
_Orphans + debt are tracked in [LEGACY_DEBT_AND_RISKS] below. Version history
(newest first) — full detail in the git history / CHANGELOG.md; these are the
headlines + the gotchas to remember:_
- **2026-08-17** — **v0.2.1 changelog made user-friendly end-to-end**: the
  release notes / popup showed the LONG technical CHANGELOG text. Two sources
  feed the popup and BOTH now carry the brief text: (1) raw
  `CHANGELOG.md` on main (rewritten brief, commit `8331cb0`) and (2) the
  **GitHub release body** — `check_now`'s fallback (`quota/updater.py`)
  serves the release body whenever the raw fetch fails, and a box that can't
  reach `raw.githubusercontent.com` (common) got the IMMUTABLE v0.2.1 body
  with the old 11 KB technical changelog. Fixed by `gh release edit v0.2.1`
  → brief body (GitHub-side, no code); a box re-check ("Check now") picks it
  up even WITHOUT the regex fix. Gotcha: the release body is immutable at
  tag time — a later CHANGELOG rewrite never reaches a box stuck on the
  fallback, so keep the release body and main's CHANGELOG section in sync.
- **2026-08-17** — **Show-details changelog bug fixed**: `parse_changelog` only
  matched BARE `## [version]` headers — the real CHANGELOG writes
  `## [0.2.1] — 2026-08-17` (date suffix), so the popup said "New versions
  since v0.2.0 (0) / No changelog available". The split regex now tolerates a
  same-line suffix (`(?:[ \t]+[^\n]*)?`) — must NOT use `\s+.*` (it crosses
  the blank line and swallows the NEXT section's header into the previous
  body). Release notes (release.yml awk `index()` substring) were unaffected.
  Pinned by `test_parse_changelog_matches_date_suffixed_headers`.
- **2026-08-17** — **v0.2.1 released + pushed**: the whole uncommitted batch
  below (self-update, period_type/end_of_month + period-math fix, disabled
  onboarding lock, DHCP-level refusals, guest-limit apply-to-existing,
  random-MAC vendor-OUI sweep fix, ARP-RTT WiFi/LAN labels, LED=presence,
  audit-fix batch, MAC whitelist/blacklist + phantom-device fix + exempt-quota
  fix) shipped as **v0.2.1** (commit `cf0146f`, tag `v0.2.1` pushed). README.md /
  README_AR.md / Structure_README.md updated; CHANGELOG.md `[0.2.1]` section
  written (it IS the release notes). Full suite 571 passed + pyflakes + node
  clean. Release workflow builds the .deb; apt repo auto-republishes.
- **2026-08-17** — **Admin-tab self-update checks (uncommitted → v0.2.1)**: the box
  compares its own version (`quota/version.py`) to the latest GitHub release
  (`updates.repo`, default UserJoo9/QuotaManager) every `updates.interval_hours`
  (24) and, on a newer version, shows an update banner with a **Show details**
  popup listing every newer CHANGELOG.md section (newest-first, Unreleased
  skipped) in a scroll frame — a far-behind box lists all intermediate
  versions. NEW `quota/updater.py` (`Updater`, stdlib fetch/run injectables for
  tests): `maybe_check` (24 h gate + `updates_enabled` DB setting) from the
  maintenance tick behind a try/except; persisted `updates_state` (checked_at/
  latest/error/changelog/last_install) so restarts never re-notify/re-check;
  auto-install downloads the `.deb` and runs `apt-get install` under a
  **transient systemd unit** (`systemd-run --unit=quota-update-install`) because
  the .deb's `prerm` stops quota-gateway — a child apt-get would die with the
  cgroup (plain apt-get fallback when systemd-run is absent). API:
  `GET/POST /api/updates`, `/api/updates/check`, `/api/updates/install`, all
  404 when unwired; snapshot `update` key. **Config gate `updates.enabled`
  (default true) is the hermetic-tests master switch** — `_cfg` turns it off
  so the first tick never dials GitHub; when off `gw.updater is None` (endpoints
  404, snapshot update:None). **The dashboard "Check automatically" toggle is
  the per-box master**: `check_now` refuses to fetch when it's off, the card
  shows "Checks are OFF — toggle ON to check for updates" (never a stale
  error/last-check), and re-enabling clears the last error (`set_enabled`).
  Banner shows once per version (localStorage
  `quota_update_banner`). Tests: `test_updater.py` NEW (14) + config + API +
  wiring — **571 passed**, pyflakes clean, JS-OK. Not pushed (no-GitHub-push
  rule).
- **2026-08-17** — **reset-day mid-month skip + consumption zeroing fixed;
  bundle type selector added (uncommitted)**: `timeutil.period_bounds` now
  returns the period CONTAINING now (before this month's reset day the period
  began last month) — previously reset day 25 with today the 16th read
  days-left 40 and the maintenance loop rolled the period immediately,
  dropping the current month's usage from the period. `ensure_period` now
  rolls only when the recorded `period_end` has passed (never by comparing
  `period_start` against the grid), so a mid-month reset-day change re-anchors
  `period_end` (`recompute_allowances`) without rolling/zeroing usage. New
  `bundle.period_type` (`renew_day` = current, `end_of_month` = the ISP's
  month-end bill: the configured day drives the reset too — many ISPs close on
  the 25th/28th — and day 0 falls back to the calendar end, the 1st; day range
  widened 0-31): DB column + idempotent ALTER, config.yaml `bundle.period_type`,
  BundleConfig, run.py `_seed_bundle_from_cfg`, netmgr snapshot, schemas +
  `_apply_bundle_values` (400 on a bad value), dashboard/report payloads,
  dashboard + Welcome selects (reset-day stays editable in both modes, its
  0-hint adapts). Tests: period_bounds
  containing-period cases (incl. January wrap), `test_changing_reset_day_mid_month_does_not_roll_or_zero_usage`,
  `test_ensure_period_reset_day_25_steady_state_never_rolls_mid_month`,
  end-of-month honoring the configured day + day-0 calendar-end fallback, API
  period_type round-trip, config-YAML period_type seeding — 552 passed,
  pyflakes clean. Not pushed (no-GitHub-push rule).
- **2026-08-17** — **guest-limit cap now applies to existing guests too
  (uncommitted)**: `set_guest_limit` previously only gated brand-new guest
  registrations — "Max guest accounts = 1" left guests that joined EARLIER
  online (verified: the fresh-case gate already worked, `count_guest_users()
  > limit` → BLOCK_ADMIN). Now lowering the cap also admin-blocks the NEWEST
  over-cap guest users' devices immediately (oldest `n` stay — sorted by
  `users.created_at`); raising it never un-blocks. Mirrors
  `set_guest_quota`'s apply-to-existing pattern. Tests: new
  `test_lowering_guest_limit_cuts_existing_over_cap` + existing guest suite
  — 544 passed. Not pushed (no-GitHub-push rule).
- **2026-08-17** — **"Also cut existing random-MAC devices" sweep no longer
  cuts real products (uncommitted)**: the one-shot sweep (and the brand-new
  DHCP refusal) keyed off the locally-administered bit ALONE — but some
  genuine legacy products ship locally-administered MACs whose OUI IS a
  registered IEEE vendor prefix (3COM 02:c0:8c, DEC aa:00:00, Olivetti
  02:aa:3c — 18 such MA-L prefixes in the bundled registry). Those got
  classified "random" and cut. `QuotaService.is_random_mac` now requires BOTH
  the local bit AND an empty vendor lookup (`vendor_for(mac) == ""`) — a
  privacy-randomized MAC carries a random OUI that never appears in
  `quota/oui.txt`, so a known vendor prefix means a real device, never a
  randomize. Covers the sweep AND `_persist_lease`'s decline-random refusal
  (same helper). Tests: is_random_mac cases for the legacy OUIs + a
  sweep-survives legacy device — 543 passed. Not pushed (no-GitHub-push
  rule).
- **2026-08-17** — **Decline random MACs now refuses at the DHCP level too
  (uncommitted)**: the random-MAC gate no longer registers a randomized
  (privacy) MAC as an "unsigned user" and admin-blocks it — it shares the
  STOP-NEW refusal path: the MAC joins the persisted
  `decline_random_refused_macs` setting (its OWN list — each gate's off
  clears only its own refusals) and the same app-owned dnsmasq fragment
  (one `dhcp-host=<mac>,ignore` line per refused MAC, both gates' sets
  unioned into one file), so dnsmasq never hands it an IP and no device row
  is minted. Real (globally-unique) MACs never reach the branch. The
  just-issued lease is kernel-cut via `snapshot_state`'s row-less pass
  (refused lists unioned with the deny list). Gate off → own list +
  fragment cleared; per-tick reconcile (step 6b) + an API immediate apply
  (`decline_random_sync` callback on `/api/network {decline_random_macs}`)
  keep them in sync. Unwritable fragment → graceful fallback to the legacy
  registered + admin-blocked path. The one-shot `also_existing` sweep
  (admin-cut existing randomized devices) is unchanged. `run.py`:
  `_sync_stop_new_ignore` generalized to `_sync_refuse_fragment` +
  `_refuse_fragment_sync`; `_apply_decline_random_now` mirrors
  `_apply_stop_new_now`. Tests: rewired gate test (refusal, fragment,
  row-less cut, real-OUI untouched, gate-off clear) + fallback test +
  service row-less test — 543 passed. Not pushed (no-GitHub-push rule).
- **2026-08-17** — **STOP NEW CONNECTIONS now refuses at the DHCP level
  (uncommitted)**: the gate no longer hands a brand-new device an IP and
  admin-blocks it — dnsmasq refuses the MAC outright, so there is no
  "unsigned user" row at all. run.py's `_persist_lease` writes the MAC to a
  persisted refuse list (DB setting `stop_new_refused_macs`) and to an
  app-owned dnsmasq fragment (`dhcp.ignore_file`,
  `/etc/dnsmasq.d/quota-ignore.conf`, one `dhcp-host=<mac>,ignore` line
  each; restart via `_reload_dnsmasq` — `dnsmasq --test` gate +
  `systemctl restart`, same pattern as DnsRuleManager), then returns WITHOUT
  registering the device. The just-issued lease stays kernel-cut via
  `snapshot_state`'s row-less pass (refused MACs are unioned into the
  deny-list pass) until it expires. Gate off → refuse list + fragment
  cleared (everyone joins again); a per-tick reconcile (`_sync_refuse_fragment`,
  step 6b) + an API immediate apply (`stop_new_sync` callback on
  `/api/guest {stop_new}`) keep them in sync. Fragment unwritable (no root
  / no dnsmasq dir) → graceful fallback to the legacy registered +
  admin-blocked path so the device stays controlled. `dhcp.enable: false`
  skips the fragment entirely (no dnsmasq on the box; the row-less block
  still applies). Decline-random now refuses at DHCP the same way (see the
  entry above); guest gate unchanged (still mint + admin-block). Tests:
  rewired gate test (refusal, fragment, row-less cut,
  gate-off clear) + fallback test + service refuse-list test + config
  defaults — 541 passed. Not pushed (no-GitHub-push rule).
- **2026-08-17** — **disabled onboarding lock for new devices (uncommitted)**:
  a brand-new DHCP device no longer auto-shares the bundle — its auto-created
  user is `quota_mode="disabled"` (0 GB, claims NO share, always quota-blocked
  even with 0 usage: `compute_allowances` special-cases it, `user_quota_blocked`
  short-circuits it) until the admin assigns shared (auto) or fixed in the
  user/device modal (new "Disabled" option in both selects; the device modal's
  mode write propagates to the user). STOP-NEW-CONNECTIONS and Decline-random
  devices are refused at DHCP entirely (see the entries above — no user is
  minted); guest mode is unaffected (still mints, admin-cut at the cap).
  Legacy tests
  enshrining auto-share-on-join were rewritten to the new contract. Tests:
  service allowance/block/enforcement-map tests + run.py `_persist_lease` test
  + API dashboard test — 539 passed. Not pushed (no-GitHub-push rule).
- **2026-08-17** — **LED = ARP presence, not lease (uncommitted)**: `connected`
  now requires the device to have answered the latest latency sweep
  (`_latency_active_ips`, fresh ≤3×interval) — a leased-but-silent device
  (asleep/off/another network) goes grey, since dnsmasq keeps the lease for
  LEASE_HOURS after a disconnect. Falls back to lease-based when the probe
  isn't running or data is stale. 534 passed at that point.
- **2026-08-17** — **router-side WiFi/LAN access labels, v2 = ARP-RTT
  classification (uncommitted)**: the monitor-mode sniffing plan hit reality —
  the live box's WiFi module has NO monitor mode, so it could never hear the
  air. Replaced with `quota/latency_probe.py` (ON by default, any hardware,
  any router firmware): the box raw-ARPs every leased client and times the
  replies — wired answers in well under a millisecond, WiFi pays airtime
  (≥1 ms), so the FASTEST sample (least affected by local scheduling noise)
  decides `WiFi`/`LAN`. Raw AF_PACKET backend (root) with a `ping` time=
  parse fallback and graceful keep-previous-label degradation; a
  consecutive-sweep streak guard (`min_consistent`) prevents flapping; the
  monitor probe, when a capable card EXISTS, still takes precedence (it also
  knows the exact SSID — `quota/wifi_probe.py` is kept for that). Run.py
  `_maybe_latency_tick` on its own cadence (`interval_s`), off-loop. First
  user-visible labels ~1 min after boot (2 agreeing sweeps × 30 s).
  Misclassification knob: `network.latency_probe.threshold_ms` (a fast 5G
  device can read LAN → lower it). Tests: `tests/test_latency_probe.py` +
  config defaults — 532 passed. Not pushed (no-GitHub-push rule).
- **2026-08-17** — **router-side WiFi/LAN access labels (uncommitted)**: the
  user's question "WiFi or LAN?" means the ROUTER's attachment point (which
  SSID / which LAN port) — the box's own NIC tag always says eth0. DHCP can't
  see it (the router bridges clients L2), so the box passively **sniffs the
  air** from a spare monitor-mode card (`quota/wifi_probe.py`, airmon-ng +
  airodump-ng, dedicated thread — firmware-agnostic): every leased device
  heard on a known BSSID is labeled `WiFi · <SSID>`, heard-but-unknown `WiFi`,
  leased-but-never-heard past `lan_after_seconds` `LAN` (grace tracked in
  memory, no flap). The existing rogue ARP scan elicits the air sightings.
  Manual per-device pin (`POST /api/devices/{id}/access`, override always
  wins the display; the auto label keeps updating in the background) covers
  exact port numbers like `LAN1`; `GET /api/wifi/ssids` feeds the modal
  picker. OFF by default (`network.wifi_probe.enabled: false`; probe card must
  be spare — the uplink is wired). Tests: `tests/test_wifi_probe.py` (+ API/
  config/migration coverage) — 522 passed. Not pushed (no-GitHub-push rule).
- **2026-08-16** — **audit-fix batch + interface tags (uncommitted)**: perf
  fixes (off-loop `nft`/`tc`/`pppd`/lease/log reads; WS payload built once per
  tick; `prune_events` hourly cap on the unbounded events table), security
  (PBKDF2 600k + legacy-hash auto-rehash, 10-fail/300s login rate limit,
  `/api/milestone/notify` now IP-ownership-gated), dead code removed (orphaned
  `/api/usage*`/`/api/events` routes + `add_topup`/`has_bundle`/`is_blocked`/
  `is_admin_blocked`/`get_usage_series`), GATEWAY_MAC row never persists a
  `quota` flag in `evaluate_blocks`, and the WiFi/LAN **source-interface tag**
  feature (`ip -j neigh` → `devices.source_interface`, `network.interface_tags`
  label map, `.iface-tag` card chip). Not pushed (user's no-GitHub-push rule).
- **2026-08-16** — phantom-device fix (uncommitted): deleting a device/user now
  writes its MACs to the **permanent deny list** (`mac_lists`); a deny-listed
  MAC with a live lease but no device row is still kernel-blocked via a
  row-less `snapshot_state` pass. Not pushed (user's no-GitHub-push rule).
- **2026-08-16** — MAC whitelist/blacklist + exempt-quota enforcement fix
  (uncommitted): `mac_lists` table, `GET/POST /api/mac-lists`, precedence
  deny > user admin > device admin > allow > quota (unless bypass) > ok;
  `snapshot_state` now uses `user_quota_blocked` so exempt users are truly
  never kernel-cut. NOTE: STOP NEW CONNECTIONS / Decline random MACs / Guest
  mode still reportedly do nothing on the live box (v0.2.0) — code path
  verified correct by tests; needs a box-side look.
- **2026-08-16** — **v0.2.0 released**: v19–v28.4 bundle shipped (WAN/LAN
  manager, VPN share + gw_allowed + tun2socks, DNS filtering, browsing history,
  Network overhaul + gates, LAN pass-through shaping).
- **2026-08-15** — v28 WAN-only shaping + tab persistence: LAN pass-through
  class `1:99` at `lan_rate_mbps` (prio-1, both directions, box's own
  addresses; rate NEVER falls back to the WAN cap); per-direction "0 =
  unlimited"; `quota_active_panel` tab persistence; WAN/LAN speed split in the
  Network tab (`set-lan-rate`, DB setting `shaping_lan_rate_mbps`).
- **2026-08-15** — v27 dashboard batch: per-user exempt-from-quota
  (`users.exempt_quota`), Decline random MACs gate (`is_random_mac` = local
  admin bit), privacy eye (mask MACs + PPPoE creds), logs scroll fix; "Network
  & Quota" → "Network".
- **2026-08-15** — v26 Logs merged into the Admin page (sidebar tab gone).
- **2026-08-15** — v25 Network & Quota merge + default guest speed limit
  (`guest_speed_limit_mbps`, shaper cap on guest users).
- **2026-08-15** — v24 WAN public-IP renewal: `POST /api/wan/renew` +
  auto-renew schedule (5-min floor, default 15, restart-resilient); sidebar
  collapse toggle removed.
- **2026-08-15** — v23 obsidian-glass cobalt UI theme (current look).
- **2026-08-15** — guest-limit cap (default 2) + STOP NEW CONNECTIONS gate
  (registered-but-immediately-admin-blocked).
- **2026-08-15** — v22 matte dark theme + fixed sidebar + masonry (superseded
  by v23's look).
- **2026-08-14** — v0.1.4 in-flight → folded into later releases: `gw_allowed`
  whitelist (box keeps ONLY its VPN-server connection open under a Gateway
  cut; `_learn_vpn_servers` auto-learns peers via `ss`, sticky while the
  switch is on), tun2socks auto-provisioner (pinned v2.7.0 + sha256, SOCKS
  probe — a dead endpoint is reported `no-proxy`, never bridged), plus the
  VPN-share flow audit fixes (whitelist gated on the switch not the tunnel;
  no routing into address-less/stale tunnels; routing pinned to the bridge
  device).
- **2026-08-12** — v0.1.3 hotfix-reshipped: `CFG_HISTORY_LOG` unbound-variable
  .deb postinst abort fixed (assignment before the fragment heredoc).
- **2026-08-12** — **v0.1.3 released** (VPN share + DNS filtering + apt repo).
- **2026-08-12** — VPN share (`quota/vpnshare.py`): one `ip rule` diverts the
  client subnet into route table 200 with default via the box's TUN; LAN
  routes kept direct; a missing tunnel device is NEVER routed into; pin
  persisted (`vpn_share_interface`).
- **2026-08-12** — signed apt repo infra (apt-repo.yml; keygen; gh-pages).
- **2026-08-11** — **v0.1.2 released** (browsing history + All-devices
  overview + theme).
- **2026-08-10** — History-tab-empty root cause: the dnslog parser rejected
  every real line (extra shape `serial ip/port query[` — dnsmasq ≥2.90) AND
  `conf-dir=` was commented in the box's dnsmasq.conf (setup now enables it).
- **2026-08-10** — History tab "All devices" household aggregate (default
  view; `device_id=None` pattern; `all`/`0` aliases).
- **2026-08-10** — v0.1.3-UI theme passes (vivid purple, then pitch-black +
  periwinkle) — superseded by v22/v23.
- **2026-08-10** — per-device browsing history feature (the SYSTEM_FLOW 10
  pipeline).
- **2026-08-10** — blocked Gateway DNS-relay charge fixed: `_program_gateway`
  reordered exemptions → gw_blocked drops → counters LAST (a blocked box's
  relayed/attempted bytes never consume the bundle).
- **2026-08-10** — DEEP ARCHITECTURAL AUDIT (5 agents, read-only): v0.1.1
  baseline clean; deps current (Aug 2026); the lease-less block defect
  CONFIRMED open (see LEGACY_DEBT); perf audit found no timing telemetry,
  on-loop tc/nft subprocess storms; refactor path ordered by blast radius.
- **2026-08-08** — v20 in-flight note (superseded: committed at v0.1.1).
- **2026-08-07** — PPPoE creds preserved on panel applies (empty fields never
  wipe saved creds); Apply dimmed when WAN active + online.
- **2026-08-07** — v19.8: `detect_ppp` judged a LIVE ppp0 as down (operstate
  is carrier-less on PPP) — now judged by negotiated IPv4.
- **2026-08-07** — v19.7: auto PPPoE diagnosis per failure mode; wan-down
  banner names the #1 cause (router not bridged).
- **2026-08-07** — v19.6: internet dot gated on the ppp0 link; creds prefill
  on load.
- **2026-08-06** — v19.5: router admin stays reachable in WAN mode (uplink
  subnet kept LOCAL).
- **2026-08-06** — v19.4: honest WAN banner + creds persisted + internet probe
  (`check_internet`, raw TCP 443, `to_thread`).
- **2026-08-06** — v19.3: cleanup (requirements-dev removed, dead code swept).
- **2026-08-06** — v19.1: pppd daemonization loop fixed (`nodetach`),
  applier-failure rollback, render_config data-loss fixes, `lan_interface`
  index-vs-name bug, Test PPPoE button.
- **2026-08-06** — v19: WAN tab applies the LAN/WAN switch LIVE
  (`quota/netmgr.py` + `scripts/topology.sh`).
- **2026-08-06** — v18: optional WAN "strong" mode (box dials PPPoE itself).
- **2026-08-06** — v17: rogue static-IP detection (`quota/arp_scan.py`) + ARP
  gateway-lock.
- **2026-08-06** — v16: UI redesign + Linux-only sweep + .deb packaging +
  local-traffic-never-counts engine exclusions.
- **2026-08-05** — Linux pivot + bundle-source fix + per-user quota model +
  restart-resurrection fix + speed shaping (v11).

## [LEGACY_DEBT_AND_RISKS] (deep audits 2026-08-08 + 2026-08-10 — pre-breaking-change baseline)
_Not yet fixed — the inventory the refactor phase should address before the
breaking changes land. Line numbers from the audits. Items marked ✔ fixed by
the 2026-08-16 audit-fix batch._

**Dead code (zero production callers):**
- `quota/db.py`: `get_device_by_ip` (:377); `get_ip_for_mac` (:632) + `set_lease`
  (:652) are test-only. **✔ removed 2026-08-16**: `add_topup` (:430),
  `has_bundle` (:665), `Device.is_admin_blocked` (:105), `Device.is_blocked`
  (:75), `get_usage_series`.
- Orphaned endpoints (no UI/JS consumer): **✔ removed 2026-08-16** —
  `GET /api/usage`, `GET /api/usage/{id}`, `GET /api/events` (+ the `events`
  table's ~25 write sites — the Activity tab is gone); `get_usage` is test-only.

**Known open defects (root-cause located, NOT fixed):**
- **Per-device block can silently not cut a lease-less device** — kernel
  `@blocked` is keyed by IP from lease rows; a lease-less device gets `ip=""`
  → never blocked. Only cover is the ARP-lock `known_ips` deny (OFF by
  default, forced OFF in WAN). Matches "per-device block not working".
- **✔ `/api/milestone/notify` IP-ownership gate (2026-08-16)**: the endpoint
  now requires the requester's source IP to own the device whose user the
  milestone belongs to (else 403).
- `/report` is default-ON for the whole client subnet (rogue static-IP device
  reads full household usage + log tail with no session; gate itself is
  sound — documented "trusted LAN" assumption).
- **✔ GATEWAY_MAC quota flag (2026-08-16)**: `evaluate_blocks` skips the
  box's row entirely — the cut is user-resolved, never persisted.
- **✔ Network-tab preview staleness (2026-08-16)**: the WS payload now carries
  a `shaping` key (`{available, applied}`) and `_reshaping_now` falls back to
  the shaper's live state before the first tick; app.js shows "Applying…".

**Performance risks (static audits; no telemetry exists to quantify):**
- **✔ off-loop engine/shaper/ppp/lease/log reads (2026-08-16)**: `shaper
  .update_state` (inside `_shaping_lock`), `engine.update_state`/
  `set_gateway_blocked`, both `detect_ppp` call sites, the dnsmasq lease-file
  read, `_read_log_tail` (`/api/logs` + `/report`) and `/report`'s per-device
  `list_leases()` are all `asyncio.to_thread`'d; `_maintenance_loop` logs a
  warning when a tick exceeds 1 s. **REFUTED**: `check_internet` /
  `check_internet_dns` ARE `asyncio.to_thread`'d — off-loop.
- DB: ~30+ commits/tick (no batching), `get_period_usage_by_user` ×2/tick,
  `get_bundle` ~×5/tick. **✔ events table (2026-08-16)**: `prune_events` drops
  rows older than 30 days on an hourly gate — the unbounded-growth risk is
  gone.
- WS: **✔ payload built once per 5 s tick and shared across all sockets
  (2026-08-16)**; each client still gets 2 snapshots/5 s; app.js does a full
  `innerHTML` rebuild per push.

**Security (low severity for a LAN admin box, but honest):**
- **✔ PBKDF2-SHA256 600k (2026-08-16)**: new hashes use 600k iterations
  (`salt$iters$dk`); legacy 200k hashes verify and auto-rehash on login.
- **✔ login rate limit (2026-08-16)**: 10 failed attempts / 300 s / source IP
  → HTTP 429, per-app in-memory limiter. Cookie still `httponly` +
  `samesite=lax` but no `secure=True`.
- Deps all current (Aug 2026), no applicable CVEs; starlette 1.4.1 is above
  the 2026 advisories (BadHost etc.) but re-pin when a newer starlette ships;
  dev-only pytest is now **9.1.1** (bumped 2026-08-16; httpx 0.28.1 is the
  current latest — the audit's "httpx 0.29" never existed); starlette's
  testclient warns it will deprecate httpx.

**Simplicity debt:**
- 3 sources of truth for bundle & topology (config.yaml + DB + ownership
  flag); two on/off switches for shaping (config + DB).
- WS payload: 26 keys/device with user aggregates duplicated per device; no
  schema versioning / delta projection.
- Topology state written by THREE writers (`netmgr.render_config`,
  `scripts/topology.sh`, `scripts/setup_gateway_kali.sh`) — the bug class
  behind the v18 revert + v19 creds-wipe.
- `run.py` `_maintenance_tick` is a 7-job god method; `quota/db.py` is one big
  schema + CRUD + events + settings file.

**Top break points for the pending breaking change (blast radius order):**
1. `run.py` `_maintenance_tick` + `_sync_shaping` — the single orchestration
   point.
2. `nftables.update_state` / `set_gateway_blocked` cache-gated rebuilds — a
   same-set re-flush opens a short unblock window.
3. `shaping.update_state` / `_state_signature` / `_burst`.
4. `service.resolve_device_state` / `quota_blocked_for` precedence.
5. `api._dashboard_payload` wire format — app.js has no schema check.
6. `db.py` idempotent migrations (must stay re-run safe against a migrated
   box DB).
7. **`quota/engine.py` is the cross-cutting choke point** — a field rename
   there ripples through 1, 2, 3, 5 and 6 simultaneously.

## [KNOWN LIMITS] (honest)
- Counting is approximate ("≈" in the UI) — counters read every ~15 s, so the
  live split lags and bytes are attributed to the device that owned an IP at
  drain time. No throttling — exceeded devices are hard-blocked (kernel drop),
  never throttled.
- **Root required**: nftables + dnsmasq (udp/53 + udp/67) + tc all need root;
  the systemd service and postinst run as root, so only a manual foreground
  run needs `sudo`.
- Subsystems degrade gracefully: no `nft`/root => no counting (DB usage still
  shown); no dnsmasq => no DHCP/DNS; no `tc`/`ifb` => no shaping (quota
  blocks + accounting unaffected). Service is `Restart=always` + systemd.
- **Electric-cut fallback is a liveness trade-off**: devices holding a router
  fallback lease during a gateway outage are not counted/controlled
  (intentional — keeps devices online when the gateway is down).
- **IPv4 only**: IPv6 Router Advertisements come straight from the router and
  never cross the gateway (uncounted, unblockable). The gateway's IPv6 is
  disabled by setup, but the ROUTER's IPv6/RA must be disabled too.
- **Static-IP bypassers are denied, not magically fixed**: the ARP gateway-lock
  cuts internet to router-gateway static-IP devices, but a static ARP entry or
  an uplink-subnet static IP still evades it (surfaced as rogue). Router MAC
  filtering / client isolation is the durable complement; hostapd or Strong
  (WAN) mode is the airtight topology.
- **Strong (WAN) mode is opt-in and needs the router hands-on**: the physical
  router rewiring (bridge/AP) can't be done from any panel — applying WAN
  while the router isn't actually bridged cuts internet until it is. A failed
  apply keeps the box up and surfaces stderr in the WAN tab. A PPPoE outage
  takes internet down until ppp0 redials.
- **Speed shaping needs real line rates**: set the Network-tab totals to the
  real line down/up — only then does the queue form where `fq_codel` can keep
  pings low under load. `tc` rates are approximate; the single-NIC egress tree
  shares bandwidth between uplink traffic and client downloads.
- **The ARP-RTT WiFi/LAN label is statistical, not measured**: a fast 5G
  device can read LAN (lower `threshold_ms`), a loaded 2.4 GHz network can
  spike both classes, and ICMP-blocking clients are unclassified without
  root (raw ARP). The streak guard kills flapping, the label only drives the
  display chip — quota enforcement never depends on it. Only a
  monitor-capable card (wifi_probe) gives the exact SSID.
- **The box's own internet is metered by default** (`engine.count_gateway`,
  default ON): box traffic is counted + charged to the protected Gateway user
  (fixed 1.0 GB), and that 1.0 GB is silently deducted from every auto-share
  bundle (behavioral change on upgrade). A Gateway block cuts the box's own
  internet only (clients unaffected). `count_gateway: false` skips counters
  but keeps drops.
- **/milestone is public and /report is source-IP-gated, not session-gated**:
  /milestone shows only the requester's own user; /report (any client-subnet
  source or `report.allowed_ips`) shows full household usage + events + log
  tail with no admin login. Both assume a trusted LAN — keep the box's
  dashboard port LAN-only.
- **LAN mode needs a fixed uplink address on the box**: router DHCP
  reservation for the machine's MAC or a static address (setup sets
  `192.168.1.110` and verifies it). If the box's IP can change, every client
  loses its gateway + DNS and the dashboard is unreachable. The uplink
  address must sit outside the router's DHCP pool. Not an issue in WAN mode.