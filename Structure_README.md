# Quota Manager — Developer Guide

How the app actually works: the topology, the packet path, the maintenance
loop, the quota model, and every subsystem — plus configuration, the REST API,
the project layout, tests, and the release process.

> End users: this is not for you. The quick install and day-to-day usage live in
> the [README](README.md).

---

## Table of contents

- [What it is](#what-it-is)
- [Topology: a one-armed gateway](#topology-a-one-armed-gateway)
- [The packet path (no Python)](#the-packet-path-no-python)
- [The maintenance loop](#the-maintenance-loop)
- [Quota model](#quota-model)
- [Speed shaping](#speed-shaping)
- [DNS filtering (domain rules, presets, per-client DNS servers)](#dns-filtering-domain-rules-presets-per-client-dns-servers)
- [Rogue devices & the ARP gateway-lock](#rogue-devices--the-arp-gateway-lock)
- [Strong (WAN) mode](#strong-wan-mode)
- [Key design decisions](#key-design-decisions)
- [Known bottlenecks & technical debt](#known-bottlenecks--technical-debt)
- [Requirements](#requirements)
- [Running from source](#running-from-source)
- [Configuration](#configuration)
- [REST API](#rest-api)
- [Project structure](#project-structure)
- [Tests](#tests)
- [Releasing a new version](#releasing-a-new-version)

---

## What it is

Quota Manager splits a metered internet bundle across **users**, not devices. A
user's allowance covers all their devices (phone + tablet + laptop share one
slice); when the user exceeds it, every device they own is cut at once, and a
per-device *exempt* flag can keep one device online.

The deployment target is **Linux on an old laptop** (Kali/Debian) because the
kernel owns the network path: nftables counts and drops at line rate with **no
Python in the packet path**. The web dashboard (FastAPI + WebSocket) is only the
control plane.

Dependency direction is strictly downward: `api → quota/core`, `quota → core`.

```
┌──────────────┐   Ethernet    ┌───────────────────────────────────────────┐
│  ISP Router  │◄─────────────│  Old laptop (24/7)                        │
│  WiFi + NAT  │              │  dnsmasq        nftables    web dashboard │
│  DHCP off    │              │  (DHCP + DNS)   (count + cut)             │
└──────────────┘              └───────▲───────────────────────────┬────────┘
                                      │ devices' gateway + DNS    │ every byte
                                ┌─────┴───────┐           ┌───────┴────────┐
                                │  Phones     │           │  TVs           │
                                │  Laptops    │           │  Consoles      │
                                └─────────────┘           └────────────────┘
```

---

## Topology: a one-armed gateway

The router keeps its **WiFi and NAT** — devices join the same SSID as always.
The gateway sits one cable upstream. Three things make every byte cross it:

1. **The router's DHCP is disabled** (or restricted to a non-overlapping
   fallback pool) and the gateway runs its own DHCP server instead.
2. The gateway keeps a **static uplink IP** on the router's LAN subnet
   (e.g. `192.168.1.110`) **and a client-subnet alias** (`192.168.2.1`) on the
   same NIC.
3. Its DHCP hands devices addresses from a **separate client subnet**
   (`192.168.2.100–200`) with **their default gateway and DNS set to the
   gateway** (options 3/6).

Every device routes through the gateway, so it sees every byte. Kernel IP
forwarding + masquerade NAT moves packets on to the router.

**Why a separate subnet + NAT?** The kernel's proxy-ARP refuses same-subnet
targets, so on the old one-armed layout the router could deliver return traffic
straight to a device, silently bypassing the gateway. Putting clients on their
own subnet (`192.168.2.0/24`) that the kernel **masquerades** out the uplink
makes every byte deterministically cross the laptop — no proxy-ARP, no scapy,
no userspace packet sniffing.

The stack is configured once by `scripts/setup_gateway_kali.sh` (run
automatically by the `.deb`'s `postinst`):

- **sysctl** — `ip_forward=1`, IPv6 off (persisted in `/etc/sysctl.d/`).
- **Static IPs on the wired NIC** — the uplink `192.168.1.110/24` *and* the
  client-subnet alias `192.168.2.1/24`, via NetworkManager (`nmcli`) or the
  ifupdown fallback. The setup script auto-detects the wired NIC (Ethernet +
  carrier, skipping WiFi/VPN) and verifies the addresses actually landed.
- **dnsmasq** — serves DHCP (udp/67) + DNS (udp/53) for the `192.168.2.x` pool,
  gateway + DNS = the laptop, dual upstreams (the router + `8.8.8.8`),
  `dhcp-authoritative` so devices migrate off stale router leases fast. One
  daemon replaces a userspace DHCP+DNS stack.
- **nftables NAT** — `inet quota_nat`, a masquerade rule covering the client
  subnet. **The app's table (`inet quota_gateway`) is created by `run.py`
  itself at startup** — the setup script never touches it.
- **systemd** — a `quota-gateway` unit (`Restart=always`, auto-start).

The script is idempotent and refuses to run while the app is live.

---

## The packet path (no Python)

On Linux, traffic flows through the kernel only. The app programs rules and
reads counters; it never sits in the data path.

### Accounting + blocking (`quota/nftables.py`)

`NftablesEngine` programs one named counter pair per device
(`q_up_<ip>` / `q_down_<ip>`, dots→underscores) in the `forward` chain, plus a
`blocked` set that two drop rules reference:

- The kernel counts at line rate; the app only reads `nft -j list counters`
  (JSON) on a 15 s tick.
- A blocked device's IP is added to `@blocked` and the kernel **drops** its
  forward-chain packets — a hard internet cut at line rate. Admin toggles work
  the same way.
- **Local (LAN) traffic never counts.** The counting rules exclude the client
  subnet and the uplink subnet (`ip daddr/saddr != <local-net>`), so
  client↔client (L2 anyway) and client↔uplink-LAN traffic (router admin, NAS,
  router-as-DNS) never consume the bundle — only internet-bound bytes are
  charged. The two `@blocked` drop rules carry the same exclusions, so a
  quota-blocked device keeps LAN access while its internet is cut.
- The `blocked` set is rebuilt only when its membership changes
  (`_last_blocked_ips` cache) — a same-set re-flush every tick would open a
  short unblock window.
- **Restart-safe accounting.** `flush table` deletes rules but **not** named
  counter objects (they keep their cumulative totals), while the in-memory
  delta baseline is lost on restart. So `start()` best-effort runs
  `nft reset counters` to zero surviving counters, and `_add_device()`
  re-seeds the baseline from any counter still carrying a pre-restart total.
  Without this, the first drain after a restart re-added the whole old total to
  `usage_daily` (a consumed-and-reset quota came back on every restart).

### Engine ↔ asyncio communication

The engine runs in a background thread and exchanges data with the asyncio side
through **thread-safe counter snapshots** (`quota/engine.py` → `SnapshotHolder`)
— no locks in the packet hot path. The maintenance loop swaps a fresh snapshot
into the holder every tick; the API + WebSocket read from it.

---

## The maintenance loop

Every ~15 s (`run.py` → `Gateway._maintenance_tick`):

1. **Rolls the quota period** if stale (month boundary), or skips when
   `reset_day=0`.
2. **Syncs device bindings** from dnsmasq's lease file — new devices
   auto-register into the dashboard.
3. **Drains engine counter deltas** into `usage_daily`.
4. **Re-evaluates block states** — each user's usage vs. their allowance, and
   fans the cut out to all of their devices (`resolve_device_state`).
5. **Syncs the shaper** — the live ip→device→user map + Network-tab settings
   into `TcShaper`, which reconciles the `tc` tree only when the state changed.
6. **Pushes fresh ip→mac / blocked maps** into the engine + the snapshot holder
   (the flushed deltas are what the dashboard shows as "live" up/down).

Every **60 s** (slower than the tick, on purpose) it also runs the **rogue LAN
scan** (`quota/arp_scan.py`) — a raw-socket ARP probe of both local subnets;
any active host NOT in the lease file is surfaced as a rogue (see
[Rogue devices](#rogue-devices--the-arp-gateway-lock)).

**Per-device browsing history** (when `cfg.history.enabled`, default on):
`_dns_history_tick` drains a bounded queue from the `DnslogTailer` thread
(`quota/dnslog.py` — it tails dnsmasq's query log every 0.5 s, parsing both
the bare `query[A] name from <ip>` shape and the verbose extra shape with the
client ip/port between serial and `query[`), resolves each
distinct requestor IP to a device via the leases join, batch-upserts
per-(device, minute, domain) counts into `dns_history`, persists the read
cursor (`dnslog_state` setting) and — past a 1 h gate — prunes each user's rows
at *their* `history_days` (NULL = the global `history.retention_days`). See
the [REST API](#rest-api) `/api/history/{device_id}` row and the
[Configuration](#configuration) `history:` block.

**Domain filtering + per-client DNS servers** (when `cfg.dns_filter.enabled`,
default on): `_sync_dns_rules` reads `domain_rules`, every user's/device's
`dns_server`, and the device list fresh each tick, renders them into two
files inside dnsmasq's `conf-dir` (`quota-tags.conf` binds every MAC to its
own DHCP tag; `quota-domains.conf` holds the tag-restricted `address=`/
`server=` lines), and reloads dnsmasq only when a file's content actually
changed. The API additionally calls this immediately after any `/api/dns/*`
or `/api/{users,devices}/{id}/dns` edit, so a rule change does not wait for
the next tick. See [DNS filtering](#dns-filtering-domain-rules-presets-per-client-dns-servers)
below for the full design.

**VPN share** (when `cfg.vpn_share.enabled`, default off): `_sync_vpn_share`
(boot + every maintenance tick + the Network-tab toggle via `_apply_vpn_now`)
reads the DB switch (`vpn_share_enabled`, set from the Network tab), reconciles
`VpnShareManager` off the event loop, persists the detected tunnel as the pin,
feeds `engine.set_vpn_relay(state == "on")` — suspending the box's OWN gateway
metering while the relay doubles the volume through its input/output hooks —
and caches `_last_vpn_status` for the API. The `_vpn_lock` keeps the toggle and
the tick from reconciling concurrently. See
[VPN share](#vpn-share) below for the full design.

---

## Quota model

The monthly allowance lives on a **user**, not a device (`users` table;
`devices.user_id` links them):

- **Fixed users** get an exact GB allowance the admin assigns ("the bedroom gets
  30 GB").
- **Auto users** equally share the bundle remainder after fixed users take their
  GB off the top.
- A user's usage is the **sum of their devices'** usage (join
  `usage_daily` → `devices` → `user`).

```
fixed_total   = Σ fixed_gb of all fixed-mode users
remaining     = max(0, bundle.total_gb − fixed_total)
auto_share    = remaining / number of auto-mode users

allowance(u)  = fixed_gb(u)   if mode = fixed
              = auto_share    if mode = auto

used(u)       = Σ usage_daily of all devices owned by u

blocked(u)    = used(u) ≥ allowance(u)   OR   admin switched the user off

device state  = resolve_device_state(u, dev):
                  user admin cut > device admin cut > user quota block
                  unless dev.bypass (exempt from user quota) => online
```

The cut is **resolved at render/enforcement time** (`service.resolve_device_state`),
never written to device rows — so a user-level admin cut is lossless and
clearing it restores all devices. A per-device `bypass` keeps one device online
despite its user's quota block; an explicit per-device admin cut always wins
(precedence above). Enforcement stays per-MAC/per-IP — the engine's `blocked`
set still drops packets at line rate; only the *decision* is per-user.

New DHCP devices auto-create their own user (one device ⇒ one user); legacy
device-only databases are migrated in place by `db.connect()` (idempotent
ALTERs + backfill).

### Bundle source of truth

`config.yaml` is the default source for `bundle.total_gb` / `bundle.reset_day`
and is re-applied on **every** startup. Once the admin edits the bundle or
recharges via the dashboard, a `bundle_source` setting flips to `dashboard` and
config.yaml stops overriding it — so a UI edit or recharge survives a restart,
and a YAML edit actually reaches the UI.

### No-auto-reset (`reset_day: 0`)

The period opens once and never rolls by itself; the bundle grows only via the
dashboard "Bundle recharged" action (keeps `period_start`), and a new month
starts only via the manual "Reset month now" action.

### Electric-cut fallback (optional)

The gateway is a single point of failure, so the router can keep a small
**non-overlapping** DHCP pool (e.g. `192.168.1.201–250`, gateway = router) on
the uplink subnet. dnsmasq serves only `192.168.2.x` — no overlap by
construction. Devices fall back to direct internet during a gateway outage and
re-join the managed pool as their leases renew (re-run setup with
`LEASE_HOURS=1` for fast re-adoption). The trade-off: fallback-leased devices
are not counted/controlled while the gateway is down.

---

## Speed shaping

`quota/shaping.py` → `TcShaper` is a second kernel-side stack that **never
touches nftables**. It reconciles an **HTB + fq_codel** tree on the single NIC.

One NIC carries both the uplink IP and the client-subnet alias, and NAT changes
which address is visible at each point, so the two directions use two trees:

- **Uploads** (client→internet) are redirected at NIC **ingress** (src still
  pre-NAT client IP) into `ifb0` and shaped there by `ip src`.
- **Downloads** (internet→client) are shaped at NIC **egress** by `ip dst`
  (conntrack already un-NAT'd).

Both are HTB with `fq_codel` on every leaf: per-device leaves
(`1:<0x8000+devid>`) sit under per-user classes (`1:<0x300+uid>`, capped at the
user's aggregate), under a download aggregate (`1:100`), under a root capped at
the **real line speed** from the Network tab. The effective cap is
`min(dev, user)` (clamped to the line total); the default class is capped at
the direction total (NOT a pass-through), so an unlimited downloader cannot
flood the modem buffer and inflate everyone's ping.

```
effective(d, dir)  = min(dev.limit(d, dir) or ∞, user.limit(u, dir) or ∞)
                     clamped to the direction total (Network tab)
                     → 0/unlimited sends the device to the default class,
                       capped at the direction total, NOT a pass-through
```

Two engine-side details worth knowing:

- The tree is rebuilt only when a **signature** of (enabled, totals, aqm,
  sorted caps) changes — the same idempotent-reconcile pattern as the nftables
  `_last_blocked_ips` cache.
- `ifb0` (the fake-bridge device uploads are redirected into) is brought up
  once in `start()` and verified to exist; if the module was already loaded
  without creating it, `start()` unloads + reloads with the right `numifbs`.
  The apply itself never re-modprobes — a no-op `modprobe ifb numifbs=1` at
  apply time silently killed shaping on a live box.

---

## DNS filtering (domain rules, presets, per-client DNS servers)

`quota/dns_rules.py` is a **third** generated-config layer, alongside
nftables (packet accounting/blocking), tc (speed shaping), and
`quota/dnslog.py` (browsing history) — but it never touches any of the
other three. It rides entirely on the DHCP+DNS server the box already runs
(`dnsmasq`), writing two extra files into its `conf-dir` (`/etc/dnsmasq.d`
by default, alongside `quota-gateway.conf` and `quota-dnslog.conf`):

- **`quota-tags.conf`** — `dhcp-host=<mac>,set:qmdev<id>` for every known
  device. This is the whole mechanism that makes *per-device* or *per-user*
  rules possible: dnsmasq selects config lines by DHCP tag, so binding a MAC
  to a tag is the "is this rule for THIS device" selector.
- **`quota-domains.conf`** — the actual rules, as tag-restricted
  `address=/domain/target` (block/redirect) or `server=/domain/#`
  (allow-list override) lines, plus tag-restricted `server=<ip>` lines for
  per-user/per-device upstream DNS servers. A user-scoped rule/override is
  fanned out to one line per device that user currently owns (dnsmasq only
  understands per-device tags).

```
domain_rules (DB) ──┐
dns_presets  (DB) ──┼─► DnsRuleManager.apply() ─► write quota-tags.conf
users.dns_server ───┤                              write quota-domains.conf
devices.dns_server ─┘                              (only if content changed)
                                                            │
                                                   dnsmasq --test (validate)
                                                            │
                                                  systemctl restart dnsmasq
```

Both files are rewritten and diffed on every maintenance tick and
immediately after any `/api/dns/*` (or `/api/{users,devices}/{id}/dns`)
edit — same signature-gated pattern as the nftables blocked set and the tc
tree: an unchanged render never touches dnsmasq. Unlike a SIGHUP (which only
re-reads `/etc/hosts` and lease-adjacent files), new `address=`/`server=`/
`dhcp-host=` lines need a **restart** to take effect, so a rule change costs
every client a brief (~1 s) DNS blip — acceptable for an admin edit, not
something that happens on its own.

**Blocklist presets** (`ads-tracking`, `social-media`, `streaming`,
`gambling`) are curated source lists — hosts-format or AdBlock-Plus-format —
fetched and compiled down to a flat domain set by `compile_source_text`.
Enabling one bulk-inserts `domain_rules` rows (one `executemany` + one
commit — see "Known bottlenecks" below) tagged
`source='preset:<id>:<scope>:<scope_id>'`; disabling, or re-enabling at a
**different** scope, deletes exactly those rows so a scope change never
leaves an orphaned rule set behind. Only the network-address-shaped subset
of an AdBlock-Plus list (`||domain^`) has a DNS-layer equivalent —
element-hiding, path, and regex rules are dropped during compilation, which
is an honest ceiling, not a bug.

**Blacklist/allow-list a domain straight from browsing history**: the
History tab's per-domain rows carry a live status badge (blocked / allowed /
redirected / no rule) and one-click "Block this device" / "Block everyone" /
"Allow" buttons, backed by `POST /api/dns/rules/quick`. The badge is computed
by `dns_rules.resolve_domain_status`, which mirrors dnsmasq's OWN matching
exactly rather than approximating it: longest-domain-match wins first (a
rule for `ads.example.com` beats one for `example.com`, dnsmasq's rule, not
an ordering choice made here), and ties are broken by the identical
scope/action ordering `render_rules` renders in (global < user < device;
allow after block within a scope) — because that ordering IS "last directive
for this tag wins" in the generated config, the same tiebreak here reports
the config's actual live behavior.

**SQLite NULL-uniqueness note** (a real bug caught in review, fixed before
merge): `domain_rules` has `UNIQUE(scope, scope_id, domain, action)` written
as `UNIQUE(scope, scope_key, domain, action)`, where `scope_key` is a
generated `COALESCE(scope_id, 0)` column. SQLite treats every `NULL` as
distinct from every other `NULL`; every global rule has `scope_id IS NULL`,
so a naive unique constraint on `scope_id` directly never collides for two
global rows — re-submitting the same global rule silently inserted a
duplicate instead of updating the existing one. `scope_key` gives the upsert
a real, non-NULL key to collide on. A DB created by a pre-fix build is
repaired in place by `Database._migrate_domain_rules_scope_key` (rebuilds
the table, keeps the newest row per scope/scope_id/domain/action, drops the
duplicates the old constraint let through).

**Known limitation, by design of the technique**: this is DNS-layer
filtering. A client using DNS-over-HTTPS/TLS to a resolver outside the box,
or one that hardcodes a destination IP, bypasses it — the same way it
already bypasses the box's regular DNS. Nothing about this feature changes
that.

**Known limitation, parallel to an existing one**: per-device/per-user rules
and DNS-server overrides depend entirely on the DHCP tag
(`quota-tags.conf`'s `dhcp-host=<mac>,set:qmdev<id>`), which dnsmasq only
assigns to a MAC it has actually leased. A **static-IP device is invisible
to tagging** the exact same way it is already invisible to the per-device
block enforcement documented in `CLAUDE.md`'s `[LEGACY_DEBT_AND_RISKS]`
("Per-device block can silently not cut a lease-less device") — a
per-device/per-user domain rule or DNS-server override on a static-IP
client silently does nothing, with no error surfaced anywhere.
**Global-scope rules are unaffected** (no tag needed). The ARP gateway-lock
(`engine.gateway_arp_lock`) only narrows this: it denies a static-IP device
that points at the ROUTER as its gateway (the common bypass), but a
static-IP device that already points at the BOX as its gateway is counted
and blocked normally by IP yet still never gets a tag — it is not a full
fix for the tagging gap, the same way it is not a full fix for the
per-device block gap.

Shaping sits after nftables in the packet path: blocked devices are already
dropped in `forward`, and counters see the real pre-NAT src / post-NAT dst
either way.

---

## Rogue devices & the ARP gateway-lock

Because the router keeps WiFi and shares the client segment, a device can assign
itself a **static IP + the router as its gateway** and its frames go straight to
the router at Layer 2 — the box never sees a byte. That device is then
uncounted, unblockable, and invisible. Two layers close it (both opt-in; the
setup script enables the lock by default):

1. **Detection** — every 60 s the gateway ARP-probes both LAN subnets (raw
   AF_PACKET, `quota/arp_scan.py`) and lists every active host that is **not**
   leased by the quota DHCP under **Unmanaged / rogue devices** in the dashboard
   (IP, MAC, vendor, online). New rogues also produce a `warning` event.
2. **Enforcement — the ARP gateway-lock** (`engine.gateway_arp_lock`,
   `quota/arp_lock.py` + `quota/nftables.py`): the gateway claims the router's
   IP **on the client subnet** — a small background responder answers
   client-subnet ARP requests for the router with the gateway's own MAC, and an
   nftables `arp`-family rule drops the router's competing replies. The
   bypasser's frames therefore arrive at the gateway, where a `forward` deny
   rule drops any client-subnet source that is not a leased DHCP address
   (`known_ips` set, rebuilt only on membership change). **The cheat stops
   working: a static-IP bypasser loses internet entirely until it uses the quota
   gateway.**

The lock is self-sustaining (dropped traffic makes the bypasser re-ARP, and it
is re-answered) and scoped to the client subnet, so legitimate uplink-subnet
hosts (NAS, the router) keep resolving the router normally.

Residual limits, stated honestly: a device with a **static ARP entry** still
evades it, and a device that picks a static IP on the **uplink** subnet
(colliding with the real LAN) is detected but not captured — for those, enable
**MAC filtering / client isolation on the router**. The fully airtight
topologies are making the gateway the AP (`hostapd`) or **Strong (WAN) mode**.

---

## Strong (WAN) mode

The default LAN topology leaves two bypass holes for a determined static-IP
cheat (a static ARP entry, or a static IP on the uplink subnet). **Strong (WAN)
mode closes them by moving the quota boundary to the line itself**: the gateway
laptop dials the PPPoE session (public IP on `ppp0`) and the router is demoted
to a pure bridge/AP. A static-IP device then has **no second router to bypass
to**.

> Off by default — the default LAN topology is byte-for-byte unchanged. Turn it
> on only if you need the airtight boundary.

**What changes.** `ppp0` carries the public IP, dnsmasq still serves the
`192.168.2.x` client pool, and the kernel masquerades that subnet out `ppp0`.
The ARP gateway-lock is forced off (no router on the client segment). The box
**keeps** the old uplink IP as a **secondary alias** on its NIC, so the router's
admin page stays reachable from every device *through the box* — and that
uplink subnet is treated as LOCAL (never consumes quota; not a bypass, since
the masquerade only covers the client subnet).

**The dashboard WAN tab applies the switch live** (`quota/netmgr.py` →
`TopologyManager`): it collects the PPPoE creds in the panel, rewrites
config.yaml + the DB setting **together** (`topology_source=dashboard` +
`topology`, so the next boot can never pick one and ignore the other), runs the
runtime applier `scripts/topology.sh` (NIC + dnsmasq + the PPPoE dial; creds via
the **environment**, never argv), and schedules a detached self-restart. On an
applier failure, config.yaml + the DB are **rolled back** to the previous state
(no restart into a half-applied topology). "Revert to LAN" restores the exact
LAN it left from the `dhcp.lan_*` + `engine.lan_gateway_arp_lock` snapshot keys
— never a guess at 192.168.1.1.

**Two physical layouts:**

1. **Single NIC — router in bridge/modem mode (primary).** One cable from the
   box to a router LAN port; the router switched to bridge/modem mode
   (WAN↔LAN bridged, NAT + DHCP off, WiFi kept as an AP if supported). Most
   Egyptian FTTH/DSL combos support bridge (WE ZTE/Huawei, Orange Livebox,
   Vodafone, e&); some ISP-locked combos need a bridge-unlock code or an ISP
   call.
2. **Two NICs — router in AP mode (universal fallback).** Box NIC1 → ONT (fiber)
   or the modem in bridge (DSL) dials PPPoE; box NIC2 → router in **AP mode**
   (WiFi only, DHCP off). Every router supports AP mode; it costs a cheap USB
   Ethernet dongle. Put the second NIC's name in the panel's *WAN interface*
   field.

**PPPoE credentials** come from the ISP contract card (the same username/
password printed for the router's WAN page) or the router's WAN status page. They
are stored in `/etc/ppp/chap-secrets` + `/etc/ppp/pap-secrets` (chmod 600),
never in the world-readable peer file, and prefill in the panel from the DB.

**Workflow (all from the WAN tab):** rewire the router → **Test PPPoE
connection** first (a throwaway dial on `ppp200` that never touches the running
topology, reporting whether the ISP accepts the creds) → **Apply now** (the
gateway rewires itself and restarts; a few seconds of internet downtime). To
revert: put the router back in routed/NAT mode, then **Revert to LAN**. The one
always-hands-on step is the physical router rewiring.

---

## VPN share

Sends the whole client subnet through a VPN the box itself runs — sing-box,
xray, WireGuard or tun2socks in **TUN mode** — so every device's internet
exits at the VPN provider's IP while quota accounting/blocking (nftables) and
speed shaping (tc) keep working. Pure subprocess + sysfs plumbing, no threads.

**The mechanism is one policy rule, not a NAT rewrite.** With the Network-tab
switch ON, `VpnShareManager.reconcile(enabled, pin)` (`quota/vpnshare.py`)
programs:

- an `ip rule add pref <vpn_share.rule_pref, 1000> from <client_subnet> lookup
  <vpn_share.route_table, 200>` — below `local`/`main`, so it only wins for IPs
  the main table has no better match for (LAN routes stay direct);
- the route-table content: `default via <tunnel-peer>` (the point-to-point
  peer `ip -o -4 addr` reports for the TUN, cached), plus direct routes for the
  client + uplink subnets via `lan_interface()` so LAN traffic never tunnels,
  mirroring the nftables local-net exclusions.

Rules only ever land when sysfs confirms the tunnel device exists (`/sys/class/
net/<iface>`), and a missing tunnel is never routed into — that would
blackhole the subnet. `peer_ip()` handles both `ip -o` shapes (bare local
address vs `local ... peer ...`); the dev-only route falls back to `scope link`
(no peer/HUTI interfaces like WireGuard's). The detected device is **pinned in
the DB** (`vpn_share_interface` set by run.py; the `vpn_share.interface`
config value is the initial pin), so a multi-VPN box or a reboot re-applies the
same interface. `remove()` tears the rule + table down deterministically and
`is_rule_installed()` makes every call idempotent — `reconcile` self-heals any
crash/reboot/tunnel-restart leftover on the next 15 s tick.

**Gateway-meter suspension (`NftablesEngine.set_vpn_relay`, vpn share only).**
Relayed volume traverses the box's own input/output hooks a second time
(client → forward → tunnel → uplink), which would be charged AGAIN to the
protected Gateway user — and a quota-cut Gateway's `gw_blocked` drop would kill
the household's VPN. Suspension inserts an unconditional
`comment "quota-vpn-relay" accept` **first** in the input/output chains (above
the DNS/DHCP exemptions, `gw_blocked` drops and `q_gw` counters) and restores
by deleting exactly that rule **by handle** (`nft -a list chain` lookup,
cache-gated like the blocked set). A failed insert is never claimed (retry next
tick); forward-chain per-device quota + the ARP lock stay untouched. The state
is enforced from the APPLIED relay state, not the switch.

**Wiring.** run.py `_sync_vpn_share` runs at boot + every maintenance tick (+
immediately on the Network-tab toggle) under `_vpn_lock`, reconciles off the
event loop, persists the pin, feeds `engine.set_vpn_relay` and caches
`_last_vpn_status` (never the switch — always the applied state) for
`GET /api/network`. `vpn_share.enabled: false` in config.yaml means the manager
is never built and the API's `status` key is absent (degraded boot no-op).

**Limits, honestly:** IPv4 only (a provider's IPv6 TUN is not routed); DoH/DoT
is untouched (DNS stays on dnsmasq, and DNS-layer filtering still applies); if
the tunnel drops, the subnet is blackholed on purpose — never silently
re-routed around the quota; the box's own internet still flows (and stays
metered into the Gateway user) while relaying — the suspension only covers the
relay volume that would otherwise be double-counted.

---

## Key design decisions

These are the non-obvious choices that keep the system correct and cheap:

- **The kernel owns the packet path.** nftables counts and drops; tc shapes.
  Python never touches a forwarded byte. This is why an old laptop is enough.
- **A separate client subnet + masquerade, not proxy-ARP.** proxy-ARP silently
  refuses same-subnet targets, letting downloads bypass the box; the client
  subnet makes every byte deterministically cross it.
- **Quota on the user, enforcement per-MAC/per-IP.** The block *decision* is
  per-user (all a user's devices cut together), but the kernel still drops by IP
  — and the decision is resolved at render time, never persisted, so clearing an
  admin cut is lossless.
- **`bundle_source` ownership.** config.yaml is the default, the dashboard
  becomes the owner after a UI edit/recharge — so neither side silently loses.
- **Restart-safe counters.** Named nftables counters survive `flush table`, so
  `start()` resets them + re-seeds the delta baseline, or a restart would
  resurrect the whole old usage total.
- **Signature-gated reconciles.** The nftables blocked set and the tc tree are
  only rewritten when their state actually changed — no periodic re-flush
  (which would open unblock windows) and no needless tc rebuilds.
- **Graceful degradation everywhere.** Missing `nft`/`tc`/`dnsmasq`/root never
  takes down the whole app: the dashboard still shows stored usage, quota
  blocks + accounting degrade independently, and each subsystem logs once and
  continues.
- **A phone-first control plane.** The dashboard, the public `/milestone` page
  and the `/report` page are all responsive + touch-first: the topbar tabs
  become a horizontally swipeable strip, grids stack to one column, the bundle
  ring shrinks, modals/overlays scroll instead of clipping (a tall modal on a
  phone never loses its buttons off-screen), and touch targets are ≥ 36 px. The
  family's day-to-day loop — checking quota, topping up, viewing the milestone
  page, reading the report — works entirely from a phone.

---

## Known bottlenecks & technical debt

Audited 2026-08-10 (5-agent deep audit — reverse engineer, conflict/regression,
refactoring architect, performance auditor, gatekeeper; **Gatekeeper PASS, zero
code changes**; suite green at **297 passed**). The canonical, living inventory is
`CLAUDE.md` → [LEGACY_DEBT_AND_RISKS]; this section is the summary a developer
needs before touching the code. None of this is fixed yet — it is the pre-
breaking-change baseline.

**Correctness / honesty gaps:**
- **A lease-less blocked device is not cut.** The kernel `@blocked` set is keyed by
  IP from lease rows (`service.snapshot_state`); a device with no active lease gets
  `ip=""` → dropped from `ip_to_mac` (run.py) → never enters the kernel set
  (nftables.py). The only cover is the ARP-lock `known_ips` deny, which defaults
  OFF in config.yaml and is forced OFF in WAN mode — so default/WAN configs leave a
  lease-less blocked device uncut. No test drives this path.
- **`/api/milestone/notify` has no auth or IP-ownership check** — any LAN host can
  POST another user's `user_id` and clear/re-arm their 50/75/100% milestone pills
  (display-integrity only; the GET reader IS resolved to the requester's own user).
- **`/report` is default-ON for the whole client subnet** (`allow_client_subnet:
  true`) — a rogue static-IP device passes the subnet gate and reads full household
  usage + events + log tail. The gate reads `request.client.host` (no XFF
  handling), so the exposure is the documented "trusted LAN" assumption, not a
  spoofable bypass.
- **A deleted-but-still-connected guest is untracked.** Its suppressed MAC keeps its
  lease (internet works) but has no device row → no counter rule until it
  disconnects and re-registers.

**Performance (no timing telemetry exists — drift/stall is unquantifiable):**
- **On-loop subprocess storms.** `shaper.update_state` rebuilds the tc tree via
  ~70–115+ sequential `subprocess.run` on the event loop (≈1.5–5 s freeze on a slow
  laptop), and an API cap-edit fires it immediately via `_reshaping_now`; the
  nftables `update_state` / `set_gateway_blocked` calls also run `nft` on-loop (~80
  at first boot); `detect_ppp` runs `ip` on-loop every WAN tick. Easy win:
  `asyncio.to_thread`.
- **Whole-file reads on the loop.** `/api/logs` and `/report` read the entire (up
  to 5 MB) log synchronously; `/report` also does `list_leases()` once per device
  (the dashboard payload hoists it once).
- **WS payload built N+1 times per 5 s** (per-client loop + `_push_loop`), and
  app.js does a full DOM rebuild per push.
- **DB:** no batching (~30+ commits/tick), `get_period_usage_by_user` ×2/tick,
  `get_bundle` ~×5/tick, and the `events` table is unbounded (no prune — the only
  real disk-growth risk).

**Simplicity debt (the refactor targets):**
- Three sources of truth for bundle & topology (config.yaml + DB + ownership flags);
  two on/off switches for shaping (YAML `shaping.enabled` vs the DB Network-tab
  switch); three writers of topology state (`netmgr.render_config`,
  `scripts/topology.sh`, `scripts/setup_gateway_kali.sh`).
- `_dashboard_payload` / `_milestone_payload` / `_report_payload` each re-implement
  the usage/allowance/percent math — a change must be re-applied in three places.
- The protected **Gateway user** is a real DB row forcing ~6 special cases across
  service/run/api/UI — and its fixed 1.0 GB is silently deducted from every
  auto-share bundle.
- Big files, big coupling: `web/app.js` (1297 lines), `api/app.py` (999),
  `quota/db.py` (872), `quota/nftables.py` (751), `run.py` (672) — each a god
  module; `quota/engine.py` is the cross-cutting type hub (a field rename ripples
  through every consumer).

---

## Requirements

| Component | Requirement | Why |
|---|---|---|
| **OS** | Kali / Debian (or any systemd Linux with nftables + dnsmasq) | kernel owns the network path |
| **Hardware** | An old laptop with **one wired Ethernet port**, powered 24/7 | cheap; that's the whole point. Strong (WAN) mode needs a **second** NIC (USB Ethernet dongle) only for the two-NIC AP-mode layout |
| **Python** | 3.10+ (system `python3` is used by the package; 3.11+ recommended) | runtime |
| **Privileges** | **root** on the gateway | nftables + dnsmasq (udp/53 + udp/67) + tc |
| **Router** | DHCP disabled (or a non-overlapping fallback pool), WiFi/NAT kept | so the gateway's DHCP + gateway handoff take effect |
| **Shaping** | `tc` (iproute2) + the `ifb` kernel module, run as root | per-device / per-user speed caps (optional — without it shaping degrades silently; quota + blocking are unaffected) |

Python deps are pinned in `requirements-linux.txt`: `fastapi`, `uvicorn[standard]`,
`aiosqlite`, `PyYAML` (+ `pytest`/`httpx`/`pyflakes` for tests & lint).

---

## Running from source

**Step 1 — clone the project onto the laptop** (or copy the folder; the setup
script auto-detects the repo root):

```bash
cd ~
git clone <your-repo-url> QuotaManager
cd QuotaManager
```

**Step 2 — create the venv and install deps FIRST**:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-linux.txt
```

> **Ordering is critical.** The setup script writes a systemd unit whose
> `ExecStart` points at `.venv/bin/python3` — **only if the venv already exists
> at that moment**. If you run the setup script first, the service falls back to
> the system `python3`, which lacks the app's deps and fails to start. If you
> already did: create the venv, then re-run the script (Step 3) — it is
> idempotent.
>
> Fresh Kali/Debian: if `python3 -m venv` errors with *"ensurepip is not
> available"*, install `python3-venv` first (`sudo apt-get install -y python3-venv`).

**Step 3 — run the setup script as root**:

```bash
sudo bash scripts/setup_gateway_kali.sh
```

It configures the whole network stack idempotently (sysctl, static IPs + client
alias, dnsmasq, nftables NAT, the `ifb` module, the systemd unit) and **enables**
the service but does not start it. It refuses to run while the app is live.

All defaults are overridable via env vars: `WAN_GATEWAY`, `LAN_IP`, `LAN_CIDR`,
`CLIENT_IP`, `CLIENT_NET`, `POOL_START`, `POOL_END`, `UPSTREAM_DNS`,
`SUBNET_MASK`, `LEASE_HOURS`, `LAN_IF` (if auto-detection picks the wrong NIC),
and for WAN mode `QUOTA_TOPOLOGY` (`lan`|`wan`), `WAN_IF`, `PPPOE_USER`,
`PPPOE_PASSWORD`.

**Step 4 — edit the generated config**:

```bash
sudo nano /etc/quota-gateway/config.yaml
```

Change only the two numbers under `bundle` (and optionally add `timezone:`);
keep every other line as generated. The `dhcp.*` keys are informational —
dnsmasq owns the real pool from `/etc/dnsmasq.d/quota-gateway.conf`.

**Step 5 — start the gateway**:

```bash
sudo systemctl enable --now quota-gateway
journalctl -u quota-gateway -f
```

You should see `database ready: …` and `nftables engine ready: …`.

**Foreground run (first look):** stop the service, then from the project
directory: `.venv/bin/python run.py --config /etc/quota-gateway/config.yaml`.
Options: `--port 9000` (override web port), `--debug` (DEBUG logging).

**First login:** reconnect every device to the WiFi so it re-leases onto
`192.168.2.x`, then open `http://192.168.2.1:8080` (default password `admin` —
change it immediately).

---

## Configuration

Everything lives in one YAML file — **`/etc/quota-gateway/config.yaml`** on the
gateway (repo copy: `config.yaml`). Runtime override:
`python run.py --config path/to/config.yaml`. All values optional; defaults shown.

```yaml
db_path: data/quota.db        # SQLite database location
log_file: logs/quota.log      # rotating log file
log_level: INFO               # DEBUG for more detail (never in the packet path)

timezone: Africa/Cairo        # IANA zone for period math; empty => local time

bundle:
  total_gb: 140.0             # your monthly ISP bundle, GB
  reset_day: 1                # day of month your ISP resets the bundle (1..28)
                              # 0 = never auto-reset (see "No-auto-reset mode")

web:
  host: 0.0.0.0               # listen on all interfaces (reachable from the LAN)
  port: 8080

dhcp:
  enable: true                # DHCP server (disable router DHCP first!)
  gateway_ip: 192.168.2.1     # THE CLIENT-SUBNET ALIAS — what devices get as gateway
  router_ip: 192.168.1.1      # upstream router (reference / DNS option only).
                              #   empty "" in WAN mode — the box dials PPPoE itself
  dns_servers: [192.168.1.1, 8.8.8.8]
  subnet: 255.255.255.0
  pool_start: 192.168.2.100   # first address handed to devices
  pool_end: 192.168.2.200     # last address handed to devices
  lease_hours: 24             # lower (e.g. 1) = devices recover faster after a power cut
  lease_file: /var/lib/misc/dnsmasq.leases   # dnsmasq's lease file (auto-discovery)

engine:
  enabled: true               # accounting + hard blocking
  backend: nftables           # Linux: the kernel owns the packet path
  count_direction: inbound    # inbound | outbound — avoids double-counting routed traffic
  table: quota_gateway        # nftables table
  # Meter the box's OWN internet into the protected Gateway user (fixed 1.0 GB).
  # Default ON (even on configs that predate the key) — a heavy download on the
  # laptop itself counts against it and can cut the box until topped up / the
  # period rolls. false skips the counters but keeps the drop rules.
  count_gateway: true
  # LOCAL (LAN) traffic never consumes the bundle: client<->client and
  # client<->uplink-LAN (router admin, NAS, router-as-DNS) are excluded from
  # the counting rules (and the block drops keep LAN access for blocked
  # devices). Empty => derive: client from dhcp.gateway_ip + subnet, uplink
  # from dhcp.router_ip + subnet. The setup script writes both explicitly.
  client_subnet: ""           # e.g. 192.168.2.0/24
  uplink_subnet: ""           # e.g. 192.168.1.0/24; WAN mode derives it from the
                              #   LAN snapshot (uplink_ip + lan_cidr) — the box keeps
                              #   a router-admin alias, so the uplink subnet stays local
  # Deployment topology: "lan" (default) = the box sits behind the router
  # (clients on their own subnet, router keeps WiFi + NAT). "wan" (optional
  # Strong mode) = the box terminates the WAN itself — dials PPPoE (public IP
  # on ppp0), the router is a pure bridge/AP, so a static-IP device has NO
  # second router to bypass to. In WAN mode the box keeps the uplink IP as a
  # router-admin alias (clients still reach the router admin page through it).
  # The dashboard WAN tab switches this LIVE
  # (Apply now / Revert to LAN — config.yaml + DB written together, gateway
  # rewires itself and restarts); the setup script writes it on first install
  # for QUOTA_TOPOLOGY. The LAN reality is snapshotted into dhcp.lan_* keys +
  # engine.lan_gateway_arp_lock so a revert is exact.
  topology: lan
  lan_gateway_arp_lock: true   # ARP lock value restored when reverting to LAN

shaping:
  enabled: true               # speed shaping (Linux tc, per-device + per-user caps).
                              #   false disables the shaper entirely.
  interface: ""               # NIC that carries the client subnet; empty => auto-detect
  client_subnet: ""           # client CIDR; empty => derived from dhcp.gateway_ip + subnet
  ifb: ifb0                   # virtual device used to shape uploads (modprobe ifb)

report:
  enabled: true               # on-demand internal report (/report + /api/report).
                              #   false => every source gets a 403.
  allow_client_subnet: true   # admit any managed client (the DHCP pool subnet)
  allowed_ips: []             # extra CIDRs/IPs, e.g. ["192.168.1.0/24", "10.0.0.5"].
                              #   run.py fills client_subnet from the engine's
                              #   resolved subnet automatically.

history:
  enabled: true               # per-device DNS browsing history (History tab).
                              #   false = stop recording (the app never reads
                              #   the query log; DNS/DHCP are untouched).
  dnsmasq_log_file: /var/log/quota-dnsmasq.log  # log-facility the setup
                              #   fragment points log-queries=extra at
  retention_days: 7           # global default; a user's "history_days" overrides

dns_filter:
  enabled: true               # domain rules / presets / per-client DNS servers.
                              #   false => the feature is entirely inert.
  conf_dir: /etc/dnsmasq.d    # dnsmasq's conf-dir (Debian/Kali default)
  tags_file: quota-tags.conf     # per-device DHCP tag bindings (generated)
  rules_file: quota-domains.conf # domain rules + DNS-server overrides (generated)
  reload_dnsmasq: true        # restart dnsmasq when the generated files change
  preset_cache_dir: data/dns_presets

vpn_share:
  enabled: false              # "VPN share": route the whole client subnet
                              #   through a VPN tunnel the box runs (TUN mode
                              #   sing-box/xray/WireGuard/tun2socks). The real
                              #   switch lives in the Network tab (DB setting);
                              #   false here = the manager is never built.
  interface: ""               # initial tunnel-device pin; empty => auto-detect
                              #   the first non-LAN, network-up interface and
                              #   pin it in the DB (vpn_share_interface)
  route_table: 200            # iproute2 table the policy rule points at
  rule_pref: 1000             # ip rule preference (below local/main)
```

> **Speed shaping is switched on in the dashboard, not in this YAML.**
> `shaping.enabled: true` here only lets the shaper exist; the actual master
> switch, your real line down/up rates, and the per-device/per-user caps live in
> the **Network** tab (stored in the DB, so they survive restarts).

---

## REST API

Authenticate with `POST /api/login` (JSON `{"password": "..."}`) → the server
sets a session cookie. The dashboard client uses the same endpoints.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dashboard` | full bundle + users + devices + usage snapshot |
| GET/POST/PATCH/DELETE | `/api/users` & `/api/users/{id}` | list / create / update / delete users (allowance, block, speed caps) |
| POST | `/api/users/{id}/topup` | add GB to a user's allowance, clears their quota block |
| GET/POST/PATCH/DELETE | `/api/devices` & `/api/devices/{id}` | list / create / update / delete devices (user, quota, bypass, speed caps) |
| POST | `/api/devices/{id}/topup` | add GB to a device, clears its quota block |
| GET | `/api/usage/{id}` · `/api/usage` | daily usage series per device / aggregated |
| GET | `/api/events?limit=30` | audit events |
| GET | `/api/logs?limit=300` | tail of the rotating log (newest first) |
| GET | `/api/history/{device_id}?window=24&limit=100` | a device's DNS browsing history — `top_domains`, `activity` (minute buckets), `recent` (latest queries), `total_queries`. Auth-gated; `window` hours clamped 1–336, `limit` capped. Bandwidth is NOT duplicated here — the History tab reads live/per-period bytes from the dashboard payload. Every `top_domains`/`recent` row also carries a `status` (`blocked`/`allowed`/`redirected`/`none`) from `dns_rules.resolve_domain_status`, powering the History tab's filter badges + quick-action buttons |
| GET/POST/PATCH/DELETE | `/api/dns/rules` | domain-filtering rules: list (optionally `?scope=&scope_id=`) / create (`{"scope","scope_id","action":"block"\|"allow"\|"redirect","domain","target_ip"}`) / enable-disable via PATCH / delete |
| POST | `/api/dns/rules/quick` | one-click block/allow from a History-tab domain row: `{"domain","action":"block"\|"allow","scope":"global"\|"device","device_id"}` |
| POST | `/api/dns/import` | paste raw hosts-format or AdBlock-Plus-format blocklist text (`{"text","format":"auto"\|"hosts"\|"adblock","scope","scope_id","action"}`) → bulk-creates domain rules in one transaction |
| GET | `/api/dns/presets` | list built-in blocklist presets (ads-tracking, social-media, streaming, gambling) with their enabled state + domain count |
| POST | `/api/dns/presets/{id}/enable` · `/disable` | fetch + compile a preset's sources into domain rules for a scope (`{"scope","scope_id"}`), or remove exactly those rules. Re-enabling at a DIFFERENT scope purges the old scope's rules first |
| POST | `/api/dns/apply` | force an immediate dnsmasq regeneration + reload (normally automatic after every DNS-related edit) |
| PATCH | `/api/users/{id}/dns` · `/api/devices/{id}/dns` | set/clear a per-user or per-device upstream DNS-server override (`{"dns_server": "1.1.1.1"}`, `""` clears it) |
| GET/POST | `/api/bundle` | read / update bundle (`total_gb`, `reset_day`, or `add_gb` to recharge mid-month). A POST makes the dashboard the bundle owner (`bundle_source=dashboard`) |
| POST | `/api/reset-month` | force an early period roll-over |
| GET/POST | `/api/guest` | guest mode: auto-register new devices with their own small allowance |
| GET/POST | `/api/network` | speed-shaping settings: `enabled`, `total_down_mbps`, `total_up_mbps`, `aqm` — plus `vpn_share: {enabled, interface, status?}` from the DB (status = the cached applied state, present only when a manager is wired — `vpn_share.enabled: false` in config.yaml means boot without one) |
| GET/POST | `/api/wan` | strong-mode topology: `GET` live status (topology/source/pending/ppp0), `POST {"topology": "lan"\|"wan", "pppoe_user", "pppoe_password", "wan_if"}` APPLIES the topology live — rewrites config.yaml + the DB together, runs `scripts/topology.sh` (NIC + dnsmasq + PPPoE dial) and schedules a restart (`restart_scheduled`, `script_output`). Creds travel to the applier via the environment, never argv. On an applier failure config.yaml + the DB are ROLLED BACK to the previous state (no restart) |
| POST | `/api/wan/test` | test the PPPoE credentials WITHOUT changing anything: dials a throwaway `ppp200` link via `scripts/test_pppoe.sh` with `{"pppoe_user", "pppoe_password", "wan_if"}` and reports `status` (success/auth-failed/no-pppoe-server/link-down/error), the negotiated local/peer IPs, `internet` (ping check), and `detail` — never touches config.yaml, the DB, `ppp0`, routing or DNS |
| GET | `/api/milestone` | **public** — the requesting device's user's consumption + per-device breakdown (resolved by source IP via its DHCP lease; `recognized: false` for a lease-less IP). Pairs with the `/milestone` page |
| POST | `/api/milestone/notify` | **public** — acknowledge a crossed milestone (`{"user_id", "milestone": 50\|75\|100}`); sets the flag once per period |
| GET | `/api/report` | **source-IP gated** (client subnet + `report.allowed_ips`, else 403) — read-only consumption report: exact bundle bytes, per-user + per-device bytes, events tail, log tail, WAN status. No session cookie needed |
| GET | `/report` | **source-IP gated** — the HTML page that renders `/api/report` |
| GET | `/milestone` | **public** — the household milestone page (own usage + per-device split, one-time milestone acknowledge) |
| POST | `/api/login` · `/api/logout` | session auth |
| GET | `/api/me` | session check |
| POST | `/api/password` | change admin password |
| WS | `/ws` | pushes `{"type":"snapshot","data":{...}}` every 5 s |

Interactive docs: `http://<gateway-ip>:8080/api/docs` (Swagger UI).

---

## Project structure

```
QuotaManager/
├── CLAUDE.md                 # SYSTEM MAP (architecture, flow, known limits)
├── README.md                 # end-user quick-start docs
├── Structure_README.md       # this file — developer docs
├── LICENSE                   # MIT license
├── CHANGELOG.md              # release changelog (newest first)
├── .github/workflows/
│   └── release.yml           # builds the .deb on a version tag -> GitHub Releases
├── packaging/DEBIAN/
│   ├── control.template      # Debian control file (Version rendered from quota/version.py)
│   ├── postinst              # venv + setup script + start the gateway service
│   └── prerm                 # stop + disable the gateway service on remove/upgrade
├── config.yaml               # Linux gateway settings (dnsmasq + nftables)
├── run.py                    # gateway wiring: engine + maintenance + shaper + uvicorn
├── requirements-linux.txt    # Linux deps (fastapi, uvicorn, aiosqlite, PyYAML + test deps)
├── scripts/
│   ├── setup_gateway_kali.sh # Linux: sysctl, client-subnet NAT, dnsmasq, ifb,
│   │                         #   dnslog fragment + logrotate, systemd unit
│   ├── topology.sh           # runtime LAN/WAN applier (panel-invoked, env-fed)
│   ├── test_pppoe.sh         # throwaway PPPoE dial — test creds, no config change
│   ├── update_oui.py         # regenerate quota/oui.txt from the IEEE registry
│   └── replay_nft_startup.sh # replay the engine's startup nft command sequence (debug)
├── core/
│   ├── config.py             # config.yaml -> typed Config dataclasses
│   ├── logging_setup.py      # non-blocking QueueHandler -> writer thread -> rotating file
│   └── timeutil.py           # month-boundary math (zoneinfo)
├── quota/
│   ├── db.py                 # SQLite schema + async access (aiosqlite); users table,
│   │                         #   devices.user_id/bypass, speed-cap columns,
│   │                         #   dns_history table + per-user history_days
│   ├── service.py            # per-user quota math, block fan-out + bypass precedence,
│   │                         #   top-ups, bundle recharge, reset_day=0, period roll,
│   │                         #   shaping settings (get/set)
│   ├── nftables.py           # NftablesEngine (Linux): kernel counters + block set
│   │                         #   + ARP gateway-lock deny rules (known_ips set);
│   │                         #   set_vpn_relay: suspends the box's OWN gateway
│   │                         #   metering while VPN share relays the household
│   ├── vpnshare.py           # VpnShareManager: "VPN share" policy routing —
│   │                         #   client subnet -> dedicated route table whose
│   │                         #   default points at the box's TUN, LAN routes
│   │                         #   kept local, idempotent reconcile + self-heal
│   ├── shaping.py            # TcShaper (Linux): per-device + per-user speed caps,
│   │                         #   low-latency fq_codel queues (HTB), two-tree design
│   ├── arp_scan.py           # rogue static-IP detection: raw-socket ARP probe of
│   │                         #   both LAN subnets -> hosts not leased by DHCP
│   ├── arp_lock.py           # ARP gateway-lock responder: claims the router's IP
│   │                         #   on the client subnet so bypassers' frames hit the box
│   ├── dnslog.py             # DNS browsing history: dnsmasq query-log parser +
│   │                         #   DnslogTailer thread (bounded queue) -> dns_history
│   ├── dns_rules.py          # DnsRuleManager: domain blacklist/allow/redirect rules,
│   │                         #   blocklist presets, per-client DNS-server overrides,
│   │                         #   resolve_domain_status (History-tab filter badges) —
│   │                         #   generated dnsmasq config, no new service
│   ├── topology.py           # WAN-topology detection: is ppp0 up (for the WAN tab)?
│   ├── netmgr.py             # TopologyManager: the WAN tab's live LAN/WAN switch
│   ├── vendor.py             # MAC OUI -> manufacturer (IEEE registry, lazy load)
│   ├── oui.txt               # bundled IEEE MA-L/MA-M/MA-S database (53.5k prefixes)
│   └── version.py            # single source of truth for the release version
├── api/
│   ├── app.py                # FastAPI factory: REST + /ws + static mount
│   └── schemas.py            # pydantic request models
├── web/
│   ├── index.html            # login + dashboard + modals
│   ├── milestone.html        # public household page: own usage + per-device split,
│   │                         #   one-time 50/75/100% milestone acknowledge
│   ├── report.html           # source-IP-gated read-only consumption report
│   └── assets/
│       ├── styles.css        # dark purple glassmorphism; responsive + touch-first
│       │                     #   (topbar tabs -> swipeable strip, cards stack,
│       │                     #   modals scroll) on phones
│       └── app.js            # WS client, dashboard render, user-grouped device cards
└── tests/
    ├── test_quota_service.py # period math, per-user allowance math, block fan-out
    │                         #   + bypass, recharge
    ├── test_api.py           # REST API integration (incl. user CRUD, recharge,
    │                         #   reset-day-0, bundle_source ownership)
    ├── test_web_ui.py        # static UI served (top-bar tabs, Network panel)
    ├── test_shaping.py       # TcShaper vs a fake `tc` binary (command generation)
    ├── test_packaging.py     # GitHub-Actions release workflow + Debian control/
    │                         #   postinst/prerm + QUOTA_NO_APT guard (contract)
    ├── test_users_migration.py # legacy device-only DB -> users backfill
    ├── test_vendor.py        # OUI -> vendor lookup (MA-L/MA-M/MA-S longest-prefix)
    ├── test_nftables.py      # NftablesEngine vs a fake `nft` binary (incl. ARP lock)
    ├── test_arp_scan.py      # ArpScanner + ARP frame build/parse
    ├── test_arp_lock.py      # ARP gateway-lock responder (fake frames/socket)
    ├── test_dnslog.py        # dnsmasq query-log parser + tailer + dns_history DB
    ├── test_config.py        # typed config parsing (Linux settings)
    ├── test_netmgr.py        # TopologyManager WAN/LAN apply + rollback + PPPoE test
    ├── test_topology.py      # detect_ppp / check_internet probes (fake `ip`)
    ├── test_vpnshare.py      # VpnShareManager vs a fake `ip`: rule/route program,
    │                         #   peer.parse, pin, reconcile, teardown
    ├── test_dns_rules.py     # hosts/ABP parsing, wildcard scopes, rendering
    └── test_run_wiring.py    # run.py wiring + live boot + bundle reconcile +
                              #   dnsmasq lease sync + live-counter regression
```

Dependencies point downward only: `api → quota/core`, `quota → core`. The engine
communicates with the asyncio side through thread-safe counter snapshots — no
locks in the packet hot path. On Linux the hot path has **no Python at all** —
the kernel counts and drops.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q        # full suite
.venv/bin/python -m pyflakes run.py core quota api tests   # lint
node --check web/assets/app.js              # JS syntax
bash -n scripts/setup_gateway_kali.sh scripts/topology.sh scripts/test_pppoe.sh
```

The suite covers:

- **Per-user quota math** — allowance on the user, usage = Σ devices, block
  fan-out + bypass precedence, top-ups, reset-day-0, bundle recharge.
- **API integration** — user CRUD, session-gated password change, bundle
  ownership via `bundle_source`, `/api/network`, `/api/logs`, `/api/rogue`,
  `/api/wan` (incl. the live apply + rollback).
- **Static UI serving** — top-bar tabs, Network panel, the rogue-devices card,
  the WAN tab.
- **The nftables engine** — against a fake `nft` binary: per-device counters,
  the blocked set, the ARP gateway-lock deny/arp rules, the WAN-mode overrides,
  restart-safe counter reseeding.
- **The ARP scanner + gateway-lock responder** — frame parse/serialize +
  fake socket, no root.
- **The WAN topology detector** — `detect_ppp` vs a fake `ip`, no root.
- **The topology manager** — `TopologyManager` WAN/LAN live apply (config.yaml +
  DB written together), applier-failure rollback, creds-via-env (never argv), the
  throwaway PPPoE test verdict.
- **The speed shaper** — against a fake `tc`: the full two-tree program, the
  signature-gated rebuild, degradation paths, ifb0 bring-up.
- **The packaging contract** — GitHub-Actions release workflow, Debian
  control/postinst/prerm, the `QUOTA_NO_APT` guard.
- **Legacy DB migration** — device-only → users backfill (idempotent,
  data-preserving).
- **MAC vendor lookup** — vs the bundled IEEE registry.
- **`run.py` wiring** — config → DB bundle reconcile, dnsmasq lease sync,
  live-counter regression, the topology override, the DNS-history drain /
  persist / prune / disabled paths, the VPN-share pin + relay-suspension path.
- **The DNS-history pipeline** — `quota/dnslog.py` parser (extra-mode shapes
  including the client ip/port between serial and `query[` — the live-box shape
  — and the bare shape, PTR filtering, non-query lines rejected), the tailer
  (EOF seek, rotation/truncation, partial lines, NUL holes, missing file), and
  the `dns_history` DB (upserts, aggregation, per-user prune).
- **The VPN-share manager** — `quota/vpnshare.py` vs a fake `ip` binary: the
  rule/route program order, `peer_ip` both output shapes, LAN-route exclusions,
  dev-only `scope link` fallback, error propagation, the pin, reconcile
  idempotence + self-heal, teardown.
- **Domain filtering** — `quota/dns_rules.py`: hosts/ABP parsing, wildcard
  scopes, resolution order, the tag-scoped dnsmasq render + reload gate.

Everything that needs hardware/root (nftables, tc, DHCP, raw sockets, PPPoE) is
simulated or disabled in tests, so the suite runs anywhere.

---

## Releasing a new version

The `.deb` is built **only by GitHub Actions** and published to **GitHub
Releases** — there is no local build step.

1. **Bump the version** — edit `quota/version.py`
   (`__version__ = "0.1.2"` → next semver) and add a `CHANGELOG.md` entry if you
   want one.
2. **Commit + push**:

```bash
git add quota/version.py CHANGELOG.md
git commit -m "Bump version to 0.2.0"
git push origin main
```

3. **Tag the release** — the tag MUST match the version (the workflow fails
   loudly otherwise):

```bash
git tag v0.2.0
git push origin v0.2.0
```

The `release` workflow (`.github/workflows/release.yml`) builds
`quota-manager_0.2.0_all.deb` and uploads it to a GitHub Release named
`v0.2.0`. GitHub Releases are immutable, so each version needs a **new** tag.
The release description is auto-composed from the `CHANGELOG.md` section for
the released version (plus the install note), so keep the CHANGELOG current —
it IS the release notes.

Tagging also **auto-publishes the `.deb` to a signed apt repository**
(`.github/workflows/apt-repo.yml`, a `workflow_run` on the `release` workflow)
hosted on GitHub Pages. Once a box has the repo configured, `apt-get update &&
apt-get install quota-manager` installs/upgrades to the newest published
version; old versions stay installable.

> Version tags are what trigger the release — pushing a commit to `main` alone
> (even a version bump) does **not** release. Tag and push when you're ready to
> ship.

### Setting up the apt repository (one-time)

Users install the repo with the key + `deb`-line commands in the README
(https://UserJoo9.github.io/QuotaManager/). That host is the `gh-pages` branch,
populated and signed by `apt-repo.yml`. Setting it up the first time:

1. **Generate a signing key** (empty passphrase) and add the armored
   **private** key as the GitHub secret `APT_REPO_GPG_KEY` (Settings → Secrets
   and variables → Actions):
   ```bash
   gpg --full-generate-key     # RSA and RSA, 4096 bits, no expiry
   # Real name: Quota Manager   Email: youssef.alkhodary@users.noreply.github.com
   # Passphrase: LEAVE EMPTY
   gpg --armor --export  youssef.alkhodary@users.noreply.github.com > quota-manager.gpg
   gpg --armor --export-secret-key youssef.alkhodary@users.noreply.github.com > quota-manager-secret.asc
   ```
   Commit `quota-manager.gpg` (the PUBLIC key) at the repo root; keep
   `quota-manager-secret.asc` **outside the repo** and paste its entire contents
   into the `APT_REPO_GPG_KEY` secret.
2. **Enable GitHub Pages** — the workflow's "Ensure GitHub Pages" step calls the
   API after the first `gh-pages` push (`pages: write`). Fallback: Settings →
   Pages → Source = *Deploy from a branch* → Branch = `gh-pages` / `(root)`
   (must happen after the branch exists).
3. **Backfill the current release** so the repo isn't empty (the release + its
   `.deb` already exist in GitHub Releases):
   ```bash
   gh workflow run apt-repo.yml --ref main -f version=0.1.2
   ```
