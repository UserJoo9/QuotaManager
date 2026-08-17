"""Config loading + the Linux-only surface (no Windows remnants).

Guards the sweep: the Linux config has no ``arp:`` section and no electric-cut
``fallback_*`` fields.
"""

from __future__ import annotations

from core import config as cfg_mod


def test_latency_probe_defaults_on_and_loads():
    """The ARP-RTT WiFi/LAN classifier is ON by default (works on any
    hardware — no monitor-capable card needed) with sane, tuneable knobs; a
    config-file value lands on the dataclass fields."""
    cfg = cfg_mod.Config()
    assert cfg.network.latency_probe.enabled is True
    assert cfg.network.latency_probe.samples == 6
    assert cfg.network.latency_probe.min_samples == 2
    assert cfg.network.latency_probe.threshold_ms == 1.0
    assert cfg.network.latency_probe.min_consistent == 2
    assert cfg.network.latency_probe.interval_s == 30.0
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text(
            "network:\n"
            "  latency_probe:\n"
            "    enabled: false\n"
            "    threshold_ms: 0.7\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.network.latency_probe.enabled is False
    assert loaded.network.latency_probe.threshold_ms == 0.7
    assert loaded.network.latency_probe.samples == 6  # untouched keys default


def test_wifi_probe_defaults_off_and_loads():
    """The passive WiFi/LAN probe (router-side SSID label) is OFF by default —
    it needs a spare monitor-mode card and airmon-ng/airodump-ng. A config-file
    value lands on the dataclass fields."""
    cfg = cfg_mod.Config()
    assert cfg.network.wifi_probe.enabled is False
    assert cfg.network.wifi_probe.interface == ""
    assert cfg.network.wifi_probe.poll_interval == 5.0
    assert cfg.network.wifi_probe.sighted_ttl == 600.0
    assert cfg.network.wifi_probe.lan_after_seconds == 300.0
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text(
            "network:\n"
            "  wifi_probe:\n"
            "    enabled: true\n"
            "    interface: wlan0\n"
            "    lan_after_seconds: 60\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.network.wifi_probe.enabled is True
    assert loaded.network.wifi_probe.interface == "wlan0"
    assert loaded.network.wifi_probe.lan_after_seconds == 60.0
    # defaults survive on a file that only sets enabled
    import tempfile as tf2
    from pathlib import Path as P2
    with tf2.TemporaryDirectory() as td:
        p = P2(td) / "config.yaml"
        p.write_text("network:\n  wifi_probe:\n    enabled: true\n",
                     encoding="utf-8")
        loaded2 = cfg_mod.load_config(p)
    assert loaded2.network.wifi_probe.interface == ""
    assert loaded2.network.wifi_probe.sighted_ttl == 600.0
    assert loaded2.network.wifi_probe.lan_after_seconds == 300.0


def test_shaping_lan_rate_mbps_defaults_to_lan_speed():
    """The shaping root + LAN pass-through cap at the LAN link rate (1 Gbps by
    default) so client<->uplink-subnet traffic is never throttled by the WAN
    line rate; a config-file value lands on the dataclass field."""
    cfg = cfg_mod.Config()
    assert cfg.shaping.lan_rate_mbps == 1000.0
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("shaping:\n  lan_rate_mbps: 2500\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.shaping.lan_rate_mbps == 2500.0


def test_config_has_no_arp_section():
    """Proxy-ARP is gone: the Linux topology masquerades the client subnet and
    never needs the scapy responder."""
    assert not hasattr(cfg_mod.Config(), "arp")


def test_engine_gateway_arp_lock_is_opt_in():
    """The ARP gateway-lock lives under ``engine:`` (never a top-level ``arp:``
    section) and defaults OFF — it needs root + the client-subnet topology."""
    cfg = cfg_mod.Config()
    assert cfg.engine.gateway_arp_lock is False
    engine = cfg_mod.EngineConfig(gateway_arp_lock=True)
    assert engine.gateway_arp_lock is True


def test_engine_count_gateway_defaults_true():
    """The box's own traffic is counted by default (charged to the protected
    "Gateway" user) — the admin can disable it, but the block still applies."""
    cfg = cfg_mod.Config()
    assert cfg.engine.count_gateway is True
    engine = cfg_mod.EngineConfig(count_gateway=False)
    assert engine.count_gateway is False
    # and a config-file value lands on the dataclass field
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("engine:\n  count_gateway: false\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.engine.count_gateway is False


def test_engine_gateway_allow_ips_defaults_empty():
    """The VPN-share gateway whitelist defaults to auto-learned only; an
    explicit list unions on top (a manual override for VPN clients the
    auto-learn step can't identify)."""
    cfg = cfg_mod.Config()
    assert cfg.engine.gateway_allow_ips == []
    engine = cfg_mod.EngineConfig(gateway_allow_ips=["1.2.3.4", "5.6.7.8"])
    assert engine.gateway_allow_ips == ["1.2.3.4", "5.6.7.8"]
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("engine:\n  gateway_allow_ips: [1.2.3.4, 5.6.7.8]\n",
                     encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.engine.gateway_allow_ips == ["1.2.3.4", "5.6.7.8"]


def test_vpn_share_tun2socks_defaults():
    """The tun2socks auto-provisioner defaults ON (userspace VPN clients like
    v2rayN never expose a kernel tun) with a v2rayN-shaped SOCKS fallback, and
    every knob is overridable via YAML."""
    cfg = cfg_mod.Config()
    vs = cfg.vpn_share
    assert vs.tun2socks is True
    assert vs.socks_proxy == "127.0.0.1:10808"
    assert vs.tun_interface == "tun0"
    assert vs.tun_ip == "10.0.0.1"
    assert vs.tun_gw == "10.0.0.2"
    assert vs.binary == "/usr/local/bin/tun2socks"
    assert vs.download_url == ""
    assert vs.download_sha256 == ""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("vpn_share:\n  tun2socks: false\n  socks_proxy: "
                     "127.0.0.1:10809\n  tun_interface: utun9\n",
                     encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.vpn_share.tun2socks is False
    assert loaded.vpn_share.socks_proxy == "127.0.0.1:10809"
    assert loaded.vpn_share.tun_interface == "utun9"
    # untouched fields keep their defaults
    assert loaded.vpn_share.tun_ip == "10.0.0.1"
    assert loaded.vpn_share.binary == "/usr/local/bin/tun2socks"


def test_engine_topology_defaults_lan():
    """The deployment topology defaults to the current LAN behaviour (box behind
    the router) and accepts the opt-in WAN ("strong") value."""
    cfg = cfg_mod.Config()
    assert cfg.engine.topology == "lan"
    engine = cfg_mod.EngineConfig(topology="wan")
    assert engine.topology == "wan"
    # Config loading with a WAN-mode file value lands on the dataclass field.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("engine:\n  topology: wan\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.engine.topology == "wan"


def test_dhcp_config_has_no_fallback_fields():
    """Electric-cut fallback lived on the router pool + our DHCP server. On the
    Linux gateway dnsmasq serves only the client subnet, so there is no
    fallback range to coordinate."""
    dhcp = cfg_mod.DhcpConfig()
    for field in ("fallback_enabled", "fallback_pool_start",
                  "fallback_pool_end"):
        assert not hasattr(dhcp, field)


def test_default_config_is_linux_gateway():
    """The single config.yaml defaults are the Linux gateway values."""
    cfg = cfg_mod.Config()
    assert cfg.dhcp.gateway_ip == "192.168.1.2"
    assert cfg.dhcp.lease_file == "/var/lib/misc/dnsmasq.leases"
    assert cfg.dhcp.ignore_file == "/etc/dnsmasq.d/quota-ignore.conf"
    assert cfg.dhcp.reload_dnsmasq is True
    assert cfg.engine.backend == "nftables"
    assert cfg.engine.table == "quota_gateway"


def test_report_config_defaults():
    """The report section defaults to on, client-subnet admission, no extra
    allow-list. ``client_subnet`` is empty until run.py fills it from the
    engine's resolved subnet."""
    cfg = cfg_mod.Config()
    assert cfg.report.enabled is True
    assert cfg.report.allow_client_subnet is True
    assert cfg.report.allowed_ips == []
    assert cfg.report.client_subnet == ""
    # explicit overrides land on the dataclass fields from a config file
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text(
            "report:\n"
            "  enabled: true\n"
            "  allow_client_subnet: false\n"
            "  allowed_ips:\n"
            "    - 192.168.1.0/24\n"
            "    - 10.0.0.5\n"
            "  client_subnet: 192.168.2.0/24\n",
            encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.report.allow_client_subnet is False
    assert loaded.report.allowed_ips == ["192.168.1.0/24", "10.0.0.5"]
    assert loaded.report.client_subnet == "192.168.2.0/24"


def test_report_config_disable_via_yaml():
    """Turning the report off in config.yaml must reach the dataclass so both
    /report + /api/report 403 everywhere."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("report:\n  enabled: false\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.report.enabled is False


def test_history_config_defaults():
    """DNS browsing history defaults on, log path + 7-day global retention."""
    cfg = cfg_mod.Config()
    assert cfg.history.enabled is True
    assert cfg.history.dnsmasq_log_file == "/var/log/quota-dnsmasq.log"
    assert cfg.history.retention_days == 7


def test_history_config_disable_via_yaml():
    """``history.enabled: false`` stops the app reading the query log entirely
    (DNS/DHCP are untouched — it only controls the tailer)."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("history:\n  enabled: false\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.history.enabled is False
    # an unknown section never breaks loading (auto-recurse + defaults)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("bogus_section:\n  x: 1\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.history.enabled is True


def test_updates_config_defaults_and_loads():
    """Self-update checks default ON with the app's own repo + a 24 h cadence,
    and a config-file value lands on the dataclass fields."""
    cfg = cfg_mod.Config()
    assert cfg.updates.enabled is True
    assert cfg.updates.repo == "UserJoo9/QuotaManager"
    assert cfg.updates.interval_hours == 24
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("updates:\n  enabled: false\n  interval_hours: 6\n",
                     encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.updates.enabled is False
    assert loaded.updates.interval_hours == 6
    # untouched keys keep their defaults
    assert loaded.updates.repo == "UserJoo9/QuotaManager"


def test_updates_config_disable_via_yaml():
    """``updates.enabled: false`` reaches the dataclass so the Gateway builds no
    updater (endpoints 404, the snapshot carries update: None)."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("updates:\n  enabled: false\n", encoding="utf-8")
        loaded = cfg_mod.load_config(p)
    assert loaded.updates.enabled is False


def test_resolve_config_path_and_directory_mounts():
    """When a directory is passed or mounted, resolve_config_path finds config.yaml."""
    import tempfile
    from pathlib import Path
    import pytest

    with tempfile.TemporaryDirectory() as td:
        dir_path = Path(td)
        cfg_file = dir_path / "config.yaml"
        cfg_file.write_text("bundle:\n  total_gb: 250\n", encoding="utf-8")

        # Passing directory directly resolves to config.yaml inside
        resolved = cfg_mod.resolve_config_path(dir_path)
        assert resolved == cfg_file

        loaded = cfg_mod.load_config(dir_path)
        assert loaded.bundle.total_gb == 250.0

    # If directory has no config.yaml, load_config fails loudly with FileNotFoundError
    with tempfile.TemporaryDirectory() as td:
        empty_dir = Path(td)
        with pytest.raises(FileNotFoundError):
            cfg_mod.load_config(empty_dir)

