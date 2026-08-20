# Changelog

All notable changes to **Quota Manager** are documented here, newest first.
The version is the single source of truth in `quota/version.py`; a release tag
(`v<major>.<minor>.<patch>`) must match it.

## [Unreleased]

## [0.3.0] — 2026-08-19

### Added

- **Notification center** — a bell icon in the top-right corner of the dashboard
  shows a red badge when there are new security alerts (failed logins, WAF
  blocks, default-password warning, WAN-over-HTTP warning). Click it to open a
  dropdown list; each item includes a timestamp and a short explanation. "Clear
  all" hides the badge until the next event. The list is remembered between
  visits so you can come back later.
- **Firewall rule forms** — adding or editing a custom firewall rule now opens
  a form (rule name, action, chain, source/destination IP, protocol, source and
  destination ports) instead of six separate browser pop-ups. The same applies
  to port forwards: a form now collects the forward name, protocol, WAN and LAN
  ports, source filter, and target IP. Each existing port forward has its own
  **Edit** button.
- **One-click HTTPS** — the Firewall tab now has an **Enforce HTTPS** card.
  Click **Enable HTTPS** to generate a self-signed TLS certificate, write it
  to disk, update the config, and restart the dashboard over HTTPS — all in one
  step. A **Remove HTTPS** button (with a confirmation dialog) appears once
  HTTPS is active and lets you roll back to plain HTTP just as easily.

### Fixed

- **Firewall could not fully block external connections** — a SYN flood rate
  limit was placed before the "drop new connections from the internet" rule, so
  the two rules never reached external traffic. The drop rule is now processed
  first, then the rate limit, so the block actually works.
- **HTTPS "Enable" button crashed on first use** — the private key file was
  `chmod`-ed before `openssl` created it, causing a `FileNotFoundError`. The
  permission lock is now applied after cert generation succeeds.
- **One-click HTTPS wrote to the wrong config.yaml on production** — the
  endpoints used `resolve_config_path()` without arguments, which falls back
  to the project-root config file. On a box where the service was started with
  `--config /etc/quota-gateway/config.yaml`, the enforce/remove endpoints
  updated the wrong file and the restart loaded the unchanged config. Fixed by
  resolving the path from the running topology manager.
- **"Remove HTTPS" silently did nothing** — the rollback endpoint used
  `data.get("web") or {}`, which creates a throwaway empty dict when the key
  is absent. The `secure_cookies: false` write never reached the YAML. Fixed
  by using `setdefault("web", {})`.

## [0.2.1] — 2026-08-17

### Added

- **Software updates** — the Admin tab now checks for newer versions, shows
  what changed ("Show details"), and can install the update right from the
  dashboard (automatically or on demand).
- **New devices join disabled** — a device joining for the first time has no
  data allowance and stays offline until you give it one (Shared or Fixed GB)
  in its settings.
- **Bundle type: end-of-month** — choose whether your bundle resets on a fixed
  day or on the ISP's month-end bill; changing the reset day mid-month no
  longer loses this month's usage.
- **WiFi / LAN labels** — each device card now shows how the device connects
  to the router (WiFi or cable) and whether it is online right now.
- **MAC whitelist / blacklist** — from the Network tab, allow specific devices
  to skip the quota, or permanently block others.
- **Deleting a device is permanent** — a deleted device stays offline instead
  of quietly reappearing a few seconds later.
- **Guest limit applies to existing guests** — lowering the max guest accounts
  turns off the newest over-limit guests right away.

### Changed

- **"Stop new connections" and "Decline random MACs"** now refuse a new device
  at the network level, so it never even shows up in the list.
- **Faster and safer internals** — snappier dashboard updates, stronger
  password protection, and login rate-limiting.

### Fixed

- **"Show details" on an update notification was empty** — the popup now
  correctly lists what's new in the available version.
- **Exempt-from-quota users were still cut off** — a user marked "Exempt"
  is no longer blocked by usage at the network level.
- **Usage could be wiped when the reset day was changed mid-month** — the
  current month's usage is now kept.
- **"Cut existing random-MAC devices" could cut real products** — the sweep
  now only targets genuinely random addresses.
- **A device on the whitelist couldn't also be blacklisted** — the two lists
  now work independently.

## [0.2.0] — 2026-08-16

### Added

- **Network tab: speed shaping is now split into WAN and LAN sections**
  (`web/index.html` + `web/assets/app.js` + `quota/service.py` +
  `api/schemas.py` + `api/app.py` + `run.py` + `quota/shaping.py`): the speed
  settings are separated into **WAN — internet** (the real line down/up rates,
  `set-total-down` / `set-total-up`) and **LAN — internal transfers**
  (`set-lan-rate`, the LAN pass-through rate; 0 = the 1000 Mbps default), each
  with its own label + help text, so the LAN pass-through rate is a
  first-class, UI-editable, DB-persisted setting instead of a config-only key.
  `NetworkUpdate.lan_rate_mbps` (optional, partial POSTs leave it untouched);
  `QuotaService.set_shaping(lan_rate_mbps=…)` persists `shaping_lan_rate_mbps`;
  `run.py` feeds it into `shaper.update_state(lan_rate_mbps=…)` on every tick
  and the API's immediate re-sync, overriding the boot-time
  `shaping.lan_rate_mbps` config value — an edit rebuilds the `1:99`
  pass-through at the new rate immediately. The live Network & Bundle
  overview card gains a **LAN (pass-through)** stat (`#np-lan`). app.js
  v=50→51.
