"""Characterization tests for pure helpers in quota.dns_rules."""
from quota import dns_rules as dr


def test_bracket_ipv6_wraps_and_preserves():
    assert dr._bracket_ipv6("2001:db8::1") == "[2001:db8::1]"
    assert dr._bracket_ipv6("[2001:db8::1]") == "[2001:db8::1]"


def test_coerce_static_lease_dict():
    assert dr._coerce_static_lease({"ip": "10.0.0.2", "ip6": "2001:db8::2"}) == ("10.0.0.2", "2001:db8::2")


def test_coerce_static_lease_tuple_and_list():
    assert dr._coerce_static_lease(("10.0.0.3", "2001:db8::3")) == ("10.0.0.3", "2001:db8::3")
    assert dr._coerce_static_lease(["10.0.0.4"]) == ("10.0.0.4", "")
    assert dr._coerce_static_lease([]) == ("", "")


def test_coerce_static_lease_scalar():
    assert dr._coerce_static_lease("10.0.0.5") == ("10.0.0.5", "")
    assert dr._coerce_static_lease(None) == ("", "")
