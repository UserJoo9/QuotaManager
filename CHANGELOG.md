# Changelog

All notable changes to **Quota Manager** are documented here, newest first.
The version is the single source of truth in `quota/version.py`; a release tag
(`v<major>.<minor>.<patch>`) must match it.

## [Unreleased]

## [0.1.4] — 2026-08-12

### Fixed

- **`.deb` installs aborted at the dnslog step** (`setup_gateway_kali.sh:
  CFG_HISTORY_LOG: unbound variable`). The script runs `set -u` but assigned
  `CFG_HISTORY_LOG` only in step 6 (config.yaml), while step 4.5 renders it
  into `/etc/dnsmasq.d/quota-dnslog.conf` — every package install died at the
  4.5 heredoc and left the package half-configured (`dpkg: error processing
  package quota-manager`). The default (`/var/log/quota-dnsmasq.log`) is now
  defined before the fragment is written and reused by config.yaml;
  `test_packaging.py` pins assignment-before-first-use so the ordering can't
  regress. Re-run the install (`apt install ./quota-manager_0.1.4_all.deb`)
  to fix an affected box — the script is idempotent.

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
