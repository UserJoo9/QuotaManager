# Changelog

All notable changes to **Quota Manager**, newest first — written in plain
language: what changed and how it affects you.

_(For developers: versions live in `quota/version.py`; a release tag must
match it. Release notes are composed from a version's section below.)_

## [0.3.3] - 2026-09-04

### Fixed
- **DNS Adult Content / Porn Blocker Fix**: Fixed an issue where enabling the Adult Content (Porn) blocker would fail or revert back to disabled after refresh due to failing GitHub blocklist downloads, massive table DOM blow-up, and dnsmasq reload timeouts.
- **Two-Layer Adult Content Protection**: Switched the Porn blocker preset to leverage Cloudflare Family DNS (`1.1.1.3` / `1.0.0.3`) for real-time dynamic adult filtering and mandatory SafeSearch enforcement across all search engines, backed by a fast, offline-curated local blocklist (`PORN_DOMAINS`) for instant zero-latency local blackholing.
- **Rule List Performance**: Fixed `/api/dns/rules` to exclude internal preset-generated rules by default, preventing the browser table from attempting to render tens of thousands of rows and crashing the frontend.
- **UI State & Error Handling**: Presets now disable the switch input while applying to prevent race conditions and report errors directly under the presets card.
- **Browsing History & Table Layout Fix**: Fixed broken layout and scrambled text in the History tab (`Top domains`) and DNS rules table caused by an overly aggressive global `innerHTML` sanitizer stripping `<tr>` and `<td>` elements and button `onclick` handlers.

## [0.3.2] - 2026-08-31

### Added
- **UI Layout Picker**: Added a dropdown to the Users & devices panel allowing you to choose between Masonry, Grid, and List layouts. This fixes the annoying jumping behavior when expanding device cards.
- **Adult Content Blocklist**: Added a new DNS filtering preset (Porn-only) using the StevenBlack list to completely block adult content at the router level.
- **QR Code for 2FA**: The TOTP/2FA setup now renders an actual QR code instead of a raw URI text, making enrollment much easier.
- **Mac Vendor Pre-computation**: MAC address vendor lookups are now pre-computed during the airmon-ng scan phase, greatly reducing CPU overhead.

### Changed
- **UI Streamlining**: Stripped out all massive paragraph-length descriptions in the dashboard, replacing them with clean, single-sentence summaries.
- **Branding**: Replaced the generic sidebar globe icon with the official QuotaManager logo with soft rounded corners.
- **Removed Animations**: Removed the bouncy translateY hover animations on dashboard cards for a more stable and professional feel.
- **DB Optimization**: Batched daily usage DB updates to minimize SQLite transaction overhead.

### Fixed
- **2FA Stuck State**: Fixed a bug where closing the 2FA setup popup before verifying the first PIN would permanently hide the QR code and lock the setup in a pending state.
- **Pytest 3.12 Compatibility**: Fixed \RuntimeError: Event loop is closed\ errors happening in the CI test suite when running with Python 3.12.

## [0.3.1] — 2026-08-23

### Added

- **Keep specific devices out of the shared VPN.** While "VPN share" is on,
  you can now mark any device — or every device belonging to one person — to
  keep using the normal connection instead of the VPN. Perfect for a gaming
  console or work laptop. The change takes effect immediately.
- **The box now repairs itself after startup.** On every boot the gateway
  checks its own important network settings and puts back anything that went
  missing (for example after a system update). A failed repair is noted in
  the log but never prevents the gateway from starting.
- **Your own devices can't be locked out by the built-in attack filter.**
  Computers and phones inside your home network are never blocked by the
  automatic attack protection, even in Strong (WAN) mode — so the dashboard
  stays reachable even if something misfires.

### Changed

- **Speed settings made simpler.** The advanced low-delay tuning options are
  gone — the best configuration (smooth video calls, low lag for everyone)
  is now always on automatically, with nothing to adjust by hand. The Network
  tab keeps only the everyday controls: the master switch, your line's real
  speeds, per-person/per-device limits, and the home-network transfer rate.

### Removed

- **The "Wi-Fi or cable" labels on device cards are gone** — along with the
  router-querying feature behind them. There was simply no reliable way to
  know from the gateway box how a device connects, so the labels were often
  wrong. The online/offline light still works as before.

### Fixed

- **Devices no longer stay stuck on the OLD VPN server.** Previously, if you
  changed your VPN server or app, the household's devices kept using the old
  server (and showed the old location) while the box itself used the new one
  — sometimes forever. Now devices automatically follow the newest connection
  within about 15 seconds. If anything ever looks stuck, switching VPN share
  off and back on forces a clean re-detection.
- **Online lights went gray while devices were actually online.** Two bugs
  made devices appear offline (gray light) even though they were browsing
  normally. Both fixed — the lights now match reality.
- **Newly connected devices show as online much faster** — roughly 20 seconds
  after joining instead of up to 50.
- **HTTPS survives moving or copying the program folder.** If the settings
  file loses its HTTPS entries but the certificate is still on disk, secure
  HTTPS is switched back on automatically at startup.
- **VPN sharing now works reliably with more VPN apps**, including ones that
  don't give their tunnel a normal network address, and apps (like nekoray)
  that used to connect and disconnect over and over.

## [0.3.0] — 2026-08-19

### Added

- **Notification bell.** A bell icon in the top-right corner shows a red badge
  when something security-related happens (failed logins, blocked attacks, a
  default password still in use, dashboard exposed to the internet). Click it
  for a list with timestamps and plain explanations.
- **Proper forms for firewall rules and port forwards.** Adding or editing a
  firewall rule or port forward now opens a clear form with labeled fields,
  instead of a chain of pop-ups. Existing port forwards also gained an
  **Edit** button.
- **One-click HTTPS.** The Firewall tab gained an **Enforce HTTPS** card:
  click **Enable HTTPS** and the dashboard generates a certificate, saves it,
  and restarts securely — all in one step. A **Remove HTTPS** button lets you
  undo it just as easily.

### Fixed

- **The firewall's "block incoming connections from the internet" rule works
  now.** Another rule was sitting ahead of it and swallowing the traffic, so
  the block never actually applied to external devices.
- **HTTPS enable crashed on first use** — a timing mistake meant it tried to
  lock a file before creating it. Fixed.
- **On installed boxes, Enable/Remove HTTPS edited the wrong settings file**
  and the change appeared to do nothing until restart. Fixed.
- **"Remove HTTPS" silently did nothing** due to a settings-saving slip. It
  now rolls back to plain HTTP properly.

## [0.2.1] — 2026-08-17

### Added

- **Software updates from the dashboard.** The Admin tab checks for newer
  versions, shows what changed, and can install the update for you.
- **New devices join disabled.** A device seen for the first time gets no
  internet allowance and stays offline until you assign it one.
- **Choose your bundle reset style.** Reset on a fixed day of the month or on
  your ISP's month-end bill date. Changing the reset day mid-month no longer
  wipes the month's usage.
- **MAC allow/block lists.** From the Network tab: let specific devices skip
  the quota entirely, or permanently ban others.
- **Deleting a device is permanent** — it stays offline instead of quietly
  reappearing seconds later.

### Changed

- **"Stop new connections" and "decline anonymous devices" now turn devices
  away at the door** — a refused device never even appears in the list.
- **Stronger passwords and smarter login throttling**, plus snappier dashboard
  updates under the hood.

### Fixed

- **"Show details" on an update notification was empty** — it now lists
  what's new.
- **Users marked "Exempt" were still getting cut off** at their allowance —
  exempt really means unlimited now.
- **Changing the reset day mid-month could wipe usage** — kept safe now.
- **The "cut existing anonymous devices" sweep could hit real products** —
  it now only targets genuinely anonymized devices.
- **A device on the allow list couldn't be put on the block list** — the two
  lists now work independently.

## [0.2.0] — 2026-08-16

### Added

- **Separate speed controls for internet vs home network.** The Network tab
  splits your real line speed (internet) from the internal transfer rate, so
  limiting the internet doesn't slow down copying files between your own
  devices.
- **Mark a person as "unlimited."** An exempt user is never cut off, however
  much they use. Manual blocks still apply.
- **Turn away anonymous devices.** Phones and laptops can hide their identity
  with a "private address"; a new Network-tab option automatically blocks
  brand-new devices that do this (optionally including ones already joined).
- **Privacy eye.** One click hides sensitive on-screen details — device IDs
  and the saved broadband username/password — useful when someone's looking
  over your shoulder or you're taking a screenshot.
- **Guest speed limit.** Cap the total bandwidth all guest accounts share.
- **Renew your public IP from the dashboard** (Strong/WAN mode): a Restart
  button re-dials the line for a fresh IP, with an optional automatic schedule
  (minimum every 5 minutes). Internet blips for a few seconds while it dials.
- **Guest account limit + STOP NEW CONNECTIONS.** Set a maximum number of
  guest accounts, or refuse every brand-new device outright.

### Changed

- **Cleaner sidebar:** bundle settings merged into the Network tab, system
  logs moved onto the Admin page, and the tab was renamed to just "Network".
- **Fresh dashboard look** — a dark glass design with a fixed left sidebar,
  two-column device cards, subtle background animation, and cobalt-blue
  accents throughout.
- **VPN sharing prefers your real VPN app.** If you run a proper VPN app on
  the box, it's used directly with no setup. The automatic helper bridge is
  now only a fallback for apps that need it — no more duplicate tunnels
  fighting over your devices.

### Fixed

- **Copying files to the box itself was being speed-limited** by the internet
  upload cap. Transfers involving the box now run at full home-network speed.
- **Home-network transfers were wrongly limited by internet caps** in several
  situations (certain settings, or settings left unset). Multiple causes
  found and fixed; local transfers stay fast everywhere.
- **Setting one direction to "unlimited" silently disabled the other
  direction's limit too** — each direction is now independent.
- **A speed cap of "2 Mbps" actually measured about 3** — caps are now
  accurate.
- **Brief VPN drop killed sharing for good.** If the VPN reconnected, the
  household could lose the shared VPN permanently. Sharing now survives
  reconnects.
- **Sharing could send everyone's traffic into a dead fake tunnel** (a
  leftover junk device), silently cutting off the whole house. Dead tunnels
  are detected and avoided.
