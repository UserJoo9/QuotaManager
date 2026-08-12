# CLAUDE SYSTEM MAP — Quota Manager

Gateway that splits a metered internet bundle (e.g. Egypt 140 GB/month) fairly
across USERS — a person's allowance covers all of their devices (phone +
tablet + laptop share one slice). When a user exceeds their allowance, every
device they own is cut at once; a per-device override can exempt a single
device. Admin dashboard: dark glassmorphism web UI (purple-tinted obsidian
gradient, vivid purple neon accents, dark frosted-glass cards, stacked user
cards). Deployment target: **Linux on an old
laptop** (Kali/Debian) — the kernel owns the network path.

## [TECH_STACK]
- Python -> **3.11** (runtime venv; 3.10+ supported by all deps)
- fastapi -> **0.141.1** (REST + WebSocket + static UI)
- uvicorn -> **0.52.1** (ASGI server, `[standard]` includes websockets)
- aiosqlite -> **0.22.1** (async SQLite, WAL mode)
- PyYAML -> **6.0.3** (config)
- auth -> stdlib `hashlib.pbkdf2_hmac` (single admin, session cookie)
- tests -> pytest **8.x** + fastapi TestClient

**Linux only**: `dnsmasq` (DHCP + DNS), `nftables` (client-subnet NAT +
accounting + hard drop), `tc` (speed shaping), Python 3.10+ runtime. Deps:
`requirements-linux.txt`.

