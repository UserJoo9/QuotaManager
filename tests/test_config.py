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
