"""ARP-RTT WiFi/LAN classification (quota/latency_probe.py) — the any-hardware
fallback for the router-side access label when the box's WiFi module has no
monitor mode.

The raw-socket backend needs root + Linux (never exercised here); the
classifier math, the ping-parse fallback and run.py's sweep wiring are tested
against fakes, mirroring the arp_scan/wifi_probe test style.
"""

import asyncio
import time

from quota.latency_probe import (LAN, UNKNOWN, WIFI, ArpRttProbe,
                                 classify_rtts)


def test_classify_min_sample_rule():
    """The FASTEST sample decides: local scheduling noise only inflates
    RTTs, so the best-case delivery is the true path latency."""
    assert classify_rtts([0.2, 0.3, 0.4, 1.9], 1.0) == LAN
    assert classify_rtts([2.5, 3.1, 4.0, 0.4], 1.0) == LAN
    assert classify_rtts([1.2, 3.5, 5.0, 2.0], 1.0) == WIFI
    assert classify_rtts([0.9, 1.1, 0.8], 1.0) == LAN
    assert classify_rtts([1.0, 1.0, 1.0], 1.0) == WIFI  # threshold inclusive


def test_classify_needs_min_samples():
    """Too few replies => UNKNOWN (caller keeps the previous label)."""
    assert classify_rtts([0.2], 1.0) == UNKNOWN
    assert classify_rtts([0.2, 0.3], 1.0) == UNKNOWN
    assert classify_rtts([], 1.0) == UNKNOWN
    assert classify_rtts([2.0, 2.1, 2.2], 1.0, min_samples=4) == UNKNOWN
    assert classify_rtts([2.0, 2.1, 2.2, 2.3], 1.0, min_samples=4) == WIFI


def test_probe_ping_fallback_parses_time_ms():
    """Without raw sockets — no root, non-Linux, or a refused factory — the
    probe parses ``time=`` values out of ping. ICMP-blocking clients are
    simply not classified. (The factory refusal makes the fallback
    deterministic on EVERY platform, including root-on-Linux CI.)"""
    calls = []

    def no_raw_socket():
        raise OSError("raw sockets unavailable (root?)")

    def fake_run(argv):
        calls.append(argv)
        if argv[0] == "ping":
            if "1.2.3.4" in argv:
                return 0, ("PING 1.2.3.4 (1.2.3.4) 56(84) bytes of data.\n"
                           "64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=2.51 ms\n"
                           "64 bytes from 1.2.3.4: icmp_seq=2 ttl=64 time=1.98 ms\n"
                           "64 bytes from 1.2.3.4: icmp_seq=3 ttl=64 time=2.10 ms\n")
            return 1, "100% packet loss"
        if "addr" in argv:
            return 0, ("2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 "
                       "scope global eth0\\\n")
        if "link" in argv:
            return 0, "2: eth0    link/ether 11:22:33:44:55:66 brd ff:ff:ff:ff:ff:ff\n"
        return 0, ""

    probe = ArpRttProbe(_cfg(), run_command=fake_run,
                        socket_factory=no_raw_socket)
    assert probe.enabled
    rtts = probe.probe(["1.2.3.4", "10.0.0.9"])
    assert rtts == {"1.2.3.4": [2.51, 1.98, 2.10]}
    assert [c[0] for c in calls] == ["ip", "ip", "ping", "ping"]
    assert classify_rtts(rtts["1.2.3.4"], 1.0) == WIFI


def test_probe_raw_backend_times_replies():
    """The raw backend (fake socket) maps each reply's ARP sender IP to the
    request timestamps — RTTs are >= 0 and replies without a pending request
    are dropped."""
    import socket
    from quota.arp_scan import arp_reply_frame

    sent = []

    class FakeSock:
        """One reply per request, delivered only AFTER its request goes out
        (interleaved semantics: a reply that lands before its request is a
        spurious/early frame and must be dropped)."""

        def __init__(self):
            self._sent = 0
            self._popped = 0
            self._garbage = True
            self._replies = [
                arp_reply_frame("aa:bb:cc:dd:ee:01", "1.2.3.4",
                                "11:22:33:44:55:66", "192.168.2.1"),
                arp_reply_frame("aa:bb:cc:dd:ee:01", "1.2.3.4",
                                "11:22:33:44:55:66", "192.168.2.1"),
            ]

        def bind(self, _a): pass

        def send(self, frame):
            self._sent += 1
            sent.append(frame)

        def settimeout(self, _t): pass

        def recv(self, _n):
            if self._garbage:
                self._garbage = False
                return b"garbage"
            if self._popped < self._sent:
                self._popped += 1
                return self._replies[self._popped - 1]
            raise socket.timeout

        def close(self): pass

    def fake_run(argv):
        if "addr" in argv:
            return 0, ("2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 "
                       "scope global eth0\\\n")
        if "link" in argv:
            return 0, "2: eth0    link/ether 11:22:33:44:55:66 brd ff:ff:ff:ff:ff:ff\n"
        return 0, ""

    cfg = _cfg()
    probe = ArpRttProbe(
        cfg, run_command=fake_run, samples=2, timeout_s=0.01,
        inter_round_s=0.0, socket_factory=FakeSock)
    rtts = probe.probe(["1.2.3.4"])
    assert rtts["1.2.3.4"] and len(rtts["1.2.3.4"]) == 2
    assert all(r >= 0 for r in rtts["1.2.3.4"])
    assert sent  # requests were actually sent


