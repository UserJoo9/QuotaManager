"""Characterization tests for pure helpers in quota.vpn_parser."""
from urllib.parse import parse_qs

from quota import vpn_parser as vp


def test_param_returns_first_value_or_default():
    params = parse_qs("type=tcp&security=tls&sni=example.com")
    assert vp._param(params, "type") == "tcp"
    assert vp._param(params, "security") == "tls"
    assert vp._param(params, "missing", "fallback") == "fallback"
    assert vp._param(params, "missing") == ""


def test_param_handles_list_values():
    params = {"k": ["first", "second"]}
    assert vp._param(params, "k") == "first"
    assert vp._param(params, "k", "d") == "first"
    assert vp._param({}, "k", "d") == "d"
