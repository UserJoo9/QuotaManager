# CLAUDE SYSTEM MAP — Quota Manager

Gateway that splits a metered internet bundle (e.g. Egypt 140 GB/month) fairly
across USERS — a person's allowance covers all of their devices (phone +
tablet + laptop share one slice). When a user exceeds their allowance, every
device they own is cut at once; a per-device override can exempt a single
device. Admin dashboard: dark-purple glassmorphism web UI. Deployment target:
**Linux on an old laptop** (Kali/Debian) — the kernel owns the network path.

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
   systemd unit for the app.
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
and uploads to GitHub Releases. `packaging/DEBIAN/postinst` builds the venv,
runs `setup_gateway_kali.sh` with `QUOTA_NO_APT=1` (the package `Depends`
already pulls dnsmasq/nftables/iproute2/kmod/python3-venv), and enables +
starts `quota-gateway`. `prerm` stops/disables the service on remove/upgrade.
Postinst/pg upgrade paths are idempotent and preserve `/etc/quota-gateway/
config.yaml` + `/var/lib/quota-gateway/quota.db`. Tests: `tests/test_packaging.py`
pins the workflow + control + lifecycle-script contract (no dpkg needed).

## [ARCHITECTURE]
```
QuotaManager/
├── CLAUDE.md                 <- this file (SYSTEM MAP)
├── README.md                 # end-user docs (install, usage, troubleshooting)
├── Structure_README.md       # developer docs (architecture, config, API,
│                             #   tests, release process)
├── LICENSE                   # MIT license
├── .github/workflows/
│   └── release.yml           # on a v* tag: build .deb -> GitHub Releases
├── packaging/DEBIAN/
│   ├── control.template      # Debian control (Version rendered from version.py)
│   ├── postinst              # venv + setup_gateway_kali.sh (QUOTA_NO_APT=1) + start
│   ├── prerm                 # stop + disable quota-gateway on remove/upgrade
│   └── changelog             # Debian changelog (rendered with the version)
├── config.yaml               # Linux gateway settings (dnsmasq + nftables)
├── run.py                    # Gateway wiring: engine + maintenance + uvicorn
├── requirements-linux.txt    # Linux deps (fastapi, uvicorn, aiosqlite, PyYAML + test deps)
├── scripts/
│   ├── setup_gateway_kali.sh # Linux: sysctl, client-subnet NAT, dnsmasq,
│   │                         #   systemd unit, info (QUOTA_NO_APT skips apt)
│   ├── topology.sh           # runtime LAN/WAN applier (panel-invoked): NIC
│   │                         #   (nmcli/ifupdown), dnsmasq, PPPoE dial; env-fed
│   ├── test_pppoe.sh         # throwaway PPPoE dial (ppp200) — test creds with
│   │                         #   NO config/topology/routing change (WAN tab)
│   └── update_oui.py         # regenerate quota/oui.txt from the IEEE registry
├── core/
│   ├── config.py             # config.yaml -> typed Config dataclasses
│   ├── logging_setup.py      # QueueHandler -> writer thread -> rotating file
│   └── timeutil.py           # month-boundary math (zoneinfo)
├── quota/
│   ├── db.py                 # SQLite schema + async access (aiosqlite); users
│   │                         #   table + devices.user_id/bypass + idempotent
│   │                         #   migration (legacy devices → own user);
│   │                         #   speed caps: devices/users limit_down/up_mbps
│   ├── service.py            # per-user quota math (allowance on the user,
│   │                         #   usage = Σ devices), block fan-out + bypass
│   │                         #   precedence, top-up, recharge, reset_day=0,
│   │                         #   period roll; shaping settings (get/set)
│   ├── nftables.py           # NftablesEngine (Linux): kernel counters + block
│   │                         #   + ARP gateway-lock deny rules (known_ips set)
│   ├── shaping.py            # TcShaper (Linux): per-device + per-user speed
│   │                         #   caps + low-latency queues (HTB + fq_codel),
│   │                         #   single-NIC two-tree design (see SYSTEM_FLOW)
│   ├── arp_scan.py           # rogue static-IP detection: raw-socket ARP probe
│   │                         #   of both LAN subnets -> hosts not leased by DHCP
│   │                         #   (shared frame build/parse + resolve_nic helpers)
│   ├── arp_lock.py           # ARP gateway-lock responder: claims the router's
│   │                         #   IP on the client subnet so bypassers' frames
│   │                         #   arrive at the box (raw-socket thread)
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
│   ├── app.py                # FastAPI factory: REST + /ws + static mount
│   └── schemas.py            # pydantic request models
├── web/
│   ├── index.html            # login + dashboard + modals
│   └── assets/
│       ├── styles.css        # dark purple glassmorphism
│       └── app.js            # WS client, dashboard render, user-grouped
│                             #   device cards, user + device controls
└── tests/
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
_(empty — all components are integrated and verified)_

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
