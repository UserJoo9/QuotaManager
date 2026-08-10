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


@dataclass
class BundleConfig:
    total_gb: float = 140.0
    reset_day: int = 1  # 1-28, day-of-month the ISP bundle resets


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


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
    #: script) so a domain-rule edit never touches the base file, and kept
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
class Config:
    db_path: str = "data/quota.db"
    log_file: str = "logs/quota.log"
    log_level: str = "INFO"
    bundle: BundleConfig = field(default_factory=BundleConfig)
    dhcp: DhcpConfig = field(default_factory=DhcpConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    web: WebConfig = field(default_factory=WebConfig)
    shaping: ShapingConfig = field(default_factory=ShapingConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    dns_filter: DnsFilterConfig = field(default_factory=DnsFilterConfig)
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


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from ``path`` (default ``config.yaml`` next to the project).

    Raises :class:`FileNotFoundError` when the resolved config file does not
    exist. Silently falling back to defaults was the trap: a missing or
    mistyped ``config.yaml`` deployed the wrong bundle size / DHCP subnet and
    the admin had no idea until devices were blocked or never counted. On the
    gateway, fail loud at boot instead of running with invented settings.
    """
    cfg_path = Path(path or os.environ.get("QUOTA_CONFIG") or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
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