- **The VPN share switch forgot it was flipped** until you pressed Save, and
  its status text froze on "waiting…" — it now saves instantly and updates
  live.
- **The automatic VPN bridge became far more reliable**: it installs
  correctly, starts correctly, and — importantly — if your VPN app isn't set
  up for sharing yet, you now get a clear message instead of every device
  silently losing the internet.

### Added (late additions)

- **Works with v2rayN-style apps automatically.** These apps don't create a
  system-level VPN tunnel, so the box now downloads a small verified helper
  (checksum-checked before use) and bridges the household through it —
  nothing to install by hand.
- **Cut the box's OWN internet without killing the household VPN.** With
  "Gateway OFF", the box keeps only its connection to the VPN server alive,
  so everyone else stays online through the shared tunnel.

## [0.1.3] — 2026-08-12

### Added

- **VPN share.** Run a VPN app on the gateway laptop and flip the switch:
  every device's internet exits through the VPN, while quotas, blocking, and
  speed limits keep working exactly as before.
- **Website blocking & parental controls (DNS tab).** Block, allow, or
  redirect any website for one person, one device, or the whole house —
  wildcards supported (like `*.youtube.com`). One-click ready-made lists
  (ads/tracking, social media, streaming, gambling), and you can point any
  person or device at a different DNS service (for example a family-friendly
  resolver). Blocked sites are badged right in the History tab with quick
  Block/Allow buttons.