def test_probe_degrades_to_empty_when_disabled():
    """A cfg whose LAN NIC cannot resolve (empty gateway) => enabled False =>
    probe() is a no-op, never opening a socket or running a command."""
    from core.config import Config

    cfg = Config()
    cfg.dhcp.gateway_ip = ""  # no client gateway => no resolvable networks
    calls = []
    probe = ArpRttProbe(
        cfg, run_command=lambda argv: calls.append(argv) or (0, ""))
    assert not probe.enabled
    assert probe.probe(["1.2.3.4"]) == {}
    assert calls == []


def test_probe_catches_slow_waking_devices():
    """A power-save device (sleeping phone / NIC-sleeping PC) wakes seconds
    after the first request. The interleaved rounds keep a reply window open
    for the WHOLE sweep, so its post-wake replies still get sampled."""
    import socket
    from quota.arp_scan import arp_reply_frame

    wake_after_drains = 3  # device only answers once it is awake
    drain_calls = [0]

    class SlowSock:
        def __init__(self):
            self._replies = [
                arp_reply_frame("aa:bb:cc:dd:ee:01", "1.2.3.4",
                                "11:22:33:44:55:66", "192.168.2.1"),
                arp_reply_frame("aa:bb:cc:dd:ee:01", "1.2.3.4",
                                "11:22:33:44:55:66", "192.168.2.1"),
                arp_reply_frame("aa:bb:cc:dd:ee:01", "1.2.3.4",
                                "11:22:33:44:55:66", "192.168.2.1"),
            ]
            self._woke = False

        def bind(self, _a): pass

        def send(self, _f): pass

        def settimeout(self, _t): pass

        def recv(self, _n):
            drain_calls[0] += 1
            if drain_calls[0] <= wake_after_drains:
                raise socket.timeout  # still waking up
            if not self._woke:
                self._woke = True
                time.sleep(0.05)  # wake latency — real RTT, well over 1 ms
            if not self._replies:
                raise socket.timeout
            return self._replies.pop(0)

        def close(self): pass

    def fake_run(argv):
        if "addr" in argv:
            return 0, ("2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 "
                       "scope global eth0\\\n")
        if "link" in argv:
            return 0, "2: eth0    link/ether 11:22:33:44:55:66 brd ff:ff:ff:ff:ff:ff\n"
        return 0, ""

    probe = ArpRttProbe(
        _cfg(), run_command=fake_run, samples=6, timeout_s=0.01,
        inter_round_s=0.0, socket_factory=SlowSock)
    rtts = probe.probe(["1.2.3.4"])
    assert len(rtts["1.2.3.4"]) == 3
    assert classify_rtts(rtts["1.2.3.4"], 1.0) == WIFI


def test_probe_degrades_to_empty_when_nic_unresolvable():
    """resolve_nic failure (no NIC owns the client gateway) => empty map,
    no command storm."""
    def fake_run(argv):
        return 0, ""  # `ip -o -4 addr show` finds nothing

    probe = ArpRttProbe(_cfg(), run_command=fake_run)
    assert probe.enabled
    assert probe.probe(["1.2.3.4"]) == {}


def _cfg():
    """A config with a resolvable client subnet (the probe only needs the
    dhcp block; every backend is faked or degraded in these tests)."""
    from core.config import Config
    from core.config import DhcpConfig

    cfg = Config()
    cfg.dhcp = DhcpConfig(
        gateway_ip="192.168.2.1",
        subnet="255.255.255.0",
        router_ip="192.168.1.1",
        uplink_ip="192.168.1.110",
    )
    return cfg


