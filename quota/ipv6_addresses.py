"""Canonical IPv6 ownership and conservative quota-enforcement coverage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from quota.ip_addresses import parse_ip_address
from quota.ipv6_probe import is_global_unicast_ipv6


def canonical_ipv6(value: str) -> str | None:
    """Return a compressed host IPv6 address, excluding non-client scopes."""
    address = parse_ip_address(value)
    if address is None or address.version != 6:
        return None
    if (address.is_link_local or address.is_multicast or address.is_unspecified
            or address.is_loopback or address.ipv4_mapped is not None):
        return None
    return address.compressed.lower()


def canonical_ipv6_addresses(values: Iterable[str | None]) -> list[str]:
    """Normalize and deduplicate an IPv6 inventory in one pass."""
    normalized = {address for value in values
                  if (address := canonical_ipv6(value or "")) is not None}
    return sorted(normalized)


def ipv6_address_inventory(
        static_address: str | None, observed_addresses: Iterable[str | None]
        ) -> tuple[str, list[str]]:
    """Canonical device IPv6 inventory and preferred static address."""
    static = canonical_ipv6(static_address or "")
    addresses = {static} if static else set()
    for value in observed_addresses:
        address = canonical_ipv6(value or "")
        if address:
            addresses.add(address)
    return static or (min(addresses) if addresses else ""), sorted(addresses)


@dataclass(frozen=True)
class IPv6Ownership:
    """Attributable observed addresses and the subset installed for enforcement."""

    ip_to_mac: dict[str, str]
    portal_ip_to_mac: dict[str, str]
    addresses_by_mac: dict[str, tuple[str, ...]]
    tracked_by_mac: dict[str, tuple[str, ...]]
    overflow_by_mac: dict[str, tuple[str, ...]]
    ambiguous_addresses: frozenset[str]
    ambiguous_macs: frozenset[str]
    capped_macs: frozenset[str]
    unverified_macs: frozenset[str]

    def coverage_for(self, mac: str) -> tuple[str, str]:
        """Return a stable status/reason pair for an operator-facing device view."""
        owner = str(mac or "").strip().lower()
        if owner in self.ambiguous_macs:
            return "partial", "ambiguous_address_owner"
        if owner in self.capped_macs:
            return "partial", "address_cap_exceeded"
        if owner in self.unverified_macs:
            return "unverified", "global_prefix_path_unverified"
        if not self.addresses_by_mac.get(owner):
            return "unverified", "no_ipv6_address_observed"
        return "managed", ""


def build_ipv6_ownership(
        neighbors: Mapping[str, str] | Iterable[tuple[str, str]],
        static_leases: Iterable[tuple[str, str]],
        known_macs: Iterable[str],
        max_per_mac: int = 8) -> IPv6Ownership:
    """Merge kernel neighbors and configured reservations without guessing owners.

    Conflicting owners make an address ambiguous and remove it from all maps.
    Unknown neighbors remain conflict evidence but are never assigned to a
    managed device. Static reservations win the deterministic per-device cap;
    portal identity retains every unambiguous address while enforcement stays
    bounded to the tracked subset.
    """
    known = {str(mac or "").strip().lower() for mac in known_macs}
    owners: dict[str, dict[str, int]] = {}

    def add(address_text: str, mac_text: str, priority: int) -> None:
        address = canonical_ipv6(address_text)
        mac = str(mac_text or "").strip().lower()
        if address is None or not mac:
            return
        by_owner = owners.setdefault(address, {})
        by_owner[mac] = min(priority, by_owner.get(mac, priority))

    neighbor_items = neighbors.items() if isinstance(neighbors, Mapping) else neighbors
    for address, mac in neighbor_items:
        add(address, mac, 1)
    for mac, address in static_leases:
        add(address, mac, 0)

    ambiguous = frozenset(address for address, by_owner in owners.items()
                          if len(by_owner) > 1)
    ambiguous_macs = frozenset(
        mac for address in ambiguous for mac in owners[address] if mac in known)
    all_by_mac: dict[str, list[tuple[int, int, str]]] = {}
    unverified: set[str] = set()
    for address, by_owner in owners.items():
        if address in ambiguous:
            continue
        mac, priority = next(iter(by_owner.items()))
        if mac not in known:
            continue
        parsed_address = parse_ip_address(address)
        if parsed_address is None:
            continue
        all_by_mac.setdefault(mac, []).append(
            (priority, int(parsed_address), address))
        if is_global_unicast_ipv6(address):
            unverified.add(mac)

    cap = max(1, int(max_per_mac))
    addresses_by_mac: dict[str, tuple[str, ...]] = {}
    tracked_by_mac: dict[str, tuple[str, ...]] = {}
    overflow_by_mac: dict[str, tuple[str, ...]] = {}
    ip_to_mac: dict[str, str] = {}
    portal_ip_to_mac: dict[str, str] = {}
    capped: set[str] = set()
    for mac, candidates in all_by_mac.items():
        candidates.sort()
        all_addresses = tuple(address for _, _, address in
                              sorted(candidates, key=lambda row: row[1]))
        tracked = tuple(address for _, _, address in
                        sorted(candidates[:cap], key=lambda row: row[1]))
        tracked_set = set(tracked)
        overflow = tuple(address for _, _, address in
                         sorted(candidates, key=lambda row: row[1])
                         if address not in tracked_set)
        addresses_by_mac[mac] = all_addresses
        tracked_by_mac[mac] = tracked
        portal_ip_to_mac.update({address: mac for address in all_addresses})
        if len(candidates) > cap:
            capped.add(mac)
            overflow_by_mac[mac] = overflow
        for address in tracked:
            ip_to_mac[address] = mac

    return IPv6Ownership(
        ip_to_mac=ip_to_mac,
        portal_ip_to_mac=portal_ip_to_mac,
        addresses_by_mac=addresses_by_mac,
        tracked_by_mac=tracked_by_mac,
        overflow_by_mac=overflow_by_mac,
        ambiguous_addresses=ambiguous,
        ambiguous_macs=ambiguous_macs,
        capped_macs=frozenset(capped),
        unverified_macs=frozenset(unverified),
    )
