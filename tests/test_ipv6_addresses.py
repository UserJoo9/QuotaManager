"""Pure ownership and coverage decisions for the IPv6 address inventory."""

from quota.ipv6_addresses import canonical_ipv6, build_ipv6_ownership


def test_ipv6_addresses_are_canonicalized():
    assert canonical_ipv6("2001:0DB8:0000:0000:0000:0000:0000:0018") == "2001:db8::18"
    assert canonical_ipv6("[fd00:da7a:2::18]") == "fd00:da7a:2::18"
    assert canonical_ipv6("192.0.2.18") is None
    assert canonical_ipv6("fe80::1%wlan0") is None


def test_multiple_neighbor_and_static_ipv6_addresses_share_one_owner():
    mac = "aa:bb:cc:dd:ee:18"
    ownership = build_ipv6_ownership(
        neighbors={
            "fd00:da7a:2::18": mac,
            "fd00:da7a:2::19": mac,
        },
        static_leases=[(mac, "fd00:da7a:2::20")],
        known_macs={mac},
    )

    assert ownership.addresses_by_mac[mac] == (
        "fd00:da7a:2::18", "fd00:da7a:2::19", "fd00:da7a:2::20")
    assert ownership.ip_to_mac == {
        "fd00:da7a:2::18": mac,
        "fd00:da7a:2::19": mac,
        "fd00:da7a:2::20": mac,
    }
    assert ownership.portal_ip_to_mac == ownership.ip_to_mac
    assert ownership.coverage_for(mac) == ("managed", "")


def test_global_ipv6_addresses_are_mapped_but_path_is_unverified():
    mac = "aa:bb:cc:dd:ee:19"
    address = "2606:4700:4700::1111"
    ownership = build_ipv6_ownership(
        neighbors={address: mac}, static_leases=[], known_macs={mac})

    assert ownership.ip_to_mac[address] == mac
    assert ownership.coverage_for(mac) == (
        "unverified", "global_prefix_path_unverified")


def test_conflicting_owners_are_excluded_and_reported_partial():
    first = "aa:bb:cc:dd:ee:20"
    second = "aa:bb:cc:dd:ee:21"
    shared = "fd00:da7a:2::20"
    ownership = build_ipv6_ownership(
        neighbors={shared: first, "fd00:da7a:2::21": first},
        static_leases=[(second, "fd00:da7a:2::20")],
        known_macs={first, second},
    )

    assert shared in ownership.ambiguous_addresses
    assert shared not in ownership.ip_to_mac
    assert ownership.ip_to_mac["fd00:da7a:2::21"] == first
    assert ownership.coverage_for(first) == (
        "partial", "ambiguous_address_owner")
    assert ownership.coverage_for(second) == (
        "partial", "ambiguous_address_owner")


def test_unknown_neighbor_conflicts_with_a_known_static_reservation():
    known = "aa:bb:cc:dd:ee:24"
    unknown = "aa:bb:cc:dd:ee:25"
    address = "fd00:da7a:2::24"
    ownership = build_ipv6_ownership(
        neighbors={address: unknown},
        static_leases=[(known, address)],
        known_macs={known},
    )

    assert address in ownership.ambiguous_addresses
    assert known in ownership.ambiguous_macs
    assert address not in ownership.ip_to_mac
    assert address not in ownership.portal_ip_to_mac
    assert ownership.coverage_for(known) == (
        "partial", "ambiguous_address_owner")


def test_ipv6_address_cap_is_deterministic_and_reported_partial():
    mac = "aa:bb:cc:dd:ee:22"
    neighbors = {f"fd00:da7a:2::{n:x}": mac for n in range(1, 12)}
    ownership = build_ipv6_ownership(
        neighbors=neighbors, static_leases=[], known_macs={mac}, max_per_mac=8)

    assert len(ownership.addresses_by_mac[mac]) == 11
    assert len(ownership.tracked_by_mac[mac]) == 8
    assert len(ownership.portal_ip_to_mac) == 11
    assert ownership.portal_ip_to_mac["fd00:da7a:2::b"] == mac
    assert ownership.overflow_by_mac[mac] == tuple(
        f"fd00:da7a:2::{n:x}" for n in range(9, 12))
    assert ownership.tracked_by_mac[mac] == tuple(
        f"fd00:da7a:2::{n:x}" for n in range(1, 9))
    assert mac in ownership.capped_macs
    assert ownership.coverage_for(mac) == ("partial", "address_cap_exceeded")
    assert ownership.coverage_for("aa:bb:cc:dd:ee:23") == (
        "unverified", "no_ipv6_address_observed")