def test_gateway_builds_probe_from_config(tmp_path):
    """run.py wiring: with the default latency_probe.enabled=True the Gateway
    owns an ArpRttProbe; with enabled=False it owns None. An injected fake is
    honored for sweep tests."""
    from core.config import Config
    from run import Gateway

    gw = Gateway(Config())
    assert gw.latency_probe is not None
    assert gw.latency_probe.enabled is True  # dhcp defaults resolve a subnet

    cfg = Config()
    cfg.network.latency_probe.enabled = False
    gw2 = Gateway(cfg)
    assert gw2.latency_probe is None


def test_latency_tick_classifies_with_streak_guard(tmp_path):
    """Sweep wiring: a leased device flips its access label only after
    ``min_consistent`` agreeing sweeps; UNKNOWN resets the streak; the
    monitor probe (available=True) yields the sweep entirely."""
    from core.config import Config, DhcpConfig
    from run import Gateway

    cfg = Config()
    cfg.db_path = str(tmp_path / "lat.db")
    cfg.dhcp = DhcpConfig(gateway_ip="192.168.2.1",
                          subnet="255.255.255.0")
    cfg.network.latency_probe.samples = 3
    cfg.network.latency_probe.min_samples = 3
    cfg.network.latency_probe.min_consistent = 2
    cfg.network.latency_probe.interval_s = 0.0  # every tick sweeps
    gw = Gateway(cfg)

    # wifi device answers slowly (WiFi airtime), the LAN one fast
    class FakeProbe:
        def probe(self, targets):
            out = {}
            for ip in targets:
                out[ip] = [4.0, 3.5, 4.2] if ip == "192.168.2.2" else [0.2, 0.3, 0.2]
            return out

    gw.latency_probe = FakeProbe()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.database.connect())
        loop.run_until_complete(gw.service.ensure_period())
        for mac, ip in (("aa:bb:cc:dd:ee:01", "192.168.2.2"),
                        ("aa:bb:cc:dd:ee:02", "192.168.2.3")):
            loop.run_until_complete(
                gw.database.upsert_device(mac, name=mac))
            loop.run_until_complete(gw.database.set_lease(mac, ip))
        # sweep 1: streaks start, no label yet (min_consistent=2)
        loop.run_until_complete(gw._maybe_latency_tick())
        dev1 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:01"))
        assert dev1.access_interface == ""
        # sweep 2: labels land
        loop.run_until_complete(gw._maybe_latency_tick())
        dev1 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:01"))
        dev2 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:02"))
        assert dev1.access_interface == "WiFi"
        assert dev2.access_interface == "LAN"
        # responders = the LED's "connected NOW" source: both devices answered
        assert gw._latency_active_ips() == {"192.168.2.2", "192.168.2.3"}
        # UNKNOWN (device stops replying) resets the streak; the label stays
        gw.latency_probe = type("P", (), {"probe": staticmethod(
            lambda targets: {})})()
        loop.run_until_complete(gw._maybe_latency_tick())
        dev1 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:01"))
        assert dev1.access_interface == "WiFi"
        # nobody answered the sweep => no responder => grey LEDs (lease alone
        # no longer proves the device is awake)
        assert gw._latency_active_ips() == set()
        # monitor probe available => the latency sweep yields
        gw.wifi_probe = type("P", (), {"snapshot": staticmethod(
            lambda: {"available": True, "error": "", "ssid_by_mac": {},
                     "wireless_macs": [], "ssids": []})})()
        loop.run_until_complete(gw._wifi_probe_tick())
        gw.latency_probe = type("P", (), {"probe": staticmethod(
            lambda targets: [])})()
        loop.run_until_complete(gw._maybe_latency_tick())
        dev1 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:01"))
        assert dev1.access_interface == "WiFi"
    finally:
        loop.run_until_complete(gw.database.close())
        loop.close()


def test_latency_tick_yields_to_monitor_probe(tmp_path):
    """Even with an available probe, one sweep before the monitor is heard
    must not fight it: the tick returns when _wifi_probe_state.available."""
    from core.config import Config, DhcpConfig
    from run import Gateway

    cfg = Config()
    cfg.db_path = str(tmp_path / "lat2.db")
    cfg.dhcp = DhcpConfig(gateway_ip="192.168.2.1",
                          subnet="255.255.255.0")
    cfg.network.latency_probe.interval_s = 0.0
    gw = Gateway(cfg)
    calls = []
    gw.latency_probe = type("P", (), {"probe": staticmethod(
        lambda targets: calls.append(1) or {})})()
    gw._wifi_probe_state["available"] = True
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.database.connect())
        loop.run_until_complete(gw._maybe_latency_tick())
        assert calls == []
    finally:
        loop.run_until_complete(gw.database.close())
        loop.close()