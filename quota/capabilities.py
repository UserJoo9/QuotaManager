"""Capability Detection — what can this environment actually do?

Determines which enforcement methods, identification techniques, and
features are available based on the actual environment (OS, tools, config).
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any

log = logging.getLogger("quota.capabilities")

# --------------------------------------------------------------------------- #
# Detection functions
# --------------------------------------------------------------------------- #

def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None

def _is_root() -> bool:
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False

def _file_exists(path: str) -> bool:
    return os.path.exists(path)

def _can_write(path: str) -> bool:
    return os.access(path, os.W_OK)

# --------------------------------------------------------------------------- #
# Capability matrix
# --------------------------------------------------------------------------- #

def detect_capabilities() -> dict[str, Any]:
    """Detect all capabilities of the current environment."""
    root = _is_root()
    nft = _has_binary("nft")
    tc = _has_binary("tc")
    dnsmasq = _has_binary("dnsmasq")
    conntrack = _file_exists("/proc/net/nf_conntrack")
    acct_sysctl = _file_exists("/proc/sys/net/netfilter/nf_conntrack_acct")

    capabilities = {
        "environment": {
            "is_root": root,
            "os": os.uname().sysname if hasattr(os, "uname") else "unknown",
        },
        "enforcement": {
            "dns": {
                "available": dnsmasq,
                "level": "supported" if dnsmasq else "requires_config",
                "notes": "dnsmasq-based DNS filtering" if dnsmasq else "dnsmasq not found",
            },
            "firewall": {
                "available": nft and root,
                "level": "supported" if (nft and root) else ("requires_config" if nft else "unsupported"),
                "notes": "nftables firewall" if (nft and root) else "nft not found or not root",
            },
            "traffic_control": {
                "available": tc and root,
                "level": "supported" if (tc and root) else ("requires_config" if tc else "unsupported"),
                "notes": "tc traffic shaping" if (tc and root) else "tc not found or not root",
            },
            "conntrack": {
                "available": conntrack,
                "level": "supported" if conntrack else "unsupported",
                "notes": "connection tracking for flow accounting" if conntrack else "no conntrack",
            },
            "conntrack_accounting": {
                "available": acct_sysctl,
                "level": "supported" if acct_sysctl else "requires_config",
                "notes": "per-flow byte counters" if acct_sysctl else "nf_conntrack_acct not available",
            },
            "proxy": {
                "available": False,
                "level": "unsupported",
                "notes": "HTTP proxy not implemented yet",
            },
            "endpoint_agent": {
                "available": False,
                "level": "unsupported",
                "notes": "Endpoint agent not implemented",
            },
            "browser_extension": {
                "available": False,
                "level": "unsupported",
                "notes": "Browser extension not implemented",
            },
        },
        "identification": {
            "dns_level": {
                "available": True,
                "level": "supported",
                "notes": "Can identify apps by domain name via DNS",
            },
            "ip_level": {
                "available": conntrack,
                "level": "supported" if conntrack else "unsupported",
                "notes": "Can identify by IP via conntrack" if conntrack else "no conntrack",
            },
            "app_level": {
                "available": False,
                "level": "unsupported",
                "notes": "Application-level identification requires proxy or DPI",
            },
            "feature_level": {
                "available": False,
                "level": "unsupported",
                "notes": "Feature-level control (Shorts, Reels) requires proxy/SSL inspection",
            },
        },
        "policy_features": {
            "global_scope": {"available": True, "level": "supported"},
            "user_scope": {"available": True, "level": "supported"},
            "device_scope": {"available": True, "level": "supported"},
            "group_scope": {"available": True, "level": "supported"},
            "app_scope": {"available": True, "level": "supported"},
            "content_category_scope": {"available": True, "level": "supported"},
            "time_scheduling": {"available": True, "level": "supported"},
            "combined_conditions": {"available": True, "level": "supported"},
            "feature_control": {"available": False, "level": "unsupported", "notes": "Requires proxy"},
            "policy_simulation": {"available": True, "level": "supported"},
            "audit_logging": {"available": True, "level": "supported"},
            "conflict_resolution": {"available": True, "level": "supported"},
        },
    }
    return capabilities


def get_capability_summary() -> str:
    """Human-readable capability summary."""
    caps = detect_capabilities()
    lines = ["=== Capability Summary ==="]
    for section, items in caps.items():
        lines.append(f"\n[{section}]")
        if isinstance(items, dict):
            for k, v in items.items():
                if isinstance(v, dict):
                    avail = v.get("available", v.get("level", "?"))
                    notes = v.get("notes", "")
                    lines.append(f"  {k}: {avail} {notes}")
                else:
                    lines.append(f"  {k}: {v}")
    return "\n".join(lines)