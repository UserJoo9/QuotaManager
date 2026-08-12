# Quota Manager

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

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
- Serves a **dark-purple glassmorphism dashboard** you can open from any phone on
  the LAN — the whole UI (dashboard, the household milestone page, and the
  consumption report) is phone-friendly and touch-first

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

---

## Table of contents

- [Installation](#installation)
- [After install — the router](#after-install--the-router)
- [Using the dashboard](#using-the-dashboard)
- [Strong (WAN) mode](#strong-wan-mode)
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

**Easiest — install from the apt repository** (one-time key + repo setup, then
upgrades via `apt update && apt upgrade`):

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

**Alternative — install a downloaded `.deb`.** Download the latest
`quota-manager_<version>_all.deb` from the
[Releases](https://github.com/UserJoo9/QuotaManager/releases) page, then:

```bash
sudo apt install ./quota-manager_0.1.2_all.deb
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

Edit the generated config to set your real monthly bundle:

```bash
sudo nano /etc/quota-gateway/config.yaml
```

Change only the two numbers under `bundle` (and optionally add a `timezone`):

```yaml
bundle:
  total_gb: 140.0        # your real monthly bundle, GB
  reset_day: 1           # day of month your ISP resets; 0 = no auto-reset
timezone: ""             # optional IANA zone, e.g. Africa/Cairo
```

Then restart:

```bash
sudo systemctl restart quota-gateway
```

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
join. Set each person's allowance from **Add user** and you're running.

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
| **Management** | the bundle ring (used / remaining / days left) and a card per **user** — allowance, usage bar, block toggle, top-up, edit, delete — with their devices listed underneath (name, MAC, manufacturer, its own quota bar + up/down split) |
| **Bundle settings** | change `total_gb` / `reset_day`, **Bundle recharged** (add mid-month GB, e.g. an ISP top-up), **Guest mode** (auto-register new devices with a small allowance), **Reset month now** |
| **Network** | speed shaping: the master switch, your **real line down/up rates**, low-latency toggle |
| **WAN** | optional "strong" mode where the laptop dials the PPPoE line itself (see below) |
| **History** | what each device is actually visiting: pick a device + a look-back window → its **top domains** (with share %), an **hourly activity** list, and the **most recent queries** (minute buckets) |
| **Admin** | change password, see the installed version |
| **System logs** | the app's log tail, with filters, search, refresh and export |

**On a phone?** The whole UI is built for it. The tab bar becomes a swipeable
strip, the bundle ring shrinks and the cards stack to one column, and every
modal/overlay scrolls instead of clipping. The same applies to the household
milestone page and the consumption report — nothing needs a desktop.

**Speed limits per device/user** — set them in the Network tab first (switch ON
and enter your real down/up Mbps), then open a user's or device's **edit** modal
and set `limit down` / `limit up` (`0` = unlimited). Limits apply within seconds.

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

**Cases to be aware of:**

- **Applied WAN before the router is actually bridged/AP** — internet is cut
  for everyone until it is. The box itself stays up (no restart into a
  half-applied state) and the WAN tab auto-diagnoses the likely cause.
- **PPPoE outage (ISP side / line)** — no internet for anyone until the line
  redials. The `quota-wan-ppp` service redials automatically
  (`Restart=always`), so this usually clears itself.
- **Wrong credentials** — the Test button reports `auth-failed` before you
  Apply, so you catch it early.
- **The box's own internet is still metered** — the Gateway user /
  `count_gateway` behaviour (see *Known limits*) applies in both topologies.

The architecture behind this is in
[Structure_README.md](Structure_README.md) → *Strong (WAN) mode*.

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

## Upgrading / removing

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

**Back up** `/var/lib/quota-gateway/quota.db` occasionally (while the app is
stopped) — it holds every device, allowance and month of usage.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Devices have no internet after setup | Router DHCP still on, or wrong client IP | Disable router DHCP (or use the fallback pool); reconnect devices; reboot |
| "nftables engine unavailable" in the log | `nft` missing or not run as root | Install nftables; the service runs as root |
| Devices get DHCP but aren't counted | their gateway isn't the laptop, or the NAT is missing | Check a device's gateway is `192.168.2.1`; verify `nft list table inet quota_nat` |
| Devices use the internet but aren't counted | client IPv6 bypasses the gateway (router hands out RA) | Disable IPv6/RA/DHCPv6 on the router — Quota Manager is IPv4 only |
| No internet after applying WAN mode | `ppp0` down — wrong credentials, or router not bridged/AP yet | WAN tab: check the ppp0 state + auto-diagnosis; press **Apply now** again; the router must be in bridge/modem (single NIC) or AP (two NIC) mode |
| Device never appears in the dashboard | dnsmasq lease path wrong | Confirm `dhcp.lease_file` matches dnsmasq's actual lease file |
| History tab shows "No browsing history recorded" | dnsmasq isn't logging queries (`conf-dir=` commented → every `/etc/dnsmasq.d/` fragment ignored), or the app predates the parser fix | Re-run the setup script (it enables `conf-dir`); `tail /var/log/quota-dnsmasq.log` to confirm queries are logged; make sure the app parses the `log-queries=extra` ip/port line shape |
| Dashboard works but nothing is counted | engine disabled, or traffic isn't routed through the laptop | Check the log; verify devices' gateway = the laptop |
| Dashboard only reachable from the laptop | `web.host` is `127.0.0.1` | Set `web.host: 0.0.0.0` |
| Forgot the admin password | — | Stop the app, delete the `admin_password` setting from the DB, restart |
| Bundle shows old values / YAML edit ignored | the bundle was edited in the dashboard (it owns the value now) | Edit from the dashboard, or clear the `bundle_source` setting in the DB |
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
- **The gateway's own internet is metered** (`engine.count_gateway`, on by
  default). The box's traffic is charged to a protected **Gateway** user with a
  fixed 1.0 GB allowance — a heavy download *on the laptop itself* can cut the
  box's own internet until the Gateway user is topped up or the period rolls
  (clients are unaffected). The 1.0 GB is silently deducted from every
  auto-share bundle the first time the period opens after an upgrade; set
  `count_gateway: false` to skip the counters.
- **Deleting a guest doesn't cut it mid-session.** A device you delete from the
  dashboard stays online (it keeps its DHCP lease and its internet) until it
  disconnects — while connected it is simply not counted, controlled, or shown.
  It re-registers (or not, per guest mode) the next time it joins.
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
