"""Configuration loading.

Reads ``config.yaml`` (path overridable via the ``QUOTA_CONFIG`` env var) and
exposes a typed :class:`Config` dataclass. All values are optional in the file
and fall back to sensible defaults documented below.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class DhcpConfig:
    """Our DHCP server scope. Defaults assume a 192.168.1.0/24 router LAN."""

    enable: bool = True
    interface: str = ""  # empty => bind 0.0.0.0
    #: The PC's own static LAN IP. Handed to clients as their default gateway
    #: (DHCP option 3) and used as the DHCP server identifier (option 54).
    #: Traffic can only be counted/blocked if clients route THROUGH this PC.
    gateway_ip: str = "192.168.1.2"
    #: Upstream router IP (used for the DNS option and reference only; the
    #: PC's default route to the internet is configured on the NIC itself).
    router_ip: str = "192.168.1.1"
    #: DNS servers handed to clients when no forwarder runs on this PC. When
    #: ``dns_forward`` is on, these become the UPSTREAM resolvers the PC relays
    #: to (default 8.8.8.8), and clients are instead told "use the gateway" so
    #: every DNS query deterministically crosses the PC (and is counted).
    dns_servers: list[str] = field(default_factory=lambda: ["192.168.1.1", "8.8.8.8"])
    #: Run a UDP/53 forwarder on the PC that relays client DNS to upstream
    #: resolvers. Without it, devices that point at the gateway (Android/iOS
    #: fallback) can never resolve a hostname and report "connected, no
    #: internet". Requires Administrator to bind port 53. When enabled, DHCP
    #: advertises the PC itself as the DNS server.
    dns_forward: bool = True
    subnet: str = "255.255.255.0"
    pool_start: str = "192.168.1.100"
    pool_end: str = "192.168.1.200"
    lease_hours: int = 24
    #: Path to dnsmasq's lease file on the Linux gateway. The Windows build
    #: serves DHCP itself (quota/dhcp.py) and ignores this; on Linux dnsmasq
    #: owns DHCP and this file is the MAC<->IP binding source.
    lease_file: str = "/var/lib/misc/dnsmasq.leases"
    #: Electric-cut fallback (optional). When the PC is down, devices have no
    #: working gateway and lose the internet. Give the ROUTER a small fallback
    #: DHCP pool (gateway = router) in a NON-OVERLAPPING range; our server
    #: never hands out these IPs, so devices fall back to direct internet when
    #: this PC is unavailable. Keep ``lease_hours`` short (e.g. 1) so devices
    #: quickly return to the PC's pool when it comes back.
    fallback_enabled: bool = False
    fallback_pool_start: str = ""
    fallback_pool_end: str = ""


@dataclass
class EngineConfig:
    """Packet engine behaviour (WinDivert on Windows, nftables on Linux)."""

    enabled: bool = True
    #: only count the inbound sighting of a forwarded packet to avoid double-count.
    count_direction: str = "inbound"
    #: engine backend: "auto" (pick by OS), "windivert", or "nftables".
    backend: str = "auto"
    #: nftables table used by the Linux engine (see quota/nftables.py).
    table: str = "quota_gateway"


@dataclass
class ArpConfig:
    """Proxy-ARP responder behaviour."""

    enabled: bool = True
    interface: str = ""  # empty => scapy picks the first suitable interface
    announce_interval_sec: int = 60


@dataclass
class BundleConfig:
    total_gb: float = 140.0
    reset_day: int = 1  # 1-28, day-of-month the ISP bundle resets


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    db_path: str = "data/quota.db"
    log_file: str = "logs/quota.log"
    log_level: str = "INFO"
    bundle: BundleConfig = field(default_factory=BundleConfig)
    dhcp: DhcpConfig = field(default_factory=DhcpConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    arp: ArpConfig = field(default_factory=ArpConfig)
    web: WebConfig = field(default_factory=WebConfig)
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
            f"config file not found: {cfg_path}. Copy config.yaml (or "
            "config-linux.yaml on the Linux gateway) to that path, or point "
            "QUOTA_CONFIG at it.")
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


def expand_ip_range(start: str, end: str) -> list[str]:
    """Expand an IPv4 ``start..end`` range into a list of dotted-quad strings.

    Raises :class:`ValueError` if ``end < start`` or either value is not a
    valid IPv4 address. Used to build both the DHCP pool and the reserved
    fallback range, so both are validated identically.
    """
    from ipaddress import ip_address
    a = int(ip_address(start))
    b = int(ip_address(end))
    if b < a:
        raise ValueError(f"IP range end {end} < start {start}")
    return [str(ip_address(i)) for i in range(a, b + 1)]


def detect_local_interface_ips() -> list[str]:
    """Return this host's IPv4 addresses (used for self-identification)."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    if not ips:
        ips.add("127.0.0.1")
    return sorted(ips)
