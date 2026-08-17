"""Passive WiFi/LAN probe (quota/wifi_probe.py) — airodump CSV parsing and
the dedicated-thread snapshot, against fake tool output / a fed CSV file.
The airmon-ng/airodump subprocesses are never touched (auto_start=False);
run.py's label resolution is covered in test_run_wiring.py."""

import asyncio
import time

from quota.wifi_probe import (CSV_SUFFIX, WifiProbe, parse_airodump_csv)

AP_CSV = """BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key
00:11:22:33:44:55, 2026-08-16 10:00:00, 2026-08-16 10:00:01, 6, 130, WPA2, CCMP, PSK, -45, 12, 0, 0.0.0.0, 6, MyNet, <no key>
00:11:22:33:44:66, 2026-08-16 10:00:00, 2026-08-16 10:00:01, 36, 195, WPA2, CCMP, PSK, -61, 5, 0, 0.0.0.0, 10, MyNet-5G, <no key>
00:11:22:33:44:77, 2026-08-16 10:00:00, 2026-08-16 10:00:01, 1, 54, WEP, WEP, OPN, -70, 3, 0, 0.0.0.0, 0, <length: 0>, <no key>

Station MAC, First time seen, Last time seen, Power, # packets, BSSID, Probed ESSIDs
aa:bb:cc:dd:ee:01, 2026-08-16 10:00:00, 2026-08-16 10:00:02, -50, 34, 00:11:22:33:44:55, MyNet
aa:bb:cc:dd:ee:02, 2026-08-16 10:00:00, 2026-08-16 10:00:02, -61, 12, 00:11:22:33:44:66, MyNet-5G
aa:bb:cc:dd:ee:03, 2026-08-16 10:00:01, 2026-08-16 10:00:01, -72, 2, (not associated), 
aa:bb:cc:dd:ee:04, 2026-08-16 10:00:01, 2026-08-16 10:00:01, -58, 9, 00:11:22:33:44:77, 
"""


def test_parse_airodump_csv_aps_and_stations():
    aps, stations = parse_airodump_csv(AP_CSV)
    assert aps == {
        "00:11:22:33:44:55": "MyNet",
        "00:11:22:33:44:66": "MyNet-5G",
    }
    # the hidden-SSID AP never enters the map
    assert "00:11:22:33:44:77" not in aps
    assert stations == [
        ("aa:bb:cc:dd:ee:01", "00:11:22:33:44:55"),
        ("aa:bb:cc:dd:ee:02", "00:11:22:33:44:66"),
        ("aa:bb:cc:dd:ee:03", ""),  # probing, not associated — still a sighting
        ("aa:bb:cc:dd:ee:04", "00:11:22:33:44:77"),  # hidden-AP BSSID kept
    ]


def test_parse_airodump_csv_tolerates_garbage():
    aps, stations = parse_airodump_csv("")
    assert aps == {} and stations == []
    aps, stations = parse_airodump_csv("not a csv line\n\nBSSID, junk\n")
    assert aps == {} and stations == []


def test_probe_reads_csv_and_builds_snapshot(tmp_path):
    probe = WifiProbe(csv_base=str(tmp_path / "scan"), auto_start=False)
    (tmp_path / f"scan{CSV_SUFFIX}").write_text(AP_CSV, encoding="utf-8")
    probe._read_csv()
    snap = probe.snapshot()
    assert snap["available"] is True
    assert snap["error"] == ""
    assert snap["ssid_by_mac"] == {
        "aa:bb:cc:dd:ee:01": "MyNet",
        "aa:bb:cc:dd:ee:02": "MyNet-5G",
    }
    # all four stations are wireless sightings (the last two without an SSID)
    assert set(snap["wireless_macs"]) == {
        "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02",
        "aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:04"}
    assert snap["ssids"] == ["MyNet", "MyNet-5G"]


def test_probe_missing_csv_is_graceful(tmp_path):
    probe = WifiProbe(csv_base=str(tmp_path / "never"), auto_start=False)
    probe._read_csv()  # no file yet — no crash, stays unavailable
    snap = probe.snapshot()
    assert snap["available"] is False
    assert snap["ssid_by_mac"] == {}
    assert snap["wireless_macs"] == []


def test_probe_sightings_expire_after_ttl(tmp_path):
    probe = WifiProbe(csv_base=str(tmp_path / "scan"), auto_start=False,
                      sighted_ttl=60.0)
    path = tmp_path / f"scan{CSV_SUFFIX}"
    path.write_text(AP_CSV, encoding="utf-8")
    probe._read_csv()
    assert "aa:bb:cc:dd:ee:01" in probe.snapshot()["wireless_macs"]
    # age every sighting past the TTL, then re-read a CSV with NO stations:
    # airodump always writes the headers, so the stale sightings expire
    now = time.time()
    for mac in list(probe._sighted):
        probe._sighted[mac] = now - 120.0
    path.write_text("BSSID, First time seen, Last time seen, channel, "
                    "Speed, Privacy, Cipher, Authentication, Power, "
                    "# beacons, # IV, LAN IP, ID-length, ESSID, Key\n\n"
                    "Station MAC, First time seen, Last time seen, Power, "
                    "# packets, BSSID, Probed ESSIDs\n", encoding="utf-8")
    probe._read_csv()
    assert probe.snapshot()["wireless_macs"] == []