## [SYSTEM_FLOW]
The router keeps WiFi + NAT, **DHCP disabled**. Clients join the router's WiFi
as usual, but their default gateway + DNS is the gateway box, so every byte
crosses it. The box puts clients on their **own subnet**
(`192.168.2.0/24`, gateway = the box's real address) and masquerades them out
the uplink — deterministic, no proxy_arp.

**Linux (target)**
1. `scripts/setup_gateway_kali.sh` configures: `ip_forward=1` + IPv6 off
   (sysctl), a static uplink IP **and** a client-subnet alias on the LAN NIC
   (auto-detects the wired NIC — Ethernet + carrier, skips WiFi/VPN; verifies
   the addresses actually landed), dnsmasq (DHCP pool + DNS forwarder,
   gateway/DNS = the box; `dhcp-authoritative` so devices migrate off stale
   router leases fast; dual upstreams = router + 8.8.8.8; lease length from
   `LEASE_HOURS`), a masquerade NAT table for the client subnet, systemd
   drop-ins (dnsmasq waits for `network-online.target`; nftables `ExecStop`
   scoped to `quota_nat` only so it never wipes the live app table), and a
   systemd unit for the app. **The box's uplink address must be fixed**: the
   script sets it static (nmcli / `/etc/network/interfaces`, default
   `192.168.1.110`, verified landed) — and on a VM the same address should also
   be **reserved on the router** for the machine's MAC, since a drifting or
   clashing address is an access outage (clients lose their gateway + DNS; the
   dashboard is unreachable).
2. `run.py --config config.yaml` starts. Clients are on their own subnet
   (`192.168.2.0/24`, gateway = the box's real address) and the kernel
   masquerades them out the uplink — every byte deterministically crosses the
   box with **no proxy_arp** (the kernel's proxy_arp refuses same-subnet
   targets, which silently let downloads bypass the box; the separate subnet
   fixes it).
3. The **nftables engine** (`quota/nftables.py`) programs one named counter
   pair per device (`q_up_<ip>` / `q_down_<ip>`, dots→underscores) in the
   `forward` chain and a `blocked` set. The kernel counts at line rate; the
   app only reconciles rules and reads `nft -j list counters` (JSON) on a
   15 s tick. **Local (LAN) traffic never counts**: the counting rules exclude
   the client subnet and the uplink subnet (`ip daddr/saddr != <local-net>`;
   `engine.client_subnet` / `engine.uplink_subnet`, derived from the `dhcp`
   block when unset — the setup script writes both explicitly), so traffic
   between clients (L2 anyway) and client↔uplink-LAN hosts (router admin, NAS,
   router-as-DNS) never consumes the bundle — only internet-bound bytes are
   charged. The two `@blocked` drop rules carry the same exclusions, so a
   quota-blocked device keeps LAN access while its internet is cut. The
   `blocked` set is rebuilt only when its membership changes
   (`_last_blocked_ips` cache) — a same-set re-flush every tick would open a
   short unblock window. The app's table (`inet quota_gateway`) is separate
   from the setup-owned NAT table (`inet quota_nat`), so the two never
   conflict. **Restart-safe accounting**: `flush table` deletes rules but NOT
   named counter objects (they keep their cumulative totals), and the in-memory
   delta baseline (`_last`) is lost on restart — so `start()` best-effort runs
   `nft reset counters` to zero surviving counters, and `_add_device()`
   re-seeds `_last` from any counter that still carries a pre-restart total.
   Without this, the first drain after a restart re-added the whole old total
   to `usage_daily` (a consumed-and-reset quota came back on every restart).
4. Every ~15 s the maintenance loop: rolls the quota period if stale (or
   skips when `reset_day=0`) → syncs device bindings from dnsmasq's lease
   file (`dhcp.lease_file`) → drains engine counter deltas into `usage_daily`
   → re-evaluates block states → pushes fresh ip→mac / blocked maps into the
   engine + snapshot holder. **Every 60 s** (slower than the tick on purpose)
   it also runs a **rogue LAN scan** (`quota/arp_scan.py`): a raw-socket ARP
   probe of both local subnets, and any active host NOT in the lease file is
   surfaced in the snapshot's `rogue` list (dashboard "Unmanaged / rogue
   devices" card + a `warning` event on first sight) — a static-IP bypasser
   that never asks the quota DHCP is otherwise invisible.
5. A blocked device: the engine adds its IP to the `blocked` set and the
   kernel **drops** its forward-chain packets — hard internet cut at line
   rate. Admin toggles work the same way.
6. **Speed shaping** (`quota/shaping.py`, Linux only) is a second kernel-side
   stack that never touches nftables: the maintenance loop feeds it the live
   ip→device→user map plus the Network-tab settings, and `TcShaper` reconciles
   an **HTB + fq_codel** tree on the single NIC. One NIC carries both the uplink
   IP and the client-subnet alias, and NAT changes which address is visible at
   each point, so the two directions use two trees: **uploads** (client→internet)
   are redirected at NIC **ingress** (src still pre-NAT client IP) into `ifb0`
   and shaped there by `ip src`; **downloads** (internet→client) are shaped at
   NIC **egress** by `ip dst` (conntrack already un-NAT'd). Both are HTB with
   `fq_codel` on every leaf: per-device leaves (`1:<0x8000+devid>`) sit under
   per-user classes (`1:<0x300+uid>`, capped at the user's aggregate), under a
   download aggregate (`1:100`), under a root capped at the **real line speed**
   from the Network tab — the effective cap is `min(dev, user)` and the default
   class is capped at the direction total (NOT a pass-through), so an unlimited
   downloader cannot flood the modem buffer and inflate everyone's ping. The
   tree is rebuilt only when a signature of (enabled, totals, aqm, sorted caps)
   changes — same idempotent-reconcile pattern as the nftables `_last_blocked_ips`
   cache. Shaping sits after nftables in the packet path: blocked devices are
   already dropped in `forward`, and counters see the real pre-NAT src / post-NAT
   dst either way.
7. FastAPI + uvicorn serves the dashboard + REST API + `/ws` push (5 s
   snapshots). WebSocket is the live-update channel; the client also polls as
   a fallback.
8. **ARP gateway-lock** (`engine.gateway_arp_lock`, default OFF in config.yaml
   but ON in the setup-generated config): a device that sets a static IP + the
   ROUTER as its gateway sends its frames straight to the router at L2 — the
   box never sees a byte (uncounted, unblockable, invisible). The lock closes
   that with two pieces that never touch the counters: a background responder
   (`quota/arp_lock.py`, raw-socket thread) claims the router's IP on the
   CLIENT subnet — it answers client-subnet ARP requests for the router with
   the box's own MAC — and an `arp`-family nftables rule drops the router's
   competing replies to client-subnet hosts; plus an engine deny rule
   (`quota/nftables.py`) drops any forwarded client-subnet source that is NOT
   in the `known_ips` set (= the leased DHCP IPs). The bypasser's frames
   therefore arrive at the box and are dropped — its internet is cut until it
   uses the quota gateway. The deny is self-sustaining (dropped traffic makes
   the rogue re-ARP, and it is re-answered). Only client-subnet requesters are
   answered, so uplink-subnet hosts (NAS, the router) keep the real router; a
   static ARP entry or an uplink-subnet static IP still evades capture
   (surfaced as a rogue instead; the router-side MAC allowlist is the durable
   complement). The `known_ips` set is rebuilt only when its membership changes
   (same cache pattern as `_last_blocked_ips`).
9. **Strong (WAN) mode** (`engine.topology=wan`, optional, OFF by default): the
   box dials the PPPoE line itself (systemd `quota-wan-ppp.service` runs `pppd
   call quota-wan`; the public IP lands on `ppp0`) and the router is demoted to
   a pure bridge/AP — a static-IP device then has NO second router to bypass
   to. Brought up by the setup script with `QUOTA_TOPOLOGY=wan` (+ optional
   `WAN_IF` for the two-NIC layout, `PPPOE_USER`/`PPPOE_PASSWORD`; the password
   goes in `/etc/ppp/{chap,pap}-secrets`, chmod 600, never the peer file).
   **v19: the dashboard WAN tab applies the switch LIVE** — `quota/netmgr.py`
   (`TopologyManager`) collects the PPPoE creds in the panel, rewrites
   config.yaml + the DB setting TOGETHER (`topology_source=dashboard` +
   `topology`, so the next boot can never pick one and ignore the other — the
   v18 revert bug), runs the runtime applier `scripts/topology.sh` (NIC +
   dnsmasq + the PPPoE dial, creds via the ENVIRONMENT never argv), and
   schedules a detached self-restart. "Revert to LAN" restores the exact LAN it
   left from the `dhcp.lan_*` + `engine.lan_gateway_arp_lock` snapshot keys
   (never a guess at 192.168.1.1). The setup script stays only for first
   install + the physical router rewiring (bridge/AP), which no panel can do.
   Under wan: the box KEEPS the uplink IP as a secondary router-admin alias, so
   the nftables engine's `resolve_local_networks` treats the uplink subnet as
   LOCAL (explicit `engine.uplink_subnet` wins; else derived from the LAN
   snapshot `dhcp.uplink_ip`+`dhcp.lan_cidr`, else `dhcp.router_ip`) — router-
   admin traffic never consumes quota, and this is NOT a bypass (the masquerade
   only covers the client subnet, so an uplink-subnet source is never NATed out
   ppp0). The ARP gateway-lock is forced off (engine `__init__` + run.py's
   startup gate, so the raw-socket thread never starts), the rogue scanner
   probes only the client subnet, and `quota/topology.py`'s `detect_ppp`
   reports the ppp0 link state into the snapshot's `wan_status` (surfaced by
   `/api/wan` + the WAN tab). The default LAN topology is byte-for-byte
   unchanged.
10. **Per-device browsing history** (`quota/dnslog.py`, ON by default): the box
   is already every client's only resolver, so the setup script installs an
   **app-owned dnsmasq fragment** (`/etc/dnsmasq.d/quota-dnslog.conf` —
   `log-queries=extra` + `log-async=20` + `log-facility=/var/log/quota-dnsmasq.log`;
   both scripts only ever rewrite `quota-gateway.conf`, so the fragment survives
   setup re-runs and WAN/LAN toggles; the script also **enables `conf-dir=` in
   `/etc/dnsmasq.conf`** when it is commented/missing — dnsmasq otherwise
   silently ignores every fragment, the live-box failure behind an empty
   History tab) plus a **logrotate** snippet
   (`copytruncate`, size 5M, rotate 3 — bounds the raw file ≤ ~20 MB even if
   the app is down). A **dedicated tailer thread** (`DnslogTailer`, the
   `arp_lock.py` pattern) polls the log every 0.5 s, strips the `\x00` sparse
   hole `copytruncate`+`log-async` can leave, caps the partial-line buffer at
   1 MB, and pushes parsed `(minute, ip, domain)` events onto a **bounded queue
   (`put_nowait`/`except queue.Full`) — overflow drops lines, never blocks DNS
   or the event loop**. The parser accepts both the bare shape
   (`query[A] example.com from 192.168.2.100`) and the verbose extra shape
   where dnsmasq ≥2.90 stamps the client ip/port between the serial and
   `query[` (`1 192.168.2.186/16773 query[A] …`). Each ~15 s tick
   `_dns_history_tick` drains the queue,
   resolves each distinct IP to a device via the leases join (rogue/lease-gap
   IPs skipped), batch-upserts per-(device, minute, domain) counts into the
   `dns_history` table, persists the read cursor (`dnslog_state` setting →
   restart-resume; first start seeks to EOF so pre-feature lines are never
   attributed), and — past a 1 h gate — prunes each user's rows at **their**
   `history_days` (NULL = the global `history.retention_days`; the per-user
   scoping means a short retention never wipes a longer one). The dashboard
   **History tab** (`GET /api/history/{device_id}`, auth-gated) shows top
   domains, hourly activity and recent queries; bandwidth reuses the existing
   per-device snapshot fields — nothing new is tracked for bytes. Rotation
   (new inode or size shrink) resets the tailer cursor; a missing log file is
   not an error (the app degrades gracefully if the fragment isn't installed).
   `history.enabled: false` stops recording entirely. The tab also offers a
   household **All devices** overview (default): `GET /api/history/all` (alias
   `0`) aggregates `dns_history` across every device — combined top domains +
   total queries, and recent rows badged with their owning device/user
   (`get_dns_history(device_id=None)`, the `get_usage_series` None pattern).

**Quota model (per user)**: the monthly allowance lives on a **user**, not a
device (`users` table; `devices.user_id` links them). Auto users equally share
the bundle remainder after fixed users take their GB off the top; a user's
usage is the sum of their devices' usage (join `usage_daily`→`devices`→`user`).
When a user exceeds their allowance, every device they own is cut together.
The cut is **resolved** at render/enforcement time (`service.resolve_device_state`),
never written to device rows — so a user-level admin cut is lossless and
clearing it restores all devices. A per-device `bypass` keeps one device online
despite its user's quota block; an explicit per-device admin cut always wins
(precedence: user admin > device admin > user quota unless bypass > ok).
Enforcement stays per-MAC/per-IP — the engine's `blocked` set still drops
packets at line rate; only the block *decision* is per-user. New DHCP devices
auto-create their own user (one device ⇒ one user); legacy device-only DBs are
migrated in place by `db.connect()` (idempotent ALTERs + backfill).

**Bundle source (fixed)**: `config.yaml` is the default source of truth for
`bundle.total_gb` / `bundle.reset_day` and is re-applied on **every** startup
(`run.py: _seed_bundle_from_cfg`). Once the admin edits the bundle or
recharges via the dashboard (`POST /api/bundle`), a `bundle_source` setting
is set to `dashboard` and config.yaml stops overriding it — so a UI edit or
recharge survives a restart, and a YAML edit actually reaches the UI.

**No-auto-reset (`reset_day=0`)**: the period opens once and never rolls by
itself; the bundle grows only via the dashboard "Bundle recharged" action
(`service.recharge(add_gb)`, keeps `period_start`) and a new month starts only
via the manual "Reset month now" action.

**Electric-cut fallback (optional)**: the gateway is a single point of
failure, so the router can be left serving a small non-overlapping DHCP pool
(gateway = router). dnsmasq serves only the client subnet (192.168.2.x) while
the router's fallback pool sits on the uplink subnet (192.168.1.x) — no overlap
by construction. Devices fall back to direct internet during a gateway outage,
and re-join the managed pool as their leases renew. Recovery speed is governed
by the DHCP lease length: re-run setup with `LEASE_HOURS=1` for fast
re-adoption (the `lease_hours` YAML key is informational — dnsmasq owns the
real lease). Trade-off: fallback-leased devices are not counted/controlled
while the gateway is down.

**Packaging + releases**: the `.deb` (`quota-manager_<ver>_all.deb`) is built
**only** by GitHub Actions — `.github/workflows/release.yml` renders
`packaging/DEBIAN/control` from `quota/version.py` (the single source of truth;
a `v*` tag must match it or the workflow fails loudly), stages the runtime
payload (run.py, core/, quota/, api/, web/, scripts/, requirements-linux.txt,
LICENSE) into `/opt/quota-manager`, runs `dpkg-deb --build --root-owner-group`,
and uploads to GitHub Releases — the release description is auto-composed from
the released version's `CHANGELOG.md` section (plus the install note), so the
release notes and the changelog never drift. `packaging/DEBIAN/postinst` builds
the venv,
runs `setup_gateway_kali.sh` with `QUOTA_NO_APT=1` (the package `Depends`
already pulls dnsmasq/nftables/iproute2/kmod/python3-venv), and enables +
starts `quota-gateway`. `prerm` stops/disables the service on remove/upgrade.
Postinst/pg upgrade paths are idempotent and preserve `/etc/quota-gateway/
config.yaml` + `/var/lib/quota-gateway/quota.db`. **A second workflow,
`apt-repo.yml`, turns the Release into a signed apt repo** (`workflow_run` on
`release` + a `workflow_dispatch` `version` backfill input): it imports the
armored private key from the `APT_REPO_GPG_KEY` secret, downloads the released
`.deb`, regenerates `Packages`/`Release` with `apt-ftparchive`, signs
(`Release.gpg` + clearsigned `InRelease`), and pushes the whole repo to the
`gh-pages` branch (with `.nojekyll`, so Jekyll never strips `pool/`/`dists/`)
hosted at https://UserJoo9.github.io/QuotaManager/ — a one-time
`deb [signed-by=…] …` source line then makes `apt-get install quota-manager`
work and every future tag land via `apt update && apt upgrade`. The PUBLIC key
is committed at `quota-manager.gpg`; old versions stay installable. Tests:
`tests/test_packaging.py` pins the workflow + control + lifecycle-script
contract (no dpkg needed), incl. the apt-repo workflow + public key.

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
├── run.py                    # Gateway wiring: engine + maintenance + uvicorn
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
│   │                         #   dns_history table + per-user history_days
│   ├── engine.py             # shared snapshot types (Linux): EngineCounters,
│   │                         #   RogueHost, EngineSnapshot, SnapshotHolder +
│   │                         #   GATEWAY_MAC sentinel — the thread-safe handoff
│   │                         #   between the kernel-side engine and asyncio;
│   │                         #   the type hub (imported by db/nftables/arp_scan/
│   │                         #   api/run; a field rename ripples everywhere)
│   ├── service.py            # per-user quota math (allowance on the user,
│   │                         #   usage = Σ devices), block fan-out + bypass
│   │                         #   precedence, top-up, recharge, reset_day=0,
│   │                         #   period roll; shaping settings (get/set)
│   ├── nftables.py           # NftablesEngine (Linux): kernel counters + block
│   │                         #   + ARP gateway-lock deny rules (known_ips set);
│   │                         #   set_vpn_relay: suspends the box's OWN gateway
│   │                         #   metering while VPN share relays the household
│   │                         #   (accept FIRST in input/output, by handle)
│   ├── vpnshare.py           # VpnShareManager: "VPN share" policy routing —
│   │                         #   client subnet -> dedicated route table whose
│   │                         #   default points at the box's TUN (sing-box/
│   │                         #   xray/WireGuard), local LAN routes kept,
│   │                         #   idempotent reconcile self-heals leftovers
│   ├── shaping.py            # TcShaper (Linux): per-device + per-user speed
│   │                         #   caps + low-latency queues (HTB + fq_codel),
│   │                         #   single-NIC two-tree design (see SYSTEM_FLOW)
│   ├── arp_scan.py           # rogue static-IP detection: raw-socket ARP probe
│   │                         #   of both LAN subnets -> hosts not leased by DHCP
│   │                         #   (shared frame build/parse + resolve_nic helpers)
│   ├── arp_lock.py           # ARP gateway-lock responder: claims the router's
│   │                         #   IP on the client subnet so bypassers' frames
│   │                         #   arrive at the box (raw-socket thread)
│   ├── dnslog.py             # DNS browsing history: dnsmasq query-log parser
│   │                         #   + DnslogTailer thread (dedicated thread,
│   │                         #   bounded queue, rotation-safe) -> dns_history
│   ├── dns_rules.py          # DnsRuleManager: host-based domain filtering —
│   │                         #   block/allow/redirect per user or device, ABP
│   │                         #   blocklist presets, rendered into dnsmasq
│   │                         #   config (conf-file -> rules/*.conf)
│   ├── topology.py           # WAN-topology detection: detect_ppp() reports
│   │                         #   whether ppp0 is up + its address pair (WAN tab)
│   ├── netmgr.py             # TopologyManager (v19/19.1): the dashboard WAN
│   │                         #   tab's live LAN/WAN switch — config.yaml + DB
│   │                         #   written together, runs scripts/topology.sh,
│   │                         #   detached restart, applier-failure ROLLBACK;
│   │                         #   lan_* snapshot keys power Revert; test_pppoe()
│   │                         #   dials a throwaway ppp200 link (creds check)
│   ├── vendor.py             # MAC OUI -> manufacturer (IEEE registry, lazy)
│   ├── oui.txt               # bundled IEEE MA-L/MA-M/MA-S database (53.5k prefixes)
│   └── version.py            # single source of truth for the release version
├── api/
│   ├── app.py                # FastAPI factory: REST + /ws + static mount +
│   │                         #   /milestone (public, own-user) + /report (IP-gated)
│   └── schemas.py            # pydantic request models
├── web/
│   ├── index.html            # login + dashboard + modals
│   ├── milestone.html        # public milestone page (requester's OWN user only)
│   ├── report.html           # source-IP-gated household usage report (no session)
│   └── assets/
│       ├── styles.css        # dark purple glassmorphism
│       └── app.js            # WS client, dashboard render, user-grouped
│                             #   device cards, user + device controls
└── tests/
    ├── test_vpnshare.py       # VpnShareManager vs a fake `ip` binary: rule/route
    │                           #   program, peer parsing, pin, reconcile, teardown
    ├── test_dns_rules.py      # hosts/ABP parsing, wildcard scopes, rendering
    ├── test_quota_service.py # period math, per-user allowance math, block
    │                         #   fan-out + bypass, recharge
    ├── test_api.py           # REST API integration (incl. user CRUD, recharge,
    │                         #   reset-day-0, bundle_source ownership)
    ├── test_web_ui.py        # static UI served (tabs, Network panel, v15/v16)
    ├── test_shaping.py       # TcShaper vs a fake `tc` binary (commands)
    ├── test_packaging.py     # release workflow + control/postinst/prerm +
    │                         #   QUOTA_NO_APT contract (no dpkg needed)
    ├── test_vendor.py        # OUI -> vendor lookup (MA-L/MA-M/MA-S longest-prefix)
    ├── test_config.py        # typed config parsing (Linux settings)
    ├── test_nftables.py      # NftablesEngine vs a fake `nft` binary
    ├── test_arp_scan.py      # rogue static-IP detection (fake raw sockets)
    ├── test_arp_lock.py      # ARP gateway-lock responder (fake frames)
    ├── test_dnslog.py        # dnsmasq query-log parser + tailer + dns_history DB
    ├── test_netmgr.py        # TopologyManager WAN/LAN apply + rollback + PPPoE test
    ├── test_topology.py      # detect_ppp / check_internet probes
    ├── test_users_migration.py # legacy device-only DB → users backfill
    │                         #   (idempotent, data-preserving)
    └── test_run_wiring.py    # run.py wiring + live boot + bundle reconcile +
                              #   dnsmasq lease sync + live-counter regression
```
Dependencies point downward only: `api -> quota/core`, `quota -> core`.
Engine ↔ asyncio communicate through thread-safe counter snapshots (no locks in
the packet hot path). On Linux the hot path has **no Python at all** — the
kernel counts and drops.

## [ORPHANS & PENDING]
_Pending work lives in TASKS.md; orphans + debt are tracked in
[LEGACY_DEBT_AND_RISKS] below. Version history (newest first):_

Checked 2026-08-12 (**v0.1.4 hotfix** — `.deb` installs died at the dnslog step):
- [x] **`setup_gateway_kali.sh: CFG_HISTORY_LOG: unbound variable` (FIXED,
      release-blocking)**: the script runs `set -euo pipefail` but assigned
      `CFG_HISTORY_LOG` only in step 6 (config.yaml, :740 old), while step 4.5
      renders it into `/etc/dnsmasq.d/quota-dnslog.conf` (`log-facility=…`,
      logrotate heredoc) — every .deb postinst aborted at the 4.5 heredoc
      (`dpkg: error processing package quota-manager`, "1 not fully installed
      or removed"). The default is now defined BEFORE the fragment is written
      (4.5 start) and reused by config.yaml; `test_packaging.py` pins
      assignment-before-first-use (`test_setup_script_defines_cfg_history_log_before_use`,
      21 packaging tests green). The venv pip retries in the same install log
      were the box's own DNS being down — environmental, pip succeeded.
- [x] version **0.1.3 → 0.1.4** + CHANGELOG `[0.1.4]` (Fixed) + tagged
      **`v0.1.4`**; the release body composes from the changelog section as
      usual. Box recovery = re-run the 0.1.4 install (postinst re-runs the
      idempotent setup).

Checked 2026-08-12 (**v0.1.3 released** — VPN share + DNS filtering + the apt repo shipped):
- [x] **version bumped `0.1.2` → `0.1.3`** (`quota/version.py`, the single source
      of truth) and tagged **`v0.1.3`**. The release ships everything that was
      uncommitted/unreleased: the **VPN share** bundle (the entry below), the
      **DNS filtering** feature (merged at `3eb999d` — domain rules, blocklist
      presets, per-client DNS servers — its first release), and the **signed
      apt repository** (the entry below — its first auto-published tag; the
      `workflow_run` publishes this Release's `.deb` to gh-pages, so
      `apt-get update && apt-get install quota-manager` now upgrades to v0.1.3;
      the earlier "no version bump, no tag / stays 0.1.2" notes below are
      superseded history).
- [x] **release description = CHANGELOG** (existing behaviour, worked as
      designed): the `[0.1.3] — 2026-08-12` section (VPN share + DNS filtering +
      apt repo bullets, one per feature) was composed into the Release body with
      the install note — no drift, nothing hand-written.
- [x] **docs synced for the release**: README (Network tab row + DNS tab row,
      a **VPN share** section with tunnel-up-first + "no silent fallback" notes,
      a DNS filtering paragraph + troubleshooting + Known-limits bullets, ToC +
      `.deb` example → 0.1.3); Structure_README (a full **VPN share** design
      section, the `vpn_share:` config block, the `/api/network` vpn_share
      payload, the maintenance-loop `_sync_vpn_share` paragraph, test-tree +
      suite entries for vpnshare/dns_rules); CLAUDE.md this entry + the
      vpnshare entry's release marker.
- [x] full suite green per-file at the release baseline (~399 total; 69 API,
      41 nftables, 24 vpnshare, 39 run-wiring, 28 dns-rules, 7 web-ui, 20
      dnslog, 21 netmgr, 19 packaging, 18 vendor, 15 shaping, 15 topology, 14
      arp_scan, 10 config, 8 arp_lock, 6 users_migration, 45 quota_service …) —
      the known Windows `test_dnslog` exit hang is unchanged; pyflakes +
      `node --check` clean.

Checked 2026-08-12 (**VPN share** — the whole client subnet through the box's tunnel, uncommitted bundle):
- [x] **`quota/vpnshare.py` (NEW)**: `VpnShareManager` — "VPN share" policy
      routing. The box runs a VPN client in TUN mode (sing-box/xray, WireGuard,
      or tun2socks); with the Network-tab switch on, ONE `ip rule` diverts the
      client subnet into a dedicated route table (`vpn_share.route_table`,
      default 200, below `local`/`main`) whose default route points at the
      tunnel — every device's internet exits at the VPN provider's IP while
      nftables forward-chain counting/blocking + tc shaping keep working.
      Pure subprocess/sysfs, no threads: `reconcile(enabled, pin)` applies/
      removes idempotently (self-heals a crash/reboot/tunnel-restart leftover
      on the next 15 s tick), `peer_ip()` caches the tunnel's point-to-point
      peer (`ip -o -4 addr`), LAN direct routes (client + uplink subnets) are
      kept OUT of the tunnel via `lan_interface()` (mirrors the nftables
      local-net exclusions), a missing tunnel device is NEVER routed into
      (that would blackhole the subnet — the rule only lands after sysfs
      confirms the interface), and `remove()`/`is_rule_installed()` tear down
      deterministically. The auto-detected tunnel is PINNED in the DB
      (`vpn_share_interface`) so a multi-VPN / restarted box re-applies the
      same interface (`vpn_share.interface` config = the initial pin).
- [x] **gateway-meter suspension** (`NftablesEngine.set_vpn_relay`, vpn share
      only): the relay volume traverses the box's OWN input/output hooks a
      second time (client → forward → tunnel → uplink), which would be charged
      AGAIN to the protected "Gateway" user — and a quota-cut Gateway's
      `gw_blocked` drop would kill the household's VPN. Suspension = an
      unconditional `comment "quota-vpn-relay" accept` **inserted FIRST** in
      the input/output chains (above DNS/DHCP exemptions, gw_blocked drops and
      q_gw counters), restored by deleting exactly that rule **by handle**
      (`nft -a list chain` lookup, cache-gated like `set_gateway_blocked`);
      a failed insert is never claimed (retry next tick), forward-chain
      per-device quota + ARP lock stay untouched. Enforced from the APPLIED
      relay state, not the switch.
- [x] **wiring** (`run.py`): `_sync_vpn_share` (boot + every maintenance tick
      + Network-tab toggle via `_apply_vpn_now`) reads the DB switch under
      `_vpn_lock`, reconciles off the event loop, persists the pin, feeds
      `engine.set_vpn_relay(status.state == "on")`, and caches
      `_last_vpn_status` for the API. `vpn_share.enabled: false` in config =
      manager never built (degraded boot no-op).
- [x] **settings + API + UI**: `service.get/set_vpn_share` (settings keys
      `vpn_share_enabled`/`vpn_share_interface`, warn event on enable);
      `NetworkUpdate.vpn_share` (optional bool — partial POST); `GET/POST
      /api/network` carry `vpn_share: {enabled, interface, status?}` (status
      from run.py's cached probe, absent when no manager is wired); Network
      tab gains the VPN-share switch + applied-state line (`renderVpnShare`,
      `?v=39`/`?v=34` cache busts); config.yaml `vpn_share:` block documented.
- [x] **tests**: `tests/test_vpnshare.py` (24 — fake `ip` binary: rule/route
      program order, `peer_ip` both shapes, LAN routes via lan_interface,
      dev-only + scope-link fallback, via-peer, error propagation, pin
      honoring, reconcile on/off idempotence + self-heal, teardown);
      `test_nftables.py` +6 relay tests (accepts-first inserts, handle
      verification, claimed-only-when-held, exact-handle restore keeping
      unrelated rules, missing-rule honesty; FakeNft now answers
      `nft -a list chain` + simulates insert/delete); one run-wiring test
      (switch → reconcile → pin persist → relay suspension/restore);
      one API test (`/api/network` vpn_share toggle round-trip); web_ui
      cache-bust pins updated. **Session bugs fixed while landing these**:
      the `peer_ip` regex demanded `inet <local>/<prefix> peer` but `ip -o`
      prints a bare local address (real boxes never matched — returned "");
      the dev-only fallback's first `ok()` failure left `state=ERROR` even
      when the `scope link` retry succeeded (reworked to explicit
      code-based retry, success resets to ON); `test_api.py`'s milestone/
      report pins were stale (?v=37 vs the served ?v=38) + test_web_ui pins
      (?v=33/v38 vs served ?v=34/v39) — cache-busts had advanced but their
      tests hadn't. Full suite green per-file (69 API, 41 nftables, 24
      vpnshare, 39 run-wiring, 7 web-ui, 28 dns-rules …) — the known Windows
      `test_dnslog` exit hang (20/20 pass, process lingers) is unchanged;
      pyflakes + `node --check` clean. **Now SHIPPED in v0.1.3** — released
      2026-08-12 (see the entry above).

Checked 2026-08-12 (signed apt repository — `apt install quota-manager` works):
- [x] **infrastructure, NOT a release — no version bump, no tag** (`quota/version.py`
      stays `0.1.2`; everything lands under CHANGELOG `[Unreleased]`). The GitHub
      Release is now also a **signed apt repo** on the `gh-pages` branch (hosted
      at https://UserJoo9.github.io/QuotaManager/), so a one-time
      `deb [signed-by=…] …` source line gives `apt-get install quota-manager`
      and `apt update && apt upgrade` for every future tag; old versions stay
      installable.
- [x] **`.github/workflows/apt-repo.yml` (NEW)** — a `workflow_run` on the
      `release` workflow completing `success` (no race with the .deb asset) plus
      a `workflow_dispatch` `version` input for backfills. Permissions:
      `contents`/`pages`/`actions`; `concurrency: group: apt-repo-publish`
      serializes gh-pages pushes. Steps: save the committed public key → resolve
      the version (workflow_run has no ref; reads the triggering run's
      `headBranch` tag via `gh run view`, falls back to the latest release) →
      `gh release download` the `quota-manager_<ver>_all.deb` → import the
      armored private key from the `APT_REPO_GPG_KEY` secret (isolated
      `GNUPGHOME`, empty-passphrase key, `--local-user $FPR`) → orphan/fetch the
      `gh-pages` working tree → stage `quota-manager.gpg` + `.nojekyll` +
      `pool/main/q/quota-manager/*.deb` → `apt-ftparchive packages` +
      `gzip -9n` + `apt-ftparchive release` (`Suite: stable`, `Architectures:
      all`, `Components: main`) → `--detach-sign` (`Release.gpg`) +
      `--clear-sign` (`InRelease`) + `gpg --verify` → commit (idempotent
      `git diff --cached --quiet` guard; `git add -f` so no stray `.gitignore`
      blocks the pool) + push → **Ensure GitHub Pages** via the API (POST on
      first run / PUT thereafter; `continue-on-error`, manual Settings fallback).
- [x] **signing key**: generated locally this session (RSA-4096, empty
      passphrase, `Quota Manager` / youssef.alkhodary@users.noreply.github.com).
      Public key committed at the repo root as **`quota-manager.gpg`** (copied
      into the gh-pages tree every run so clients fetch it from the same host);
      the private key went to the user's Desktop (`quota-manager-secret.asc`,
      outside the repo) for pasting into the `APT_REPO_GPG_KEY` Actions secret.
- [x] **`release.yml`**: the release-body install note gained an
      "Or, with the apt repository configured (see README)" `apt-get update &&
      apt-get install quota-manager` line (future tags only; v0.1.2's published
      note unchanged).
- [x] **tests**: `tests/test_packaging.py` +6 — `apt-repo.yml` exists/parses,
      triggers on `release` completion, manual `version` backfill input,
      downloads the .deb from Releases (`gh release download` +
      `GH_TOKEN`), signs+publishes (every needle: `apt-ftparchive`, `gnupg`,
      `--detach-sign`, `--clear-sign`, `Release.gpg`, `InRelease`, `.nojekyll`,
      `pool/`, `gh-pages`, `quota-manager.gpg`), and the committed public key
      exists / starts `-----BEGIN PGP PUBLIC KEY BLOCK-----` / is not gitignored.
      **19 passed** at the packaging level; docs: README (apt repo = primary
      install + upgrade path, direct-.deb = "Alternative"), Structure_README
      (Releasing note + "Setting up the apt repository (one-time)" with
      keygen/secret/Pages/backfill), CHANGELOG `[Unreleased]` `### Added`.
- [x] **one-time user steps remain** (docs + handoff): paste
      `quota-manager-secret.asc` into the `APT_REPO_GPG_KEY` secret (then delete
      the file), ensure Pages serves `gh-pages` (the workflow's API step tries
      it; fallback Settings → Pages → "Deploy from a branch" → `gh-pages`),
      then `gh workflow run apt-repo.yml --ref main -f version=0.1.2` to backfill
      v0.1.2. Keys can be rotated by replacing the committed public key + the
      secret (the workflow copies the committed key each run).

Checked 2026-08-11 (**v0.1.2 released** — the whole uncommitted bundle shipped):
- [x] **version bumped `0.1.1` → `0.1.2`** (`quota/version.py`, the single source of
      truth) and tagged **`v0.1.2`**; the release carries all the previously
      uncommitted work: per-device browsing history + dnslog parser fix + the
      `conf-dir` setup fix, the History-tab **All devices** household overview
      (the "unversioned" [08-10] entry), the vivid purple obsidian-glass UI +
      pitch-black retune (the "v0.1.3 UI" entries — they stayed unversioned, so
      the theme ships here), and the History-tab scroll. **The earlier
      "stays 0.1.1" / "no version.py bump, no tag" notes below are now
      superseded history.**
- [x] **release description = CHANGELOG.md** (`release.yml`): the Upload step now
      uses `body_path` — a "Compose release notes" step writes the install note +
      the released version's `CHANGELOG.md` section (awk from `## [<ver>]` to the
      next top-level `## [` header; `###` sub-headings kept) into
      `${RUNNER_TEMP}/release_body.md`. The changelog IS the release notes; no
      drift. `test_packaging.py` pins it
      (`test_release_workflow_embeds_changelog_in_release_body`). CHANGELOG's
      `## [Unreleased]` section renamed → `## [0.1.2] — 2026-08-11` (+ a fresh
      empty `## [Unreleased]` placeholder at top); README install example →
      `quota-manager_0.1.2_all.deb`.
- [x] **no changes to the .deb payload / setup contract** — only version, notes,
      docs, workflow body. Full suite **333 passed** at the release baseline
      (the Windows `dns_history` DB-test hang did not reproduce on the release
      run).

Checked 2026-08-10 (History tab empty on the live box — parser rejected every real log line):
- [x] **root cause**: dnsmasq ``log-queries=extra`` on the box stamps the client ip/port after the
      serial (``Aug 10 00:00:54 dnsmasq[862442]: 1 192.168.2.186/16773 query[A] icosa-sg.coloros.com
      from 192.168.2.186``), but ``_QUERY_RE`` (quota/dnslog.py) expected ``query[`` immediately
      after the serial — so ``parse_dnslog_line`` returned ``None`` for EVERY real line and
      ``dns_history`` stayed empty even though the log filled (log OK, tailer OK, drain OK — the
      parser silently dropped everything). Fix: the regex now accepts an optional
      ``(?:[0-9A-Fa-f:.]+/\d+\s+)?`` chunk between the serial and ``query[`` (bare and serial-only
      shapes unchanged; ``forwarded``/``reply`` lines with the same prefix still skipped). Verified
      against the exact captured line (+ ``test_parse_dnslog_extra_ip_port_shape``, extended
      ``test_parse_dnslog_ignores_non_query_lines``). **Same session, setup-script gap**: the box's
      ``/etc/dnsmasq.conf`` had ``conf-dir=`` COMMENTED, so dnsmasq silently ignored every
      ``/etc/dnsmasq.d/`` fragment (DHCP pool, DNS, the query-log fragment) — setup_gateway_kali.sh
      now uncomments or appends ``conf-dir`` so the fragments actually load.
Checked 2026-08-10 (History tab "All devices" household overview, unversioned):
- [x] **the History tab opens on a household "All devices" aggregate** (the
      v0.1.2 per-device viewer, now default-overview; no version.py bump —
      stays 0.1.1): the device dropdown gains a default **All devices** option
      (`web/index.html`, `syncHistoryDeviceSelect` preserves the selection /
      normalizes to "all"); selecting it renders combined recent activity across
      EVERY device in chronological order with each query badged by its owning
      device/user (`[Yahya]` … — `histDeviceName` resolves user_name → name →
      MAC → #id; `.hist-device-badge` purple-glass pill) plus a unified top
      domains + household query-total summary (bandwidth summed over
      `dashboard.users[].devices[]`). Picking a specific device filters to that
      device only, unchanged. **Backend**: `quota/db.py` `get_dns_history` gains
      a `device_id=None` aggregate branch (the `get_usage_series` None pattern —
      drops the device filter, SUMs domains across devices, stamps
      `recent[].device_id`); `api/app.py` `device_history` accepts
      `device_id: int | str` with **`"all"` and `0` both meaning the aggregate**
      (404 semantics for real ids untouched); the per-device wire shape is
      byte-identical (`device_id` is only exposed on recent rows in the
      aggregate). Cache-busts styles **?v=35 → 36** + app.js **?v=31 → 32**
      (index L7/L601, milestone.html + report.html stylesheet links, test pins
      test_web_ui.py L64-65/L264-265 + test_api.py L1322/L1374). Tests:
      `test_get_dns_history_all_devices_aggregates` (test_dnslog.py),
      `test_history_all_devices_aggregates` + `test_history_device_0_is_all`
      (test_api.py), expanded test_web_ui pins. Full suite **332 passed**
      (329-pass baseline + 2 API + 1 DB aggregate tests), pyflakes +
      `node --check` clean. **No quota/version.py change, no tag.**

Checked 2026-08-10 (v0.1.3 UI — vivid purple obsidian glass + stacked user cards):
- [x] **theme flipped back to vivid purple, CSS-only** (`web/assets/styles.css`
      + cache-bust links; zero JS/HTML-structure changes — the card layout is
      `.device-grid`-driven): `:root` base → deep purple-tinted obsidian
      gradient (`--bg` `#08070d` / `--bg-2` `#0f0b18`); dark translucent glass
      cards (`--glass-bg` `rgba(20,15,30,0.6)` + `backdrop-filter` `blur(16px)`
      + 1 px `rgba(255,255,255,0.08)` glossy edge, `--blur` 32 → **16 px**);
      vivid purple accents (`--accent` `#8b5cf6`, `--accent-2` `#7c3aed`,
      `--accent-3` `#c4b5fd`) for primary buttons, selected tabs, badges,
      progress; `.nav-tab.active` gains a **neon glow** (`rgba(139,92,246,0.16)`
      bg + `border-color rgba(139,92,246,0.45)` + `0 0 14px rgba(139,92,246,0.25)`
      shadow). All hardcoded purple-family literals swept to the new palette
      (btn border/primary shadows, focus rings, `select option` bg,
      `.ring::before` disc, `.bypass-tag`, `.banner`/`.guide code`/
      `.settings-card p code`, scrollbar). **Users & Devices cards restructured
      to a single full-width stacked column**: `.device-grid` →
      `grid-template-columns: 1fr` (+ `.user-card { width: 100% }`), the 2/3-col
      media-query overrides REMOVED so the stack holds at every width.
      `.btn.small` radius 8 → **10 px** (all buttons now 10–12 px). **Untouched**:
      semantic status dots (green online), red "Blocked", pink Gateway pill,
      `.device-live` ↑/↓. Cache-bust `?v=34 → ?v=35` (index.html L7 +
      milestone.html + report.html + test pins in test_web_ui.py L65/L259 +
      test_api.py L1322/L1374); `.ms-pill.done` border retuned to light
      lavender (`rgba(196,181,253,0.35)`); new stacked-layout regression
      assertion inside `test_assets_served` (CRLF-normalized). Screenshot-style
      render verified via an out-of-repo Edge-headless preview (`%TEMP%\quota-ui-preview\`)
      — obsidian gradient, purple neon ring, frosted cards, stacked full-width
      cards. Full suite **329 passed** (+1 assertion inside an existing test),
      pyflakes + `node --check` clean (no Python/JS edits).

Checked 2026-08-10 (UI refinement — pitch-black base + desaturated cool periwinkle + stronger glass):
- [x] **dashboard theme retuned, CSS-only** (`web/assets/styles.css`, single file touched):
      base `--bg`/`--bg-2` `#0b0812`/`#130c1e` → **true `#000000`**; every purple accent
      (`--accent` `#a78bfa` → **`#8FA0C9` cool periwinkle**, `--accent-2` `#7c3aed` → `#6B77A5`,
      `--accent-3` `#c4b5fd` → `#AAB6DA`) + all 24 hardcoded purple literals → desaturated
      periwinkle/lavender-gray (verified by grep: zero stray purple values remain). Glass upgraded:
      `--glass-bg` → `rgba(255,255,255,.07)`, `--glass-bg-2` → `.12`, `--glass-border` → `rgba(255,255,255,.22)`,
      `--blur` 22 → **32 px**, `--shadow` deeper; `.glass` gains an inset top highlight
      (`inset 0 1px 0 rgba(255,255,255,.08)`) so cards read as cut frosted glass; `.btn` gets the
      same 1px frosty-white frame (the quiet "Log out" ghost button included). Ambient flare radials
      (body + `.bg-glow`) swapped violet → low-alpha cool periwinkle. Ring/bars/toggles/primary
      buttons flip via `var(--accent)` automatically. **Untouched**: semantic status dots (green
      online etc.), red "Blocked" tag, pink Gateway pill, `.device-live` ↑/↓ colors, all data/positions
      legible. Cache-bust `?v=33 → ?v=34` (index.html L7 + test_web_ui.py L65/L259). The milestone +
      report pages carry their OWN inline `<style>` blocks and stale `?v=32` links — both bumped to
      `?v=34` and pinned in test_api.py; their lone purple literal (`.ms-pill.done` border) retuned
      to periwinkle; `app.js` verified zero color literals (all class-driven). Screenshot-style
      rendering verified via an out-of-repo Edge-headless preview (`%TEMP%\quota-ui-preview\`) —
      pitch-black base, periwinkle accents, frosty translucent cards, crisp `79.06 GB`, flare behind
      cards. Full suite **329 passed** (+2 cache-bust assertions inside existing milestone/report
      tests), pyflakes + `node --check` clean (no Python/JS edits).
Checked 2026-08-10 (per-device browsing history — the dnsmasq query-log pipeline):
- [x] **Detailed Traffic & Browsing History per device** (v0.1.2, uncommitted): the dashboard
      **History tab** shows what each device actually visited — top domains, hourly activity,
      recent queries — with per-device bandwidth reusing the existing snapshot (nothing new
      metered). Capture is the box's own dnsmasq: `log-queries=extra` puts the requestor IP on
      every query line; the setup script installs an **app-owned fragment**
      (`/etc/dnsmasq.d/quota-dnslog.conf`, survives setup re-runs + WAN/LAN toggles because both
      scripts only rewrite `quota-gateway.conf`) + a logrotate snippet (`copytruncate`, size 5M,
      rotate 3). A **dedicated tailer thread** (`quota/dnslog.py`, the `arp_lock.py` pattern —
      poll 0.5 s, strip `\x00` sparse holes, 1 MB partial-line cap, bounded queue that drops on
      overflow so DNS + the event loop are never blocked) feeds `_dns_history_tick` every ~15 s,
      which batch-upserts per-(device, minute, domain) counts into a new `dns_history` table,
      persists the read cursor (`dnslog_state` — restart-resume; first start seeks to EOF so
      pre-feature lines never attribute) and — on a 1 h gate — prunes **per user** at their
      `history_days` (NULL = global `history.retention_days`, default 7; scoped so a short
      retention never wipes a longer one). `GET /api/history/{device_id}` (auth, window clamped
      1-336 h). Full suite **329 passed** (+31: `test_dnslog.py` parser/tailer/DB, run-wiring
      drain/persist/prune/disabled, API auth/top-domains/404/window, web-ui tab + cache-bust),
      pyflakes + `node --check` + `bash -n` clean. Docs: CLAUDE.md SYSTEM_FLOW step 10 + tree +
      this entry; config.yaml `history:` block; CHANGELOG/README/Structure_README synced. One
      deliberate KNOWN LIMIT: the box's own gateway metering (`count_gateway`) and the new query
      log are unrelated — the DNS-relay charge is a separate, already-fixed counter-order bug
      ([08-10] entry above). **TODO (honest)**: the raw log is root-only + the dashboard is
      auth-gated, but recording is ON by default once setup is re-run — a privacy note for the
      household lives in README/Structure_README.
Checked 2026-08-10 (blocked Gateway no longer consumed — the DNS-relay charge is gone):
- [x] **a blocked Gateway consumed ~30 MB/day of pure DNS relay; fixed.** Root cause: in
      `quota/nftables.py` `_program_gateway` the q_gw counter rules were programmed FIRST in the
      input/output chains — before the DNS/DHCP exemption accepts and before the gw_blocked
      drops — so (a) the household DNS that the block deliberately lets through (dnsmasq relays
      to 8.8.8.8) was counted *and* passed, and (b) bytes a blocked box dropped were still
      counted. ~130-250 B/query+response × 1-3 q/s across household devices → the observed
      30 MB/day against the Gateway user's 1.0 GB (YouTube genuinely cut — the block worked; the
      accounting was wrong). Fix: reordered `_program_gateway` so exemptions run FIRST (relayed
      service traffic is never counted), the gw_blocked drops NEXT (a dropped packet terminates
      the chain — a blocked box's attempted bytes never consume the bundle), and the counters
      LAST (only non-local, non-exempted traffic that survives the block is metered). DHCP with
      `saddr 0.0.0.0` / `daddr 255.255.255.255` (also not local) is no longer charged either.
      `+ test_gateway_exempted_and_blocked_traffic_never_reach_counters` (pins the order);
      `scripts/replay_nft_startup.sh` synced. Full suite **298 passed**; pyflakes clean.
- [x] **docs note**: Structure_README "Known bottlenecks" never repeated the DNS-forward charge
      claim, so no change needed there.

Checked 2026-08-10 (DEEP ARCHITECTURAL AUDIT — 5 agents, read-only, Gatekeeper PASS):
- [x] **the "v20 in-flight — UNCOMMITTED" note is STALE**: the entire v20 bundle AND the v21-era
      pages are COMMITTED at **v0.1.1** (`quota/version.py` = "0.1.1"); the working tree is CLEAN
      at HEAD a9a26ec. Agents: Reverse Engineer, Conflict & Regression Analyst, Refactoring
      Architect, Performance & Log Auditor, Gatekeeper (certified **zero modifications**).
- [x] **dependencies verified current** (Aug 2026): fastapi 0.141.1, uvicorn 0.52.1,
      aiosqlite 0.22.1, PyYAML 6.0.3 all CURRENT-LATEST, no applicable CVE (the PyYAML
      CVE-2026-24009 is a consumer-side `yaml.load` bug; this code only `safe_load`s);
      starlette 1.4.1 is ABOVE all three 2026 advisories (CVE-2026-48710 "BadHost",
      CVE-2026-54282, CVE-2026-48817) and the app's route-level `Depends(_require_auth)` auth is
      structurally immune to BadHost regardless; dev-only pytest 8.3.5 / httpx 0.28.1 are
      outdated (pytest 9.1.1 / httpx 0.29 current). Starlette's testclient now warns it will
      deprecate httpx (future `httpx2`).
- [x] **all 7 break-point claims re-verified against the committed tree** (6 CONFIRMED,
      migrations CONFIRMED re-run-safe; line numbers shifted — `service.snapshot_state` now
      :280-315, `nftables.update_state` :333-377). **The lease-less block defect is CONFIRMED
      still open**: `snapshot_state` gives a lease-less device `ip=""` → dropped from
      `ip_to_mac` (run.py:443-446) → never enters the kernel `blocked` set (nftables.py:363-364);
      the ARP-lock `known_ips` deny is the only cover and defaults OFF / forced OFF in WAN. No
      test drives a lease-less blocked device into `update_state` (the service-layer tests only
      assert the `blocked` map, never the kernel set).
- [x] **NEW defects (08-10, absent from the 08-08 inventory)**:
      (a) **`/api/milestone/notify` is UNAUTHENTICATED + has no IP-ownership check** — any LAN
      host can POST another user's `user_id` and clear/re-arm their 50/75/100% milestone pills
      (api/app.py:386-395; display-integrity only, no quota effect; inconsistent with the GET
      reader, which IS IP-resolved to the requester's own user);
      (b) **`/report` is default-ON for the whole client subnet** (`report.allow_client_subnet:
      true`, config.yaml:120-123) — a ROGUE static-IP device on 192.168.2.x passes the subnet
      gate and reads full household usage + events + log tail with no session. The gate itself is
      sound (`request.client.host`, no XFF handling, no off-path spoof) — the exposure is the
      documented "trusted LAN" assumption, but it's a default-on data surface;
      (c) **a deleted-but-still-connected guest is UNCCOUNTED + UNCONTROLLED** — the suppressed
      MAC keeps its lease with no device row → no counter rule until it disconnects and
      re-registers (documented in `_persist_lease`'s own comment, run.py:273-275; no dashboard
      card surfaces it);
      (d) ~~**the box's dnsmasq upstream DNS-forwarding is charged to the Gateway user** — client
      DNS queries relayed to 8.8.8.8 traverse the output/input hooks and count against the 1.0 GB
      (counter exclusions cover local subnets, not port 53; the DNS-accept exemption is
      order-after-counter) — systematic, invisible, tiny~~ — **FIXED 08-10**: reordered
      `_program_gateway` so the DNS/DHCP exemption accepts and the gw_blocked drops run before
      the q_gw counters — relayed service traffic and blocked bytes are never metered
      (+ `test_gateway_exempted_and_blocked_traffic_never_reach_counters`);
      (e) **`evaluate_blocks` persists `block_state='quota'` onto the GATEWAY_MAC device row**
      when the protected user goes over (cosmetic — the resolved state, not the row, drives
      `set_gateway_blocked`).
- [x] **PERFORMANCE audit — ZERO timing telemetry exists anywhere** (`time.monotonic` only gates
      the rogue-scan cadence; no per-tick duration is measured) — drift/stall is unquantifiable.
      CONFIRMED on-loop stalls (worst on a slow laptop): `shaper.update_state` rebuilds the tc
      tree via **~70-115+ sequential `subprocess.run`** on the event loop (run.py:512;
      shaping.py:299-306) ≈ 1.5-5 s, and an API cap-edit fires it IMMEDIATELY via
      `_reshaping_now` (user-visible freeze); `engine.update_state`/`set_gateway_blocked` run
      `nft` subprocesses on-loop (run.py:450/455, ~80 at first boot); `detect_ppp` runs `ip`
      on-loop per WAN tick (run.py:559, LAN exempt); `_read_log_tail` reads the WHOLE 5 MB log
      synchronously on-loop (api/app.py:46-64; both `/api/logs` and `/report`); `/report` also
      does `list_leases()` PER DEVICE (api/app.py:447 — the dashboard payload hoists it once at
      :231); OUI one-time ~100-500 ms on-loop parse. **REFUTED (08-08 claim wrong)**:
      `check_internet`/`check_internet_dns` ARE `asyncio.to_thread`'d (run.py:577-579) — off-loop.
      DB: ~30+ commits/tick (a floor, no batching), `get_period_usage_by_user` ×2/tick
      (service.py:239,291), `get_bundle` actually ~×5/tick (not 3), `events` table UNBOUNDED (no
      `DELETE FROM events` anywhere). WS: payload built **N+1 times per 5 s** (per-client loop
      api/app.py:963-964 + `_push_loop` :975) and each client receives 2 snapshots/5 s;
      app.js does a full `innerHTML` rebuild per push.
- [x] **ARCHITECTURE verdict**: modular monolith, WEAKLY layered — api/app.py (999 lines) + run.py
      (672) reach straight down through every layer; web/app.js 1297, quota/db.py 872,
      quota/nftables.py 751. All 7 Simplicity-First violations re-confirmed; new detail: the
      **Gateway user is a REAL DB row forcing ~6 special cases** (quota_blocked_for,
      is_setup_complete, milestone_state, gw_view, run.py gateway drain, API/UI guards) AND
      silently deducts 1.0 GB from every auto-share bundle (rule buried in seeding, not the quota
      model); **`/milestone` + `/report` duplicate the dashboard payload math** — a 3rd copy of
      the usage/allowance/percent loop (api/app.py:333-475); milestone flags live as DB columns
      the period-roll must clear. DDD bounded-context map proposed (Gateway/Accounting,
      Quota/Policy, Admin/Ops, Presentation) + refactor path ordered by blast radius
      (1. decompose `_maintenance_tick` → 2. harden the nft cache-gates into one helper →
      3. pure-function tc tree builder → 4. decision-table block precedence → 5. collapse the 3
      payload builders into one `presentation/views.py` + payload `schema` version →
      6. split db access methods by interface → 7. land the engine.py type change LAST).
- [x] test suite re-run at the audit baseline: **297 passed** (1 StarletteDeprecationWarning — the
      httpx→httpx2 testclient warning) — the committed tree is a green baseline

Checked 2026-08-08 (v20 in-flight — UNCOMMITTED; working tree is the audit baseline):
- [ ] **the working tree carries an uncommitted "v20" bundle** (~1,855 lines / 25 files,
      `git status`): (1) `engine.count_gateway` — the box's OWN traffic is counted +
      chargeable (`q_gw_up/down` input/output hooks, `gw_blocked` set,
      `set_gateway_blocked`, restart-safe reseed; `false` skips counters but keeps drops);
      (2) protected **Gateway** user + sentinel `GATEWAY_MAC` device (fixed 1.0 GB,
      seeded idempotently; `service.quota_blocked_for` cuts at `used >= allowance` even
      at 0; API/UI guards; **silently deducts 1.0 GB from every auto-share bundle — a
      behavioral change on upgrade**); (3) **guest-deletion suppression**
      (`suppressed_macs` table, checked FIRST in `_persist_lease`); (4) **immediate
      shaping re-sync** (`_shaping_lock` + `_reshaping_now` — no page refresh on cap
      edits); (5) **tc burst/cburst fix** (`_burst` = rate/20 ≈ 50 ms bucket — fixes the
      "2 Mbps cap reads 3 Mbps" overshoot); (6) **DNS-probe fallback**
      (`check_internet_dns`, raw UDP) so the WAN dot stays honest while the box's own
      egress is kernel-dropped; (7) PPPoE **concurrent-session** test verdict.
      `quota/engine.py` now owns the shared snapshot types (single definition; every
      consumer imports it). **TASKS.md is stale on 2 of 3 bug reports**: speed-drift +
      page-refresh are FIXED here; "per-device block not working" is NOT — see
      [LEGACY_DEBT_AND_RISKS]. Verified by the 2026-08-08 deep audit (Gatekeeper PASS).

Checked 2026-08-07 (PPPoE creds no longer wiped by panel applies + Apply dimmed when WAN is online):
- [x] **creds prefill empty = the DB was wiped, not the JS**: the WAN tab
      prefills `wan-user`/`wan-pass`/`wan-if` from GET /api/wan's DB settings,
      and the box's working creds sat in /etc/ppp/chap-secrets (the dial
      succeeded) — but the DB settings were empty. Root cause: EVERY panel apply
      overwrote them — `netmgr.apply()` and the API's no-manager fallback
      unconditionally saved `pppoe_user or ""` etc., so a "Revert to LAN" (which
      posts just `{topology:"lan"}`, no creds) erased the saved credentials the
      prefill reads. Only a non-empty value is now saved — an empty field
      preserves the last-known creds (both save paths; a LAN revert no longer
      destroys a future WAN prefill) (+ `test_apply_revert_preserves_saved_pppoe_creds`
      in test_netmgr.py, `test_wan_persist_no_manager_preserves_saved_creds` in
      test_api.py). **Box recovery**: re-type the creds once in the WAN tab and
      Apply (or INSERT the settings row), then the prefill works forever.
- [x] **"Apply now" dimmed when WAN is already active + online**: with WAN
      configured, ppp0 up, and internet reachable, "Apply now" has nothing to
      do (it would just re-apply + restart the gateway) — it is now disabled
      (`.btn:disabled` dimming) and only **Test PPPoE connection** + **Revert to
      LAN** stay active. A pending toggle flip (draft) or a broken link keeps
      Apply enabled (there IS something to change or fix). `linkUp` computed
      once in `renderWan` (`?v=26`, + web_ui pins)
- [x] full suite: **243 passed**, pyflakes clean, `node --check` clean,
      `bash -n` clean on scripts

Checked 2026-08-07 (v19.8 detect_ppp read a LIVE ppp0 as down — box report):
- [x] **the bug was in the detector, not the dial**: the box's journal showed
      pppd CONNECTED (`PAP authentication succeeded`, `local IP 197.121.113.253
      remote 10.10.12.17`, `primary/secondary DNS`, service `active (running)
      2h 30min`) while the WAN tab read `ppp0 down / Public IP —`. The dial
      path (netmgr env -> topology.sh peer/secrets/unit -> pppd) was verified
      correct; the reader was wrong. `detect_ppp`'s fast path trusted
      `/sys/class/net/ppp0/operstate` — but **PPP interfaces are carrier-less**:
      the kernel reports operstate `unknown` (or `down`) even while pppd holds a
      negotiated point-to-point IP, so the fast path short-circuited to "down"
      without ever running `ip`. The dev-box tests never caught it because
      `/sys/class/net/ppp0` does not exist there (the fast path is skipped).
- [x] **fix**: `detect_ppp` now judges the link by its negotiated IPv4
      (`ip -o -4 addr show dev ppp0` carrying `inet <local> peer <peer>`), never
      by operstate; sysfs is used only to tell "down" from "unknown" when `ip`
      fails. A dialed-up line with an IP reads `up` with local/peer; an
      interface with no IPv4 (discovery/LCP) reads `down`; no interface + no
      `ip` reads `unknown` (+ `test_ppp_up_even_when_sysfs_operstate_unknown`,
      `test_ppp_up_even_when_sysfs_operstate_down`; `test_ppp_down_when_sysfs_says_down`
      updated — `ip` is now always consulted)
- [x] full suite: **241 passed**, pyflakes clean, `node --check` clean,
      `bash -n` clean on scripts

Checked 2026-08-07 (v19.7 WAN tab self-diagnoses why ppp0 is down):
- [x] **auto PPPoE diagnosis when the link is down**: WAN configured + ppp0 down
      reads "the box is dialing but nothing answers" — but the panel never said
      WHY. `maybeAutoDiagnose()` now auto-runs the throwaway test (`POST
      /api/wan/test` → `scripts/test_pppoe.sh`) ONCE per page load when the WAN
      tab is open, WAN is configured, and ppp0 != up (never on an up link, never
      with a pending toggle draft, never from init's hidden-panel refreshWan),
      and `renderPppoeVerdict()` turns each failure mode into an ACTIONABLE fix:
      `no-pppoe-server` → "your router is NOT bridged / line not synced" +
      bridge-mode steps, `auth-failed` → re-check creds, `link-down` → modem/ISP
      side / check quota-wan-ppp, `error` → missing pppd / wrong WAN interface.
      The manual Test button shares the same renderer (`?v=25`)
- [x] **wan-down banner names the #1 cause**: now states "the box is dialing but
      nothing answers" + "the #1 cause: the router is NOT in bridge/modem mode
      yet" and points at the auto-diagnosis below + Revert to LAN
- [x] full suite: **239 passed**, pyflakes clean, `node --check` clean
      (cache-bust `?v=24 -> ?v=25`)

Checked 2026-08-07 (v19.6 WAN-tab contradictions — honest internet dot + load-time creds):
- [x] **internet dot gated on the ppp0 link** (`run.py _wan_status`): the probe
      measures the BOX's own reachability, so in the half-applied state (router
      not bridged yet) the box still reaches the internet via the router's NAT
      while ppp0 is down — the green dot then contradicted the honest wan-down
      banner. In WAN mode ppp0 IS the internet path: `internet = probe AND
      (effective != "wan" OR ppp0 == "up")`, so "ppp0 down" and "internet ●
      Online" can never coexist; LAN mode keeps the probe as the whole story.
      (+ `test_wan_internet_gated_on_ppp0_link`: down+probe->red, up+probe->green,
      up+dead-probe->red)
- [x] **saved PPPoE creds prefill on page load**: `refreshWan()` (the only thing
      that reads `/api/wan` and prefills `wan-user`/`wan-pass`/`wan-if`) ran only
      on a WAN-tab click — a fresh page load into the WAN panel showed empty
      creds until the tab was re-clicked. Init now calls `await refreshWan()`
      after `refreshAll()` (creds still never ride the WS push).
- [x] full suite: **239 passed**, pyflakes clean, `node --check` clean (cache-bust
      `?v=23 -> ?v=24`)

Checked 2026-08-06 (v19.5 automatic router-admin access in WAN mode):
- [x] **router admin reachable by default in WAN mode**: the applier
      (`scripts/topology.sh _nic_wan`) no longer DELETES the uplink IP — it keeps
      it as a SECONDARY alias on the LAN NIC (nmcli + ifupdown-persistent via
      `_nic_apply` extras), so clients reach the router admin page through the
      box's connected route. Same for the setup script's WAN branch. The user's
      manual `sudo ip addr add 192.168.1.2/24 …` becomes automatic (`LAN_IP`/
      `LAN_CIDR` already flow from `TopologyManager.applier_env` via the LAN
      snapshot; `uplink_ip()` always resolves, defaults 192.168.1.110/24).
- [x] **uplink subnet is LOCAL in WAN mode** (`resolve_local_networks`): the box
      carries the router-admin subnet, so router-admin traffic must not consume
      quota — explicit `engine.uplink_subnet` is now HONORED in WAN mode (was
      warned+ignored), else derived from `dhcp.uplink_ip`+`dhcp.lan_cidr`, else
      `dhcp.router_ip`. Not a bypass: the masquerade only covers the client
      subnet, so an uplink-subnet static source is never NATed out ppp0.
      `_match`/`_blocked` drop exclusions carry it too (blocked devices keep
      router-admin access, matching LAN mode).
- [x] **honest ppp0-down banner**: no longer claims "router admin page
      unreachable" (the alias is kept) — internet down vs. router page reachable
      are now separate facts (+ WAN-tab guide note "Router admin stays
      reachable")
- [x] **tests**: `test_wan_mode_keeps_router_admin_subnet_local`,
      `test_wan_mode_derives_router_admin_subnet_from_lan_snapshot` (new);
      `_wan_engine` helper + client-derivation test updated; ARP-lock-off test
      unchanged. Full suite green; `bash -n` clean on both scripts.

Checked 2026-08-06 (v19.4 WAN status honesty + creds prefill + internet green dot):
- [x] **honest WAN banner**: the ACTIVE banner only claims WAN is carrying
      traffic when ppp0 is actually up (`linkUp = (wan.ppp0 || "") === "up"`);
      a configured-but-down dial shows a red `wan-down` banner ("gateway is NOT
      dialing the line… router admin page unreachable") instead of falsely
      reading "WAN is ACTIVE" (+ `.banner.wan-active/.wan-down` CSS)
- [x] **PPPoE creds persisted + prefilled**: `TopologyManager.apply()` + the API's
      no-manager fallback now `set_setting("pppoe_user"/"pppoe_password"/"wan_if")`
      alongside the topology; `GET /api/wan` appends them (DB settings, served
      ONLY here — the WS-pushed `wan` key never carries the password), and
      `refreshWan()` prefills the WAN-tab fields (skipped while `wanToggleDirty`)
- [x] **Internet green dot (every 15 s tick)**: `quota/topology.check_internet()`
      — raw-IP TCP connect to 1.1.1.1 / 8.8.8.8:443 (no ICMP-root, no DNS), first
      host that connects wins; `Gateway(internet_probe=…)` injectable +
      `_wan_status()["internet"]` run via `asyncio.to_thread` so a dead line
      (≤2 s timeout) never blocks the event loop; WAN tab "Internet" row renders
      `● Online` (green) / `● Offline` (red) / `—` (pre-first-tick). Replaces the
      dead router LED in WAN mode (`?v=22`, +4 check_internet tests)
- [x] **green "live" pill removed** — the useless `#conn-status` "● live" pill next
      to Log out is gone (element + `setConn()` + `.pill.{live,off}` CSS all
      dead-code swept)
- [x] **maintenance-loop race fixed (test)**: `startup()`'s background loop fires
      its FIRST tick immediately, and the new `asyncio.to_thread` internet probe
      in `_wan_status` widened the window for it to interleave with a test's
      manual tick (a second, empty shaper sync). `test_maintenance_tick_syncs_shaper`
      now cancels the loop via `_cancel_maintenance(gw)` so it measures manual
      ticks only (production never runs a tick by hand — no race there); the two
      holder tests inject `internet_probe`
- [x] full suite: **237 passed**, pyflakes clean, `node --check` clean,
      `bash -n` clean on all scripts

Checked 2026-08-06 (v19.3 cleanup + final workflow review):
- [x] **requirements-dev.txt removed** — fully redundant with
      `requirements-linux.txt` (unreferenced anywhere); stale root `__pycache__`
      pycs + `.pytest_cache` deleted
- [x] **`import asyncio` consolidated** — api/app.py had three function-local
      `import asyncio` (ws_endpoint, _push_loop, _lifespan) with no top-level
      import; now one top-level `import asyncio`
- [x] **dead `expand_ip_range` removed** — orphaned Windows-era DHCP helper in
      core/config.py that no production code calls (only its own tests); function
      + the 2 tests + the docstring note in tests/test_config.py deleted
- [x] **dead `config-linux.yaml` migration fallback removed** from
      scripts/setup_gateway_kali.sh — the file was deleted in the v15 sweep, so
      nothing on a box can ever hold it; the setup script read it as a one-time
      pre-0.1.0 migration source that no longer exists
- [x] full workflow review (api/app.py, run.py, quota/{service,nftables,db,
      shaping,netmgr,arp_scan,arp_lock,topology,engine,vendor}, core/{config,
      timeutil,logging_setup}, web/{index.html,app.js}, config.yaml,
      scripts/{topology,test_pppoe,setup_gateway_kali}.sh) — no other orphans or
      dead references; import graph closes; **233 passed**, pyflakes clean,
      `node --check` clean, `bash -n` clean on all scripts

Checked 2026-08-06 (v19.1 WAN-workflow audit fixes + PPPoE connection test):
- [x] **pppd daemonization loop (FIXED, severe)**: `quota-wan-ppp.service` used
      `Type=simple` + `Restart=always` but `ExecStart=pppd call quota-wan` —
      pppd daemonizes after the link is up, so systemd killed the daemon every
      5 s and re-dialed forever (an infinite connect/disconnect loop). Both unit
      writers now use `pppd call quota-wan nodetach` (topology.sh + the setup
      script) so pppd stays in the foreground and systemd owns one long-lived
      process
- [x] **applier-failure rollback (FIXED)**: a failed `topology.sh` run previously
      left config.yaml + the DB at the new topology, so the next boot would apply
      a topology its NIC never got (boot WAN onto a LAN NIC = everyone cut).
      `apply()` now snapshots the pre-apply config text (`_read_config`) + DB
      settings and `_rollback_apply()` restores both on rc != 0 (deleting a
      config.yaml the apply created when none existed before), plus an `error`
      event — no restart is ever scheduled into a half-applied state (+2 tests:
      rollback-to-defaults, rollback-after-successful-LAN)
- [x] **render_config data loss (FIXED)**: an apply silently dropped `log_level`,
      `dhcp.interface` and `engine.count_direction` (config fields the loader
      keeps but render didn't emit) — a WAN apply could lose the count
      direction / log level / DHCP NIC. All three now flow through (+1 test)
- [x] **lan_interface index-vs-name bug (FIXED)**: the NIC fallback parsed
      `2: eth0 inet ...` with `split(":",1)[0]` → returned the INDEX `2`, not
      `eth0`, which the applier passed to nmcli/dnsmasq. Now parses the NAME
      after the `:` (+1 test)
- [x] **upstream_dns dead-router bug (FIXED)**: a one-entry `[router]` DNS list
      made WAN mode forward DNS to the router, which doesn't exist on the WAN
      segment (every upstream query went nowhere). `upstream_dns()` now skips
      the router IP and falls back to 8.8.8.8 (+1 test)
- [x] **PPPoE connection test (NEW)**: the WAN tab's **Test PPPoE connection**
      button — `POST /api/wan/test` (auth) → `TopologyManager.test_pppoe()`
      → `scripts/test_pppoe.sh`, a THROWAWAY dial that never touches the running
      topology: `unit 200` (ppp200, never ppp0), no defaultroute/usepeerdns,
      /etc/ppp chap/pap-secrets backed up + restored in an EXIT trap, temp peer
      removed, and only /32 routes for the two ping targets (removed too). Reports
      `status` = success | auth-failed | no-pppoe-server | link-down | error,
      the negotiated local/peer IPs, and whether internet is reachable. `WanTest`
      schema; `?v=20`; UI shows ✓/✗ with the detail (+3 netmgr tests: success /
      auth-failed / missing-script + 3 API tests: endpoint / 503 no-manager /
      500 failure)
- [x] **WAN-toggle draft-race (FIXED)**: flip-then-Apply within the 5 s WS window
      lost the flip (the next render re-keyed the toggle off the server state).
      `wanToggleDirty` now freezes the toggle + creds panel + shows a "Mode change
      pending" banner until Apply/Revert succeeds (cleared on success) or
      refreshWan reverts it (apply failed)
- [x] full suite: **235 passed**, pyflakes clean, `node --check` clean,
      `bash -n` clean on topology.sh / setup_gateway_kali.sh / test_pppoe.sh

Checked 2026-08-06 (v19 WAN tab applies the LAN/WAN switch LIVE — no setup script):
- [x] **quota/netmgr.py (NEW)**: `TopologyManager` — the dashboard WAN tab's live
      switch. `apply(topology, pppoe_user, pppoe_password, wan_if)` writes
      config.yaml + the DB setting TOGETHER (`topology_source=dashboard` +
      `topology`), runs `scripts/topology.sh` via an injectable `run_command`,
      and schedules a detached self-restart (`sleep 2 && systemctl restart
      quota-gateway`, survives the process being killed). Creds reach the applier
      via the ENVIRONMENT, never argv (no `ps` exposure). `render_config()` keeps
      every other value (bundle, pool, shaping) flowing through; `default_flow_style=None`
      renders scalar lists inline. **LAN snapshot** (`dhcp.lan_router_ip` /
      `lan_dns_servers` / `uplink_ip` / `lan_cidr` + `engine.lan_gateway_arp_lock`)
      is preserved in BOTH topologies so "Revert to LAN" restores the exact LAN
      (`lan_values()` falls back to the setup defaults on old boxes). Injectables:
      `run_command`, `spawn_restart`, `addr_cmd` (+ `tests/test_netmgr.py`, 11 tests
      incl. WAN→LAN round-trip + applier-failure-no-restart + env-not-argv)
- [x] **scripts/topology.sh (NEW)**: the runtime applier, invoked by the running
      app (which is root). Reads everything from env (TOPO, LAN_IF, LAN_IP,
      LAN_CIDR, SUBNET_MASK, CLIENT_IP, CLIENT_NET, WAN_GATEWAY, UPSTREAM_DNS,
      POOL_START, POOL_END, LEASE_HOURS, WAN_IF, PPPOE_USER, PPPOE_PASSWORD).
      NIC apply via nmcli, falling back to `/etc/network/interfaces.d/quota-gateway`
      (ifupdown); `_nic_wan` deletes the uplink IP + default route (client subnet
      only), `_nic_lan` re-adds uplink IP + client alias + `default via $WAN_GATEWAY`
      (verifies both addrs landed); dnsmasq config rewritten per topology (wan omits
      `server=$WAN_GATEWAY`), `dnsmasq --test` before restart; `_pppoe_wan` writes
      `/etc/ppp/peers/quota-wan` + chap/pap-secrets (chmod 600) + enables
      `quota-wan-ppp.service`; `_pppoe_lan` disables the dial + flushes ppp0.
      Requires root; never restarts the app (the manager does that). `bash -n` clean
- [x] **setup-script revert bugs (FIXED)**: `setup_gateway_kali.sh` LAN mode now
      disables+stops `quota-wan-ppp.service` / kills pppd / flushes ppp0 (a LAN
      re-install no longer leaves the PPPoE dial running), and deletes any
      stale `topology`/`topology_source` DB settings so the LAN config can't be
      re-forced to WAN by a leftover dashboard override (the v18 revert bug);
      config.yaml heredoc writes the `lan_*` snapshot keys + `lan_gateway_arp_lock: true`
- [x] **api/app.py**: `create_app(..., topology_manager=None)`; `POST /api/wan`
      (v19) validates `lan`/`wan`, then either applies live via
      `topology_manager.apply(...)` (returns `restart_scheduled` +
      `script_output`; `RuntimeError` → 500) or falls back to the v18
      persist-only path when no manager is wired (tests/degraded boot);
      `api/schemas.py` `WanUpdate` gains `pppoe_user`/`pppoe_password`/`wan_if`
      (+ 2 API tests: apply-live-with-fake-manager, apply-failure-is-500)
- [x] **run.py**: `Gateway(cfg, config_path=...)` stores the on-disk config path;
      `startup()` builds `self.topology_manager` AFTER the DB topology override
      (so its LAN snapshot reads the final cfg); `_serve()` builds the app AFTER
      `startup()` so the endpoint closes over the real manager (+ 1 run-wiring test)
- [x] **WAN tab UI (v19)**: PPPoE creds fields (`#wan-user` / `#wan-pass`) + the
      optional `#wan-if`, **Apply now** / **Revert to LAN** buttons (`#wan-apply-btn`
      / `#wan-revert-btn`), a busy state ("Applying… (gateway restarts)"), the
      script-output tail in `#wan-apply-msg`, an ACTIVE-WAN banner, and the guide
      rewritten around the panel apply (bridge/AP + two-NIC `wan_if`). JS:
      `submitWan()` / `revertWan()` replace the v18 toggle. `?v=18` cache bust
      (+ web_ui pins)
- [x] **config.yaml + README**: `lan_*` snapshot keys + `lan_gateway_arp_lock`
      documented; "Strong mode" section rewritten for the live panel apply
      (Turning it on/off all from the WAN tab; the one hands-on step = the router
      rewiring); UI/API tables + troubleshooting + Known-limits bullet updated

Checked 2026-08-06 (v18 optional WAN "strong" mode — box dials the PPPoE):
- [x] **config**: `EngineConfig.topology: str = "lan"` (`lan`|`wan`; `lan` = current
      behaviour, default) + config.yaml `engine.topology` docs; `router_ip` /
      `uplink_subnet` documented as empty in WAN mode (+ test_config pin)
- [x] **quota/topology.py (NEW)**: `detect_ppp(interface="ppp0", run_command=…,
      sysfs_root=…)` — reads `/sys/class/net/<iface>/operstate` (fast path: not
      "up" → down, no subprocess), else `ip -o -4 addr show` parsed for
      `inet <ip> [peer <peer>]`; returns `{"state": up|down|unknown, local, peer}`;
      never raises (missing sysfs / no `ip` / no root → "unknown"). Injectable
      run_command + sysfs_root keep tests root-free (+ `tests/test_topology.py`,
      6 tests)
- [x] **engine (`quota/nftables.py`)**: under `topology=wan`,
      `resolve_local_networks` returns ONLY the client subnet and warns+ignores an
      explicit `engine.uplink_subnet`; `NftablesEngine.__init__` forces
      `_arp_lock = False` under wan (no router on the client segment). Quota
      counters + `@blocked` drops unchanged — enforcement stays at line rate
- [x] **snapshot**: `EngineSnapshot.wan_status: dict = field(default_factory=dict)`
- [x] **run.py**: `_apply_topology_override()` (the bundle_source pattern:
      `topology_source=dashboard` + `topology` setting win over config.yaml on the
      NEXT restart, applied before the engine/scanner build); `_wan_status()` fed
      into every holder swap; `self.arp_scanner` moved to a startup-built
      None-guard (fixes construction-ordering: the scanner resolves its probe
      networks at construction, so it must be built AFTER the DB override — WAN
      probes only the client subnet); ArpLock gate hardened to skip wan
      (+ 7 run-wiring tests)
- [x] **API**: `GET/POST /api/wan` (auth) — GET returns the live `wan_status`;
      POST validates `lan`/`wan` (else 400), stores `topology_source=dashboard`
      + `topology`, adds a `warn` event, returns `applies_on_restart: true`; the
      dashboard payload carries `"wan"` (WS push + endpoint share `_dashboard_payload`)
      (+ 5 API tests incl. round-trip + 400 + session gate)
- [x] **setup script**: `QUOTA_TOPOLOGY=${QUOTA_TOPOLOGY:-lan}` (validated
      `lan|wan`), `wan_mode()` helper + `PPP_IF="${WAN_IF:-$LAN_IF}"`; WAN branch
      sets ONLY the client alias (no uplink IP); dnsmasq heredoc omits
      `server=$WAN_GATEWAY`; new step 5.6 installs `ppp` (+ `modprobe pppoe`,
      guarded modules-load append AFTER step 5.5's `ifb` overwrite), writes
      `/etc/ppp/peers/quota-wan` (`user` or `noauth`, creds → chap/pap-secrets
      chmod 600), and `quota-wan-ppp.service` (`Restart=always`); config vars
      (`CFG_ROUTER_IP`/`CFG_DNS_SERVERS`/`CFG_UPLINK_SUBNET`/`CFG_ARP_LOCK`/
      `CFG_TOPOLOGY`) keep the LAN generated config byte-identical; app unit
      `After` gains `quota-wan-ppp.service` in wan; WAN info report with NEXT
      STEPS (bridge/AP layouts, creds, revert) (`bash -n` clean)
- [x] **packaging**: `control.template` Depends += `ppp` (+ test_packaging pin)
- [x] **dashboard WAN tab**: nav tab + `#panel-wan` (toggle + "applies on next
      restart" banner + 3-step guide incl. both physical layouts + revert note +
      live status preview) + `renderWan`/`refreshWan`/`toggleWan` JS
      (`?v=17`, + web_ui pins)
- [x] **full suite: 210 passed**, pyflakes + `node --check` + `bash -n` clean

Checked 2026-08-06 (v17 rogue static-IP detection + ARP gateway-lock):
- [x] **rogue detection**: `quota/arp_scan.py` — every 60 s the maintenance
      loop raw-ARP-probes both LAN subnets and surfaces any active host that is
      NOT leased by the quota DHCP (`EngineSnapshot.rogue`, populated off-loop
      via `to_thread`; new rogues → `warning` event). Dashboard "Unmanaged /
      rogue devices" card (ip, mac, vendor, online dot) fed by a `rogue` key in
      `_dashboard_payload()` + `GET /api/rogue`. A static-IP device with the
      router as gateway is otherwise invisible: never counted, never blocked,
      not in the lease file (`?v=16`, +test_arp_scan.py +test_api/test_web_ui pins)
- [x] **ARP gateway-lock (the "DoS the cheater")**: `engine.gateway_arp_lock`
      (default ON in the setup-generated config, OFF in config.yaml). A
      bypasser's frames go straight to the ROUTER at L2, so the only in-box
      lever is ARP. (a) `quota/arp_lock.py` — a raw-socket thread claims the
      router's IP on the CLIENT subnet (answers client-subnet ARP requests for
      the router with the box's own MAC; uplink-subnet hosts untouched); (b)
      `quota/nftables.py` — an `arp`-family rule drops the router's replies to
      client-subnet hosts, and a `forward` deny drops any client-subnet source
      NOT in the `known_ips` set (= leased IPs, rebuilt only on membership
      change). Intercepted traffic is blackholed — the cheat's internet is cut
      until it uses the quota gateway. Self-sustaining: dropped traffic makes
      the rogue re-ARP, and it is re-answered. Degrades to a no-op without
      root/raw sockets; a static ARP entry or an uplink-subnet static IP still
      evades capture (documented; router MAC-filtering is the durable complement)
- [x] **docs**: README "Rogue devices & the ARP gateway-lock" section + a Known
      limit bullet + tree/tests updates; config.yaml documents
      `gateway_arp_lock`; setup script writes `gateway_arp_lock: true` + NEXT
      STEPS guidance; CLAUDE.md SYSTEM FLOW step 4 (rogue scan) + step 8 (lock)

Checked 2026-08-06 (v16 UI redesign + Linux-only sweep + .deb packaging):
- [x] **v16 UI/UX redesign**: full-width fluid layout (max-width 1500px);
      Management user cards in a responsive 2-3 column grid; Bundle/Network/
      Admin split into 2-column layouts (forms left, live preview card right);
      device lists → collapsible accordions (collapsed by default, state
      survives the 5 s WS re-render via `expandedUsers`); right-aligned icon
      action group; "ACTIVE" badge → color-coded status dot (green online /
      gray offline / amber+`Quota` / red+`Blocked` with a text tag); Logs page
      at ~95% width with level filters (ALL/INFO/WARNING/ERROR) + search +
      refresh + export (100% client-side); navbar polish, uniform radius scale
      (`--radius-sm…xl`), `:focus-visible` rings (`?v=15`, +1 web_ui test)
- [x] **.deb packaging (GitHub Actions)**: `.github/workflows/release.yml`
      builds `quota-manager_<ver>_all.deb` on a `v*` tag and uploads it to
      GitHub Releases (no local build script); `packaging/DEBIAN/control.template`
      (Version rendered from `quota/version.py`; the tag must match the version
      or the workflow fails loudly), `postinst` (venv → setup script with
      `QUOTA_NO_APT=1` → enable+start the service; idempotent, preserves
      `/etc` config + DB), `prerm` (stop+disable on remove/upgrade);
      `setup_gateway_kali.sh` gained the `QUOTA_NO_APT` guard; `.gitignore`
      ignores `*.deb`; `tests/test_packaging.py` pins the whole contract
      (workflow YAML/triggers/payload, control fields, exec-bit lifecycle
      scripts, QUOTA_NO_APT) with zero shelling-out (11 tests)
- [x] **docs**: README rewritten Linux-only (`.deb` install, source install,
      upgrading, "Releasing a new version" — bump version.py → push → tag vX →
      workflow releases; removed all Windows sections); CLAUDE.md SYSTEM FLOW /
      ARCHITECTURE / KNOWN LIMITS swept of Windows references, packaging +
      release flow documented
- [x] **local traffic never consumes the bundle**: same-subnet client↔client is
      L2 (never forwards), but client↔uplink-LAN traffic (router admin UI, NAS,
      router-as-DNS) crosses the `forward` hook and WAS counted in both
      directions. The nftables engine now excludes both LAN subnets from the
      per-device counter rules (`ip daddr/saddr != <local-net>`;
      `engine.client_subnet`/`engine.uplink_subnet` — explicit wins, derived
      from the `dhcp` block when unset, invalid CIDRs warn + fall back), and
      the two `@blocked` drop rules carry the same exclusions so a quota-blocked
      device keeps LAN access while its internet is cut. Setup script writes
      both explicitly (`CLIENT_NET` + a new `_ip_net_of` LAN-network helper
      from `LAN_IP`/`LAN_CIDR`); config.yaml documents the keys (+4 regression
      tests in test_nftables.py)
- [x] **Linux-only sweep (v15 P1)**: repo trimmed to the Linux stack —
      `requirements-linux.txt` (no pydivert/scapy), `scripts/setup_gateway_kali.sh`
      + `update_oui.py`, `config.yaml` as the example config; deleted the
      Windows-only modules (`quota/{dhcp,dns,arp}.py`, WinDivert `engine.py`,
      `requirements.txt`, `scripts/setup_gateway.ps1`) and their tests; `run.py`
      Linux-only
- [x] full suite: **153 passed**, pyflakes clean (code)

Checked 2026-08-05 (Linux pivot + bundle-source fix + per-user quota model):
- [x] core (config / logging_setup / timeutil) + `backend`/`table`/`lease_file` fields
- [x] db layer + schema + `bundle_source` setting
- [x] quota service + unit tests (incl. reset_day=0, bundle recharge)
- [x] **nftables engine** (`quota/nftables.py`) + fake-`nft` tests (11 tests)
- [x] **bundle source fix**: config.yaml reconciled every boot unless
      `bundle_source=dashboard` (+ reconcile / ownership tests)
- [x] **live-counter fix**: holder now carries flushed deltas (regression test)
- [x] run.py Linux rewire: `IS_LINUX`, backend auto-selection, dnsmasq lease
      sync, lazy Windows-only imports (+ wiring tests)
- [x] `config.yaml` + `requirements-linux.txt` + setup script aligned
      with the engine's nftables table
- [x] **deploy hardening** (4-lens adversarial verify, 18 confirmed defects
      applied): LAN_IF wired-NIC picker + subnet preflight, dnsmasq
      `dhcp-authoritative` + dual upstream + `LEASE_HOURS` knob, boot-race +
      ExecStop systemd drop-ins, `_last_blocked_ips` blocked-set cache, IPv6
      router-bypass warning, README path/env fixes
- [x] **per-user quota model**: `users` table + `devices.user_id`/`bypass`;
      allowance on the user, usage = Σ devices; block fan-out + bypass
      precedence (`resolve_device_state`), user admin cut resolved-not-persisted;
      idempotent migration backfills legacy devices → own user (+
      `test_users_migration.py`); new DHCP devices auto-create their own user;
      per-user block/edit/top-up REST routes + user-grouped dashboard
- [x] **manual-reset fix**: "Reset month now" restarts the period **from today**
      (was a silent no-op when `reset_day>0`); `ensure_period` no longer undoes
      an early reset (+2 regression tests); frontend hidden-invalid-field fix
      for the user/device modals (`step="any"`, `?v=6`)
- [x] **same-day reset fix**: usage is stored day-granular (`usage_daily` per
      device/date), so on `reset_day=0` a reset pointed the period at today but
      today's already-recorded usage still counted — the button looked dead
      (the deployed 6.02 GB report). `reset_month` now calls
      `db.clear_usage(old_period_start)` to zero the current period's rows
      before moving `period_start`; history before the period start survives
      (+2 regression tests)
- [x] **MAC vendor lookup**: bundled IEEE database (`quota/oui.txt`, regenerable
      via `scripts/update_oui.py`); `quota/vendor.py`
      resolves a MAC's manufacturer offline (lazy load, zero deps) and strips
      registry legal boilerplate for display; API adds `vendor` to every device,
      dashboard falls back to the vendor for unnamed devices (+ a small vendor
      tag next to the MAC when a real name exists) (`?v=7`)
- [x] **restart-resurrection fix**: `flush table` keeps named counters (cumulative
      totals) while the in-memory baseline is lost on restart, so the first drain
      after a restart re-added the whole old total to `usage_daily` — a
      consumed-and-reset quota came back (the vendor patch's restart surfaced it).
      `start()` now best-effort `nft reset counters` + `_add_device()` re-seeds
      `_last` from carried-over counters (+2 regression tests)
- [x] **per-device consumption monitor**: each device row now shows ITS OWN
      period usage — a device bar (this device's share of the user's allowance)
      + ↓/↑ split — fed from `db.get_period_usage()`; the user-aggregate
      `used_gb`/`percent` fields are unchanged (`device_used_gb`/`device_up_gb`/
      `device_down_gb`/`device_percent` added; +1 regression test) (`?v=8`)
- [x] **UI restructure**: "Usage this period" chart section removed (the `/api/usage`
      endpoints stay for tests/API); "Bundle settings" + "Bundle recharged" +
      "Reset month now" live in one card, "Admin" (change password) in its own
      card; new tabbed card — **Activity** (events) + **System logs** (tail of
      `logs/quota.log`, newest first, via new auth-gated `GET /api/logs` fed from
      `create_app(log_path=…)`, wired in `run.py` from `cfg.log_file`);
      Chart.js script tag + `chart.umd.js` asset fully removed from the UI
      (`?v=9`, +2 API tests) (`/api/logs` returns empty on missing file)
- [x] **speed shaping (v11)**: `quota/shaping.py` `TcShaper` — per-device + per-user
      internet speed caps and low-latency (bufferbloat-free) queues via Linux `tc`
      (HTB + `fq_codel`), single-NIC two-tree design (ifb0 for uploads, NIC egress
      for downloads), signature-diff reconcile, graceful degradation; `limit_down/
      up_mbps` columns + migration on `devices`/`users`; `service.get/set_shaping`
      (Network-tab settings stored in DB); `GET/POST /api/network`; UI Network tab
      + speed fields in device/user modals + `↓N ↑M` tags; setup script loads
      `ifb` (iproute2) (`?v=11`, FakeTc tests)
- [x] full suite: **119 passed**, pyflakes clean (code)

## [LEGACY_DEBT_AND_RISKS] (deep audits 2026-08-08 + 2026-08-10 — pre-breaking-change baseline)
_From the 5-agent audits (08-08 and 08-10, Gatekeeper PASS on both, working tree clean at HEAD
a9a26ec = v0.1.1). Not yet fixed — this is the inventory the refactor phase should address before
TASKS.md's breaking changes land. The 08-10 re-audit CONFIRMED every item below (line numbers
shifted slightly) and added the findings marked [08-10] (full detail in the 2026-08-10 entry
above)._**

**Dead code (zero production callers — safe to remove in the refactor phase):**
- `quota/db.py`: `add_topup` (:430), `has_bundle` (:665), `Device.is_admin_blocked` (:105),
  `Device.is_blocked` (:75), `get_device_by_ip` (:377); `get_ip_for_mac` (:632) + `set_lease`
  (:652) are test-only.
- **Orphaned endpoints** (no UI/JS consumer since the v9 chart removal): `GET /api/usage`,
  `GET /api/usage/{id}`, `GET /api/events` (+ the `events` table's ~25 `add_event` write sites —
  the Activity tab that read them is gone). `get_usage` / `get_usage_series` (db.py) are
  exercised only by tests.

**Known open defects (root-cause located, NOT fixed):**
- **Per-device block can silently not cut a lease-less device.** The kernel `@blocked` set is
  keyed by IP from lease rows (`service.snapshot_state`); a device with no active lease (static
  IP / expired at snapshot time) gets `ip=""` → excluded from `ip_to_mac` → never blocked. The
  only cover is the ARP gateway-lock `known_ips` deny, which defaults OFF in config.yaml and is
  forced OFF in WAN mode — so default/WAN configs leave a lease-less blocked device uncut
  (`service.py:272-307`, `nftables.py:333-406`; the regression test passes only because its
  fixture gives the device a lease row). **This matches TASKS.md's "per-device block not
  working".**
- **[08-10] `/api/milestone/notify` is unauthenticated + has no IP-ownership check**
  (`api/app.py:386-395`) — any LAN host can POST an arbitrary `user_id` and clear/re-arm their
  50/75/100% milestone pills. Display-integrity only, but the GET reader IS IP-resolved to the
  requester's own user, so the two endpoints disagree.
- **[08-10] `/report` is default-ON for the whole client subnet** (`report.allow_client_subnet:
  true`) — a rogue static-IP device on 192.168.2.x passes the subnet gate and reads full
  household usage + events + log tail with no session. Gate is sound (`request.client.host`, no
  XFF handling) — the exposure is the documented "trusted LAN" assumption.
- **[08-10] a deleted-but-still-connected guest is UNCCOUNTED + UNCONTROLLED** — the suppressed
  MAC keeps its lease with no device row → no counter rule until it disconnects and re-registers
  (run.py:273-275 documents this; no dashboard card surfaces it).
- **[08-10 → FIXED 08-10] the box's dnsmasq upstream DNS-forwarding was charged to the Gateway
  user** — client DNS queries relayed to 8.8.8.8 traverse the output/input hooks and counted
  against the 1.0 GB (the counter rules were programmed FIRST in each chain, before the
  DNS/DHCP exemption accepts and the gw_blocked drops, so exempted relay + blocked drops were
  metered). A blocked Gateway consumed ~30 MB/day of pure DNS relay. Fixed by reordering
  `_program_gateway` (quota/nftables.py) so exemptions run first (relayed service traffic is
  never counted), the gw_blocked drops next (a dropped packet terminates the chain — a blocked
  box's attempted bytes never consume the bundle), and the q_gw counters LAST (only non-local,
  non-exempted traffic that survives the block is metered) (+ regression test
  `test_gateway_exempted_and_blocked_traffic_never_reach_counters`; replay script synced).
- **[08-10] `evaluate_blocks` persists `block_state='quota'` onto the GATEWAY_MAC device row**
  when the protected user goes over (cosmetic — the resolved state, not the row, drives
  `set_gateway_blocked`).
- **Network-tab preview staleness:** the WS payload carries NO shaping key, so
  `renderNetworkPreview` renders cached `networkConfig`; a second admin's cap change is invisible
  until that client opens the Network tab. `_reshaping_now` also no-ops before the first tick
  (`_last_ip_to_mac` is empty at boot).

**Performance risks (static audits 08-08 + 08-10 — live measurement still pending; no telemetry
exists to quantify any of this):**
- `TcShaper.update_state` runs ~80 `tc` subprocesses synchronously ON the event loop on any tree
  change (~0.5–2 s stall); `detect_ppp` runs a subprocess on-loop per WAN tick; `GET /api/logs`
  blocks the loop reading the whole 5 MB log per call; the OUI 1.7 MB lazy-load parses on-loop
  once. **No per-tick timing telemetry exists anywhere** — drift/stall can't be quantified. WS
  payload is built twice per client every 5 s (per-client loop + `_push_loop`); the renderer does
  a full DOM rebuild each push.
- **[08-10 re-verified, with numbers]** `shaper.update_state` is actually ~70-115+ sequential
  `tc` subprocesses on the loop (run.py:512, shaping.py:299-306) ≈ 1.5-5 s on a slow laptop, and
  an API cap-edit fires it IMMEDIATELY via `_reshaping_now` (run.py:518-527) — a Network-tab save
  freezes the loop; `engine.update_state`/`set_gateway_blocked` run `nft` on-loop (run.py:450/455,
  ~80 at first boot); `detect_ppp` runs `ip` on-loop WAN-only (run.py:559, LAN exempt);
  `_read_log_tail` reads the WHOLE log on-loop (api/app.py:46-64; both `/api/logs` and `/report`);
  `/report` also does `list_leases()` PER DEVICE (api/app.py:447 — the dashboard payload hoists
  it once at :231). **REFUTED from the 08-08 list:** `check_internet`/`check_internet_dns` ARE
  `asyncio.to_thread`'d (run.py:577-579) — off-loop, the old claim was wrong.
- **DB:** ~30 separate commits per tick (no batching — a floor, actually more with N devices);
  `get_period_usage_by_user` runs twice per tick and `get_bundle` ~5× (service.py:153/255/292 +
  inside each usage call); the `events` table is UNBOUNDED (no prune — the only real disk-growth
  risk); usage/lease writes are per-item serialized awaits. [08-10: all re-confirmed with
  file:line; the "three times" figure was low — it's ~5×/tick.]

**Security (low severity for a LAN admin box, but honest):**
- PBKDF2-SHA256 at 200k iterations (OWASP 2023+ recommends 600k for SHA-256).
- Session cookie `httponly` + `samesite=lax` but no `secure=True`; no login rate-limiting.
- Deps: all pins current-latest (Aug 2026), no applicable CVEs. Starlette 1.4.1 is above the
  2025 Range-header, CVE-2026-48710 "BadHost", CVE-2026-54282, and CVE-2026-48817 fixes, but
  `StaticFiles` is in the latter advisory's blast radius — re-pin when a newer starlette ships.
  [08-10: route-level `Depends(_require_auth)` auth is structurally immune to BadHost regardless;
  dev-only pytest 8.3.5 / httpx 0.28.1 are outdated; starlette's testclient now warns it will
  deprecate httpx (future `httpx2`).]

**Simplicity debt (advisory seams — see audit):**
- 3 sources of truth for bundle & topology (config.yaml + DB + ownership flag); **two on/off
  switches for shaping** (`config.yaml shaping.enabled` gates existence, DB `shaping_enabled`
  gates activity).
- WS payload: 26 keys/device with the user's aggregates duplicated per device; no schema
  versioning / delta projection.
- Topology state written by THREE writers (`netmgr.render_config`, `scripts/topology.sh`,
  `scripts/setup_gateway_kali.sh`) — the bug class behind the v18 revert + v19 creds-wipe.
- `run.py` `_maintenance_tick` is a 7-job god method; `quota/db.py` is 837 lines of schema +
  CRUD + events + settings + gateway seeding in one file.

**Top break points for the pending breaking change (blast radius order):**
1. `run.py` `_maintenance_tick` + `_sync_shaping` (:389-516) — the single orchestration point.
2. `nftables.update_state` / `set_gateway_blocked` cache-gated rebuilds (:333-377, :505-526) —
   a same-set re-flush opens a short unblock window.
3. `shaping.update_state` / `_state_signature` / `_burst` (:261-402, :97-110).
4. `service.resolve_device_state` / `quota_blocked_for` precedence (:178-220).
5. `api._dashboard_payload` wire format (:139-295) — app.js has no schema check.
6. `db.py` idempotent migrations (must stay re-run safe against a migrated box DB).
7. **`quota/engine.py` is the cross-cutting choke point** — a field rename there ripples
   through 1, 2, 3, 5 and 6 simultaneously.

## [KNOWN LIMITS] (honest)
- Counting is approximate ("≈" in the UI) — nftables counters are read every
  ~15 s, so the live split lags slightly and bytes are attributed to the device
  that owned an IP at drain time. No throttling — exceeded devices are
  hard-blocked (kernel `nftables` drop), never throttled.
- **Root required**: nftables + dnsmasq (udp/53 + udp/67) + tc all need root;
  the systemd service and the .deb's postinst run as root, so only a manual
  foreground run needs `sudo`.
- Subsystems degrade gracefully: no `nft` / no root => no counting (dashboard
  still shows DB usage); no dnsmasq => no DHCP/DNS; no `tc`/`ifb` => no speed
  shaping (limits off, quota blocks + accounting unaffected). The service is
  `Restart=always` + systemd, so a crash or reboot brings the gateway back.
- **Electric-cut fallback is a liveness trade-off**: devices holding a router
  fallback lease during a gateway outage are not counted or controlled (quota
  enforcement is suspended until the gateway returns and leases renew). This
  is intentional — it keeps devices online when the gateway is down.
- **IPv4 only**: Quota Manager counts and blocks IPv4. If the router/ISP is
  dual-stack, WiFi clients take IPv6 Router Advertisements straight from the
  router and that traffic never crosses the gateway — uncounted and
  unblockable. The gateway's own IPv6 is disabled by the setup script, but the
  ROUTER's IPv6/RA must be disabled too; the setup script + README now spell
  this out. Accept that IPv6-using apps bypass the quota if the router cannot
  disable IPv6.
- **Static-IP bypassers are denied, not magically fixed**: the ARP gateway-lock
  (SYSTEM FLOW step 8) cuts internet to a device that uses the ROUTER as its
  gateway, but it is ARP-level: a device with a **static ARP entry** still
  evades it, and an **uplink-subnet** static-IP device is detected (rogue card)
  but not captured. Router-side MAC filtering / client isolation is the durable
  complement; making the gateway the AP (`hostapd`) or enabling **Strong (WAN)
  mode** (SYSTEM FLOW step 9) is the fully airtight topology.
- **Strong (WAN) mode is opt-in and needs the router hands-on**: it needs a
  bridgeable router (single-NIC layout) or a second NIC + AP-mode router
  (two-NIC fallback, works on every router), and a PPPoE outage takes the
  internet down until ppp0 redials. The WAN tab applies the switch LIVE and
  automatically (config.yaml + DB written together, gateway restarts), but the
  **physical router rewiring (bridge/AP) is always hands-on** — applying WAN
  while the router isn't actually bridged cuts internet until it is. A failed
  apply keeps the box up (no restart into the half-applied state) and surfaces
  the applier's stderr in the WAN tab.
- **Speed shaping needs real line rates**: the Network-tab totals must be set
  to the **real line down/up rates** — only then does the queue form at the tc
  layer where `fq_codel` can keep pings low under load (otherwise a fast
  unlimited client saturates the modem buffer and every user's latency rises).
  `tc` shape rates are approximate (Mbps, not byte-exact) and the single-NIC
  egress tree shares one NIC's bandwidth between uplink traffic and client
  downloads. Shaping needs `tc` (iproute2) + root + the `ifb` module; without
  them it degrades silently — limits off, quota blocks + accounting unaffected.
- **The box's own internet is metered by default** (`engine.count_gateway`,
  default ON — and defaults ON even on upgraded configs that predate the key):
  traffic from the box itself is counted and charged to the protected Gateway
  user (fixed 1.0 GB). That 1.0 GB is silently deducted from every auto-share
  bundle when the period first opens (behavioral change on upgrade; fixed-mode
  allowances unaffected). A Gateway block (`gw_blocked`, input/output hooks
  only) cuts the box's own internet — clients on the forward chain are
  unaffected. `count_gateway: false` skips the counters but keeps the drops.
- **/milestone is public and /report is source-IP-gated, not session-gated**:
  /milestone shows only the requester's own user; /report (any client-subnet
  source or `report.allowed_ips`) shows full household usage + events + log
  tail with no admin login. Both assume a trusted LAN — keep the box's
  dashboard port LAN-only.
- **LAN mode needs a fixed uplink address on the box**: either a **router DHCP
  reservation** for the machine's MAC or a **static address set on the machine
  itself** (the setup script sets `192.168.1.110` static via nmcli /
  `/etc/network/interfaces` and verifies it landed). If the box's IP can
  change — a lease expires, a reboot, or the router leasing that address to
  another device — every client loses its gateway + DNS and the dashboard is
  unreachable. The uplink address must also sit outside (or be excluded from)
  the router's DHCP pool so the router can't hand it out. Not an issue in WAN
  mode, where the box dials PPPoE itself.
