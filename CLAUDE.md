# CLAUDE SYSTEM MAP — Quota Manager

Gateway that splits a metered internet bundle (e.g. Egypt 140 GB/month) fairly
across devices, cuts a device off when it exceeds its allowance, and gives the
admin a dark-purple glassmorphism web dashboard. Deployment target: **Linux on
an old laptop** (Kali/Debian) — the kernel owns the network path. A Windows
build is preserved and still supported.

## [TECH_STACK]
- Python -> **3.11** (runtime venv; 3.10+ supported by all deps)
- fastapi -> **0.141.1** (REST + WebSocket + static UI)
- uvicorn -> **0.52.1** (ASGI server, `[standard]` includes websockets)
- aiosqlite -> **0.22.1** (async SQLite, WAL mode)
- PyYAML -> **6.0.3** (config)
- Chart.js -> **4.5.0** (bundled offline at `web/assets/chart.umd.js`)
- auth -> stdlib `hashlib.pbkdf2_hmac` (single admin, session cookie)
- tests -> pytest **8.x** + fastapi TestClient

**Linux (target)**: `dnsmasq` (DHCP + DNS), `nftables` (client-subnet NAT +
accounting + hard drop). Deps: `requirements-linux.txt` (no pydivert/scapy —
they are Windows-only).

**Windows (legacy, still supported)**: pydivert -> **3.1.3** (WinDivert
accounting + drop; needs admin), scapy -> **2.5.0** (proxy-ARP; needs Npcap),
own DHCP server (`quota/dhcp.py`, udp/67) + DNS forwarder (`quota/dns.py`,
udp/53). Deps: `requirements.txt`.

