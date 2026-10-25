# Changelog

All notable changes to **Quota Manager**, newest first — written in plain
language: what changed and how it affects you.

_(For developers: versions live in `quota/version.py`; a release tag must
match it. Release notes are composed from a version's section below.)_

## [0.5.0] — 2026-10-25

### Added — the new dashboard
- **One new dashboard for everything.** It opens at the usual address (`/`) and replaces the old one completely — the old dashboard was removed. Old bookmarks to the preview address (`/ui/`) forward automatically.
- **Look & feel**: dark by default with a light mode, full **Arabic (right-to-left) and English**, accent colours, six ready-made **system themes** (Cobalt, Emerald, Violet, Crimson, Amber, Nord), animated background effects (Mesh, Dust, Rain, Orbs — or off), adjustable density, and a **privacy eye** that hides names and device addresses before you share your screen. A short welcome guide appears on first run.
- **Overview**: live bundle ring, remaining GB, days left, daily average and projected usage; live traffic chart (live speed / usage by day / top devices right now); status tiles for internet, devices, VPN and security; top consumers; and a "needs attention" list.
- **People & devices**: add and edit people and devices, top up, move a device to another person, kick, block or blacklist, approve new guests, set speed limits, unlimited usage, VPN bypass and custom DNS. Opening a person or a device opens just that one. Your gateway stays pinned at the top: you can cut its internet if you need to, but it is protected from being deleted or blacklisted.
- **Quota**: bundle size, renewal day, top-up and recharge packs.
- **Network**: line speed limits, VPN sharing, random-MAC gate, MAC whitelist/blacklist, fixed IP reservations, and guest / new-device rules including *stop new connections*.
- **Internet**: line status, public IP renewal and schedule, IPv6 status and check, **remote access with Tailscale**, and the **WAN / PPPoE card** — test your connection, apply or revert with a two-step confirmation.
- **Security**: firewall with apply / revert and a rollback timer, rules, port forwards, bans, HTTPS, and the DNS ad-blocking shield with categories, custom rules and list import.
- **VPN**: connect / disconnect, add, edit, test and delete servers, choose who goes through the VPN, live speed and logs.
- **Insights**: usage reports and top consumers per device and period.
- **Share**: file, folder and text sharing, plus **live device-to-device transfer** (files, multiple files or whole folders, with accept / decline and progress).
- **Public pages** (`/share`, `/milestone`, `/report`, the guest approval portal and the calibration page) redesigned to match the new dashboard: same colours and fonts, light / dark following your dashboard choice, better on phones. Behaviour is unchanged.
- **Share page (`/share`)** redesigned to match the new dashboard: same colours and fonts, light / dark following your dashboard choice (plus a quick toggle), cleaner file and device cards, better on phones. Upload, download and live-transfer behaviour is unchanged.
- **System**: appearance, password, two-factor sign-in, updates and logs.
- Every feature of the old dashboard has a home in the new one, except the old speedometer gauge styles, which are replaced by the new bundle ring and system themes.

### Added — LAN monitoring
- **Local network (LAN) traffic is now tracked next to the internet**: per person, per device and per day. A **WAN | LAN** switch above the Overview chart gives you the same three views (live speed, usage by day, top devices) for each, and every person and device shows its internet and local usage side by side. LAN is for monitoring only — it never counts against the bundle.
- What the gateway can see: traffic between a device and the router, a NAS or other hosts on the router's network, and between a device and the gateway itself (dashboard, sharing, direct transfer). Devices talking to each other on the same Wi-Fi go through the router directly and cannot be seen by the gateway.
- **Smart bundle tips** on the Overview: when the bundle runs out you get quick top-up buttons, when it will not last until renewal you get a daily-usage target, and otherwise a projection of what will be left. (Only record top-ups you have actually bought and activated with your provider.)

### Added — network
- **Live traffic names every destination**: when a server has no website name, the page shows who owns it instead of a bare number, and remembers it. Only the destination address is looked up; turn it off with `history.ip_owner_lookup: false`.
- **IPv6 for every device (dual-stack)**: each device gets an IPv6 address next to its IPv4 one. IPv6 traffic counts against the allowance, is cut together with IPv4 when a device is blocked or over quota, and obeys speed limits.
- **Smart IPv6 fallback**: if your provider has no IPv6, apps switch to IPv4 instantly instead of hanging. The system re-checks every 10 minutes and turns IPv6 on by itself when it becomes available.
- **Static IPv6 reservation** (optional) next to the static IPv4 one.
- **Bypass warning**: a device that takes IPv6 straight from your router shows an "IPv6 bypass" tag — turn off IPv6 / Router Advertisement on the router to fix it.
- **Reach your dashboard from anywhere with Tailscale**: Internet page → *Remote access*. Install, sign in once and get a private, encrypted address for the dashboard that works from any network, with nothing to open on your public IP. Many internet lines block or drop connections coming from outside, and this works around that.
- **Installing Tailscale no longer cuts the internet**: the gateway now re-checks forwarding, the NAT table and its packet engine right after the install and every few seconds, and restores whatever a package install removed - no service restart needed. Install progress shows four simple steps instead of the installer's raw output, and the card lists your other Tailscale devices (online, direct or relayed).
- **Alerts stay until you dismiss them**: the "suspicious activity" card on Overview (failed logins + firewall blocks) no longer disappears after an hour - it stays until you press *Mark as seen*, and comes back only for new activity. The live speed chart also survives a page refresh now: the gateway remembers the last few minutes.
- **First sign-in no longer looks stuck**: when the quick-setup dialog opened right after signing in, the dashboard behind it was not drawn until the dialog was closed. It is drawn immediately now.

### Changed
- **Telegram alerts for IP changes were removed**: they are no longer needed because the Tailscale address does not change when your public IP does.
- **Live traffic shows real destinations**: next to a device uploading a video you now see the sites receiving it (for example `facebook.com ⬆ 2.1 Mbps`), taken from the real connections. The guessed app labels ("TikTok", "Idle / Background") are gone because they could be wrong.
- **Settings survive re-running setup**: Firewall, DNS and VPN settings are kept (the previous `config.yaml` is backed up as `config.yaml.prev`) instead of silently going back to defaults.
- The setup script no longer disables IPv6. **Updating needs no manual step** — the new version turns IPv6 on by itself.
- **Firewall snapshots** moved out of the project folder, so replacing the folder cannot remove them.

### Fixed
- **Guest approval portal**: its status polling, automatic redirect and "Check now" button were blocked by an over-strict browser security policy; they now work.
- **Dashboard reachable from the internet with remote access off**: when the box sits behind the ISP router, anyone who reached your public IP on the dashboard port (through a router port-forward, DMZ or UPnP) could open the login page even though the firewall switch was off. The switch is now **"Allow dashboard access from the internet"** and, while off (the default), new connections to the dashboard from outside your home networks are refused by the gateway itself. Turn it on only if you want remote access.
- **Firewall no longer switches itself off after an update**: if its rules disappear they are restored within seconds (failed attempts retry every minute), and your firewall settings are backed up next to the database.
- **Old look after an update**: browsers kept an old copy of the page scripts; they are now re-checked on every load, so no more Ctrl+F5.
- **Phantom "Guest" devices** after enabling IPv6 (named "0" or a long number) are ignored now.
- **VPN share "Connected" but no internet**: the VPN client now resolves its server through the system DNS, and the server address is resolved before the tunnel starts.
- **VPN sharing stopped the internet when IPv6 was on**: fixed automatically on update. `dhcp.ipv6_enable: false` now fully switches IPv6 off again.
- **Phantom "online" lights**: a device that left or was switched off no longer stays green for hours.
- **Misleading "98%" in Live traffic**: a quiet network now says "idle" instead of showing a fake share.
- **More accurate live speeds**: calculated over the real measuring period, and stale readings are no longer shown.
- **Firewall "Revert" did nothing** after a single Apply since boot, and later Applies kept reverting to the very first one. Revert now goes back exactly one step and the result survives a restart.
- **IPv6 "Check" button** showed "not initialized" (HTTP 503); it works now.
- **Live transfer to another device**: a multi-file / folder send (ZIP) was reported as a cancelled download by the browser, and on some runs the first bytes of a file were lost when the receiver's download started a moment late. Both fixed.
- **Share page**: after sharing a text it stayed out of the list (and in the text box) until you left the page; dropping files in the drop zone now falls back to a plain file drop when the browser gives no folder entries.
- **System → Logs**: the ERROR filter no longer shows warnings that merely contain the word "error".
- **Small polish**: enabling HTTPS and removing a MAC from the allow/deny list now ask for a second tap; DNS category names and common error messages (wrong password, wrong 2FA code, update check refused) show in Arabic; a note on the firewall rules card explains that LAN-mode rules from the local network have no effect.

## [0.4.3] — 2026-10-19

### Added
- **Live Traffic Bandwidth Monitor & Top Consumer Radar**:
  - Added a dedicated **Live traffic** view in the Browsing History section placed right before "Top Apps & Platforms".
  - **Real-Time Bandwidth Speedometer & Live Graph**: Displays an interactive, real-time download and upload throughput graph updating every 2 seconds, tracking total active network traffic, peak bandwidth, and live download and upload line speeds.
  - **Top Consumer Spotlight**: Instantly identifies the device consuming the highest amount of bandwidth right now, showing the device name, user, IP address, exact speed (e.g., in Mbps/Kbps), and percentage share of current household traffic.
  - **Active App & Platform Detection**: Automatically detects and shows what each device is actively doing or streaming (e.g. YouTube, TikTok, Netflix, Google, Steam, Zoom, Shahid VIP, etc.) with brand colors, service icons, and recent domains.
  - **Live Device Ranking**: Ranks all active household devices by their current bandwidth consumption, allowing you to click any device to immediately inspect its detailed browsing history and app usage.
- **Local Share (Zero-Install High-Speed LAN File & Text Sharing)**:
  - A dedicated **Local Share** tab in the sidebar allowing instant file and text sharing across all devices in your home (smartphones, PCs, laptops, tablets, and smart TVs) directly over your local Wi-Fi.
  - **Live Direct Transfer**: Transfer files and folders directly from device to device in real time over Wi-Fi without using router storage space. Ideal for routers with limited storage.
  - **Instant Device Discovery (Radar Beacon)**: Devices currently open on Local Share automatically detect each other on the local Wi-Fi network, allowing 1-click direct file beam.
  - **Zero App Installation**: No apps required on your phone or computer. Anyone connected to the home network can access the portal directly in their web browser.
  - **Folder Upload**: Upload entire directories and folders directly; automatically packaged into clean archive bundles preserving folder structures without uploading individual files one by one.
  - **Multi-Selection & Batch ZIP**: Select multiple files or "Select All" with a single click to download them together as a single ZIP package or delete them in batches.
  - **Mobile-Optimized Interface**: Clean responsive layout specifically designed for smartphones preventing overlapping buttons or cut off filenames.
  - **Ultra-Fast Local Network Transfers**: Transfers happen entirely within your local home network (Wi-Fi/Ethernet) at maximum physical line speed without consuming internet quota.
  - **Quick Mobile QR Code**: Scan a QR code on your PC screen with your smartphone camera to instantly open the Local Share portal.
  - **Interactive Drag & Drop**: Easily drop any file or folder to share it immediately with real-time progress, transferred bytes, and live speed meter.
  - **Instant Clipboard & Link Sharing**: Copy and paste links, notes, or Wi-Fi passwords to beam them across all connected household devices in real time with a 1-click copy button.
  - **Multi-Folder & Multi-File Bundling**: Select one or multiple files or folders simultaneously across both standard uploads and Live Direct device-to-device transfers, bundled seamlessly into archives with preserved directory structures.
  - **Automatic Expiration & Cleanup**: Shared files automatically expire and are cleared after 2 hours to keep the router's storage light and tidy.
- **Convert Guest to Regular User**:
  - Easily convert temporary guest users to permanent regular users with a single click ("👤 Make Regular") directly from user cards, device rows, or the user settings dialog.
  - Converting a guest account immediately removes guest speed restrictions and exempts the account from monthly reset auto-deletion, granting full administrative control over customized bandwidth speeds and data quotas.
- **Fair Bandwidth Sharing & VPN Anti-Starvation**:
  - When your monthly bundle expires and you connect to a VPN, connection speed is automatically and fairly distributed among all active household devices.
  - If only one device is downloading, it automatically receives 100% of the available connection speed.
  - When multiple devices request internet simultaneously, the bandwidth is shared equally and fairly so that heavy downloads on one phone or computer cannot freeze or disconnect other devices.
  - Seamlessly utilizes the maximum internet line speed already configured in the Network tab (WAN speed limits) without requiring duplicate or separate settings.
- **Guest Approval Notification & Search Interception (Captive Portal)**:
  - When a new guest connects to the home network and opens a web browser to search or browse before the network administrator has approved their device, they are automatically greeted with a friendly, clear status page explaining that their device is awaiting admin approval.
  - Displays device information (device name, IP, and MAC address) along with a live indicator showing that approval is pending.
  - Automatically checks for administrator approval in the background every few seconds.
  - The moment the administrator clicks "Accept" in the dashboard, the page automatically turns green, shows the granted quota, and redirects the guest to their requested search or website within 3 seconds without needing to reload or reconnect.

### Changed
- **Live Traffic Radar & Graph Modernization**:
  - Arranged the real-time throughput graph and top consumer card side-by-side on desktop displays to optimize screen space and provide an instant overview.
  - Upgraded the throughput graph to ultra-smooth Bezier curves and glowing gradient fills for fluid, real-time visualization.
  - Corrected the graph's speed axis typography with crisp, proportional numbers eliminating squished or distorted text.
  - Refined the sidebar active tab indicator's left edge to align seamlessly with the pill's corner radius.
- **Simplified Interface Terminology**: Removed confusing technical terms (such as "0-Disk Stream" and "Direct RAM relay") across all badges, modals, and tooltips, replacing them with clear, user-friendly labels like "Direct Wi-Fi" and "Live Direct Transfer".
- **Streamlined Action Controls**: Cleaned up the upload area and device cards to just two simple, balanced buttons (`Files` and `Folders`), eliminating cluttered and redundant buttons while maintaining full support for multi-item selection.
- **Streamlined VPN Speed Controls**: Simplified the "VPN mode — speed" section in the Network tab from 3 confusing inputs to just 2 direct, clean fields (`VPN down Mbps` and `VPN up Mbps`), removing the redundant percentage input so users can directly specify download and upload line limits.
- **Direct Stream Speed & High-Throughput Buffering**: Upgraded Live Direct transmission chunk size to 512 KB, implemented ultra-fast hardware-efficient data verification, optimized network flow pacing to eliminate artificial delays, and removed in-memory duplication on receiving devices to unlock full local Wi-Fi transfer line speeds.
- **Direct Multi-File & Folder Transfer**: Added instant folder selection and multi-file selection to Live Direct, automatically bundled into an archive with preserved directory structures.
- **Live Direct Transfer 50 MB Safety Limit**: Added a safe 50 MB size limit on Live Direct device-to-device transfers to guarantee smooth, fast transmissions without stalling or exhausting mobile device resources. Files and folders larger than 50 MB prompt a helpful notification guiding the user to standard Local Share upload instead.

### Fixed
- **Instant Web Dashboard Startup & Zero-Delay Restart**:
  - Fixed an issue where restarting the service after transferring updates or configuration files caused the web dashboard to become temporarily unreachable (`ERR_CONNECTION_REFUSED`) for 15 to 40 seconds.
  - The web interface now starts listening and responding within milliseconds of service startup, allowing immediate page access while background system rules finish loading smoothly in parallel.
  - Optimized service restart timers to eliminate artificial delays when restarting the gateway.
- **Duplicate PC Device Cards in Local Share**: Fixed an issue where a computer (or any device with multiple browser tabs open or reconnected sessions) appeared twice on the nearby devices radar for other connected phones and tablets. Each physical device on the home network now appears exactly once.
- **VPN Connection Stability & Speed Customization**:
  - You can now specify custom VPN line limits (`VPN down Mbps` and `VPN up Mbps`) directly from the Network tab without editing configuration files.
  - Eliminated connection stalls and queue congestion when running high-bandwidth downloads over VPN by dynamically regulating transmission pacing and prioritizing control signals.
  - Upstream traffic from client devices is fairly balanced so that no single device can saturate the VPN tunnel and freeze the network for others.
- **VPN Activation with Uninterrupted DNS & Ad Blocking**:
  - Fixed an issue where activating the VPN tunnel bypassed local DNS servers and sent raw queries to external DNS providers.
  - Ad-blocking rules, parental protection filters, custom domain blocklists, and DNS search history analytics now continue to apply flawlessly across all devices when connected to a VPN, while local LAN network traffic is safely excluded from encryption before rules are applied.
- **Phone Screen Sleep & Lock Disconnections**: Added automatic Screen Wake Lock protection during active Live Direct transfers, preventing mobile phone screens from dimming or going to sleep and interrupting file streams mid-transfer. Added visual reminders to keep the screen active.
- **Reliable Direct Download & Completion Save Button**: Added a dedicated "Save / Download" button and completion notification directly inside the transfer dialog on receiving devices. If a mobile browser blocks automatic background downloads, users can now tap the button to immediately save their received files.
- **Instant Direct Beam Handshake**: Eliminated the initial delay when tapping "Send Direct" by registering device names immediately upon connection, using warm network keep-alive signals, and ensuring sender-receiver pairing even if devices reconnect.
- **Large Multi-Folder Direct Stream Freezing**: Fixed an issue where sending large folders or multi-folder bundles (such as several albums totaling hundreds of megabytes) caused transfers to stall on "Waiting for receiver to accept…". Streaming now initializes instantly without high browser memory usage, includes connection keep-alive heartbeats, and features automatic peer resolution to prevent dropped requests.
- **Large Payload Memory Crash & Disappearing Files**: Fixed an issue where receiving large payloads (such as 780 MB multi-folder transfers) caused mobile browsers to exhaust their memory limit and crash or reload the tab, causing the received data to be lost. Receiving devices now stream directly into their device storage through a native stream pipeline, keeping browser memory usage at virtually zero and completely preventing tab crashes.
- **Direct Stream 0 KB / Opus File Issue**: Fixed an issue where binary transfer headers could cause received files to save as 0 KB or with a `.opus` audio extension on mobile devices.
- **Receiver Progress Display**: Fixed an issue where the receiving device's progress bar remained at 0% during direct transmission; progress and live transfer speed now display accurately.
- **Gateway Local Traffic VPN Tunneling**: Fixed an issue where connecting to a VPN routed all household client devices through the tunnel while the gateway machine itself still used the direct unencrypted ISP connection. The gateway machine's own traffic now seamlessly routes through the active VPN tunnel alongside connected devices.
- **Local Share Self-Device Filtering in Live Transfer**: Fixed an issue where a computer or smartphone could appear in its own list of online devices in Local Share when opening multiple tabs or refreshing the page. The device list now accurately displays only other nearby devices connected to your home network.
- **Heavy Download VPN Starvation & Program/Device Freezing**:
  - Resolved an issue where a heavy download on one computer or phone in VPN mode (especially when the monthly bundle is finished) would cause the internet to disconnect on all other household devices, and would even freeze browsing and messaging apps on the downloading computer itself.
  - Fixed an issue where upstream DNS queries to resolve website addresses were being sent directly out the expired ISP connection instead of through the active VPN tunnel, causing new websites and apps to hang and time out while established downloads continued.
  - Added concurrent multi-resolver querying and upstream upload filtering to ensure that internet browsing, gaming, social apps, and video streaming stay fast and uninterrupted on all devices, even when someone is downloading at maximum speed.

## [0.4.2] — 2026-10-09

### Fixed
- **Dashboard Action Buttons on Mobile & Modern Browsers**: Fixed an issue where several interactive controls across the dashboard — including VPN node connection, add-on package deletion, static IP reservation removal, MAC filtering rules, and firewall management controls — were unresponsive when clicked or tapped.
- **VPN Connection Feedback**: Added clear visual status indicators ("Connecting…" and "Disconnecting…") when switching VPN nodes, providing immediate feedback that your request is being handled.
- **Mobile Touch Responsiveness**: Enlarged the touch target areas of smaller action buttons on phone screens for easier and more reliable tapping.

## [0.4.1] — 2026-10-05

### Added
- **Automotive Speedometer Gauge**: A brand-new, high-precision monthly bundle dial featuring a vibrant safe zone (0–80%) and an intense glowing scarlet redline warning zone (80–100%) so you can see your remaining quota at a glance.
- **3 Distinct Speedometer Gauge Styles**: Choose your preferred monthly bundle gauge design directly from the Settings menu:
  - **Sports Needle**: A real rotating precision tachometer needle that sweeps across the dial as data is used.
  - **Digital Segmented LED**: High-tech illuminated dashboard blocks inspired by racing telemetry.
  - **Electric Horizon Arc**: A minimalist, futuristic glowing neon curve with a scarlet warning cap.
- **Themes & Appearance Engine**: Customize your dashboard's visual style with 4 selectable color schemes: Obsidian Glass (Default), Cyberpunk Neon, Emerald Mint, and Sunset Crimson.
- **Dynamic Background FX**: Pick from 4 ambient animated canvas backgrounds (Network Mesh, Cosmic Starfield, Digital Rain Matrix, or Clean Static).
- **Temporary Device Kick (5-Second Disconnect)**: Easily disconnect or kick any device for 5 seconds to force it to drop connections or refresh its IP lease without permanently blacklisting or banning it from the network.
- **Smooth Panel Transitions**: Lightning-fast, instant tab switching across the sidebar with zero repeated loading flickers on previously visited pages.

### Changed
- **Organized Sidebar Bundle Widget**: Redesigned the sidebar metrics below the gauge into a clean 3-row layout: Remaining quota and days left on top, a dedicated full-width period badge in the middle that never crowds the edges, and clear device/user counters at the bottom.
- **Card Ordering**: The Protected Gateway is now permanently pinned at the top of your network list, registered users sit neatly in the middle, and guest/unassigned devices are grouped at the bottom.
- **System Logs Scroll Window**: The system logs viewer in the Admin panel is now cleanly contained in a scrollable console window so long logs never push the rest of the page down.
- **Streamlined Pure Linux Stack**: Cleaned up the packaging and runtime to focus exclusively on native bare-metal Linux (Debian & Kali) performance with no container overhead.

### Fixed
- **DNS Category Filters & Parental Controls**: Fixed an issue where opening the DNS tab could show a blank list of category presets, ensuring all parental controls (Adult content, Gambling, Social media, Streaming) and the master protection switch are always visible and responsive.
- **Gauge Dial Visual Polish**: Fixed an issue where the gauge arc could show duplicate ghost arcs or overlap the title bar, ensuring needle length, center pivot, and text labels are perfectly framed.
- **Gauge Selector Button Labels**: Fixed an issue where long style names could spill outside their selector buttons on smaller screens.
- **VPN Proxy Routing While Quota Blocked**: Fixed an issue where cutting the gateway's own quota would block outgoing connections to your configured VPN servers, ensuring proxy traffic and management remain reachable.

## [0.4.0] — 2026-10-01

### Added
- **Built-in VPN Service**: You can now connect your whole network — or select devices — to a VPN directly from the dashboard, without needing any VPN apps installed on your phones, TVs, or computers.
  - **One-Click Link Import**: Easily paste standard VPN links or scan QR codes (supports VLESS, VMess, Shadowsocks, Trojan, and WireGuard).
  - **Live Speed Meters**: Watch live download and upload speeds and track total data transferred through the VPN.
  - **Choose Who Uses the VPN**: Selectively route specific family members or devices through the VPN while keeping gaming consoles or work laptops on your regular internet line.
  - **Proxy Settings Editor**: Edit server addresses, ports, and connection settings right inside the dashboard.
  - **Always Connected**: The VPN automatically reconnects if the gateway restarts, the internet blips, or you refresh your browser.
- **Telegram Alerts on Public IP Changes**:
  - Receive an instant message on your phone via Telegram whenever your home router gets a new public IP address from your provider.
  - Each alert includes the new IP address, timestamp, and a clickable link to open your dashboard from outside your home.
  - Includes a "Send Test Message" button in the WAN tab to verify your bot instantly.
- **Network-Wide Ad Blocker & Family Safety Shield**:
  - Block ads, trackers, and popup banners across all connected devices in your home — including smart TVs and mobile apps — with zero client software needed.
  - Two simple protection levels: **Standard** (everyday fast ad blocking) and **Ultra PRO** (maximum shield against aggressive ads, telemetry, and tracking).
  - Built-in parental control switches to block adult websites, gambling, social media, or streaming video across the house or on specific devices.
- **Browsing History & Activity Insights**:
  - Visual charts showing the most visited apps and websites (like YouTube, TikTok, Netflix, Facebook) and daily activity timelines per device.
- **Fixed Device IP Reservations**:
  - Permanently reserve specific IP addresses for your printers, servers, or gaming consoles directly from the Network tab with a single click.

### Fixed
- **Seamless VPN Internet Connection**: Fixed an issue where connecting to a VPN could freeze network traffic or temporarily cut internet access across the house.
- **VPN Latency & Ping Tests**: Fixed ping tests so response times update accurately and display an active testing spinner.
- **Browsing History Loading Spinner**: Added a smooth loading indicator when opening the History tab so you know data is loading right away instead of seeing an empty screen.
- **Cleaner Notifications**: Fixed an issue where alert notifications could repeat or stack on top of each other.
- **Mode Switching Reliability**: Switching between Direct Internet (WAN) mode and Standard Router (LAN) mode is now completely seamless and error-free.
- **Reduced Log Clutter**: Removed repeated background log warnings, keeping the system logs clean and readable.

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
