# Quota Manager

<a href="README_AR.md"><img src="https://img.shields.io/badge/%D8%A7%D9%84%D8%B9%D8%B1%D8%A8%D9%8A%D8%A9-Arabic-green" alt="العربية"></a> ![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

<table>
  <tr>
    <td width="300" align="center"><img src="docs/logo/favicon.png" width="280" alt="Quota Manager logo"></td>
    <td>

Split your metered internet bundle fairly across every person in the house. Each
**user** gets an allowance (fixed GB, or an equal share of what's left), their
devices all share it, and the moment the allowance runs out **every device they
own is cut at once**.

In countries where internet bundles are metered (e.g. Egypt's 140 GB/month plans),
phones, TVs, laptops and consoles all fight over one connection with no way to
budget it. **Quota Manager** turns an old laptop running 24/7 into a smart
gateway:

- Counts exactly what every device and every user consumes each month
- Gives each user a monthly allowance their devices share
- **Hard-cuts a user's internet** the moment they run out (a per-device *exempt*
  flag keeps one device online)
- Caps any device's or user's **internet speed** and keeps gaming ping low while
  others download
- Serves a **dark obsidian-glass dashboard** you can open from any phone on
  the LAN — the whole UI (dashboard, the household milestone page, and the
  consumption report) is phone-friendly and touch-first

    </td>
  </tr>
</table>

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

**For developers** — how the app actually works (architecture, config, API,
tests, release process): [Structure_README.md](Structure_README.md).

## Screenshot

![Quota Manager dashboard](docs/screenshots/dashboard.png)

---

## Table of contents

- [Screenshot](#screenshot)
- [Installation](#installation)
- [Using the dashboard](#using-the-dashboard)
- [Strong (WAN) mode](#strong-wan-mode)
- [VPN share (route the household through a VPN)](#vpn-share-route-the-household-through-a-vpn)
- [Day to day](#day-to-day)
- [Upgrading / removing](#upgrading--removing)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)

---

## Installation

You need a computer with **one wired Ethernet port**, powered 24/7, running
**Kali or Debian** — an old laptop, a used mini PC, or a Raspberry Pi all
work. It becomes the gateway that every device routes through.

### No spare machine? Use the PC or laptop you already have (wired only)

If you don't own a second computer, the gateway can run inside a **Debian
virtual machine** on the PC or laptop you already use. Three things matter:

- **A wired connection.** The machine must reach the router with an Ethernet
  cable (a cheap USB-to-Ethernet adapter works). **WiFi will not work** — the
  gateway must sit on the router's network at the hardware level, which a
  wireless link can't provide.
- **Bridged networking.** Set the VM's network adapter to *bridged* so it
  appears on the router's network like a real computer.
- **Always on.** The machine must stay running 24/7 — when it sleeps, shuts
  down, or restarts, everyone loses internet.
- **A fixed address — reserve it or set it static.** The gateway must keep a
  permanent IP on the router's network: either **reserve one on the router**
  (a DHCP reservation for the VM's MAC) or **set it static on the box** (the
  setup script does this by default). If the VM's IP ever changes, everyone
  loses access — see *The gateway's addresses (LAN mode)* below. In a VM,
  bridged networking puts the box on the router's LAN exactly like a real
  machine, so the same rule applies.

From there, follow the steps below as usual: the `.deb` installs *inside* the
VM, and the whole gateway (routing, network stack, dashboard) runs there. This
is also a great way to try Quota Manager before committing any hardware.

### 1. Install the package

#### Method A — apt repository (Debian / Kali)

**Easiest for bare-metal installs** (one-time key + repo setup, then upgrades via `apt update && apt upgrade`):

```bash
sudo install -d /etc/apt/keyrings
curl -fsSL https://UserJoo9.github.io/QuotaManager/quota-manager.gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/quota-manager.gpg
echo "deb [signed-by=/etc/apt/keyrings/quota-manager.gpg] https://UserJoo9.github.io/QuotaManager stable main" | \
  sudo tee /etc/apt/sources.list.d/quota-manager.list
sudo apt-get update
sudo apt-get install quota-manager
```

The repository is signed with the key above and re-published automatically on
every release, so upgrades are just `sudo apt-get update && sudo apt-get
upgrade`.

---

#### Method B — Docker / Dockge (Any Linux machine / Server)

Run Quota Manager containerized on any Linux machine, mini-PC, or home server.
The multi-arch image (`amd64` / `arm64`) includes all runtime dependencies
(`nftables`, `dnsmasq`, `iproute2`, Python).

**Docker Compose (`docker-compose.yml`):**

```yaml
services:
  quota-manager:
    image: ghcr.io/userjoo9/quotamanager:latest
    container_name: quota-manager
    network_mode: host
    privileged: true
    restart: unless-stopped
    environment:
      - TZ=Africa/Cairo
      - QUOTA_CONFIG=/app/config.yaml
      - QUOTA_PORT=8080
      - PYTHONUNBUFFERED=1
    volumes:
      - /opt/quota-manager/config.yaml:/app/config.yaml:rw
      - /opt/quota-manager/data:/var/lib/quota-gateway:rw
      - /opt/quota-manager/logs:/var/log/quota-gateway:rw
      - /opt/quota-manager/dnsmasq.d:/etc/dnsmasq.d:rw
      - /opt/quota-manager/leases:/var/lib/misc:rw
```

```bash
# Start container
docker compose up -d
```

> 📖 **Full Docker & Dockge Guide:** see [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md) for network details, environment variables, and security considerations.

---

#### Method C — downloaded `.deb`

Download the latest `quota-manager_<version>_all.deb` from the
[Releases](https://github.com/UserJoo9/QuotaManager/releases) page, then:

```bash
sudo apt install ./quota-manager_0.2.1_all.deb
```

> **Fresh Kali/Debian box? Run `sudo apt-get update` first.** A brand-new
> install has never downloaded package lists, so apt reports *"no installation
> candidate"* for every dependency (`python3-venv`, `dnsmasq`, …) and aborts.
> On Kali a missing signing key shows up first as `NO_PUBKEY …` / *"repository
> … is not signed"* — fix with `sudo apt install --reinstall
> kali-archive-keyring`, then `sudo apt-get update`, then retry the install.
> (Full table in Troubleshooting.)

The package installs everything automatically: the Python app, the network
stack (dnsmasq, nftables), and a service that starts the gateway at boot.
Your device must be connected to the router by cable with internet during
this step.

### 2. Set your bundle

The dashboard asks for your bundle the first time you log in (step 4): a
one-time **welcome panel** appears with two required fields —

- **Internet bundle this month (GB)** — your real monthly allowance, e.g.
  `140` for a 140 GB/month plan
- **Reset day of the month** — the day your ISP resets the bundle (`0` = no
  auto-reset; you recharge from the dashboard instead)

plus a **bundle type** selector:

- **Renew day** (default) — the bundle resets on your configured reset day
- **End of month** — the ISP's *month-end bill*: the same configured day drives
  the reset (many ISPs close the month on the 25th/28th), and day `0` falls
  back to the calendar end (the 1st)

It also lets you change the admin password in the same step. All values can be
changed later in the dashboard's **Bundle settings** card.

Prefer editing config instead? The same two numbers live under `bundle` in
`/etc/quota-gateway/config.yaml` (plus an optional `timezone`, which the
welcome panel doesn't ask for). Edit and restart:

```bash
sudo nano /etc/quota-gateway/config.yaml
```

```yaml
bundle:
  total_gb: 140.0        # your real monthly bundle, GB
  reset_day: 1           # day of month your ISP resets; 0 = no auto-reset
  period_type: renew_day # renew_day | end_of_month (the ISP's month-end bill)
timezone: ""             # optional IANA zone, e.g. Africa/Cairo
```

```bash
sudo systemctl restart quota-gateway
```

> **Note:** once the bundle is set from the dashboard, the dashboard owns the
> value — a later config.yaml edit is ignored until you clear the
> `bundle_source` setting (see Troubleshooting).

### 3. Turn off the router's DHCP

Log into the router admin page (usually `http://192.168.1.1`), find the DHCP /
LAN settings, and switch DHCP **off**. Keep **WiFi** (same SSID and password)
and **NAT** on — devices still join the router's WiFi, but now get their IP,
gateway and DNS from the laptop. **Also disable IPv6 / Router Advertisement
(RA)** on the router (Quota Manager is IPv4 only).

> **Optional — electric-cut fallback.** If you'd rather devices keep the
> internet during a power cut, don't switch DHCP fully off — give the router a
> small pool on a *different* subnet (e.g. `192.168.1.201–250`). The laptop
> only serves `192.168.2.x`, so the pools never overlap. Devices return to the
> managed pool as their leases renew.

### 4. Log in

Reconnect every device to the WiFi (toggle airplane mode / reboot) so it gets a
new address from *your* DHCP, then open the dashboard from any device:

```
http://192.168.2.1:8080
```

Default password is **`admin`** — **change it immediately** (Admin tab).

> **Can't reach the dashboard?** A device still holding an old `192.168.1.x`
> lease can't reach `192.168.2.1`. Reconnect it so it re-leases, or open
> `http://192.168.1.110:8080` instead.

**Done.** New devices appear in the dashboard automatically the first time they
join — but as a safety lock they join **disabled**: a brand-new device's user
gets **0 GB and no share of the bundle**, so the device is cut off until you
open its user/device edit modal and assign **Shared** (auto) or **Fixed** GB.
Set each person's allowance from **Add user** and you're running.

### The gateway's addresses (LAN mode)

> **The machine must have a fixed address — don't skip this.** In LAN mode the
> gateway box needs a permanent IP on the router's network. Either **reserve an
> IP on the router** (a DHCP reservation for the machine's MAC) or **set it
> static on the machine itself** (the setup script does this automatically,
> default `192.168.1.110`). If the box's IP can change — a lease expires, the
> box reboots, or the router hands the address to another device — **everyone
> loses access**: devices lose their gateway + DNS and the dashboard becomes
> unreachable.

The gateway box runs on **two fixed addresses**:

- **Uplink** — on the router's subnet, toward the router (default
  `192.168.1.110/24`).
- **Client subnet** — the network it serves (`192.168.2.1/24`). Devices get a
  `192.168.2.x` address from the box's DHCP, and their **gateway + DNS is the
  box**.

Whichever way you fix the box's address, make sure it's one the router's DHCP
pool won't hand to another device (reserve/exclude it on the router, or pick an
address outside the pool). If the box's uplink IP drifts or clashes, devices
lose their gateway and DNS.

The dashboard lives on both addresses: `http://192.168.2.1:8080` from client
devices, or `http://192.168.1.110:8080` from the uplink LAN (useful when a
device still holds an old `192.168.1.x` lease).

### Running from source (developers)

See [Structure_README.md](Structure_README.md) → *Running from source*.

---

## Using the dashboard

| Tab | What it does |
|---|---|
| **Management** | the bundle ring (used / remaining / days left) and a card per **user** — allowance, usage bar, block toggle, top-up, edit, delete — with their devices listed underneath (name, MAC, manufacturer, its own quota bar + up/down split). Each device card also shows **how it's connected**: a **WiFi / LAN** chip (how the device reaches the *router* — the box answers every client's ARP and times the reply; wired answers in well under a millisecond, WiFi pays airtime — plus the box-side NIC tag) and a **presence LED** that goes grey when the device stops answering (asleep / off / on another network), even if it still holds a DHCP lease. A user can be flagged **Exempt from quota** (never quota-blocked, however much they use — manual blocks still work) |
| **Network** | everything about the bundle and the internet path in one place: change `total_gb` / `reset_day`, **Bundle recharged** (add mid-month GB, e.g. an ISP top-up), **Guest mode** (auto-register new devices with a small allowance + a default **guest speed limit** and a **guest limit** — max guest accounts, stops MAC-spoofing spam; lowering the cap also cuts existing over-cap guests — plus a **STOP NEW CONNECTIONS** gate that makes dnsmasq *refuse* brand-new devices), **Reset month now**, the shaping master switch with your **real line down/up rates** + low-latency toggle (caps shape **internet only** — LAN traffic passes through at full speed), **VPN share** (route every device's internet through the VPN the laptop runs, see below), a **Decline random MACs** gate, a **MAC whitelist / blacklist** (whitelisted MACs are never quota-blocked; blacklisted MACs are always blocked — the blacklist wins over the whitelist, `bypass` and manual states; edits apply instantly, no reboot; **deleting a device or user blacklists its MACs permanently** — a deleted device never re-registers while still connected, stays kernel-blocked even without a device row, and comes back only when you remove its MAC from the deny list), and a live bundle/network overview |
| **WAN** | optional "strong" mode where the laptop dials the PPPoE line itself (see below) |
| **Admin** | security & credentials (change the dashboard password), **Software updates** (check for a newer release, see what changed, install it from the dashboard — see below) and **System Info & About** (app, installed version), with the **System Logs** console embedded full-width below — level filter (ALL / INFO / WARNING / ERROR), search, refresh and export, in a scrollable terminal view |
| **DNS** | domain filtering (block / allow / redirect a domain for a user, a device, or everyone; blocklist presets; per-client DNS servers) |
| **History** | what each device is actually visiting: pick a device + a look-back window → its **top domains** (with share %), an **hourly activity** list, and the **most recent queries** (minute buckets) |

The sidebar footer's **eye** toggle masks on-screen sensitive details — MAC
addresses (device rows, rogue rows, the device modal) and the saved PPPoE
credentials (the username AND password fields are cleared while it is on and
re-prefilled from the DB when turned off) — so the dashboard can be shown
without giving away device identities. The preference is remembered; only the
display is masked, nothing is ever lost.

**On a phone?** The whole UI is built for it. The tab bar becomes a swipeable
strip, the bundle ring shrinks and the cards stack to one column, and every
modal/overlay scrolls instead of clipping. The same applies to the household
milestone page and the consumption report — nothing needs a desktop.

**Speed limits per device/user** — set them in the Network tab first (switch ON
and enter your real down/up Mbps), then open a user's or device's **edit** modal
and set `limit down` / `limit up` (`0` = unlimited). Limits apply within seconds.
A default **Guest speed limit** (Network tab → Guest mode) caps the
aggregate bandwidth of every guest account the same way — set `0` (default) to
leave guests unlimited.

**Speed limits shape the internet, not your LAN** — the caps above apply to
**WAN** traffic only. The Network tab's speed section is split into two: **WAN —
internet** (your real down/up rates) and **LAN — internal transfers**
(`set-lan-rate`, the LAN pass-through rate, `0` = the 1000 Mbps default).
Client↔uplink-subnet traffic (the NAS, the router admin page, LAN transfers)
is never throttled by the line rate: it rides a dedicated pass-through class at
the **full LAN link rate** while the WAN cap, the low-latency queues and the
"min(device, user)" maths stay byte-for-byte unchanged. A LAN-rate edit applies
immediately and survives restarts (it is a dashboard setting, not a config
file).

**Decline random MACs** (Network tab → Connection & security) — phones and
laptops that rotate their MAC for privacy carry **no vendor OUI** (the address
is locally-administered), so the box can't identify or budget them. While the
switch is on, a brand-new device with a randomized MAC is **refused at the DHCP
level** — dnsmasq simply never hands it an address, so no device row is even
created. The **"Also cut random-MAC devices already joined"** checkbox runs a
one-shot sweep over the devices already on the network (real-OUI devices are
never touched — the sweep only cuts addresses whose OUI is *not* in the
bundled IEEE registry, so genuine legacy products with locally-administered
MACs are safe).

**STOP NEW CONNECTIONS** (same section) works the same way: while it's on,
dnsmasq refuses brand-new devices outright instead of letting them join and
immediately blocking them. Guests and already-registered devices are
unaffected; turning the gate off clears the refusal list and everyone can join
again.

**Exempt from quota** — a user's **edit** modal has an "Exempt from quota
(unlimited usage)" checkbox: an exempt user is never quota-blocked, however
much they use, while manual blocks still work. The user card shows a
"unlimited" badge. Handy for the box's own VPN relay or an always-on server —
set it and forget it.

**Browsing history per device** — the **History** tab shows the exact domains a
device resolves (top domains, activity by the hour, recent queries). It's
recorded from dnsmasq's own query log (`log-queries=extra`), so nothing on the
box or your DNS is slowed: a background thread tails the log, and the raw file
is bounded by logrotate while the database rows age out by retention
(**7 days by default**; a user's **edit** modal has a "History retention" field
to override per person, and `history.enabled: false` in `config.yaml` stops
recording entirely). dnsmasq only loads the query-log fragment when `conf-dir`
is enabled in `/etc/dnsmasq.conf` — the setup script uncomments or appends it
automatically, so a plain re-run of the setup script is all a stock install needs.

**Domain filtering per device / user / household** — the **DNS** tab blocks,
allows or redirects domains straight from the box's DNS (no new service):
pick a user or device (or *Global*), enter a domain — wildcards work
(`*.youtube.com`) — and an action (**Block**, **Allow**, or **Redirect** to
another IP). Turn on a **blocklist preset** (ads-tracking, social-media,
streaming, gambling) instead and the box fetches + compiles the curated lists
itself. The History tab doubles as a shortcut: every domain row shows its
current status (blocked / allowed / redirected) with one-click "Block this
device" / "Block everyone" / "Allow" buttons. You can also give a user or
device its own **upstream DNS server** (edit modal → DNS server) — e.g. one
with family filtering. Rules apply within seconds (dnsmasq reloads itself,
~1 s blip). Everything is on by default; `dns_filter.enabled: false` in
`config.yaml` turns the whole feature off. One honest limit: a client using
DNS-over-HTTPS/TLS to an outside resolver bypasses it, just as it already
bypasses the box's normal DNS.

---

## Strong (WAN) mode

The default LAN setup has two ways a determined static-IP cheater can slip past
the box (a *static ARP entry*, or a static IP on the uplink subnet). **Strong
(WAN) mode closes them by moving the quota boundary to the line itself**: the
gateway laptop dials the PPPoE session itself (the public IP lands on `ppp0`)
and the router is demoted to a pure **bridge/AP**. A static-IP device then has
**no second router to bypass to** — every byte must cross the box.

It's **off by default**, and the default LAN topology is byte-for-byte unchanged
until you switch. Turn it on only if you need the airtight boundary.

**What changes on the box.** `ppp0` carries the public IP, dnsmasq still serves
the `192.168.2.x` client pool, and the kernel masquerades that subnet out
`ppp0`. The ARP gateway-lock is forced off (no router on the client segment).
The box keeps its old uplink IP as a *secondary alias*, so the **router admin
page stays reachable from every device through the box** — and traffic to that
uplink subnet never consumes the metered bundle (not a bypass: the masquerade
only covers the client subnet).

**Two physical layouts** (pick one):

1. **Single NIC — router in bridge/modem mode (primary).** One cable from the
   box to a router LAN port; switch the router to bridge/modem mode (WAN↔LAN
   bridged, NAT + DHCP off, WiFi kept as an AP if supported). Most Egyptian
   FTTH/DSL combos support bridge (WE ZTE/Huawei, Orange Livebox, Vodafone,
   e&); some ISP-locked combos need a bridge-unlock code or an ISP call.
2. **Two NICs — router in AP mode (universal fallback).** Box NIC1 → ONT
   (fiber) or the modem in bridge (DSL) dials PPPoE; box NIC2 → router in
   **AP mode** (WiFi only, DHCP off). Every router supports AP mode; it costs a
   cheap USB Ethernet dongle. Put the second NIC's name in the panel's *WAN
   interface* field.

**PPPoE credentials** come from the ISP contract card (the same username /
password printed for the router's WAN page) or the router's WAN status page.
They're stored in `/etc/ppp/chap-secrets` + `/etc/ppp/pap-secrets` (not the
world-readable peer file) and prefill in the panel.

**Workflow — all from the WAN tab.** Rewire the router → **Test PPPoE
connection** first (a throwaway dial on `ppp200` that never touches the running
topology; it reports whether the ISP accepts your credentials) → **Apply now**
(the box rewires itself and restarts — a few seconds of internet downtime). To
leave WAN mode: put the router back in routed/NAT mode, then **Revert to LAN**.
The one always-hands-on step is the physical router rewiring — no panel can
move the cable.

**Renewing the public IP (same as restarting the router).** On most Egyptian
lines the ISP hands a fresh public IP to every new PPPoE session — and since
the box dials the line itself, a renewal is now a dashboard action instead of a
router restart. The WAN tab has a **Restart PPPoE — renew public IP** button
(internet drops for a few seconds while the session re-dials) and an
**auto-renew** schedule (`Renew every (min 5)`, default off/15 min) that does
it on a timer — useful when a long-lived IP starts getting throttled or when a
service needs a fresh address. Both are disabled while ppp0 is down (a renewal
needs a working line); the *Last renewed* line shows when it last ran, and the
schedule survives gateway restarts.

**Cases to be aware of:**

- **Applied WAN before the router is actually bridged/AP** — internet is cut
  for everyone until it is. The box itself stays up (no restart into a
  half-applied state) and the WAN tab auto-diagnoses the likely cause.
- **PPPoE outage (ISP side / line)** — no internet for anyone until the line
  redials. The `quota-wan-ppp` service redials automatically
  (`Restart=always`), so this usually clears itself.
- **Wrong credentials** — the Test button reports `auth-failed` before you
  Apply, so you catch it early.
- **A renewal (manual or auto) drops internet for a few seconds** while the
  PPPoE session re-dials — the auto-renew minimum of 5 minutes exists so a typo
  can't hammer the line; a *manual* restart is always your call.
- **The box's own internet is still metered** — the Gateway user /
  `count_gateway` behaviour (see *Known limits*) applies in both topologies.

The architecture behind this is in
[Structure_README.md](Structure_README.md) → *Strong (WAN) mode*.

---

## VPN share (route the household through a VPN)

If you run a VPN client on the gateway laptop (sing-box, xray, WireGuard —
or **v2rayN**, see below), the Network tab's **VPN share** switch sends every
device's internet through that tunnel — the whole household appears at the VPN
provider's IP.

**What stays working:** per-device quota counting + hard blocks (the kernel
`forward` chain) and per-device/per-user speed shaping are untouched; the box's
own DNS/DHCP still serve the LAN, and direct LAN traffic (routers, NAS) never
enters the tunnel. The correct tunnel device is detected automatically and
remembered, so a reboot or a restart of the VPN client re-applies the same one;
anything left over from a crash or tunnel restart is cleaned up by the next
15 s tick.

**v2rayN and other userspace clients (no install needed):** v2rayN's "TUN
mode" is a userspace netstack — a kernel tunnel device never appears, so the
routing engine has nothing to route into. VPN share handles this
automatically: the box **downloads and runs `tun2socks` itself** (one-time,
from the pinned v2.7.0 GitHub release, sha256-verified), auto-detects
v2rayN's local SOCKS listener (default `127.0.0.1:10808`) and bridges it to a
real `tun0` — just flip the switch, nothing to install by hand. **A real
kernel tunnel (xray/sing-box/WireGuard) is always preferred and needs no
config edits** — the tun2socks bridge only engages as a fallback when no
kernel tunnel exists, so the same default config works for every VPN client.
The Network tab shows honest progress/failure messages (e.g. "no VPN SOCKS
proxy found — start the VPN client first") instead of silently retrying.

**How it works under the hood:** one `ip rule` diverts the client subnet into a
dedicated route table whose default route points at the tunnel — cheaper and
more robust than rewriting the masquerade. The traffic rides the box's own
network stack twice (client → tunnel → line), so while VPN share is ON the
relay is never double-charged to the protected Gateway user. **You can even
cut the Gateway's own internet (Gateway OFF) and the household tunnel
survives**: the box keeps ONLY its connection(s) to the VPN server reachable
(learned automatically from the VPN client's sockets every ~15 s, plus any
`engine.gateway_allow_ips` override), so the box itself is offline while every
device still exits at the VPN provider's IP. The feature is off by default
(`vpn_share.enabled: false` means the manager isn't even built).

**What to know:**

- **The tunnel (or the VPN client) must be up first.** Flip the switch, then
  start the VPN client (or start the client, then flip). With a kernel-TUN
  client the rule only lands when the tunnel device actually exists *and*
  carries an IP address; with v2rayN the bridge starts once v2rayN's SOCKS
  listener answers — a missing or address-less tunnel is never routed into
  (that would blackhole the subnet). A momentarily dropped tunnel is harmless:
  the box keeps its route to the VPN server (so it can re-dial) and clients
  fall back to the direct line until the tunnel returns.
- **IPv4 only, like everything else here.** VPN providers' IPv4 TUNs are
  what's routed; there's no IPv6 relay.
- **All devices share the tunnel's bandwidth and fate.** If the VPN drops,
  traffic stays blackholed until the tunnel returns (no silent fallback to the
  direct line — that would silently uncount every byte). 

The design is in
[Structure_README.md](Structure_README.md) → *VPN share*.

---

## Day to day

Open the dashboard and check the **bundle ring** — are you on pace for the
month? Scan the user cards for **Quota exceeded** tags and decide whether to top
up or leave them cut off. Name any new "Unnamed" device (its manufacturer tag
helps tell phones from TVs). That's the whole loop.

**To top up a user mid-month:** user card → *top-up* → enter GB. They're
unblocked instantly if they were cut.

**"How much do I have left?" — the household page.** Any device on the quota
network can open `http://<gateway-ip>:8080/milestone` (no login). It shows that
device's user: their used / allowance, a progress bar, and a **per-device
breakdown** (each device's own GB, with ↑/↓ split). Crossing 50% / 75% / 100%
is flagged once per month on the page (a "new" pill), and acknowledging it is
a one-time click — the flag won't nag again until the period rolls.

**Internal consumption report.** From a whitelisted machine (any device on the
client subnet, or an IP in the `report.allowed_ips` list — see
`config.yaml`), open `http://<gateway-ip>:8080/report` for a read-only,
admin-free view: exact bundle bytes, per-user cards with exact per-device
bytes, recent events and the gateway log tail. Nothing on the box ever opens
it automatically — it's there when you want it, and every other source gets a
403.

---

## Software updates

The Admin tab's **Software updates** card checks the GitHub releases page for
the box (by default every 24 h; disable with the "Check automatically" toggle
or `updates.enabled: false` in `config.yaml`). When a newer version exists a
banner appears across the top of the dashboard — click **Show details** for a
scrollable list of every new version's changelog (a box that's far behind lists
all the intermediate versions). The banner shows once per version.

From the same card you can check now (Check for updates), and **install the
update from the dashboard** — the box downloads the `.deb` and runs the
install behind the scenes (it stays online throughout; the gateway service
restarts once as part of the upgrade, so internet drops for a few seconds).
"Auto-install" does the same automatically whenever a check finds a newer
version. Your config and database are preserved on upgrade (see below).

> The check needs the box to reach `api.github.com`. If it shows "Couldn't
> reach GitHub" (e.g. a timeout), it retries automatically at the next
> interval — verify the box has internet (WAN tab) first.

---

## Upgrading / removing

### apt installs (Method A / C)

```bash
# Upgrade (apt repository): your config + database survive
sudo apt-get update
sudo apt-get install --only-upgrade quota-manager

# ...or download the new .deb and install it (no repository)
# sudo apt install ./quota-manager_<new-version>_all.deb

# Remove (keeps config + database)
sudo apt remove quota-manager

# Remove entirely (also deletes /opt/quota-manager)
sudo apt purge quota-manager
```

### Docker installs (Method B)

```bash
# Upgrade pre-built image
docker compose pull && docker compose up -d

# Stop container
docker compose down

# View logs
docker compose logs -f
```

**Back up** your database occasionally (while the service is stopped) — it holds every device, allowance, and history:
- **apt installs:** `/var/lib/quota-gateway/quota.db`
- **Docker installs:** `./data/quota.db` (or your host mount path)

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Devices have no internet after setup | Router DHCP still on, or wrong client IP | Disable router DHCP (or use the fallback pool); reconnect devices; reboot |
| "nftables engine unavailable" in the log | `nft` missing or not run as root | Install nftables; the service runs as root |
| Devices get DHCP but aren't counted | their gateway isn't the laptop, or the NAT is missing | Check a device's gateway is `192.168.2.1`; verify `nft list table inet quota_nat` |
| Devices use the internet but aren't counted | client IPv6 bypasses the gateway (router hands out RA) | Disable IPv6/RA/DHCPv6 on the router — Quota Manager is IPv4 only |
| No internet with VPN share ON | the VPN client / tunnel isn't running | Start the VPN client first (TUN mode) — the rule only lands once the tunnel exists; check it's the interface shown in the Network tab |
| A domain isn't blocked/redirected | dnsmasq never got the rule, or the client bypasses the box's DNS | DNS tab → the rule's scope matches the device; `tail /var/log/quota-dnsmasq.log`; DoH/DoT clients bypass DNS-layer filtering by design |
| No internet after applying WAN mode | `ppp0` down — wrong credentials, or router not bridged/AP yet | WAN tab: check the ppp0 state + auto-diagnosis; press **Apply now** again; the router must be in bridge/modem (single NIC) or AP (two NIC) mode |
| Restart PPPoE / auto-renew is greyed out | ppp0 is down — a renewal needs a live PPPoE line | Check the ppp0 state first (WAN tab); the internet must be working before a renewal can run |
| Device never appears in the dashboard | dnsmasq lease path wrong | Confirm `dhcp.lease_file` matches dnsmasq's actual lease file |
| History tab shows "No browsing history recorded" | dnsmasq isn't logging queries (`conf-dir=` commented → every `/etc/dnsmasq.d/` fragment ignored), or the app predates the parser fix | Re-run the setup script (it enables `conf-dir`); `tail /var/log/quota-dnsmasq.log` to confirm queries are logged; make sure the app parses the `log-queries=extra` ip/port line shape |
| Dashboard works but nothing is counted | engine disabled, or traffic isn't routed through the laptop | Check the log; verify devices' gateway = the laptop |
| Dashboard only reachable from the laptop | `web.host` is `127.0.0.1` | Set `web.host: 0.0.0.0` |
| Forgot the admin password | — | Stop the app, delete the `admin_password` setting from the DB, restart |
| Bundle shows old values / YAML edit ignored | the bundle was edited in the dashboard (it owns the value now) | Edit from the dashboard, or clear the `bundle_source` setting in the DB |
| Software updates card says "Couldn't reach GitHub" | the box can't reach `api.github.com` (no internet, DNS, or GitHub blocked) | Verify the WAN/internet dot; `curl -m 20 https://api.github.com/repos/UserJoo9/QuotaManager/releases/latest` on the box; the check retries automatically next interval |
| Speed limits don't apply | Network tab never configured (switch off or rates still 0) | Network tab → toggle ON → set your **real** down/up Mbps → Save. A device's own cap is in its edit modal |
| No speed shaping at all | `tc` missing, no `ifb` module, or not root | `apt-get install iproute2`; `modprobe ifb numifbs=1`; run the service as root; re-run the setup script |
| Internet died after a reboot | gateway service not enabled, or rules not persisted | `sudo systemctl enable --now quota-gateway`; re-run the setup script (idempotent) |
| `E: Package 'python3-venv' has no installation candidate` | Fresh box — package lists never downloaded | `sudo apt-get update`, then retry the install |
| `The repository … is not signed` / `NO_PUBKEY ED65462EC8D5E4C5` | Missing Kali signing key on a fresh install | `sudo apt install --reinstall kali-archive-keyring`, then `sudo apt-get update` |
| *"not available, but is referred to by another package"* / "replaced by dnsmasq-base" | Stale lists — the package exists, apt just doesn't know it yet | `sudo apt-get update` and retry |
| *"Target Packages … configured multiple times"* | Duplicate repo lines (`sources.list` + a `sources.list.d` file) | Remove the duplicate `deb … kali-rolling …` line, keep one |

---

## Known limits

- **Counting is approximate** (the dashboard shows "≈") — counters are read
  every ~15 s, so the live split lags slightly.
- **Hard blocks, not throttles.** Exceeded users are cut off (kernel drop);
  speed *caps* exist separately in the Network tab.
- **IPv4 only.** If your router/ISP is dual-stack, WiFi clients may take IPv6
  straight from the router, which never crosses the gateway — uncounted and
  unblockable. Disable IPv6/RA on the router.
- **Single point of failure.** A power cut to the laptop takes down the managed
  network unless the electric-cut fallback pool is set (see Installation step 3).
- **Static-IP bypassers are denied, not magically fixed.** The ARP gateway-lock
  cuts internet to a device that uses the router as its gateway, but a device
  with a *static ARP entry* still evades it. Router-side MAC filtering is the
  durable complement; **Strong (WAN) mode** is the airtight topology.
- **Strong (WAN) mode needs hands-on router work.** The physical rewiring
  (bridge/AP mode) is always manual — no panel can move the cable. A PPPoE
  outage means no internet until the line redials (the service does that
  automatically).
- **VPN share relies on the laptop's VPN client.** It must run in TUN mode
  (kernel tunnel) or as a userspace client with a local SOCKS listener
  (v2rayN — the box auto-bridges it with a downloaded tun2socks) and the
  client must be up — the household's internet is blackholed (deliberately,
  never silently re-routed around the quota) if the tunnel drops. The relay
  doubles the volume crossing the box's own network stack, so buying the VPN
  typically costs you your ISP bundle's data cap spend plus the VPN provider's
  quota. DNS filtering still applies (dnsmasq is untouched by the tunnel).
- **The gateway's own internet is metered** (`engine.count_gateway`, on by
  default). The box's traffic is charged to a protected **Gateway** user with a
  fixed 1.0 GB allowance — a heavy download *on the laptop itself* can cut the
  box's own internet until the Gateway user is topped up or the period rolls
  (clients are unaffected). The 1.0 GB is silently deducted from every
  auto-share bundle the first time the period opens after an upgrade; set
  `count_gateway: false` to skip the counters.
- **Deleting a device or user is permanent until you say otherwise.** A device
  you delete is blacklisted (see the Network tab) and stays **kernel-blocked
  even while connected** — it keeps its DHCP lease but has no internet, no
  row in the dashboard, and no usage is counted for it. Remove its MAC from the
  deny list to let it back in (it re-registers on the next lease tick).
- **Update checks need the box to reach GitHub.** The self-update check dials
  `api.github.com`; a box without internet (or that can't reach GitHub) shows
  a "Couldn't reach GitHub" status and retries at the next interval. The check
  never affects internet, DNS or quota for devices.
- **The household milestone page (`/milestone`) is public** — no login, by
  design. It only ever shows the *requesting device's own user* (resolved by
  its source IP); it never reveals other users' data.
- **The consumption report (`/report`) is gated by source IP, not the admin
  password.** Any device on the client subnet (or in `report.allowed_ips`) can
  open it with no login — it shows the full household usage, recent events and
  the log tail. Keep the box's dashboard port LAN-only; don't port-forward it.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute, with attribution.
Not affiliated with any ISP.
