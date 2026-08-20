"""Configuration loading.

Reads ``config.yaml`` (path overridable via the ``QUOTA_CONFIG`` env var) and
exposes a typed :class:`Config` dataclass. All values are optional in the file
and fall back to sensible defaults documented below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class DhcpConfig:
    """DHCP scope. Defaults assume a 192.168.1.0/24 router LAN."""

    enable: bool = True
    interface: str = ""  # empty => bind 0.0.0.0
    #: The gateway's own static LAN IP. Handed to clients as their default
    #: gateway (DHCP option 3) and used as the DHCP server identifier.
    #: Traffic can only be counted/blocked if clients route THROUGH this box.
    gateway_ip: str = "192.168.1.2"
    #: Upstream router IP (used for the DNS option and reference only; the
    #: gateway's default route to the internet is configured on the NIC itself).
    router_ip: str = "192.168.1.1"
    #: DNS servers handed to clients. dnsmasq on the gateway relays to these
    #: upstream resolvers and is itself advertised as the client's DNS, so
    #: every DNS query deterministically crosses the box (and is counted).
    dns_servers: list[str] = field(default_factory=lambda: ["192.168.1.1", "8.8.8.8"])
    #: Accept a DNS forwarder role (informational on Linux — dnsmasq always
    #: forwards; kept for API/config compatibility).
    dns_forward: bool = True
    subnet: str = "255.255.255.0"
    pool_start: str = "192.168.1.100"
    pool_end: str = "192.168.1.200"
    lease_hours: int = 24
    #: Path to dnsmasq's lease file — dnsmasq owns DHCP on the gateway and
    #: this file is the MAC<->IP binding source the maintenance loop reads.
    lease_file: str = "/var/lib/misc/dnsmasq.leases"
    #: App-owned dnsmasq fragment refusing DHCP to "STOP NEW CONNECTIONS"
    #: MACs (one ``dhcp-host=<mac>,ignore`` line each). Written by run.py,
    #: survives re-runs, and is emptied when the gate is turned off — the
    #: setup script never touches it (it only rewrites quota-gateway.conf).
    ignore_file: str = "/etc/dnsmasq.d/quota-ignore.conf"
    #: dnsmasq only picks up NEW ``dhcp-host=...ignore`` lines on a restart
    #: (SIGHUP only re-reads hosts/lease files). True (default) restarts
    #: dnsmasq whenever the fragment actually changed; False writes the file
    #: but skips the reload, for an admin who wants to batch several edits
    #: before a manual ``systemctl restart dnsmasq``.
    reload_dnsmasq: bool = True
    #: --- LAN-reality snapshot (written by the setup script / the runtime
    #: topology apply in BOTH topologies, so the dashboard's WAN-tab Revert can
    #: restore exactly what was there before a WAN experiment). WAN mode erases
    #: ``router_ip`` / ``dns_servers`` from the ACTIVE keys; these keep the LAN
    #: values. Empty => quota/netmgr.py falls back to the setup defaults.
    lan_router_ip: str = ""
    lan_dns_servers: list[str] = field(default_factory=lambda: [])
    #: The box's static uplink IP + prefix on the router's LAN (e.g. 192.168.1.110/24).
    uplink_ip: str = ""
    lan_cidr: int = 24


@dataclass
class EngineConfig:
    """Packet engine behaviour (nftables on the Linux gateway)."""

    enabled: bool = True
    #: only count the inbound sighting of a forwarded packet to avoid double-count.
    count_direction: str = "inbound"
    #: Accepted for config compatibility; the Linux gateway always uses the
    #: nftables engine (run.py ignores the value).
    backend: str = "nftables"
    #: nftables table used by the engine (see quota/nftables.py).
    table: str = "quota_gateway"
    #: Managed client subnet (e.g. "192.168.2.0/24"). Traffic to/from it is
    #: LOCAL — same-subnet client<->client is L2 anyway, and this guards against
    #: stray routed paths — and must never count against the metered bundle.
    #: Empty => derive from ``dhcp.gateway_ip`` + ``dhcp.subnet``.
    client_subnet: str = ""
    #: Uplink LAN subnet (e.g. "192.168.1.0/24") — the router's LAN. Traffic
    #: between a client and an uplink-subnet host (router admin UI, NAS, the
    #: router as DNS) crosses this box's forward hook, so WITHOUT this exclusion
    #: it would be counted against the quota. Empty => derive from
    #: ``dhcp.router_ip`` + ``dhcp.subnet``.
    uplink_subnet: str = ""
    #: ARP gateway-lock: actively deny internet to any device that tries to use
    #: the ROUTER (not this box) as its gateway — i.e. a static-IP bypass. The
    #: engine captures the router's IP on the client subnet (ARP interception) so
    #: the rogue's frames reach the box, then drops client-subnet -> router-IP
    #: traffic. Requires root + the LAN interface; see quota/nftables.py.
    gateway_arp_lock: bool = False
    #: Deployment topology. "lan" (default — byte-for-byte today): the box sits
    #: behind the router on the LAN, clients on their own subnet, router keeps
    #: WiFi + NAT; the box counts/blocks what the kernel forwards. "wan" (optional
    #: strong mode): the box terminates the WAN itself (dials PPPoE, public IP on
    #: ppp0) and the router is a pure bridge/AP — a static-IP device then has NO
    #: second router to bypass through. In "wan" mode the box keeps the uplink IP
    #: as a router-admin alias (clients still reach the router admin page through
    #: it), so the uplink subnet IS local, and the ARP gateway-lock is forced off
    #: (no router on the client segment to lock against). The dashboard WAN
    #: tab overrides this on the NEXT restart via the "topology_source"/"topology"
    #: settings (the "bundle_source" pattern); the setup script writes the value
    #: for QUOTA_TOPOLOGY.
    topology: str = "lan"
    #: The ARP gateway-lock value used when reverting from WAN to LAN (the
    #: setup script enables it in LAN mode). Mirrors the ``lan_*`` dhcp keys —
    #: the active ``gateway_arp_lock`` flips to False in WAN mode but the LAN
    #: reality is preserved here.
    lan_gateway_arp_lock: bool = True
    #: Count the gateway box's OWN internet traffic (input/output hooks,
    #: ``q_gw_up``/``q_gw_down``) and charge it to the protected "Gateway"
    #: user. Off => the box's traffic is uncounted (its quota block, if any,
    #: still applies via the gateway chains).
    count_gateway: bool = True
    #: Explicit VPN-server IPs that must stay reachable when the box's own
    #: internet is cut (Gateway OFF) while "VPN share" relays the household.
    #: Normally the relay's endpoints are AUTO-learned from the VPN client's
    #: established ``ss`` sockets — this is the manual override for a VPN
    #: client the auto-learn step can't identify (or a fixed server you want
    #: always allowed). Values are IPv4/CIDR; empty (default) disables.
    gateway_allow_ips: list[str] = field(default_factory=list)


@dataclass
class BundleConfig:
    total_gb: float = 140.0
    reset_day: int = 1  # 1-31, day-of-month the ISP bundle resets
    #: "renew_day" (reset on reset_day) or "end_of_month" (calendar month).
    period_type: str = "renew_day"


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    #: Mark session cookies ``Secure`` (browser sends them only over HTTPS).
    #: Auto-forced on when ``tls_certfile`` is set; keep False for a plain
    #: HTTP LAN box (Secure cookies would silently drop every login).
    secure_cookies: bool = False
    #: Optional TLS (uvicorn ``ssl_certfile``/``ssl_keyfile`` — a self-signed
    #: cert is fine; the admin does the one-time trust step). Setting these
    #: forces ``secure_cookies``. Empty strings = plain HTTP.
    tls_certfile: str = ""
    tls_keyfile: str = ""
    #: Serve the FastAPI auto-generated docs (/api/docs Swagger UI + the full
    #: /api/openapi.json schema). OFF by default: the schema is a structured
    #: endpoint map an attacker on a WAN-facing box would mine in seconds.
    #: Enable only on a trusted dev box; the running admin surface is the
    #: dashboard itself.
    docs_enabled: bool = False


@dataclass
class ShapingConfig:
    """Linux tc speed shaping (HTB + fq_codel)."""

    enabled: bool = True
    #: LAN interface to shape on. Empty => auto-detect (the NIC whose subnet
    #: contains ``dhcp.gateway_ip``). On the single-NIC gateway this is the
    #: same interface that carries the uplink + the client alias.
    interface: str = ""
    #: Client subnet (e.g. "192.168.2.0/24") whose ingress is redirected into
    #: the ifb for upload shaping. Empty => derive from gateway_ip + subnet.
    client_subnet: str = ""
    #: ifb device used for the upload (ingress-redirect) tree.
    ifb: str = "ifb0"
    #: LAN link rate (Mbps). The HTB root + a LAN pass-through class are capped
    #: here while internet traffic keeps its WAN caps: client<->uplink-subnet
    #: traffic (NAS, router admin, LAN transfers) rides the pass-through at full
    #: LAN speed instead of being throttled by the WAN line rate. The uplink
    #: subnet is resolved the same way as the nftables engine (explicit
    #: ``engine.uplink_subnet`` wins, else derived from the dhcp block). ``0``
    #: falls back to the direction total (no LAN headroom).
    lan_rate_mbps: float = 1000.0


@dataclass
class ReportConfig:
    """On-demand internal reporting dashboard (source-IP gated).

    Served at ``/report`` + ``/api/report`` — a read-only consumption view
    (exact bytes/quota per user and device, events, log tail) that does NOT
    require the admin session. Access is gated by the requesting client's IP:
    clients on the managed subnet and/or an explicit allow-list are admitted,
    everything else gets a 403. Passive/on-demand only — nothing ever
    auto-opens it.
    """

    enabled: bool = True
    #: Admit any request whose source IP is inside the managed client subnet
    #: (the DHCP pool the box hands out, e.g. 192.168.2.0/24). On by default:
    #: the household's own devices are the intended audience.
    allow_client_subnet: bool = True
    #: Extra CIDRs/IPs admitted regardless of subnet (admin machines, the box's
    #: own uplink IP, a VPN range). e.g. ["192.168.1.0/24", "10.0.0.5"].
    allowed_ips: list[str] = field(default_factory=list)
    #: The managed client subnet as a CIDR (e.g. "192.168.2.0/24"). run.py
    #: fills this from ``engine.client_subnet`` (or derives it from the dhcp
    #: block), so the app never needs to re-derive it. Empty => the subnet
    #: admission is a no-op (only ``allowed_ips`` admits).
    client_subnet: str = ""


@dataclass
class HistoryConfig:
    """Per-device DNS browsing history (what each device queries).

    Captured from dnsmasq's query log: ``log-queries=extra`` makes dnsmasq log
    one line per query with the requestor IP, and the app tails that file and
    buckets queries per device/minute/domain into the ``dns_history`` table.
    ``enabled: false`` stops the app from reading the log — recording ceases
    entirely (the raw log, if the fragment is installed, keeps filling until
    logrotate bounds it, but nothing is attributed to devices).
    """

    enabled: bool = True
    #: Where dnsmasq writes the query log (``log-facility=``). Written by the
    #: setup script's app-owned fragment /etc/dnsmasq.d/quota-dnslog.conf.
    dnsmasq_log_file: str = "/var/log/quota-dnsmasq.log"
    #: Global default retention in days (per-user ``users.history_days``
    #: overrides; NULL = this value). History older than the cutoff is pruned
    #: hourly, so the DB stays bounded.
    retention_days: int = 7


@dataclass
class VpnShareConfig:
    """"VPN share" — route the whole client subnet through the box's VPN.

    The box runs a VPN client in TUN mode (sing-box / xray / WireGuard /
    tun2socks bridging any local SOCKS/HTTP proxy). With the dashboard's
    "VPN share" switch on, every managed client's internet traffic is
    routed into that tunnel via policy routing (an ``ip rule`` from the
    client subnet into a dedicated route table whose default route points
    at the tunnel device). The kernel continues to count + block per
    device in the nftables ``forward`` chain, so quota enforcement and
    speed shaping keep working — the bytes just exit at the VPN provider's
    IP. ``enabled: true`` here only lets the manager exist; the actual
    master switch lives in the dashboard (Network tab) / DB settings, the
    same shape as ``shaping``.
    """

    enabled: bool = True
    #: Optional interface pin (e.g. "utun4", "wg0"). Empty => auto-detect:
    #: the first TUN-ish interface (``/sys/class/net/*/type`` == 65534 —
    #: ARPHRD_NONE: tun/utun/wireguard), preferring one with an IPv4
    #: address. The detected name is stored in the DB at apply time so a
    #: multi-VPN box stays pinned to the same tunnel.
    interface: str = ""
    #: Route table + rule priority for the client-subnet policy routing.
    #: Must not collide with the main (32766) or local (0) tables.
    route_table: int = 200
    rule_pref: int = 1000
    #: Auto-provision the tun2socks bridge when VPN share is on but no
    #: kernel TUN interface exists (userspace-netstack clients like v2rayN
    #: never create one). ``quota/tun2socks.py`` downloads the pinned,
    #: sha256-verified binary (one-time), spawns it against the VPN
    #: client's local SOCKS proxy, and stops it when VPN share is off.
    #: Disable only when you run your OWN kernel-TUN client (sing-box /
    #: xray / WireGuard) — a second tun would confuse the tunnel detector.
    tun2socks: bool = True
    #: Fallback SOCKS proxy the bridge targets. Auto-detection prefers the
    #: VPN client's actual LOCAL listener (``ss -tlnp`` matching
    #: v2ray/sing-box/xray) and falls back to this value.
    socks_proxy: str = "127.0.0.1:10808"
    #: tun device tun2socks creates + addresses it assigns itself
    #: (``-device`` / ``-tun-ip`` / ``-tun-gw``). VpnShareManager's tunnel
    #: detector prefers ``tun*`` names, so these defaults are picked up
    #: automatically.
    tun_interface: str = "tun0"
    tun_ip: str = "10.0.0.1"
    tun_gw: str = "10.0.0.2"
    #: Install path for the downloaded binary.
    binary: str = "/usr/local/bin/tun2socks"
    #: Pin the release asset. Empty URL = auto-built from the pinned
    #: RELEASE_TAG + architecture; empty sha256 = the built-in per-arch
    #: table. An unverified (no sha256) binary is NEVER installed.
    download_url: str = ""
    download_sha256: str = ""


@dataclass
class DnsFilterConfig:
    """Domain-level filtering: per-user/per-device blacklists, allow-list
    exceptions, custom host redirects, curated blocklist presets, and
    per-user/per-device upstream DNS-server overrides.

    Implemented entirely as GENERATED dnsmasq configuration — this box
    already owns DHCP + DNS (see ``DhcpConfig``), so no new service is
    started and the nftables/tc packet paths are untouched. See
    ``quota/dns_rules.py`` for the renderer/parsers and
    ``quota.db``'s ``domain_rules`` / ``dns_presets`` tables for storage.
    """

    enabled: bool = True
    #: Directory dnsmasq scans for ``*.conf`` (Debian/Kali ship
    #: ``conf-dir=/etc/dnsmasq.d`` in ``/etc/dnsmasq.conf`` by default — the
    #: setup script does not need to add this on a stock install).
    conf_dir: str = "/etc/dnsmasq.d"
    #: Filenames written INSIDE conf_dir. Kept separate from
    #: ``quota-gateway.conf`` (the DHCP/DNS base config written by the setup
    #: script) and ``quota-dnslog.conf`` (the browsing-history feature's own
    #: fragment) so a domain-rule edit never touches either, and kept
    #: separate from EACH OTHER so tags (rarely change) and rules (change
    #: often) can be diffed/rewritten independently.
    tags_file: str = "quota-tags.conf"
    rules_file: str = "quota-domains.conf"
    #: dnsmasq only picks up NEW ``address=``/``server=``/``dhcp-host=``
    #: lines on a restart — SIGHUP only re-reads ``/etc/hosts`` and
    #: lease-adjacent files. True (default) restarts dnsmasq whenever the
    #: generated files actually changed (~1 s DNS blip for clients); False
    #: writes the files but skips the reload, for an admin who wants to
    #: batch several edits before a manual ``systemctl restart dnsmasq``.
    reload_dnsmasq: bool = True
    #: Where fetched blocklist presets are cached on disk (raw text, so a
    #: restart does not need to re-fetch before an already-enabled preset's
    #: rules can be rebuilt). Relative paths resolve under the project root.
    preset_cache_dir: str = "data/dns_presets"


@dataclass
class WifiProbeConfig:
    """Passive WiFi/LAN access probe (quota/wifi_probe.py, OFF by default).

    The router bridges clients L2, so the box's own NICs all show the same
    uplink — the router-side "is this device on WiFi or wired" answer needs
    the AIR. Enabled, the probe puts a spare WiFi NIC of the box into monitor
    mode (airmon-ng + airodump-ng, both Kali staples) and passively hears
    every client's frames: a leased device heard on the air is "WiFi · <SSID>"
    (the real ESSID from the AP's beacons), one never heard past the grace
    period is "LAN". The probe interface MUST be a card not used for anything
    else (the uplink is the wired NIC in the gateway design).
    """

    enabled: bool = False
    #: Monitor-capable WiFi NIC (e.g. "wlan0"). Empty => auto-detect the
    #: first wlan* interface from ``iw dev``.
    interface: str = ""
    #: CSV re-read cadence (seconds).
    poll_interval: float = 5.0
    #: A station sighting stays "wireless" this long after its last frame
    #: (associated-but-idle devices do not flap to LAN).
    sighted_ttl: float = 600.0
    #: A leased device never heard on the air for this long is labeled "LAN".
    lan_after_seconds: float = 300.0


@dataclass
class LatencyProbeConfig:
    """WiFi/LAN classification by ARP round-trip time (ON by default).

    Works on ANY hardware — no monitor-mode WiFi card needed (monitor sniffing
    is optional; see :class:`WifiProbeConfig`). The box ARPs each leased
    client and times the replies: a wired device answers in well under a
    millisecond, a WiFi device pays airtime on top (typically 1 ms and up).
    ``min(rtts) >= threshold_ms`` => WiFi. Only the fastest sample counts —
    local scheduling noise only ever inflates RTTs.

    The raw-socket ARP backend (root) falls back to ``ping`` time= parsing,
    then to "keep the previous label" when probing is impossible. When the
    monitor-mode probe is available it takes precedence (it also knows the
    exact SSID).
    """

    enabled: bool = True
    #: ARP requests (or ping probes) per device per sweep.
    samples: int = 6
    #: Minimum replies before a device is classified at all. Keep it LOW:
    #: a power-save device (sleeping phone) answers 2 of the 6 requests and
    #: would otherwise sit UNKNOWN forever; with the streak guard, two
    #: agreeing sub-ms min-samples cannot be a WiFi phone (airtime alone
    #: exceeds the threshold).
    min_samples: int = 2
    #: Fastest RTT at/above which the device counts as WiFi (ms).
    threshold_ms: float = 1.0
    #: Consecutive agreeing sweeps required before the label flips (no flap).
    min_consistent: int = 2
    #: Sweep cadence (seconds).
    interval_s: float = 30.0
    #: Per-sweep receive timeout (seconds).
    timeout_s: float = 0.5


@dataclass
class NetworkConfig:
    """Per-device WiFi/LAN source-interface tags.

    run.py learns each leased client's source NIC from the kernel neighbor
    table (``ip -j neigh``) and stores it per device. ``interface_tags`` maps
    a NIC name to a human label for the dashboard chip — e.g. ``{"eth0":
    "LAN", "wlan0": "WiFi"}``. An interface without a label falls back to its
    raw name; empty mapping shows the raw name everywhere.

    ``wifi_probe`` (OFF by default) upgrades the chip from the box-side NIC
    to the ROUTER-side access point: the box's monitor-mode WiFi card hears
    which SSID each device is actually associated with (see
    :class:`WifiProbeConfig`). It needs a monitor-capable card — when the box
    lacks one, ``latency_probe`` (ON by default) answers WiFi-vs-LAN with
    ARP round-trip times on any hardware (see :class:`LatencyProbeConfig`).
    """

    interface_tags: dict[str, str] = field(default_factory=dict)
    wifi_probe: WifiProbeConfig = field(default_factory=WifiProbeConfig)
    latency_probe: LatencyProbeConfig = field(default_factory=LatencyProbeConfig)


@dataclass
class UpdateConfig:
    """Self-update checks (quota/updater.py).

    ``enabled: false`` turns the whole subsystem off — no 24 h check, no
    Admin-tab update card (the endpoints 404 and the snapshot carries
    ``update: None``), exactly like ``history.enabled``. The dashboard's
    per-gateway "check automatically / auto-install" toggles live in the DB
    settings and are honored when this master switch is on.
    """

    enabled: bool = True
    #: GitHub owner/repo holding the releases + the CHANGELOG.md.
    repo: str = "UserJoo9/QuotaManager"
    #: Release-check cadence (hours).
    interval_hours: int = 24


@dataclass
class SynFloodConfig:
    """SYN-flood guard for the box + forwarded services (non-local NEW SYNs)."""

    rate: int = 10
    burst: int = 20


@dataclass
class BruteForceConfig:
    """Login brute-force -> kernel ban (hooked into the API login route)."""

    threshold: int = 10  # failures within the existing 300 s window
    ban_seconds: int = 1800


@dataclass
class ScanDetectConfig:
    """Port-scan detection via the ``fw_scan_watch`` dynamic set."""

    enabled: bool = True
    syn_threshold: int = 200  # new SYNs per 60 s watch window before a ban
    ban_seconds: int = 3600


@dataclass
class FirewallConfig:
    """Kernel firewall (``inet quota_firewall``), see ``quota/firewall.py``.

    A separate nftables table layered NEXT TO the quota engine (forward/input
    hook priority -100, before the engine's priority 0) — it never touches the
    ``quota_gateway``/``quota_nat``/``quota_arp_lock`` tables. The deployment
    posture is DERIVED from ``engine.topology`` at render time (never stored):
    LAN = permissive-out with explicit denies; WAN = default-deny NEW inbound
    on ppp0 (dashboard never exposed unless ``wan_confirmed``). Port-forwards
    + DMZ are WAN-only.

    ``firewall:`` in config.yaml SEEDS the DB setting ``firewall_config``
    (JSON) on first boot; the DB is the runtime master after that (the
    bundle/shaping pattern). Every apply is sanitized (a deny rule covering
    the client subnet / box IPs is refused — the admin can't lock themself
    out), snapshotted (``data/firewall_snapshots/`` + ``firewall_last_good``),
    and verified by a watchdog that auto-reverts on lockout. Bans
    (brute-force / port-scan / manual) land in ``@fw_bans`` with kernel
    timeouts; the Firewall log view = DB events + counter deltas.
    """

    enabled: bool = True
    #: Seconds the safe-apply watchdog waits before re-verifying the ruleset
    #: and auto-reverting to the last-good config.
    watchdog_seconds: int = 45
    #: IP the watchdog protects (never denied by any rule). Empty => derived
    #: from the client subnet (the box's gateway address).
    probe_ip: str = ""
    #: Box services exposed on the internet under WAN mode (fw_input accepts).
    services: list[dict[str, Any]] = field(default_factory=list)
    #: WAN-mode inbound port forwards (dnat + forward accept).
    port_forwards: list[dict[str, Any]] = field(default_factory=list)
    #: WAN-mode DMZ target (catch-all dnat); empty = off.
    dmz: str = ""
    #: Ordered custom rules (input/forward, allow/deny).
    rules: list[dict[str, Any]] = field(default_factory=list)
    #: CIDR allowlist (bypasses the WAN default-deny).
    allow_cidrs: list[str] = field(default_factory=list)
    #: CIDR blocklist (deny > allow).
    deny_cidrs: list[str] = field(default_factory=list)
    syn_flood: SynFloodConfig = field(default_factory=SynFloodConfig)
    brute_force: BruteForceConfig = field(default_factory=BruteForceConfig)
    scan_detect: ScanDetectConfig = field(default_factory=ScanDetectConfig)
    #: Country-blocking consumes the ``firewall_geo`` DB setting (a JSON
    #: country-code -> CIDR map); inert when off or the map is empty. The map
    #: must be maintained externally (the module does not bundle/refresh geo
    #: databases).
    geo_block: bool = False
    #: Explicit opt-in to expose the dashboard web port on ppp0 under WAN
    #: mode. NEVER enabled implicitly — the dashboard is LAN-only by default.
    wan_confirmed: bool = False


@dataclass
class WafConfig:
    """Request-level WAF (``api/waf.py``), embedded in the web app.

    The kernel firewall inspects the network layer (IP/port/rate); it cannot
    see inside an HTTP request. The WAF is a Starlette middleware in front of
    every route that inspects actual request content — size/header caps,
    method allowlist, path traversal, SQLi/XSS/command-injection signatures,
    scanner User-Agent fingerprints, Content-Type enforcement and per-endpoint
    request-rate limits — before a handler ever runs.

    Mode is derived from ``engine.topology`` by default (``mode="auto"``):
    WAN = strict (blocking), LAN = log-only (a mis-fire must not break the
    LAN dashboard; the router is still the primary firewall there). The
    ``fail_mode`` knob sets what happens if the middleware itself errors:
    ``"closed"`` (WAN: the dashboard becomes unreachable rather than silently
    losing protection) or ``"open"`` (pass through + log).
    """

    enabled: bool = True
    #: "auto" | "strict" | "log" | "off"  ("auto" = strict on WAN, log on LAN).
    mode: str = "auto"
    #: Body size cap (bytes) — larger requests are 413'd before parsing.
    max_body_bytes: int = 1_048_576
    #: Max request header count and per-header bytes (431 on overflow).
    max_headers: int = 40
    max_header_bytes: int = 8_192
    #: "closed" (unreachable rather than unprotected) | "open" (pass through).
    fail_mode: str = "closed"
    #: WAF hits from one source within ``ban_window_seconds`` that trigger an
    #: automatic firewall IP ban (``0`` disables the auto-ban).
    auto_ban_after: int = 8
    ban_seconds: int = 1800
    ban_window_seconds: int = 300
    #: Per-path request-rate caps: ``{path_prefix: [max, window_seconds]}``.
    #: Tighter than the TCP-level rate limit; applies per source IP.
    endpoint_limits: dict[str, list[int]] = field(default_factory=lambda: {
        "/api/login": [20, 60],
        "/api/report": [60, 60],
        "/api/dashboard": [120, 60],
    })
    #: Rule exceptions: ``[{rule_id, path?, source_ip?}]`` — a specific rule
    #: is bypassed for a path/source so one misfiring rule never forces the
    #: whole WAF off.
    exceptions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Config:
    db_path: str = "data/quota.db"
    log_file: str = "logs/quota.log"
    log_level: str = "INFO"
    bundle: BundleConfig = field(default_factory=BundleConfig)
    dhcp: DhcpConfig = field(default_factory=DhcpConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    web: WebConfig = field(default_factory=WebConfig)
    shaping: ShapingConfig = field(default_factory=ShapingConfig)
    vpn_share: VpnShareConfig = field(default_factory=VpnShareConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    dns_filter: DnsFilterConfig = field(default_factory=DnsFilterConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    firewall: FirewallConfig = field(default_factory=FirewallConfig)
    waf: WafConfig = field(default_factory=WafConfig)
    timezone: str = ""  # empty => system local timezone


def _as_dataclass(dc: Any, data: dict[str, Any] | None) -> Any:
    """Fill a dataclass from a dict, ignoring unknown keys (forward-compatible)."""
    if not data:
        return dc
    known = {f for f in dc.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in known}
    # Nested dataclasses recurse.
    for field_name in kwargs:
        target = getattr(dc, field_name, None)
        value = kwargs[field_name]
        if hasattr(target, "__dataclass_fields__") and isinstance(value, dict):
            kwargs[field_name] = _as_dataclass(target, value)
    return type(dc)(**kwargs)  # type: ignore[call-arg]


def resolve_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve a config path string or directory to an existing or target config file.

    If given a directory (common with Docker volume mounts when the host path did
    not exist prior to container boot), search inside for ``config.yaml`` or ``config.yml``.
    """
    raw_path = path or os.environ.get("QUOTA_CONFIG") or DEFAULT_CONFIG_PATH
    cfg_path = Path(raw_path)
    if cfg_path.is_dir():
        for candidate in (cfg_path / "config.yaml", cfg_path / "config.yml"):
            if candidate.is_file():
                return candidate
        return cfg_path / "config.yaml"
    return cfg_path


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from ``path`` (default ``config.yaml`` next to the project).

    Raises :class:`FileNotFoundError` when the resolved config file does not
    exist. Silently falling back to defaults was the trap: a missing or
    mistyped ``config.yaml`` deployed the wrong bundle size / DHCP subnet and
    the admin had no idea until devices were blocked or never counted. On the
    gateway, fail loud at boot instead of running with invented settings.
    """
    cfg_path = resolve_config_path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"config file not found: {cfg_path}. Copy config.yaml to that "
            "path, or point QUOTA_CONFIG at it.")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config()
    for section, value in (data or {}).items():
        if hasattr(cfg, section) and isinstance(value, dict):
            current = getattr(cfg, section)
            if hasattr(current, "__dataclass_fields__"):
                setattr(cfg, section, _as_dataclass(current, value))
        elif isinstance(value, dict):
            # Unknown top-level sections are ignored (forward-compatible).
            pass
        else:
            setattr(cfg, section, value)
    return cfg