## [SYSTEM_FLOW]
The router keeps WiFi + NAT, **DHCP disabled**. Clients join the router's WiFi
as usual, but their default gateway + DNS is the gateway box, so every byte
crosses it. On Linux the box puts clients on their **own subnet**
(`192.168.2.0/24`, gateway = the box's real address) and masquerades them out
the uplink — deterministic, no proxy_arp. The Windows legacy build uses the
same-subnet one-armed layout with proxy-ARP (see below).

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
2. `run.py --config config-linux.yaml` starts. Clients are on their own subnet
   (`192.168.2.0/24`, gateway = the box's real address) and the kernel
   masquerades them out the uplink — every byte deterministically crosses the
   box with **no proxy_arp** (the kernel's proxy_arp refuses same-subnet
   targets, which silently let downloads bypass the box; the separate subnet
   fixes it).
3. The **nftables engine** (`quota/nftables.py`) programs one named counter
   pair per device (`q_up_<ip>` / `q_down_<ip>`, dots→underscores) in the
   `forward` chain and a `blocked` set. The kernel counts at line rate; the
   app only reconciles rules and reads `nft -j list counters` (JSON) on a
   15 s tick. The `blocked` set is rebuilt only when its membership changes
   (`_last_blocked_ips` cache) — a same-set re-flush every tick would open a
   short unblock window. The app's table (`inet quota_gateway`) is separate
   from the setup-owned NAT table (`inet quota_nat`), so the two never
   conflict.
4. Every ~15 s the maintenance loop: rolls the quota period if stale (or
   skips when `reset_day=0`) → syncs device bindings from dnsmasq's lease
   file (`dhcp.lease_file`) → drains engine counter deltas into `usage_daily`
   → re-evaluates block states → pushes fresh ip→mac / blocked maps into the
   engine + snapshot holder.
5. A blocked device: the engine adds its IP to the `blocked` set and the
   kernel **drops** its forward-chain packets — hard internet cut at line
   rate. Admin toggles work the same way.
6. FastAPI + uvicorn serves the dashboard + REST API + `/ws` push (5 s
   snapshots). WebSocket is the live-update channel; the client also polls as
   a fallback.

**Windows (legacy)**: same flow, but the Python stack replaces the kernel
pieces — `quota/dhcp.py` (udp/67) + `quota/dns.py` (udp/53) + scapy
proxy-ARP replace dnsmasq; `quota/engine.py` (WinDivert thread) replaces
nftables. ICS used to own udp/53 and was left disabled; if you return to
Windows, restore the `SharedAccess` service and re-enable it.

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
(gateway = router). On Windows, Quota Manager validates the ranges don't
overlap at startup and its DHCP server (`reserved_ips`) never hands out the
router's fallback range; on Linux, dnsmasq serves only the client subnet
(192.168.2.x) while the router's fallback pool sits on the uplink subnet
(192.168.1.x) — no overlap by construction. Devices fall back to direct
internet during a gateway outage, and re-join the managed pool as their leases
renew. Recovery speed is governed by the DHCP lease length: re-run setup with
`LEASE_HOURS=1` for fast re-adoption (the `lease_hours` YAML key is
informational on Linux — dnsmasq owns the real lease). Trade-off:
fallback-leased devices are not counted/controlled while the gateway is down.

## [ARCHITECTURE]
```
QuotaManager/
├── CLAUDE.md                 <- this file (SYSTEM MAP)
├── config.yaml               # Windows gateway settings
├── config-linux.yaml         # Linux gateway settings (dnsmasq + nftables)
├── run.py                    # Gateway wiring: engine + maintenance + uvicorn
│                             #   (IS_LINUX picks the subsystem stack)
├── requirements.txt          # Windows deps (pydivert, scapy)
├── requirements-linux.txt    # Linux deps (no pydivert/scapy)
├── scripts/
│   ├── setup_gateway.ps1     # Windows: IPEnableRouter, firewall, info report
│   └── setup_gateway_kali.sh # Linux: sysctl, client-subnet NAT, dnsmasq,
│                             #   systemd unit, info
├── core/
│   ├── config.py             # config.yaml -> typed Config dataclasses
│   ├── logging_setup.py      # QueueHandler -> writer thread -> rotating file
│   └── timeutil.py           # month-boundary math (zoneinfo)
├── quota/
│   ├── db.py                 # SQLite schema + async access (aiosqlite)
│   ├── service.py            # hybrid quota math, blocks, top-up, recharge,
│   │                         #   reset_day=0, period roll
│   ├── engine.py             # PacketEngine thread (WinDivert) + snapshots
│   ├── nftables.py           # NftablesEngine (Linux): kernel counters + block
│   ├── dhcp.py               # Windows DHCP server (udp/67) + reserved range
│   ├── dns.py                # Windows DNS forwarder (udp/53)
│   └── arp.py                # Windows proxy-ARP responder (scapy)
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
Dependencies point downward only: `api -> quota/core`, `quota -> core`.
Engine ↔ asyncio communicate through thread-safe counter snapshots (no locks in
the packet hot path). On Linux the hot path has **no Python at all** — the
kernel counts and drops.

## [ORPHANS & PENDING]
_(empty — all components are integrated and verified)_

Checked 2026-08-03 (Linux pivot + bundle-source fix):
- [x] core (config / logging_setup / timeutil) + `backend`/`table`/`lease_file` fields
- [x] db layer + schema + `bundle_source` setting
- [x] quota service + unit tests (incl. reset_day=0, bundle recharge)
- [x] **nftables engine** (`quota/nftables.py`) + fake-`nft` tests (11 tests)
- [x] **bundle source fix**: config.yaml reconciled every boot unless
      `bundle_source=dashboard` (+ reconcile / ownership tests)
- [x] **live-counter fix**: holder now carries flushed deltas (regression test)
- [x] run.py Linux rewire: `IS_LINUX`, backend auto-selection, dnsmasq lease
      sync, lazy Windows-only imports (+ wiring tests)
- [x] `config-linux.yaml` + `requirements-linux.txt` + setup script aligned
      with the engine's nftables table
- [x] **deploy hardening** (4-lens adversarial verify, 18 confirmed defects
      applied): LAN_IF wired-NIC picker + subnet preflight, dnsmasq
      `dhcp-authoritative` + dual upstream + `LEASE_HOURS` knob, boot-race +
      ExecStop systemd drop-ins, `_last_blocked_ips` blocked-set cache, IPv6
      router-bypass warning, README path/env fixes
- [x] full suite: **83 passed**, pyflakes clean

## [KNOWN LIMITS] (honest)
- Windows per-packet Python accounting tops out well below 1 Gbps; fine for
  home DSL/FTTH (<=100 Mbps). Linux nftables counts at line rate (kernel-side)
  — the per-packet Python ceiling is a Windows-only concern. Counting is
  approximate ("≈" in the UI) either way.
- No throttling — exceeded devices are hard-blocked (Windows) or kernel-dropped
  (Linux).
- Windows needs Administrator (pydivert + DHCP udp/67) and Npcap for proxy-ARP;
  Linux needs root for nftables + dnsmasq.
- Subsystems degrade gracefully: no pydivert / no nft => no counting (dashboard
  still shows DB usage); no Npcap => download under-reported (Windows); no
  admin => no DHCP.
- Windows Update reboots take the gateway down — run `run.py` as a Scheduled
  Task / NSSM service and configure Active Hours (Linux: systemd unit).
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
