# Quota Manager

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

Split your metered internet bundle fairly across every device in the house — and cut a device off the moment it runs out.

In countries where DSL/FTTH bundles are metered (e.g. Egypt's 140 GB/month plans, after which you get throttled or surcharged), a household with phones, TVs, laptops and consoles all competing for one connection has no way to *budget* it. **Quota Manager** turns an old laptop (24/7) into a per-device quota gateway:

- Counts exactly how much **every device** consumes each month
- Gives each device its own monthly allowance (fixed GB **or** an equal share of the remaining bundle)
- **Hard-cuts a device's internet** the moment it exceeds its allowance — or whenever you flip its switch off
- Serves a **dark-purple glassmorphism dashboard** you can open from any phone on the LAN

**Target deployment is Linux** (Kali/Debian on an old laptop) — the kernel owns the network path and counts at line rate. A **Windows build is preserved and still supported** as a legacy option.

```
┌──────────────┐   Ethernet    ┌──────────────────────────────────────────────┐
│  ISP Router  │◄─────────────│  Kali/Debian laptop (24/7)                    │
│  WiFi + NAT  │              │ dnsmasq        nftables       web dashboard   │
│  DHCP off    │              │ (DHCP + DNS)   (kernel count + drop)          │
└──────────────┘              │               (FastAPI + WS)                  │
                              └───────▲─────────────────────────────┬─────────┘
                                      │ devices' gateway + DNS      │ every byte
                                ┌─────┴───────┐             ┌───────┴────────┐
                                │  Phones     │             │  TVs           │
                                │  Laptops    │             │  Consoles      │
                                └─────────────┘             └────────────────┘
```

> The router normally has **DHCP off** so the laptop is the only DHCP server. If you
> want devices to keep the internet during a power cut, leave the router a small
> **non-overlapping fallback pool** instead (see [Electric-cut fallback](#electric-cut-fallback)).

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [Using the dashboard](#using-the-dashboard)
- [REST API](#rest-api)
- [Tests](#tests)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)

---

## Features

- **Per-device accounting** — every byte that crosses the gateway is attributed to a device by its IP/MAC, counted in `usage_daily`.
- **Hybrid quota model** — each device is either:
  - **`fixed`** — the admin assigns an exact GB allowance (e.g. "the PS5 gets 30 GB"), or
  - **`auto`** — automatically gets an equal share of whatever is left of the bundle after the fixed allocations.
- **Hard blocking** — an exceeded device is cut off at the packet level (kernel `nftables` drop on Linux, WinDivert drop on Windows). No throttling; it's a clean cut. Re-enabled instantly by a top-up or a toggle.
- **Admin on/off switch** — per-device internet control with one click, independent of quota.
- **Top-ups** — add extra GB to a device mid-month without waiting for the reset.
- **Bundle recharge** — some ISPs let you buy extra data mid-month. Hit **Bundle recharged** in the dashboard, enter the added GB, and every auto device's share is recalculated immediately (no period reset).
- **No-auto-reset mode** — set `reset_day: 0` and the period never rolls on its own; the bundle only grows via "Bundle recharged" (perfect if your ISP resets on an irregular schedule or you recharge mid-month).
- **Electric-cut fallback** — the gateway is a single point of failure. Give the router its own small DHCP pool in a non-overlapping range and devices keep the internet when the laptop is down (see [Electric-cut fallback](#electric-cut-fallback)).
- **Auto-discovery** — new devices that join the network appear in the dashboard automatically (from DHCP leases); name them and assign a quota.
- **Monthly period logic** — auto-rolls at the configured reset day (or never, with `reset_day: 0`), keeps per-day history, shows days remaining.
- **Live dashboard** — WebSocket push (5 s snapshots) with a bundle progress ring, per-device quota bars, up/down split, usage chart (Chart.js, bundled offline), and an activity feed.
- **Single admin auth** — PBKDF2-hashed password, signed session cookie. Change it from the dashboard.
- **Graceful degradation** — if a subsystem can't start (no root, no `nft`, no Npcap), the rest keeps working and the dashboard still shows stored usage.

---

## How it works

### Topology: a one-armed gateway

The router keeps its WiFi and NAT — devices join the same SSID as always. The only changes:

1. **The router's DHCP is disabled** (or restricted to a non-overlapping fallback pool) and the gateway runs its own DHCP server instead.
2. The gateway keeps a **static uplink IP** on the router's LAN subnet (e.g. `192.168.1.110`) **and adds a client-subnet alias** (`192.168.2.1`) on the same NIC.
3. Its DHCP hands devices addresses from a **separate client subnet** (e.g. `192.168.2.100–200`) with **their default gateway and DNS set to the gateway**.

Every device routes through the gateway, so it sees every byte. The gateway's kernel IP forwarding + masquerade NAT moves packets on to the router.

**Why a separate subnet + NAT?** The kernel's proxy-ARP refuses same-subnet targets, so on the old one-armed layout the router could deliver return traffic straight to a device, silently bypassing the gateway. Putting clients on their own subnet (`192.168.2.0/24`) that the kernel **masquerades** out the uplink makes every byte deterministically cross the laptop — no proxy-ARP needed.

### Linux (target): the kernel owns the network

Setup is done once by `scripts/setup_gateway_kali.sh`, which configures `ip_forward=1` + IPv6 off, a static uplink IP **and** a client-subnet alias (`192.168.2.1`) on the NIC, dnsmasq, and the nftables NAT ruleset (`inet quota_nat`). The app's own table (`inet quota_gateway`) is created by `run.py` at startup.

- **dnsmasq** serves both DHCP (udp/67) and DNS (udp/53) for a `192.168.2.x` pool (gateway + DNS = the laptop) — one daemon replaces the whole Windows userspace stack, and there is no ICS-style port-53 war.
- **Kernel NAT** (`inet quota_nat`) masquerades `192.168.2.0/24` out the uplink — no proxy-ARP, no scapy/Npcap.
- **`quota/nftables.py`** programs one named counter pair per device (`q_up_<ip>` / `q_down_<ip>`, dots→underscores) in the `forward` chain, plus a `blocked` set that two drop rules reference. The **kernel counts and drops at line rate** — no Python in the packet path. The app only reconciles rules and reads `nft -j list counters` (JSON) every ~15 s.
- Device bindings come from **dnsmasq's lease file** (`dhcp.lease_file`, default `/var/lib/misc/dnsmasq.leases`) — new devices are auto-registered into the dashboard.

### Windows (legacy): the userspace stack

`quota/dhcp.py` (udp/67) + `quota/dns.py` (udp/53) + `quota/arp.py` (scapy proxy-ARP) replace dnsmasq; `quota/engine.py` (WinDivert via `pydivert`, dedicated thread) replaces nftables.

- Diverts only packets touching the **DHCP pool** (filter scoped to `dhcp.pool_start…pool_end`), so the PC's own traffic and DHCP broadcasts are never captured or re-injected.
- Attributes bytes to a device by IP → in-memory counters (`up`/`down`).
- For a **blocked** device, it simply doesn't re-inject the packet — WinDivert drops anything not re-injected. That's the hard internet cut.
- Counts only **one direction** of a forwarded packet (default `inbound`) so kernel-routed traffic isn't double-counted.
- Zero locks and zero logging in the hot path; counters are swapped out atomically every maintenance tick.

> If you return to Windows, the `SharedAccess` service (ICS) was left disabled — restore it and re-enable ICS for that build.

### The maintenance loop

Every ~15 s (`run.py` → `Gateway._maintenance_loop`):

1. Rolls the quota period if it's stale (month boundary).
2. (Linux) learns device bindings from dnsmasq's lease file.
3. Drains the engine's counter deltas into `usage_daily`.
4. Re-evaluates every device's block state from usage vs. allowance.
5. Pushes fresh IP→MAC / blocked maps into the engine and the snapshot holder (the flushed deltas are what the dashboard shows as "live" up/down).

### Quota math

```
fixed_total  = Σ fixed_gb of all fixed-mode devices
remaining    = max(0, bundle.total_gb − fixed_total)
auto_share   = remaining / number of auto-mode devices

allowance(i) = fixed_gb(i)   if mode = fixed
             = auto_share    if mode = auto

blocked(i)   = used_gb(i) ≥ allowance(i)   OR   admin switched it off
```

---

## Requirements

### Linux (target)

| Component | Requirement | Why |
|---|---|---|
| **OS** | Kali / Debian (or any systemd Linux with nftables + dnsmasq) | kernel owns the network path |
| **Hardware** | An old laptop with **one wired Ethernet port**, powered 24/7 | cheap; that's the whole point |
| **Python** | 3.11+ (3.10–3.14 supported) | runtime |
| **Privileges** | **root** on the gateway | nftables + dnsmasq (udp/53 + udp/67) |
| **Network** | A static uplink IP on the router's LAN **and** a client-subnet alias (`192.168.2.1`) on the laptop | the alias is the devices' gateway |
| **Router** | DHCP disabled (or a non-overlapping fallback pool), WiFi/NAT kept | so our DHCP + gateway handoff take effect |

Dependencies (pinned in `requirements-linux.txt`): `fastapi`, `uvicorn[standard]`, `aiosqlite`, `PyYAML` (+ `pytest`/`httpx`/`pyflakes` for tests & lint). No pydivert, no scapy — they are Windows-only.

### Windows (legacy)

| Component | Requirement | Why |
|---|---|---|
| **OS** | Windows 10/11 (64-bit) | WinDivert, DHCP, Npcap |
| **Privileges** | **Administrator** | pydivert (WinDivert driver) + DHCP on UDP port 67 |
| **Npcap** | Optional but **recommended** | raw L2 socket for proxy-ARP (scapy). *Without it, download is under-counted.* |

Dependencies (pinned in `requirements.txt`): `fastapi`, `uvicorn[standard]`, `pydivert`, `aiosqlite`, `PyYAML`, `scapy`, `tzdata`.

---

## Installation

Work through the Linux steps in order. Every path and IP below is the default the setup script bakes in; only Step 6 asks you to change anything.

### Linux (target) — step-by-step

**Step 1 — Prerequisites**

- An old laptop with **one wired Ethernet port** (if it only has WiFi, you need a USB→Ethernet adapter) — it becomes the 24/7 gateway.
- **Kali or Debian** installed and bootable, connected to the router by cable, and **currently able to reach the internet**. The router's DHCP must still be ON at this point: the setup script in Step 4 runs `apt-get install` and needs the network.
- Your router keeps **WiFi + NAT on** (you only disable its DHCP in Step 5).
- Devices to manage: phones, TVs, laptops — they join the router's WiFi as always.

**Step 2 — Get the project onto the laptop**

```bash
cd ~
git clone <your-repo-url> QuotaManager
cd QuotaManager
```

(You can also just copy the project folder anywhere on the laptop — the setup script auto-detects the repo root. If you copy the folder instead of cloning, run Steps 3 and the Step 7 foreground command from inside that folder — wherever it is — since the venv and the systemd unit are rooted at the project folder.)

**Step 3 — Create the virtualenv and install dependencies FIRST**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-linux.txt
```

> **This ordering is critical.** The setup script in Step 4 writes a systemd unit whose `ExecStart` points at `.venv/bin/python3` — but **only if the venv already exists at that moment**. If you run the setup script first, the service falls back to the system `python3`, which doesn't have the app's dependencies and fails to start. If you already ran the script without a venv: create the venv now, then simply **re-run** the script (Step 4) — it is idempotent and will pick up the venv.

> **Fresh Kali/Debian note:** if `python3 -m venv .venv` errors with *"ensurepip is not available"*, install the `venv` module first (Kali/Debian splits it out of the python3 package): `sudo apt-get update && sudo apt-get install -y python3-venv`.

`requirements-linux.txt` is the Linux dep file (FastAPI, uvicorn, aiosqlite, PyYAML + pytest/httpx for tests). Do **not** use `requirements.txt` on Linux — it contains Windows-only packages (pydivert/scapy) and will fail to install.

**Step 4 — Run the setup script**

```bash
sudo bash scripts/setup_gateway_kali.sh
```

Run it **as root** (Kali's default user is already root, but this works either way). It configures the whole network stack, idempotently:

- **Kernel forwarding + IPv6 off** — `ip_forward=1`, IPv6 disabled, persisted in `/etc/sysctl.d/99-quota-gateway.conf`.
- **Installs dnsmasq + nftables** via `apt-get`.
- **Static IPs on the wired NIC** — the laptop's uplink `192.168.1.110/24` (gateway `192.168.1.1`) *and* the client-subnet alias `192.168.2.1/24`, set through NetworkManager (`nmcli`) or the ifupdown fallback.
- **dnsmasq (DHCP + DNS)** — writes `/etc/dnsmasq.d/quota-gateway.conf`: hands devices `192.168.2.100–200` with **gateway + DNS = `192.168.2.1`** (options 3/6), forwards DNS upstream to the router + `8.8.8.8` (two resolvers so one blocked/filtered resolver doesn't kill all client DNS), logs leases to `/var/lib/misc/dnsmasq.leases`. It validates the config (`dnsmasq --test`) then restarts.
- **nftables NAT** — writes `/etc/quota-gateway/nftables.gateway.nft` (symlinked to `/etc/nftables.conf`): the table `inet quota_nat` masquerades `192.168.2.0/24` out the uplink. The app's accounting/block table `inet quota_gateway` is **created by run.py itself at startup** — the script never touches it.
- **App config + dirs** — creates `/var/lib/quota-gateway` and `/var/log/quota-gateway`, and writes `/etc/quota-gateway/config-linux.yaml` (you edit it in Step 6).
- **systemd unit** — writes `/etc/systemd/system/quota-gateway.service` (auto-start, `Restart=always`), runs `daemon-reload`, and **enables** it. It does **not** start it — that's Step 7.

Notes:

- **Idempotent** — safe to re-run anytime to re-apply settings.
- It **refuses to run while the app is live** (it must not reconfigure the network under a running gateway) — stop it first with `systemctl stop quota-gateway`.
- All the IPs above are defaults; if your LAN differs, set these as environment variables before running the script: `WAN_GATEWAY`, `LAN_IP`, `LAN_CIDR`, `CLIENT_IP`, `CLIENT_NET`, `POOL_START`, `POOL_END`, `UPSTREAM_DNS`, `SUBNET_MASK` (if your LAN is not /24), `LEASE_HOURS` (dnsmasq's DHCP lease length in hours — set `1` for the electric-cut fallback) — and `LAN_IF` if auto-detection picks the wrong NIC (`APP_DIR` overrides the project folder the setup script auto-detects).

**Step 5 — On the router: disable DHCP (keep WiFi + NAT on)**

Log into the router admin page (usually `http://192.168.1.1`), find the DHCP server / LAN settings, and switch DHCP **off**. Keep WiFi (same SSID/password) and NAT on — devices still join the router's WiFi, but now get their IP, gateway and DNS from the laptop.

While you're in the router admin page, **also disable IPv6 / Router Advertisement (RA) on the WiFi and LAN**. Quota Manager is **IPv4 only** — disabling IPv6 on the laptop's own NIC does *not* stop the router handing IPv6 out on its WiFi. If the router/ISP is dual-stack, clients get an IPv6 default route via the router and their IPv6 traffic never crosses the laptop: it is neither counted nor blockable. If the router cannot disable IPv6, accept that IPv6-using apps bypass the quota.

**Optional — electric-cut fallback.** The gateway is a single point of failure. If you want devices to keep the internet while the laptop is off, give the **router** a small DHCP pool on the *uplink* subnet instead of turning DHCP fully off — e.g. `192.168.1.201–250`, gateway `192.168.1.1`. The laptop's dnsmasq only serves `192.168.2.x`, so the pools never overlap by construction. (Trade-off: while a device holds a fallback lease it isn't counted/controlled — see [Electric-cut fallback](#electric-cut-fallback).)

**Step 6 — Edit the generated config**

```bash
sudo nano /etc/quota-gateway/config-linux.yaml
```

This is an **edit-in-place** — do **not** replace the file with a new one. In the generated file, change only the two numbers under `bundle` (and optionally add the `timezone:` line); keep every other line (`db_path`, `log_file`, `dhcp.*`, `engine.backend: nftables`, `web.*`) exactly as generated.

```yaml
bundle:
  total_gb: 140.0        # your real monthly bundle, GB
  reset_day: 1           # day of month your ISP resets; 0 = no auto-reset
timezone: ""             # optional IANA zone, e.g. Africa/Cairo; empty = system local time
```

Notes:

- **On the Linux build the `dhcp.*` keys in this YAML are informational.** dnsmasq is what actually serves the pool and hands devices their gateway/DNS (options 3/6), from `/etc/dnsmasq.d/quota-gateway.conf` — a file the setup script writes from its own env vars, not from this YAML. The only `dhcp.*` keys run.py reads on Linux are `lease_file` and `lease_hours`, and the generated file already has both correct. To actually change the pool or the gateway/DNS devices receive, set the setup script's `CLIENT_IP` / `POOL_START` / `POOL_END` env vars and re-run it (Step 4). Leave the generated values (`192.168.2.1` / `192.168.2.100` / `192.168.2.200`) as-is.
- The service hasn't started yet, and `bundle` values are re-applied from the YAML on every boot anyway, so no restart is needed.

**Step 7 — Start the gateway**

```bash
sudo systemctl daemon-reload
sudo systemctl enable quota-gateway
sudo systemctl start quota-gateway
```

(The setup script already enabled the service — re-running `enable` is harmless.) Watch it come up:

```bash
journalctl -u quota-gateway -f
```

You should see:

```
database ready: /var/lib/quota-gateway/quota.db
nftables engine ready: table inet.quota_gateway (forward chain, blocked set, per-device counters)
```

**Foreground run (optional, for a first look).** Run **EITHER** the service above (recommended) **OR** the foreground command — **not both**. If you already started the service, stop it first: `sudo systemctl stop quota-gateway`. Then, as root, from the project directory (that's where the venv lives):

```bash
.venv/bin/python run.py --config /etc/quota-gateway/config-linux.yaml
```

**Step 8 — First login**

Before you open the dashboard, one reachability note: a device still holding an **old `192.168.1.x` lease cannot reach `192.168.2.1`** — the router doesn't route `192.168.2.0/24`, so the packet is forwarded to the ISP and dies. If the device you'll log in from is in that state, reconnect it to the WiFi (or toggle airplane mode / reboot it) so it re-leases onto the client subnet first — or, while it still holds the old lease, open `http://192.168.1.110:8080` instead (same subnet as the laptop, reachable directly).

Once it's on the client subnet, open:

```
http://192.168.2.1:8080
```

The default password is **`admin`** (seeded on first boot). **Change it immediately**: Settings → **Change password** (it asks for the current password).

**To seed a different default password** (rather than `admin`), it must be set in the *service's* environment **before the first start** — a plain `export` in your shell won't reach it. Add `Environment=QUOTA_ADMIN_PASSWORD=...` to the `[Service]` section of `/etc/systemd/system/quota-gateway.service`, then `sudo systemctl daemon-reload`. After the first boot it's too late — `admin` is already hashed into the database; reset it via the Troubleshooting "Forgot the admin password" row below.

**Step 9 — Verify**

Reconnect every device to the WiFi (or toggle airplane mode / reboot it) so it asks for a new address from *your* dnsmasq. Each device lands in `192.168.2.100–200` and **auto-registers** in the dashboard (bindings come from dnsmasq's lease file `/var/lib/misc/dnsmasq.leases`; you'll see "New device on network" events).

Optionally confirm the kernel rules (as root):

```bash
nft list table inet quota_nat       # the masquerade that moves client traffic out the uplink
nft list table inet quota_gateway   # the app's per-device counters + the 'blocked' set
```

**Anything wrong?** The [Troubleshooting](#troubleshooting) table at the bottom of this README covers the common failure modes (devices with no internet after setup, missing counters, DHCP lease path, service not restarting after reboot, and more).

### Windows (legacy)

```powershell
cd QuotaManager
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt

# (Recommended) verify your Python is 64-bit — pydivert needs it
python -c "import struct; print(struct.calcsize('P') * 8, 'bit')"
```

Then run `run.py` **as Administrator** (pydivert + DHCP need it), and complete the one-time network setup as Administrator:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_gateway.ps1
```

That enables kernel IP forwarding (`IPEnableRouter = 1`), adds firewall allow rules, prints the PC's IPs/MACs, and checks Npcap — **reboot afterwards** (forwarding takes effect on boot). On the router: disable its DHCP (or leave it a small non-overlapping fallback pool — see [Electric-cut fallback](#electric-cut-fallback)) and give the laptop a static/reserved IP on the router's LAN subnet (e.g. `192.168.1.110`) — its uplink.

---

## Configuration

Everything lives in a YAML file — **`config-linux.yaml`** on Linux, **`config.yaml`** on Windows (all values optional; defaults shown). Runtime override: `python run.py --config path/to/config.yaml`.

> **How bundle settings flow:** `config.yaml` is the **default source of truth** for `bundle.total_gb` / `bundle.reset_day` and is **re-applied on every startup**, so editing the YAML reaches the dashboard (this was a bug — the YAML only seeded on the very first boot). Once you edit the bundle or recharge it from the dashboard's *Bundle settings* form, a `bundle_source` setting flips to `dashboard` and config.yaml stops overriding — so a UI edit or a recharge survives a restart, and a YAML edit actually shows up until then.

```yaml
db_path: data/quota.db        # SQLite database location
log_file: logs/quota.log      # rotating log file
log_level: INFO               # DEBUG for more detail (never in the packet path)

timezone: Africa/Cairo        # IANA zone for period math; empty => local time

bundle:
  total_gb: 140.0             # your monthly ISP bundle, GB
  reset_day: 1                # day of month your ISP resets the bundle (1..28)
                              # 0 = never auto-reset (see "No-auto-reset mode" below)

web:
  host: 0.0.0.0               # listen on all interfaces (reachable from the LAN)
  port: 8080

dhcp:
  enable: true                # DHCP server (disable router DHCP first!)
  gateway_ip: 192.168.2.1     # THE CLIENT-SUBNET ALIAS — what devices get as gateway
  router_ip: 192.168.1.1      # upstream router (reference / DNS option only)
  dns_servers: [192.168.1.1, 8.8.8.8]
  subnet: 255.255.255.0
  pool_start: 192.168.2.100   # first address handed to devices
  pool_end: 192.168.2.200     # last address handed to devices
  lease_hours: 24             # lower (e.g. 1) = devices recover faster after a power cut
  lease_file: /var/lib/misc/dnsmasq.leases   # Linux: dnsmasq's lease file (auto-discovery)
  # Electric-cut fallback (optional) — see the section below:
  # fallback_enabled: true
  # fallback_pool_start: 192.168.1.201
  # fallback_pool_end: 192.168.1.250

engine:
  enabled: true               # accounting + hard blocking
  backend: auto               # auto = nftables on Linux / windivert on Windows;
                              #        or force one explicitly
  count_direction: inbound    # inbound | outbound — avoids double-counting routed traffic
  table: quota_gateway        # nftables table (Linux only)

arp:
  enabled: false              # proxy-ARP is NOT used on Linux — clients are on
                              # their own subnet (192.168.2.0/24). Windows needs it.
  interface: ""               # empty => scapy auto-picks the LAN interface
  announce_interval_sec: 60
```

> **`gateway_ip` is the most important field on Windows builds.** It must be the **gateway's own static IP**. If clients get the wrong gateway (e.g. the router), their traffic never crosses the gateway and can't be counted or blocked. On **Linux** this key is informational — dnsmasq actually hands devices their gateway/DNS from the setup script's `CLIENT_IP` env var (see [Installation → Step 6](#installation)); keep the generated value as-is.

### No-auto-reset mode (`reset_day: 0`)

Some ISPs don't reset on a fixed day of the month — or you buy extra data mid-month. With `reset_day: 0`:

- The quota period is opened **once** and **never rolls over** automatically.
- The bundle only changes when you click **Bundle recharged** in the dashboard (enter how many GB you added; every auto device's share is recomputed instantly, and `period_start` is preserved).
- To start a genuinely new month, use **Settings → Reset month now** manually.

The dashboard shows `—` for "days left" and the period shows `→ manual` when no auto-reset is set.

### Electric-cut fallback

The gateway is the single point of failure: if the power goes out, devices lose their gateway and the internet. To keep them online, the **router** takes over during the outage:

1. **Leave the router's DHCP on.** Give it a small **non-overlapping** pool (e.g. `192.168.1.201–250`) with its normal gateway. Because the two pools don't overlap, there's never an IP conflict.
2. On Windows, set the matching range in `config.yaml` (at startup Quota Manager **validates the ranges don't overlap** and refuses to start if they do). On Linux, set dnsmasq's range manually in the setup script.
3. **Keep leases short.** Set a short lease on the router, and on Linux re-run the setup script with `LEASE_HOURS=1` (that becomes dnsmasq's DHCP lease length — `lease_hours` in config-linux.yaml is informational on Linux and does not change what dnsmasq hands out). Then when the gateway comes back, devices renew quickly and return to its pool (and quota enforcement).

**The honest trade-off:** while a device holds a fallback lease (gateway down), it is **not counted or controlled** — quota enforcement is suspended for the whole LAN during an outage. That's the price of keeping devices online. When the gateway recovers, devices re-join the managed pool as their leases renew or they reconnect.

---

## Running

### Linux (target)

```bash
# As root:
.venv/bin/python run.py --config config-linux.yaml

# Options
.venv/bin/python run.py --config /etc/quota-gateway/config-linux.yaml
.venv/bin/python run.py --port 9000      # override web port
.venv/bin/python run.py --debug          # DEBUG logging
```

**First login:** open `http://<gateway-ip>:8080` from a device on the client subnet. The default password is `admin` (change it immediately in **Settings → Change password**). To seed a different default password, add `Environment=QUOTA_ADMIN_PASSWORD=...` to the `[Service]` section of `/etc/systemd/system/quota-gateway.service` **before the first start** (then `sudo systemctl daemon-reload`) — a plain `export` won't reach the service, and after first boot `admin` is already hashed in the DB.

### Windows (legacy)

```powershell
.\.venv\Scripts\python.exe run.py        # must run as Administrator
```

### Keeping it running 24/7

Quota Manager only works if the gateway is always on.

- **Linux:** the setup script already writes and enables `quota-gateway.service` (auto-start on boot, `Restart=always`) — just run `systemctl start quota-gateway` (or `systemctl enable --now quota-gateway`).
- **Windows:** a **Scheduled Task** at logon, or **NSSM** to run it as a Windows service. Configure **Active Hours** so Windows Update doesn't reboot mid-month, and set the power plan to *Never sleep*.

**Back it up:** `data/quota.db` (Linux: `/var/lib/quota-gateway/quota.db`) holds every device, allowance and month of usage — copy it occasionally while the app is stopped.

---

## Using the dashboard

| Area | What it does |
|---|---|
| **Bundle ring** | used vs. total for the month, remaining GB, period dates, days left, device count, blocked count |
| **Devices** | one glass card per device: name, MAC, IP, quota badge (`Active` / `Quota exceeded` / `Blocked by admin`), progress bar toward its allowance, up/down since last update, **block toggle**, **edit / top-up**, **delete** |
| **Add device** | register a MAC manually (new devices also appear automatically from DHCP leases) |
| **Usage chart** | stacked daily download/upload bars for the current period |
| **Bundle settings** | change `total_gb` / `reset_day` (takes effect for the period), **Bundle recharged**, **Reset month now**, **Change password** |
| **Activity** | audit feed: new devices, blocks, top-ups, bundle recharges, config changes |

**Top-up** adds GB to a device's allowance and instantly unblocks it if it was quota-blocked. **Bundle recharged** adds GB to the whole bundle and recomputes every auto device's share — the same flow as a mid-month ISP top-up.

**Day to day:** open the dashboard and check the bundle ring — are you on pace for the month? Scan the device list for **Quota exceeded** badges and decide whether to top up or leave them cut off. Name any new "Unnamed" device. Glance at **Activity** for anything unexpected. That's the whole loop — the heavy lifting (counting, blocking, month math) is automatic; your only ongoing job is naming devices, assigning quotas, and deciding who gets topped up.

---

## REST API

Authenticate with `POST /api/login` (JSON `{"password": "..."}`) → the server sets a session cookie. The dashboard client uses the same endpoints.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dashboard` | full bundle + devices + usage snapshot |
| GET/POST/PATCH/DELETE | `/api/devices` & `/api/devices/{id}` | list / create / update / delete devices |
| POST | `/api/devices/{id}/topup` | add GB, clears quota block (`{"extra_gb": 20}`) |
| GET | `/api/usage/{id}` · `/api/usage` | daily usage series (chart data) |
| GET | `/api/events?limit=30` | activity feed |
| GET/POST | `/api/bundle` | read / update bundle (`total_gb`, `reset_day`, or `add_gb` to recharge the bundle mid-month). A POST makes the dashboard the bundle owner (`bundle_source=dashboard`) |
| POST | `/api/reset-month` | force an early period roll-over |
| POST | `/api/login` · `/api/logout` | session auth |
| GET | `/api/me` | session check |
| POST | `/api/password` | change admin password |
| WS | `/ws` | pushes `{"type":"snapshot","data":{...}}` every 5 s |

Interactive docs: `http://<gateway-ip>:8080/api/docs` (Swagger UI).

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q      # full suite
.venv/bin/python -m pyflakes run.py core quota api tests   # lint
```

The suite is **83 tests** covering quota math (period bounds, allowance model, blocks, top-ups, reset-day-0, bundle recharge), API integration (including session-gated password change, bundle ownership via `bundle_source`, 400-on-wrong-password), static UI serving, DHCP pool logic + reserved fallback range, DNS forwarder relay, electric-cut fallback validation, the **nftables engine** (against a fake `nft` binary — 11 tests), and `run.py` gateway wiring (config.yaml → DB bundle reconcile on every boot, dnsmasq lease sync, live-counter regression). All subsystems that need hardware/root (nftables, pydivert, DHCP, Npcap) are simulated or disabled in tests, so they run on any machine without admin.

---

## Project structure

```
QuotaManager/
├── CLAUDE.md                 # SYSTEM MAP (architecture, flow, known limits)
├── README.md                 # this file
├── LICENSE                   # MIT license
├── config.yaml               # Windows gateway settings
├── config-linux.yaml         # Linux gateway settings (dnsmasq + nftables)
├── run.py                    # gateway wiring: engine + maintenance + uvicorn
├── requirements.txt          # Windows deps (pydivert, scapy)
├── requirements-linux.txt    # Linux deps (no pydivert/scapy)
├── scripts/
│   ├── setup_gateway.ps1     # Windows: IPEnableRouter, firewall, info report
│   └── setup_gateway_kali.sh # Linux: sysctl, dnsmasq, nftables base, info
├── core/
│   ├── config.py             # config.yaml -> typed Config dataclasses
│   ├── logging_setup.py      # non-blocking QueueHandler -> writer thread -> rotating file
│   └── timeutil.py           # month-boundary math (zoneinfo)
├── quota/
│   ├── db.py                 # SQLite schema + async access (aiosqlite)
│   ├── service.py            # hybrid quota math, blocks, top-ups, bundle recharge,
│   │                         #   reset_day=0 (manual), period roll
│   ├── engine.py             # PacketEngine thread (Windows): count + drop (pydivert)
│   ├── nftables.py           # NftablesEngine (Linux): kernel counters + block set
│   ├── dhcp.py               # Windows DHCP server (udp/67) -> MAC/IP learning,
│   │                         #   never hands out the router's fallback range
│   ├── dns.py                # Windows DNS forwarder (udp/53) -> relay client queries
│   └── arp.py                # Windows proxy-ARP responder (scapy) -> return traffic
├── api/
│   ├── app.py                # FastAPI factory: REST + /ws + static mount
│   └── schemas.py            # pydantic request models
├── web/
│   ├── index.html            # login + dashboard + modals
│   └── assets/
│       ├── styles.css        # dark purple glassmorphism
│       ├── app.js            # WS client, dashboard render, device controls
│       └── chart.umd.js      # Chart.js 4.5.0 bundled offline
└── tests/
    ├── test_quota_service.py # period math, allowance math, blocks, recharge
    ├── test_api.py           # REST API integration (incl. recharge, reset-day-0,
    │                         #   bundle_source ownership)
    ├── test_web_ui.py        # static UI served
    ├── test_dhcp.py          # DHCP pool allocation + reserved fallback range
    ├── test_dns.py           # DNS forwarder relay (upstream stub, no admin)
    ├── test_fallback_wiring.py # electric-cut fallback config validation
    ├── test_nftables.py      # NftablesEngine vs a fake `nft` binary
    └── test_run_wiring.py    # run.py wiring + live boot + bundle reconcile +
                              #   dnsmasq lease sync + live-counter regression
```

Dependencies point downward only: `api → quota/core`, `quota → core`. The engine communicates with the asyncio side through thread-safe counter snapshots — no locks in the packet hot path. On Linux the hot path has **no Python at all** — the kernel counts and drops.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Devices have no internet after setup | Router DHCP still on (and no fallback pool), or `gateway_ip` wrong (Windows) / setup-script `CLIENT_IP` wrong (Linux) | Disable router DHCP (or give it a non-overlapping fallback pool); set `gateway_ip` (Windows) or the setup script's `CLIENT_IP` (Linux) to the **gateway's** IP; reboot |
| "nftables engine unavailable" in the log | `nft` missing, or not run as root | Install nftables; run `run.py` as root; check `nft --version` |
| "nft add failed ... Operation not permitted" | no root / no CAP_NET_ADMIN | Run as root |
| Devices get DHCP but their traffic isn't counted (Linux) | devices aren't on the client subnet, the NAT/masquerade is missing, or they bypass the laptop | Verify devices' gateway is `192.168.2.1` (the laptop's client-subnet alias); check the masquerade with `nft list table inet quota_nat` and the counters with `nft list table inet quota_gateway` |
| Devices use the internet but aren't counted (dual-stack router) | client IPv6 bypasses the gateway (RA straight from the router) | Disable IPv6/RA/DHCPv6 on the router for the LAN — Quota Manager is IPv4 only |
| Device binding never appears (Linux) | dnsmasq lease file path wrong | Confirm `dhcp.lease_file` matches dnsmasq's actual file (`/var/lib/misc/dnsmasq.leases`); `dhcp-leasefile` in `/etc/dnsmasq.d/quota-gateway.conf` |
| "pydivert not installed — packet engine unavailable" in the log (Windows) | pydivert missing, or not run as admin | `pip install -r requirements.txt`; launch `run.py` as Administrator |
| "proxy-ARP disabled" in the log (Windows) | Npcap not installed, or no admin | Install Npcap; run as admin (download is then under-counted until fixed) |
| "DHCP server failed to start: binding udp/67 requires Administrator privileges" | Not elevated | Run as Administrator (UDP 67 needs admin) |
| No counting at all, but dashboard works | `engine.enabled: false`, engine unavailable, or traffic isn't routed through the gateway | Check the log; verify devices' gateway = gateway; enable the engine |
| Dashboard reachable only on the gateway | `web.host` is `127.0.0.1` | Set `web.host: 0.0.0.0` |
| Forgot the admin password | — | Stop the app and clear the `admin_password` value in the DB, or set `QUOTA_ADMIN_PASSWORD` and delete the `settings` row — it is re-created on next boot |
| Bundle shows old values / YAML edit ignored | `bundle_source` is `dashboard` (you edited the bundle in the UI) | Either edit from the dashboard, or clear the setting: `sudo sqlite3 /var/lib/quota-gateway/quota.db "DELETE FROM settings WHERE key='bundle_source';"` — config.yaml takes over again on next boot |
| Counters reset to 0 after reboot | Expected — engine counters are in-memory | DB history persists; only live since-boot counters reset |
| Gateway rebooted overnight and the internet died (Linux) | `quota-gateway.service` not started/enabled, or dnsmasq/nftables rules not persisted | `systemctl enable --now quota-gateway`, and re-run `scripts/setup_gateway_kali.sh` (idempotent) to re-apply sysctl/dnsmasq/nftables |
| Windows rebooted overnight and the gateway died (legacy) | Windows Update / power plan | Run as a Scheduled Task or NSSM service; configure Active Hours; disable sleep |
| Devices keep the internet but aren't counted/controlled after a power cut | They're holding a router **fallback** lease | Expected — quota enforcement is suspended while the gateway is down. Devices return to the gateway's pool as their leases renew (keep leases short) or when they reconnect |

---

## Known limits

- **Throughput.** On Windows, per-packet Python accounting tops out well below 1 Gbps — fine for home DSL/FTTH (≤100 Mbps). On Linux, nftables counts at line rate (kernel-side), so this ceiling is a Windows-only concern. Counting is approximate (the UI shows "≈") either way.
- **No throttling.** Exceeded devices are **hard-blocked** (kernel drop on Linux, WinDivert drop on Windows) — there's no per-device bandwidth shaping.
- **Privilege.** Linux needs root (nftables + dnsmasq); Windows needs Administrator (pydivert + DHCP) and ideally Npcap (proxy-ARP). Without the engine, counting stops but the dashboard still shows DB usage.
- **IPv4 only.** The engine/accounting is scoped to IPv4 for v1.
- **Single point of failure.** A power cut to the gateway takes down the managed network unless the electric-cut fallback pool is configured.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute, with attribution.
Not affiliated with any ISP.

## Contributing

This is a home-gateway tool; keep it simple. If you add a feature, add a test, keep `[ORPHANS & PENDING]` in `CLAUDE.md` honest, and update this README.