- **Install and update with apt.** Every release is published to a signed apt
  repository, so Linux boxes can `apt install quota-manager` and receive
  upgrades the native way.

### Fixed

- **Package installation crashed halfway through** on some boxes (an
  installer-script typo). Reinstalling the fixed version repairs an affected
  box.

## [0.1.2] — 2026-08-11

### Added

- **Browsing history per device (History tab).** Pick a device and a time
  window (24 hours to 14 days) to see its most-visited sites, hourly activity,
  and recent lookups. Each person can have their own retention period, and an
  **All devices** view combines the whole household with names attached
  (`[Yahya]`, `[Mom]`, …). Recording is capped so it never grows unbounded
  and never slows down browsing.

### Changed

- **New dashboard theme** — deep purple frosted-glass styling, and device
  cards stacked full-width so everything has room to breathe.

### Fixed

- **Browsing history stayed empty even though recording was on** — two setup
  problems (a log-format mismatch and a disabled include-folder) meant queries
  were silently thrown away. Both fixed; history now fills in.

## [0.1.1] — 2026-08-08

### Added

- **Household usage page.** Any device on the quota network can open the
  milestone page (no login) and see *its owner's* usage: used vs allowance, a
  progress bar, per-device breakdown, and one-time flags at 50% / 75% / 100%
  that you acknowledge with a single click.
- **Internal report page.** A read-only overview (exact bytes per person and
  device, recent events, log tail) available only to devices on your own
  network.
- **The box's own internet use is counted too**, charged to a protected
  "Gateway" user, so updates and VPN traffic on the box itself don't eat the
  family's bundle invisibly.
- **Phone-friendly web UI** across the dashboard and public pages — swipeable
  tabs, single-column layout, touch-sized buttons.

### Changed

- Guest devices keep their names and history when they reconnect.
- Changing a speed cap applies immediately — no refresh needed.
- The internet-status indicator stays honest even while the box restricts
  itself.

### Notes

- **Upgrade note:** from this version the Gateway user's fixed 1 GB allowance
  is deducted from every "shared equally" bundle automatically. Fixed
  allowances are unaffected.

## [0.1.0] — 2026-08-06

Initial Linux release: the gateway box splits your ISP bundle fairly between
people (each person's allowance covers all their devices), counts and cuts
internet at the network level, supports per-device overrides, per-person speed
limits, detection of uninvited devices that bypass DHCP, Strong (WAN) mode
where the box dials the line itself, and one-command install via `.deb`.
