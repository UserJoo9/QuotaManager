"""Characterization tests for pure helpers in quota.ipv6_setup."""
import ipaddress

from quota import ipv6_setup as ip6


def test_parse_prefix_valid_v6():
    net = ip6._parse_prefix("2001:db8::/32")
    assert isinstance(net, ipaddress.IPv6Network)
    assert str(net) == "2001:db8::/32"


def test_parse_prefix_valid_v4():
    net = ip6._parse_prefix("192.168.0.0/16")
    assert isinstance(net, ipaddress.IPv4Network)


def test_parse_prefix_invalid_returns_none():
    assert ip6._parse_prefix("not-a-prefix") is None
    assert ip6._parse_prefix("") is None


def test_parse_prefix_host_bits_strict_false():
    # strict=False allows host bits set
    net = ip6._parse_prefix("2001:db8::5/32")
    assert net is not None
    assert str(net.network_address) == "2001:db8::"