- **Speed shaping: LAN traffic is no longer throttled** (`quota/shaping.py` +
  `core/config.py` + config.yaml): the Network-tab "Speed limits & latency"
  controls now shape **internet (WAN)** traffic only — client<->uplink-subnet
  traffic (NAS, router admin, LAN transfers) rides a **prio-0 pass-through
  class `1:99`** under each HTB root at the **full LAN link rate**, so LAN
  transfers never pay the WAN cap. The root `1:1` caps at
  `shaping.lan_rate_mbps` (default 1000) — headroom only the pass-through can
  use; the default `1:2` + aggregate `1:100` + device leaves all stay capped
  at the WAN line rate, so bufferbloat control is unchanged. The uplink subnet
  resolves exactly like the nftables engine (`engine.uplink_subnet` wins, else
  derived from the dhcp block — the LAN snapshot, else the box's own NIC
  addresses). The LAN filters
  run at `prio 1`, ahead of every `prio 2` device filter; both directions are
  covered (ifb0 upload tree: `ip dst <uplink>`; egress download tree:
  `ip src <uplink>` for LAN downloads to clients AND `ip dst <uplink>` for
  re-injected LAN uploads, so they aren't re-capped by the default class).
  All priorities are deliberately non-zero: `tc` treats an explicit
  `prio 0` filter as "no priority" and auto-assigns it AFTER every real
  priority, so the pass-through would silently lose to the device caps (the
  live-box "LAN still throttled" bug — the pass-through existed but its
  filters sorted behind the `prio 1` device filters).
  `fq_codel` rides the pass-through leaf too; `netmgr.render_config` carries
  `lan_rate_mbps` through WAN/LAN applies.
- **Remember the last visited sidebar tab** (`web/assets/app.js`): `switchPanel`
  persists the active panel (`quota_active_panel`, localStorage) and init
  restores it after login — a page refresh returns to the tab you were on
  (Management / Network / WAN / Admin / History / DNS), falling back to the
  default Management page if the saved panel no longer exists. app.js v=49→50.
- **Per-user "exempt from quota"** (user modal + `POST /api/users` +
  `PATCH /api/users/{id}`): an `exempt_quota` flag on a user lifts the
  usage-vs-allowance quota gate — an exempt user is **never** quota-blocked,
  however much they use (the dashboard + report resolve through the new
  `QuotaService.user_quota_blocked()`, the single choke point shared by
  `_user_quota_map`). Manual admin cuts (user/device level) still apply; the
  per-device `bypass` is redundant for an exempt user's devices, so the device
  modal disables it (`#d-bypass-exempt-note`). The user card shows a
  "unlimited" badge; the device-modal + DNS-picker user dropdowns tag exempt
  users "— unlimited". New `exempt_quota` column (idempotent ALTER migration),
  `User.exempt_quota`, wire key in `_dashboard_payload`/`_report_payload`;
  `UserCreate`/`UserUpdate.exempt_quota`. app.js v=46→47, styles.css v=47→48.
- **Decline random MACs** (Network tab → Connection & security): a toggle that
  refuses brand-new devices whose MAC is **randomized** (locally-administered
  bit — `0x02` in the first byte, the exact bit OSes set for privacy MACs, so
  the real vendor OUI is gone and the device is anonymous/unidentifiable). A
  first-seen randomized MAC is still registered (visible + counted) but
  **immediately admin-blocked** with a `warning` event — the same gate pattern
  as STOP NEW CONNECTIONS. A **"Also cut random-MAC devices already joined"**
  checkbox runs a one-shot sweep that admin-blocks every existing randomized
  device (real-OUI devices are never touched); blocked ones stay cut until an
  admin acts. `QuotaService.is_random_mac()` (pure helper) +
  `decline_random_macs()`/`set_decline_random_macs(enabled, also_existing)`
  (setting keys `decline_random_macs` / `decline_random_macs_existing`, the
  one-shot flag always resets); `run.py._persist_lease` new gate branch (after
  stop-new, before the guest branch); `NetworkUpdate.decline_random_macs` +
  `.decline_random_macs_existing`; `GET/POST /api/network` carry the switch.
  UI: `#decline-random-toggle` + `#decline-random-existing` + `#decline-random-msg`.
- **Privacy eye** (sidebar quick-action): an eye toggle (localStorage pref,
  default on) that masks on-screen sensitive details — MAC addresses in the
  device rows, rogue rows and the device modal (`aa:bb:cc:••:••:••` via
  `macText()`) and stops the WAN tab from prefilling the saved PPPoE
  credentials (**username and password** — the username prefill is gated on
  `privacyHide` exactly like the password, so a masked panel shows neither).
  The masking is **two-way**: while the eye is on, `refreshWan` actively
  CLEARS both credential fields (not merely skips the prefill), so a value
  revealed and then re-hidden vanishes immediately instead of lingering in the
  DOM until a refresh. Only display is masked; the edit fields keep their real
  values, and the eye re-renders in place (device/rogue lists + the open
  device modal + a WAN refetch). `#privacy-eye`,
  `macText()`/`togglePrivacy()`/`setPrivacyButton()`. app.js v=47→49.
- **Default guest speed limit** (Network & Quota tab, `POST /api/guest`): a
  **Guest speed limit (Mbps)** field caps the **aggregate** bandwidth of every
  guest account (their whole allowance set, not per-device) — the tc shaper
  applies it as each guest user's ceiling, `min`'d with any explicit user cap,
  `0` = unlimited. It applies to guests already registered (like the guest
  quota) and rides the existing `_sync_shaping` pipeline, so it needs no new
  kernel mechanics. `QuotaService.guest_speed_limit_mbps()` /
  `set_guest_speed_limit()` (setting key `guest_speed_limit_mbps`, clamps ≥ 0);
  `GuestUpdate.speed_limit_mbps`; the value is returned by `GET/POST
  /api/guest`; app.js v=44→45.
- **WAN public-IP renewal — restart the PPPoE dial from the dashboard** (WAN
  tab, `quota/topology.py` + `run.py` + `api`): the box dials the line itself,
  so a **Restart PPPoE — renew public IP** button (`POST /api/wan/renew`)
  restarts the `quota-wan-ppp` systemd unit — the ISP hands the new session a
  fresh public IP (the same effect as restarting the router, without touching
  the box). Internet drops for a few seconds while ppp0 re-dials. An
  **auto-renew schedule** (`POST /api/wan/renew-config`, `enabled` +
  `minutes`) re-dials periodically — interval clamped to a **5-minute floor**
  (no upper bound; default 15), so a typo can't hammer the line. The schedule
  only runs in WAN mode **with ppp0 actually UP** (a down dial means the
  internet isn't working — renewing into a dead line is pointless), and the
  button + schedule are disabled while ppp0 is down (WAN-tab note explains
  why). The last-renewal timestamp (`wan_ip_renew_last`) is persisted, so a
  gateway restart mid-schedule never re-renews immediately (the `dnslog_state`
  resume pattern). `quota/topology.py` gains `restart_pppoe()` (best-effort,
  never raises); settings keys live on `QuotaService`
  (`wan_ip_renew_enabled` / `wan_ip_renew_minutes` / `wan_ip_renew_last`);
  the renew config + last-renewed time ride `_wan_status()` → the WS snapshot
  + `GET /api/wan` (no extra query for the WAN tab). Renewals log a `warn`
  event. UI: Restart button + auto-renew block (`#wan-restart-btn`,
  `#wan-renew-toggle`/`#wan-renew-minutes`/`#wan-renew-save`,
  `#wan-renew-last`, `#wan-renew-disabled-note`), app.js v=43→44.
- **Sidebar collapse toggle removed** (v24 cleanup): the fixed sidebar is now
  always full-width — the ☰ toggle, `#app.sidebar-collapsed` rules and the
  `sidebar-toggle` JS binding are gone (styles.css v=44→45).
- **Guest limit + "STOP NEW CONNECTIONS" gates** (Bundle settings tab,
  `POST /api/guest`): a max guest-account cap (default **2**, `guest_limit`)
  stops a MAC-changing device from minting a fresh guest allowance forever —
  once the cap is reached, a brand-new device is still registered as a guest
  (visible + counted) but **immediately admin-blocked** (kernel drop) with a
  `warning` event. The **STOP NEW CONNECTIONS** toggle (`stop_new_connections`)
  refuses every brand-new device: a first-seen MAC is registered and cut on
  sight, while already-registered devices keep joining normally. Both gates
  take effect on the next dnsmasq lease sync (~15 s) and are enforced through
  the existing per-device admin-block path (`BLOCK_ADMIN`), so a refused device
  shows as "Blocked" in the dashboard until an admin unblocks or deletes it.
  New `count_guest_users()` in `quota/db.py`; service getters/setters; app.js
  v=39→40.

### Changed

- **Network & Quota tab renamed to "Network"** (v27) — the sidebar + nav label
  reads just **Network** now (the panel still holds the bundle config, guest
  controls and connection toggles). test_web_ui pins updated.
- **Admin page polish (v27)** — the **System Info & About** card drops the
  "Application" row (keeps the Version row + description), and the **System
  Logs** console now actually scrolls: `.admin-layout` uses a fixed
  `height: calc(100vh - 98px)` (+ a 480 px floor) and `.admin-logs-card .logs`
  is `flex: 1 1 0; min-height: 0; overflow-y: auto` — the old
  `min-height: calc(100vh - 98px)` on the wrapper was the reason the viewer
  never grew beyond one screen.
- **Bundle ring interior is opaque (v27)** — `.ring::before` uses `var(--bg)`
  instead of a translucent `rgba(7,8,10,0.55)`, so the conic gauge reads clean
  over the glass panels instead of showing the content behind it.
- **DNS pickers show the owning user (v27)** — the DNS-tab rule target + import
  device dropdowns label each device with its user
  (`Name (User)`); the user scope lists stay name-only.
- **Logs merged into the Admin page (v26)** — the dedicated **Logs** sidebar
  tab is gone; the sidebar is now Management / Network & Quota / WAN / Admin /
  History / DNS. The Admin page is restructured into a 2-column top grid —
  **Security & Credentials** (change-password) + **System Info & About**
  (app, version) — with the **full System Logs console embedded below**,
  full-width: the level filter (ALL/INFO/WARNING/ERROR), search, **Refresh**
  and **Export** toolbar are unchanged, and the viewer is a scrollable
  monospace terminal that flex-fills the remaining viewport height
  (`min-height: calc(100vh - 98px)`), keeping the level accents (INFO
  cobalt, WARNING amber, ERROR crimson). Entering the Admin tab loads the
  logs; the WS refresh hook now checks `panel-admin` visibility. All element
  ids survive (the JS contract is untouched). Cache-busts: styles.css
  v=46→47, app.js v=45→46.
- **Bundle settings merged into the Network tab (v25)** — the separate
  "Bundle settings" sidebar link is gone. One **Network & Quota** page now
  holds the bundle configuration (total/reset-day + recharge + reset-month),
  the guest-mode controls (mode toggle, quota, **speed limit**, max-accounts
  + STOP NEW CONNECTIONS) and the Connection & security toggles (shaping with
  its sub-fields + AQM, VPN share) in a 2-column layout (≈65% config /
  ≈35% single live **Network & Bundle overview** card: bundle gauge + progress
  bar + shaping/VPN/devices preview). All element ids survive (the JS contract
  is untouched). Cache-busts: styles.css v=45→46, app.js v=44→45.
- **Dashboard redesigned — obsidian glass, "$1M enterprise" theme (v23)** —
  the matte charcoal look is gone. Midnight-obsidian base (`#07080A` →
  `#0D0E12`) with ambient cobalt radial glows plus an **ultra-subtle drifting
  particle canvas** (`#bg-particles`, ~11 dust nodes at 1–2.5 px radii and
  `0.15–0.25` opacity so they float behind every card without cluttering
  text/gauges; DPR-aware, pauses on hidden tab, disabled for
  `prefers-reduced-motion`). Electric Cobalt Blue (`#3B82F6` / `#2563EB`)
  replaces every green: status dots, primary buttons (gradient + soft glow),
  rings, toggles, focus rings, active nav. Panels are **heavy frosted
  obsidian glass** (`rgba(13,16,23,0.65)` + `backdrop-filter: blur(20px)`,
  14 px radii, crisp 1 px `rgba(255,255,255,0.08)` borders) across the
  sidebar, cards, modals and the status pill; the bundle ring is a gradient
  conic gauge and data bars use a cobalt `linear-gradient`, both with a soft
  blue ambient glow (`0 0 15px rgba(59,130,246,0.25)`). **Sidebar**: brand
  icon-badge + refined logo hierarchy, vertical SVG nav with a subtle hover
  glide, and a clean footer = status pill + quick-action row (**admin profile
  avatar/user section removed**; `#logout-btn` is now an icon button). Main
  canvas `max-width: 1600px`. Typography: SF Pro / Inter stack with tight
  tracking for headings, monospace (`ui-monospace`) for IPs/MACs/metrics
  (ring, stat values, device usage, live split); transitions on the
  `cubic-bezier(0.16,1,0.3,1)` spring. Cache-busts: styles.css v=41→43,
  app.js v=40→42 (index + milestone/report).
- **Dashboard redesigned — developer-grade dark matte theme (v22)** — the
  purple "AI-gradient" glassmorphism is gone. Pitch-black sidebar + deep
  charcoal (`#0B0C0E`) content on solid card surfaces (`#121418`/`#18181B`)
  with fine `#27272A` borders; flat fills (zero gradient/glow/blur) and solid
  muted status colors (emerald `#10B981` online/active, muted crimson
  `#DC2626` blocked/errors, slate idle, blue/teal upload/download). The top
  bar is now a **fixed collapsible left sidebar** (☰ toggle → 64 px icon rail)
  with vertical nav + inline SVG icons, an internet-status pill and Admin
  profile in the footer; the **Users & Devices list is a 2-column masonry**
  (CSS `columns`, `break-inside: avoid`) so an expanded accordion card
  lengthens its column instead of leaving a grid hole. 4–6 px radii,
  `:focus-visible` emerald rings, solid conic bundle-ring arc, solid bar
  fills, flat buttons. Cache-busts: styles.css v=40→41, app.js v=38→39.
- **VPN share prefers the real kernel tunnel; tun2socks is only the fallback** —
  `_sync_vpn_share` previously reconciled the tun2socks bridge FIRST and made
  it authoritative, so even with a real kernel TUN present (xray/sing-box/
  WireGuard) the bridge spawned a redundant tun0 and hijacked the route. Now
  the routing manager is reconciled FIRST — any kernel tunnel wins with no
  config edits and no bridge download. The bridge engages only when the
  routing finds no tunnel (userspace v2rayN), stays running only while its OWN
  device carries the subnet, and is stopped when a different real tunnel
  routes the traffic. `vpn_share.interface`/`tun2socks` overrides are no
  longer needed for kernel-TUN clients (defaults `""`/`true` are correct for
  both).
- **Tunnel auto-detect ranks named tunnel devices by link type, not just
  name** (`quota/vpnshare.py`): the rank regex now matches the `tun`/`utun`/
  `wg`/`vpn` substring anywhere in the name, so kernel-TUN clients that name
  their device differently (xray's `xray_tun`, sing-box) rank alongside the
  classic `tun0`/`wg0` — and a junk ARPHRD_NONE leftover can't out-rank a real
  tunnel. (Also fixes a latent inversion where the generic group sorted
  before the named one.)

### Fixed

- **Client→box LAN traffic was throttled by the WAN upload cap — the RustDisk
  case** (`quota/shaping.py`): the client-subnet ingress redirect sends every
  client packet into `ifb0` (including traffic destined for the box itself),
  but the LAN pass-through only matched `ip dst <uplink subnet>` — so a file
  transfer to the gateway's OWN IP (`192.168.2.1`, e.g. RustDisk to the box)
  matched nothing and fell to the default class, capped at `total_up` ("WAN
  upload 2 → very slow; 0 → very fast"). The box's own addresses are now
  always added to the pass-through (`_find_own_addresses` parses the kernel's
  `ip -o -4 addr show dev <iface>`): `ip dst <own>` on the upload tree
  (client→box) and `ip src <own>` on the egress tree (box→client responses).
  The pass-through class now programs whenever there is an uplink subnet **or**
  the box has own addresses, so a NIC with only the client alias still passes
  client→box traffic. `_state_signature` includes own addresses.
- **LAN pass-through filters silently sorted BEHIND the device caps — the
  prio-0 fix never landed** (`quota/shaping.py` + `tests/test_shaping.py`):
  the live box proved `tc filter add … prio 0` is treated as "no priority"
  and auto-assigned to `pref 49151/49152` — AFTER every real priority — so the
  `1:99` pass-through filters lost to the `prio 1` device filters and LAN
  traffic was still throttled by the WAN caps even though the code looked
  correct (the arbiter: a manual `tc filter add … prio 0` on a dummy
  interface renders as `pref 49152`). All filter priorities are now
  non-zero: the LAN pass-through runs at `prio 1` (ahead) and device filters
  at `prio 2`, so `tc` honors the ordering and a LAN-cross packet always
  reaches `1:99`. `test_shaping.py` pins the new prios.
- **A 0 (unlimited) WAN upstream/downstream silently disabled ALL speed shaping**
  (`quota/shaping.py`): `update_state` tore the whole tc tree down unless
  **both** totals were > 0, so "0 = unlimited" for one direction killed the
  caps on the other too (the live-box report: WAN up = 0 → LAN transfers
  uncapped, but also the WAN download caps stopped applying). Each direction is
  now built independently — the down tree only when `total_down > 0`, the
  ingress redirect + upload tree only when `total_up > 0` — so a 0 means
  *that direction* is unlimited and the other keeps its caps + its LAN
  pass-through.
- **LAN pass-through rate silently degraded to the WAN cap when
  `shaping.lan_rate_mbps` was unset/0** (`quota/shaping.py` + setup script): on
  a box whose config.yaml lacks the `lan_rate_mbps` key (setup-generated
  configs did not write it) and whose `core/config.py` predates the field, the
  pass-through class `1:99` WAS programmed but at **the WAN total** — so LAN
  transfers inherited the Network-tab caps (the live-box report: "upload set to
  2.5, LAN up&down throttled too"). `_tree_cmds` no longer falls back to the
  line total: it uses the documented **1000 Mbps default** (with a `warning`
  log naming the key), so the pass-through is always wider than the WAN line.
  The setup script now writes `shaping.lan_rate_mbps: 1000` into the generated
  config.yaml so fresh installs are self-describing.
- **LAN pass-through silently did not program on a box with an unresolvable
  uplink subnet** (`quota/nftables.py` + `quota/shaping.py`): the Network-tab
  speed limits still throttled LAN traffic because the `1:99` class was never
  created — `resolve_local_networks` only fell back to the LAN snapshot
  (`uplink_ip` + `lan_cidr`) in WAN mode, so **LAN mode with an empty
  `router_ip` (a live box report: `router_ip: ''` + `uplink_subnet: ''`)
  derived nothing**, and a stale `core/config.py` that drops the snapshot keys
  made it worse. The LAN branch now falls back to the snapshot exactly like the
  WAN branch, and the shaper gains a **last-resort derivation from the box's
  own NIC addresses** (`ip -o -4 addr show dev <iface>` → the directly-
  connected subnet that is not the client subnet) — so the pass-through
  programs even when the config carries no uplink info at all. The shaper's
  `start()` log now states the resolved uplink subnet + pass-through state
  (`tc shaper ready: … uplink 192.168.1.0/24 (LAN pass-through on)`) for
  on-box diagnosis.
- **VPN-share whitelist survived only while the tunnel was UP — a blip killed
  the relay for good** (`run.py` `_sync_vpn_share`): the `gw_allowed`
  whitelist (the box's ONLY egress under a Gateway OFF cut) was cleared
  whenever `status.state != "on"` — i.e. it tracked the *tunnel*, not the
  *switch*. A momentary tunnel drop (VPN client reconnecting) then removed the
  box's route to the VPN server, so it could never re-dial and the household
  tunnel died permanently. The whitelist is now gated on the SWITCH: it stays
  learned/sticky while `vpn_share_enabled` is on (re-dial always possible) and
  only the switch OFF clears it.
- **VPN share refused to route into a tunnel that exists but has no IPv4**
  (`quota/vpnshare.py`): a stale pin to a junk ARPHRD_NONE device that merely
  *exists* in sysfs (the live-box "evice" — it routes nothing) was honored and
  the whole client subnet was blackholed. `reconcile()` now drops a pin whose
  device is gone OR carries no IPv4 (re-detecting instead), and `apply()`
  itself refuses to route into an address-less device (waiting up to 2 s for a
  freshly spawned tun2socks to gain its address) — the subnet can never be
  routed into a device that isn't actually a live tunnel.

- **VPN-share switch reverted to OFF on refresh** — flipping the Network-tab
  switch and refreshing the page silently undid it, because the toggle had no
  change listener: nothing persisted until the panel's separate **Save**
  button was clicked. The switch now saves **immediately on change** (partial
  POST with only `vpn_share`, so it never clobbers the shaping totals — the
  same auto-save pattern as the guest-mode toggle).
- **VPN-share status stuck on "Waiting for the gateway to apply…"** — the live
  status only re-rendered when the Network tab re-fetched `/api/network`, so
  after the toggle applied it never advanced on its own. The WS snapshot (5 s)
  now carries the `vpn_share` switch + kernel state (`_dashboard_payload` →
  `_vpn_share_payload`), and `render()` calls `renderVpnShare` so the status
  moves to "Sharing through…" without a manual refresh.
- **"archive has no tun2socks binary" — the automatic bridge failed to install**
  — tun2socks v2.7.0 ships its goreleaser zip with the binary named
  `tun2socks-<os>-<arch>` (e.g. `tun2socks-linux-amd64`), but the extractor
  looked for a bare `tun2socks` member and bailed. It now accepts any
  `tun2socks*` member. (v2rayN's "TUN mode" is a userspace netstack — it never
  creates a kernel `tun0`, so the detector correctly routes to this automatic
  bridge; the download just needed to land.)
- **tun2socks "just exited" immediately after the download** — the spawned
  bridge kept dying because the argv passed **`-tun-ip`/`-tun-gw`**, flags
  tun2socks **v2 removed** (v2.7.0's CLI is `-device`/`-proxy`/`-interface`/
  `-tun-pre-up`/`-tun-post-up`/…). An undefined flag makes Go's `flag` package
  print an error and `os.Exit(2)`, so the process died on every spawn. The argv
  now passes only `-device`/`-proxy`; the address is applied separately by
  `_configure_interface` (`ip addr add <tun_ip>/24 dev <iface>` + `ip link set
  up`), since v2 creates the TUN and brings the link UP itself but assigns no
  address.
- **"Device for nexthop is not up" when routing into the bridge** — the routing
  manager refused to add `default dev tun0` because the link wasn't up at that
  instant (tun2socks hadn't created it yet, or the single best-effort
  `ip link set up` had already failed). Two layers now fix it: `_configure_interface`
  retries `ip link set dev <iface> up` until the device actually exists and is
  up (5 s window) before stamping the address, and the **routing manager itself
  self-heals** (`quota/vpnshare.py`): `apply()` ensures the tunnel link is UP
  right before programming the default route and retries the add (with the
  `scope link` fallback) across 3 attempts, so a settling tunnel can't abort
  VPN share. On final failure the message now carries the interface's REAL
  state from `ip -o link show` (missing / down / unaddressed) instead of the
  kernel's bare "not up". (+4 tests: link-up-before-route order, non-fatal
  bring-up failure, retry-after-settle, real-state error message.)
- **VPN share blackholed every device when a stale/junk TUN device existed**
  (live-box report: "Sharing through evice — but the internet stopped in all
  devices"): the auto-detector can find a leftover hand-created TUN (type
  ARPHRD_NONE) that nothing reads, route the whole client subnet into it, and
  silently drop every packet. The routing is now **pinned to the tun2socks
  bridge's device while the bridge runs** — a stale pin or junk tunnel can
  never divert the subnet again (`run.py _sync_vpn_share`; the first reconcile
  already passes the bridge interface, and the persist comparison uses the
  original DB pin). Wiring test updated to assert the authoritative-bridge
  pinning.
- **tun2socks bridged into a DEAD SOCKS endpoint (silent blackhole, live-box
  report)**: `ss -tlnp` matched no VPN listener (v2rayN had no SOCKS inbound),
  so the fallback `127.0.0.1:10808` was used WITHOUT verifying anything listens
  there — tun2socks spawned, reported "running", and dropped every device
  packet. `Tun2socksManager.reconcile` now PROBES the fallback endpoint
  (`socket.create_connection`, 1 s — the proxy accepts TCP before any SOCKS
  handshake) and reports an honest `no-proxy` status ("no VPN SOCKS proxy
  listening on 127.0.0.1:10808 — enable the SOCKS inbound in the VPN client")
  instead of spawning a blackhole. A proxy found live via `ss` skips the probe.
  Injectable `proxy_probe` keeps tests off the network (+4 tests: dead-fallback
  never spawns, live-fallback still spawns, detected-proxy skips probe, honest
  message).

### Added

- **tun2socks auto-provisioner** — VPN share now works with VPN clients that
  use a userspace netstack (v2rayN): no kernel TUN ever appears, so there was
  nothing to route into. When the routing manager finds no tunnel, the new
  `quota/tun2socks.py` **downloads the pinned, sha256-verified tun2socks
  binary itself** (one-time, from the v2.7.0 GitHub release, per-arch
  checksums enforced — an unverified or unknown-arch binary is never
  installed), auto-detects the VPN client's local SOCKS listener (`ss -tlnp`,
  v2ray/sing-box/xray; `vpn_share.socks_proxy` fallback, default
  `127.0.0.1:10808`), spawns the bridge (`tun0`, `-tun-ip 10.0.0.1`) and
  re-runs the routing — all automatic, nothing to install by hand. The child
  is stopped when VPN share turns off; a missing proxy / failed download /
  crashed child each surface an honest Network-tab message instead of
  blackholing the subnet. Config: `vpn_share.tun2socks: false` disables it
  (you run your own kernel-TUN client — a second tun would confuse the tunnel
  detector).

- **Gateway OFF + VPN share coexist** — you can now cut the box's OWN internet
  (Gateway OFF in Management) while "VPN share" keeps the household online: the
  box keeps ONLY its connection(s) to the VPN server reachable, so the tunnel
  the relay rides survives the cut. The whitelist (`gw_allowed` set in
  `quota/nftables.py`) is **auto-learned** every ~15 s from the VPN client's
  established sockets (`ss` match: v2ray/sing-box/xray/tun2socks), kept sticky
  so the client can re-dial, with an `engine.gateway_allow_ips` override for
  clients the auto-learn can't identify; loopback stays exempt so the dashboard
  and the tun2socks↔VPN-client hop keep working. Replaces the old unconditional
  relay suspension (`set_vpn_relay`) — the box is genuinely cut now, not
  silently unblocked, and the Network-tab VPN panel says so in plain language.

## [0.1.3] — 2026-08-12

### Added

- **VPN share** — the whole client subnet through the VPN the box runs. Run a
  VPN client on the gateway laptop in TUN mode (sing-box / xray / WireGuard /
  tun2socks) and flip the **VPN share** switch in the Network tab: every
  device's internet exits at the VPN provider's IP while per-device quota
  counting/blocking (nftables forward chain) and speed shaping (tc) keep
  working. One `ip rule` diverts the client subnet into a dedicated route table
  whose default points at the tunnel; direct LAN routes (client + uplink
  subnets) stay local. The tunnel is auto-detected and **pinned** in the DB
  (`vpn_share_interface`) so a multi-VPN / rebooted box re-applies the same
  interface; the idempotent reconcile self-heals any leftover rule on the next
  15 s tick. The box's OWN gateway metering is auto-suspended while relaying
  (`nftables.set_vpn_relay`) — the relay volume would otherwise be counted a
  second time against the protected Gateway user (and a quota-cut Gateway would
  kill the household's VPN). Config: `vpn_share:` block; `vpn_share.enabled:
  false` = manager never built.
- **Domain filtering** (dashboard **DNS** tab) — host-based filtering at the
  box's DNS: **block / allow / redirect** any domain for a **user, device, or
  globally** (wildcards supported, e.g. `*.youtube.com`), turn on **blocklist
  presets** (ads-tracking, social-media, streaming, gambling — hosts or
  AdBlock-Plus source lists), and set a **per-user / per-device upstream DNS
  server** (e.g. a family-friendly resolver). Rules render into dnsmasq's
  `conf-dir` (`quota-tags.conf` per-MAC DHCP tags + `quota-domains.conf`
  tag-restricted `address=`/`server=` lines), so no new service runs and an
  unchanged render never touches dnsmasq. The History tab's per-domain rows
  carry a live blocked/allowed/redirected badge with one-click
  Block-everyone / Block-this-device / Allow buttons (`/api/dns/rules/quick`).
  Config: `dns_filter:` block (`enabled: true` by default; `false` = entirely
  inert).
- **Signed apt repository** so Linux boxes install/upgrade Quota Manager the
  native way (`apt-get update && apt-get install quota-manager`). `.github/
  workflows/apt-repo.yml` fires after every successful `release` run, downloads
  the `.deb` from the GitHub Release, and publishes it to a GPG-signed apt repo
  on the `gh-pages` branch (hosted at
  https://UserJoo9.github.io/QuotaManager/). A one-time `deb [signed-by=…] …`
  source line makes installs and future upgrades come straight from apt; old
  versions stay installable. The signing public key is committed at
  `quota-manager.gpg` (private key lives in the `APT_REPO_GPG_KEY` Actions
secret); a `workflow_dispatch` `version` input backfills already-released
   versions. See README → *Install the package*.

### Fixed

- **`.deb` installs aborted at the dnslog step** (`setup_gateway_kali.sh:
  CFG_HISTORY_LOG: unbound variable`). The script runs `set -u` but assigned
  `CFG_HISTORY_LOG` only in step 6 (config.yaml), while step 4.5 renders it
  into `/etc/dnsmasq.d/quota-dnslog.conf` — every package install died at the
  4.5 heredoc and left the package half-configured (`dpkg: error processing
  package quota-manager`). The default (`/var/log/quota-dnsmasq.log`) is now
  defined before the fragment is written and reused by config.yaml;
  `test_packaging.py` pins assignment-before-first-use so the ordering can't
  regress. Re-run the install (`apt install ./quota-manager_0.1.3_all.deb`)
  to fix an affected box — the script is idempotent.

## [0.1.2] — 2026-08-11

### Added

- **Per-device browsing history** (dashboard **History** tab). Pick a device
  and a look-back window (24 h / 3 d / 7 d / 14 d) to see its **top domains**
  with share %, an **hourly activity** list, and the **most recent queries**
  (minute buckets). Capture rides the box's own dnsmasq (`log-queries=extra` —
  every query line carries its requestor IP), so bandwidth is not re-tracked:
  the tab reuses the existing per-device live/period bytes from the dashboard
  payload.
- **`GET /api/history/{device_id}`** (auth-gated; `window` hours clamped
  1–336, `limit` capped) returns `top_domains`, `activity`, `recent`,
  `total_queries`.
- **Per-user retention** — `users.history_days` (NULL = the global default).
  Set it in a user's edit modal ("History retention (days, blank = default)").
- **Storage bounds, no DNS slowdown**: the setup script writes an app-owned
  dnsmasq fragment (`/etc/dnsmasq.d/quota-dnslog.conf`) + a logrotate snippet
  (copytruncate, 5 MB, rotate 3) so the raw log stays ≤ ~20 MB; a dedicated
  tailer thread (`quota/dnslog.py`) buckets queries into a `dns_history` table
  (per device × minute × domain) and an hourly gate prunes each user's rows at
  *their* retention. Overflow drops query lines, never blocks DNS or the loop.
- **Household "All devices" history overview** — the History tab opens on an
  **All devices** default: combined recent activity across every device in
  chronological order, each query badged with its owning device/user
  (`[Yahya]`, `[Youssef]`, `[Mom]`), plus a unified top-domains + total-query
  summary for the household (bandwidth summed over devices). Picking a specific
  device filters to that device only, byte-for-byte unchanged. `GET
  /api/history/all` (alias `/api/history/0`) returns the aggregate — same wire
  shape as a device, with `recent[].device_id` stamped for the badges; per-device
  responses stay identical.

### Changed

- `setup_gateway_kali.sh` installs the dnslog fragment + logrotate and writes
  the `history:` block (`enabled: true`, `dnsmasq_log_file:
  /var/log/quota-dnsmasq.log`, `retention_days: 7`) into the generated
  config.yaml. `history.enabled: false` stops recording entirely (DNS/DHCP
  untouched).

### Fixed

- **History stayed empty even though dnsmasq was logging.** Real
  `log-queries=extra` lines stamp the client ip/port after the serial
  (`1 192.168.2.186/16773 query[A] ...`), but the parser regex expected
  `query[` directly after the serial — so every real line was silently
  dropped (`parse_dnslog_line` → `None`). The regex now accepts the optional
  ip/port chunk; bare and serial-only shapes are unchanged, and
  `forwarded`/`reply` lines with the same prefix are still skipped.
- `setup_gateway_kali.sh` now enables `conf-dir=` in `/etc/dnsmasq.conf` when
  it is commented out or missing — otherwise dnsmasq silently ignores every
  `/etc/dnsmasq.d/` fragment (DHCP pool, DNS, the query-log fragment).

### Changed

- **Dashboard theme — vivid purple "obsidian glass"** (`web/assets/styles.css`,
  CSS-only; zero JS/HTML-structure changes): background shifted to a deep
  purple-tinted obsidian gradient (`#08070d → #0f0b18`), cards are dark
  translucent frosted glass (`rgba(20,15,30,0.6)` + 16 px blur + a 1 px glossy
  edge), and all accents moved to the vivid purple family (`#8b5cf6` /
  `#7c3aed`) — primary buttons, selected tab (now with a neon glow), badges and
  progress. **Users & Devices cards are now stacked full-width in a single
  column** (`.device-grid` → `1fr`, media-query overrides removed) so names,
  IP/MAC badges, bars, toggles and actions get horizontal room. All pages
  bumped to `?v=35` (index/milestone/report + test pins); `.ms-pill.done`
  border retuned to match.
- Dashboard theme retuned, CSS-only (`web/assets/styles.css`): pitch-black
  base (`#000000`), all purple accents desaturated to a calm cool periwinkle
  (`#8FA0C9`), and a much stronger glassmorphism (32 px blur, translucent
  frosty-white fills, 1 px frosted-white edge on cards *and* buttons, subtle
  periwinkle light flare behind the cards). Status dots, block/limit colors
  and all data remain exactly as before. The **milestone** and **report**
  pages (own inline styles + `?v=32` links) are bumped to the same `?v=34`
  cache-bust so the new theme reaches every page, and their one remaining
  purple literal (`.ms-pill.done` border) is retuned to match.

## [0.1.1] — 2026-08-08

### Added

- **Household milestone page** (`/milestone`, public, no login). A device on the
  quota network sees *its own user's* consumption: used / allowance, a progress
  bar, a **per-device breakdown** (each device's own GB with ↑/↓ split), and
  one-time milestone pills at 50% / 75% / 100% — crossing a milestone is flagged
  "new" once per period and acknowledged with a single click.
- **Internal consumption report** (`/report` + `/api/report`). A read-only,
  admin-free view gated by **source IP** (any managed client on the DHCP subnet,
  plus an explicit `report.allowed_ips` allow-list; everything else gets a 403).
  It shows exact bundle bytes, per-user and per-device exact bytes, recent
  events and the gateway log tail. Nothing on the box ever opens it
  automatically.
- **Gateway's own traffic is counted and chargeable** (`engine.count_gateway`).
  The box's own internet use is metered and charged to a protected **Gateway**
  user (fixed 1.0 GB, sentinel `GATEWAY_MAC` device, seeded idempotently). Set
  `count_gateway: false` to skip the counters while keeping the drop rules.
- **Phone-compatible web UI** across the dashboard, milestone page and report:
  the top-bar tabs become a horizontally swipeable strip, cards stack to one
  column, the bundle ring shrinks, modals/overlays scroll instead of clipping,
  and touch targets are ≥ 36 px.

### Changed

- Guest (auto-registered) devices are no longer deleted when their lease
  expires — a reconnecting device keeps its name and history
  (`suppressed_macs`).
- Editing a speed cap re-syncs the shaper **immediately** — no page refresh.
- The WAN internet indicator uses a raw-DNS probe as a fallback so it stays
  honest while the box's own egress is kernel-dropped.

### Fixed

- **tc burst/cburst rate overshoot** — a "2 Mbps cap" previously measured
  ~3 Mbps; the burst now matches the configured rate (~50 ms bucket).
- **WAN internet dot contradicted the wan-down banner** in the half-applied
  state — the probe is now gated on the `ppp0` link in WAN mode.
- **PPPoE concurrent-session test verdict** — a second test dial while one is
  already live is now reported correctly.
- **Report page showed `—` for `ppp0`** even when the link was up (the reader
  treated the string state as an object).
- Report access honoured `report.enabled: false` everywhere (page + API).

### Notes

- **Behavioral change on upgrade:** the protected Gateway user's 1.0 GB is
  silently deducted from every auto-share bundle the first time the period is
  opened. Fixed-mode allowances are unaffected.

## [0.1.0] — 2026-08-06

Initial Linux release: the one-armed gateway (separate client subnet +
masquerade, no proxy-ARP), nftables accounting + hard block, per-user quota
model with per-device bypass, speed shaping (HTB + fq_codel), rogue static-IP
detection + ARP gateway-lock, Strong (WAN) mode with a live dashboard switch,
and the `.deb` release pipeline.