def test_probe_thread_start_stop_smoke(tmp_path):
    """The dedicated thread runs its loop without touching any tool when
    auto_start=False — start()/stop() must be safe and join cleanly."""
    probe = WifiProbe(csv_base=str(tmp_path / "scan"), auto_start=False,
                      poll_interval=0.01)
    probe.start()
    try:
        for _ in range(50):
            if probe.running:
                break
            time.sleep(0.01)
        assert probe.running
    finally:
        probe.stop()
    assert probe.running is False
    probe.stop()  # idempotent


def test_snapshot_is_a_copy():
    probe = WifiProbe(csv_base="/tmp/nope", auto_start=False)
    snap = probe.snapshot()
    snap["ssids"].append("junk")
    snap["ssid_by_mac"]["x"] = "y"
    assert probe.snapshot()["ssids"] == []
    assert probe.snapshot()["ssid_by_mac"] == {}


def test_probe_disabled_on_windows_never_starts():
    """The probe is OFF by default in config.yaml — a default boot must not
    spawn any subprocess. Covered structurally: run.py only builds it when
    cfg.network.wifi_probe.enabled is truthy (test_run_wiring asserts the
    wiring); here we just assert the default is inert."""
    probe = WifiProbe(auto_start=False)
    assert probe._auto_start is False


def _cfg(tmp_path):
    from core.config import Config, NetworkConfig, WifiProbeConfig
    cfg = Config()
    # isolate the DB — Config() defaults to data/quota.db (the live-box path),
    # and a shared repo DB would pollute these tests across runs
    cfg.db_path = str(tmp_path / "test.db")
    cfg.network = NetworkConfig(
        wifi_probe=WifiProbeConfig(enabled=True, interface="wlan0",
                                   lan_after_seconds=0.0))
    return cfg


def test_gateway_startup_starts_probe(tmp_path):
    """run.py wiring: with wifi_probe.enabled, startup() builds + starts the
    probe (auto_start is on — the real thread would spawn tools, so a test
    injects a stub BEFORE startup via the None-guard, same as arp_scanner)."""
    from run import Gateway
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    calls = []

    class StubProbe:
        def start(self): calls.append("start")
        def stop(self): calls.append("stop")
        def snapshot(self):
            return {"available": True, "error": "", "ssid_by_mac": {},
                    "wireless_macs": [], "ssids": []}

    gw.wifi_probe = StubProbe()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.startup())
        assert calls == ["start"], "startup must start the configured probe"
        loop.run_until_complete(gw._wifi_probe_tick())
        assert gw._wifi_probe_state["available"] is True
    finally:
        loop.run_until_complete(gw.shutdown())
        loop.close()
    assert calls[-1] == "stop", "shutdown must stop the probe"


def test_wifi_probe_tick_labels_devices(tmp_path):
    """Resolution precedence: SSID sighting > plain wireless sighting > LAN
    after the grace window; manual overrides are never overwritten."""
    from run import Gateway
    cfg = _cfg(tmp_path)
    gw = Gateway(cfg)
    gw.wifi_probe = type("P", (), {"snapshot": staticmethod(
        lambda: {"available": True, "error": "", "ssid_by_mac": {
            "aa:bb:cc:dd:ee:01": "MyNet"},
            "wireless_macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            "ssids": ["MyNet"]})})()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.database.connect())
        loop.run_until_complete(gw.service.ensure_period())
        for mac in ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02",
                    "aa:bb:cc:dd:ee:03"):
            loop.run_until_complete(
                gw.database.upsert_device(mac, name=mac))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:01", "192.168.2.1"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:02", "192.168.2.2"))
        loop.run_until_complete(
            gw.database.set_lease("aa:bb:cc:dd:ee:03", "192.168.2.3"))
        loop.run_until_complete(gw._wifi_probe_tick())
        # ssid-sighted + plain-sighted devices are WiFi right away; the
        # unsighted one enters the grace window first, so label it on the
        # SECOND tick (lan_after=0 -> deadline already passed)
        loop.run_until_complete(gw._wifi_probe_tick())
        assert loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:01")
        ).access_interface == "WiFi · MyNet"
        assert loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:02")
        ).access_interface == "WiFi"
        assert loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:03")
        ).access_interface == "LAN"
        # a manual override is never overwritten by the probe
        dev3_row = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:03"))
        loop.run_until_complete(gw.database.update_device(
            dev3_row.id, access_override="LAN1"))
        loop.run_until_complete(gw._wifi_probe_tick())
        dev3 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:03"))
        assert dev3.access_override == "LAN1"
        assert dev3.access_interface == "LAN"
        # a LAN-labeled device heard on the air flips back to WiFi
        gw.wifi_probe = type("P", (), {"snapshot": staticmethod(
            lambda: {"available": True, "error": "", "ssid_by_mac": {},
                    "wireless_macs": ["aa:bb:cc:dd:ee:03"], "ssids": []})})()
        loop.run_until_complete(gw._wifi_probe_tick())
        dev3 = loop.run_until_complete(
            gw.database.get_device(mac="aa:bb:cc:dd:ee:03"))
        assert dev3.access_interface == "WiFi"
    finally:
        loop.run_until_complete(gw.database.close())
        loop.close()