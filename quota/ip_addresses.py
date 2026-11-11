"""Shared IP address and network parsing helpers."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_ip_address(value: object) -> IPAddress | None:
    """Parse one host address; reject prefixes, brackets, and scoped literals."""
    raw = str(value or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if not raw or "/" in raw or "%" in raw:
        return None
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def parse_ip_network(value: object) -> IPNetwork | None:
    """Parse a CIDR or host network without accepting scoped IPv6 literals."""
    raw = str(value or "").strip()
    if not raw or "%" in raw:
        return None
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None


def address_family(value: object, *, network: bool = False) -> int | None:
    """Return address family 4 or 6 for a valid host or network literal."""
    parsed = parse_ip_network(value) if network else parse_ip_address(value)
    return parsed.version if parsed is not None else None


def canonical_ip_address(value: object) -> str | None:
    """Return the normalized text for a valid IPv4 or IPv6 host address."""
    address = parse_ip_address(value)
    if address is None:
        return None
    return address.compressed.lower() if address.version == 6 else str(address)


def split_ip_addresses(values: Iterable[object] | None
                       ) -> tuple[list[str], list[str]]:
    """Return canonical, sorted IPv4 and IPv6 host addresses separately."""
    ipv4: set[str] = set()
    ipv6: set[str] = set()
    for value in values or ():
        address = parse_ip_address(value)
        if address is None:
            continue
        target = ipv6 if address.version == 6 else ipv4
        target.add(address.compressed.lower() if address.version == 6
                   else str(address))
    return sorted(ipv4), sorted(ipv6)


def split_ip_networks(values: Iterable[object] | None
                      ) -> tuple[list[str], list[str]]:
    """Return normalized, deduplicated IPv4 and IPv6 networks separately."""
    ipv4: list[str] = []
    ipv6: list[str] = []
    seen4: set[str] = set()
    seen6: set[str] = set()
    for value in values or ():
        network = parse_ip_network(value)
        if network is None:
            continue
        target, seen = (ipv4, seen4) if network.version == 4 else (ipv6, seen6)
        normalized = str(network)
        if normalized not in seen:
            target.append(normalized)
            seen.add(normalized)
    return ipv4, ipv6
