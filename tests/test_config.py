"""Config loading + the Linux-only surface (no Windows remnants).

Guards the sweep: the Linux config has no ``arp:`` section and no electric-cut
``fallback_*`` fields.
"""

from __future__ import annotations

from core import config as cfg_mod


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
