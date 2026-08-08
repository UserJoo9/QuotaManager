# Changelog

All notable changes to **Quota Manager** are documented here, newest first.
The version is the single source of truth in `quota/version.py`; a release tag
(`v<major>.<minor>.<patch>`) must match it.

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
