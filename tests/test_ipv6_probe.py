"""Tests for Ipv6Probe (ISP IPv6 reachability tester & dual-stack state manager)."""

import pytest

from quota.ipv6_probe import Ipv6Probe, is_global_unicast_ipv6


def test_is_global_unicast_ipv6():
    # Valid global unicast IPv6 addresses
    assert is_global_unicast_ipv6("2606:4700:4700::1111") is True
    assert is_global_unicast_ipv6("2001:4860:4860::8888") is True
    assert is_global_unicast_ipv6("2a00:1450:4009:820::200e/64") is True
    assert is_global_unicast_ipv6("[2606:4700:4700::1111]") is True

    # Local / Private / Non-routable addresses
    assert is_global_unicast_ipv6("::1") is False  # Loopback
    assert is_global_unicast_ipv6("fe80::1ff:fe00:3a60") is False  # Link-local
    assert is_global_unicast_ipv6("fd00:da7a:2::1") is False  # ULA
    assert is_global_unicast_ipv6("fc00::1") is False  # ULA
    assert is_global_unicast_ipv6("ff02::1") is False  # Multicast
    assert is_global_unicast_ipv6("") is False
    assert is_global_unicast_ipv6("not-an-ip") is False
    assert is_global_unicast_ipv6("192.168.1.1") is False


def test_detect_wan_global_ipv6_parsing():
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        if cmd[:3] == ["ip", "-6", "-o"] and "addr" in cmd:
            output = (
                "1: lo    inet6 ::1/128 scope host \\       valid_lft forever preferred_lft forever\n"
                "2: eth0    inet6 fe80::a00:27ff:fe4e:66a1/64 scope link \\       valid_lft forever preferred_lft forever\n"
                "2: eth0    inet6 fd00:da7a:2::1/64 scope global \\       valid_lft forever preferred_lft forever\n"
                "3: ppp0    inet6 2001:db8:1234::1/64 scope global \\       valid_lft forever preferred_lft forever\n"
            )
            return 0, output
        return 0, ""

    probe = Ipv6Probe(run_command=fake_runner)
    detected = probe.detect_wan_global_ipv6()
    assert detected == "2001:db8:1234::1"


def test_detect_default_ipv6_route():
    def fake_runner(cmd):
        if "route" in cmd:
            return 0, "default via fe80::1 dev ppp0 metric 1024 pref medium"
        return 0, ""

    probe = Ipv6Probe(run_command=fake_runner)
    assert probe.detect_default_ipv6_route() is True


@pytest.mark.anyio
async def test_probe_now_integration():
    status_changed_events = []

    def on_change(active, details):
        status_changed_events.append((active, details))

    def fake_runner(cmd):
        if "addr" in cmd:
            return 0, "2: eth0 inet6 2600:1700::1/64 scope global"
        if "ping" in cmd:
            return 0, "1 packets transmitted, 1 received, 0% packet loss"
        return 0, ""

    probe = Ipv6Probe(
        run_command=fake_runner,
        probe_targets=["2606:4700:4700::1111"],
        on_status_change=on_change,
    )

    # Force check_reachability to simulate successful reachability
    async def mock_reachability(timeout_sec=3.0):
        return True, "Mock TCP reachability success"

    probe.check_reachability = mock_reachability

    res = await probe.probe_now()
    assert res["wan_ipv6_active"] is True
    assert res["wan_ipv6_address"] == "2600:1700::1"
    assert res["last_probe_status"] == "active"
    assert len(status_changed_events) == 1
    assert status_changed_events[0][0] is True


@pytest.mark.anyio
async def test_deactivation_needs_consecutive_failures():
    """One lost probe must not flap IPv6 off; fail_threshold in a row does."""
    events = []
    probe = Ipv6Probe(run_command=lambda cmd: (0, ""), fail_threshold=2,
                      on_status_change=lambda a, d: events.append(a))
    results = iter([True, False, False, True])

    async def fake(timeout_sec=3.0):
        return next(results), "x"

    probe.check_reachability = fake
    assert (await probe.probe_now())["wan_ipv6_active"] is True
    st = await probe.probe_now()                    # 1st failure: still active
    assert st["wan_ipv6_active"] is True and st["last_probe_status"] == "active_degraded"
    assert (await probe.probe_now())["wan_ipv6_active"] is False   # 2nd: off
    assert (await probe.probe_now())["wan_ipv6_active"] is True    # recovers at once
    assert events == [True, False, True]
