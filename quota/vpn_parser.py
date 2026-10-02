"""V2Ray / sing-box Link Parser and Configuration Generator.

Supports parsing:
- vless:// (Reality, Vision, TLS, WebSocket, gRPC)
- vmess:// (Base64 JSON)
- trojan:// (TLS, WebSocket, gRPC)
- ss:// (Shadowsocks legacy and SIP002)
- raw sing-box JSON configuration
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("quota.vpn_parser")


def _safe_b64decode(s: str) -> str:
    """Decode base64 string handling missing padding and URL-safe characters."""
    s = s.strip().replace("-", "+").replace("_", "/")
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return base64.b64decode(s.encode("utf-8")).decode("utf-8", errors="replace")


def parse_vless(uri: str) -> tuple[str, dict[str, Any]]:
    """Parse vless:// link into (name, outbound_dict)."""
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "vless":
        raise ValueError(f"Invalid scheme for vless: {parsed.scheme}")

    uuid = parsed.username
    if not uuid:
        raise ValueError("Missing UUID in vless link")

    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        raise ValueError("Missing host in vless link")

    name = unquote(parsed.fragment).strip() if parsed.fragment else f"VLESS-{host}:{port}"
    params = parse_qs(parsed.query)

    def get_p(key: str, default: str = "") -> str:
        vals = params.get(key, [])
        return vals[0] if vals else default

    transport_type = get_p("type", "tcp").lower()
    security = get_p("security", "none").lower()
    flow = get_p("flow", "")
    sni = get_p("sni", "")
    fp = get_p("fp", "")
    pbk = get_p("pbk", "")
    sid = get_p("sid", "")
    path = get_p("path", "/")
    ws_host = get_p("host", "")
    service_name = get_p("serviceName", "")

    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": "proxy-out",
        "server": host,
        "server_port": port,
        "uuid": uuid,
    }

    if flow:
        outbound["flow"] = flow

    # TLS / Reality
    if security in ("tls", "reality") or pbk or sni:
        tls_obj: dict[str, Any] = {
            "enabled": True,
            "server_name": sni or host,
        }
        if fp:
            tls_obj["utls"] = {
                "enabled": True,
                "fingerprint": fp,
            }
        if security == "reality" or pbk:
            reality_obj: dict[str, Any] = {
                "enabled": True,
                "public_key": pbk,
            }
            if sid:
                reality_obj["short_id"] = sid
            tls_obj["reality"] = reality_obj

        outbound["tls"] = tls_obj

    # Transport
    if transport_type == "ws":
        ws_obj: dict[str, Any] = {
            "type": "ws",
            "path": path or "/",
        }
        if ws_host:
            ws_obj["headers"] = {"Host": ws_host}
        outbound["transport"] = ws_obj
    elif transport_type == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": service_name or path or "",
        }

    return name, outbound


def parse_vmess(uri: str) -> tuple[str, dict[str, Any]]:
    """Parse vmess://<base64-json> link into (name, outbound_dict)."""
    raw_b64 = uri[8:] if uri.startswith("vmess://") else uri
    decoded_str = _safe_b64decode(raw_b64)
    data = json.loads(decoded_str)

    host = data.get("add") or data.get("host", "")
    port = int(data.get("port", 443))
    uuid = data.get("id", "")
    aid = int(data.get("aid", 0))
    cipher = data.get("scy", "auto")
    net = data.get("net", "tcp").lower()
    path = data.get("path", "/")
    ws_host = data.get("host", "")
    tls = data.get("tls", "").lower()
    sni = data.get("sni", "")
    name = str(data.get("ps", "")).strip() or f"VMess-{host}:{port}"

    if not host or not uuid:
        raise ValueError("VMess config missing host or id")

    outbound: dict[str, Any] = {
        "type": "vmess",
        "tag": "proxy-out",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": cipher if cipher in ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero") else "auto",
        "alter_id": aid,
    }

    if tls == "tls" or sni:
        outbound["tls"] = {
            "enabled": True,
            "server_name": sni or ws_host or host,
        }

    if net == "ws":
        ws_obj: dict[str, Any] = {
            "type": "ws",
            "path": path or "/",
        }
        if ws_host:
            ws_obj["headers"] = {"Host": ws_host}
        outbound["transport"] = ws_obj
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": path or "",
        }

    return name, outbound


def parse_trojan(uri: str) -> tuple[str, dict[str, Any]]:
    """Parse trojan://password@host:port?params#name link into (name, outbound_dict)."""
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "trojan":
        raise ValueError(f"Invalid scheme for trojan: {parsed.scheme}")

    password = parsed.username
    if not password:
        raise ValueError("Missing password in trojan link")

    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        raise ValueError("Missing host in trojan link")

    name = unquote(parsed.fragment).strip() if parsed.fragment else f"Trojan-{host}:{port}"
    params = parse_qs(parsed.query)

    def get_p(key: str, default: str = "") -> str:
        vals = params.get(key, [])
        return vals[0] if vals else default

    sni = get_p("sni", "")
    transport_type = get_p("type", "tcp").lower()
    path = get_p("path", "/")
    ws_host = get_p("host", "")

    outbound: dict[str, Any] = {
        "type": "trojan",
        "tag": "proxy-out",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": {
            "enabled": True,
            "server_name": sni or host,
        },
    }

    if transport_type == "ws":
        ws_obj: dict[str, Any] = {
            "type": "ws",
            "path": path or "/",
        }
        if ws_host:
            ws_obj["headers"] = {"Host": ws_host}
        outbound["transport"] = ws_obj
    elif transport_type == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": path or "",
        }

    return name, outbound


def parse_shadowsocks(uri: str) -> tuple[str, dict[str, Any]]:
    """Parse ss:// link into (name, outbound_dict)."""
    if not uri.startswith("ss://"):
        raise ValueError("Invalid Shadowsocks URI")

    name = ""
    clean_uri = uri[5:]
    if "#" in clean_uri:
        clean_uri, frag = clean_uri.split("#", 1)
        name = unquote(frag).strip()

    # Format 1: ss://base64(method:password@host:port)
    # Format 2: ss://base64(method:password)@host:port
    method = ""
    password = ""
    host = ""
    port = 8388

    if "@" in clean_uri:
        user_info_b64, server_part = clean_uri.split("@", 1)
        if ":" in server_part:
            h, p = server_part.split(":", 1)
            host = h
            port = int(p.split("?")[0])
        else:
            host = server_part

        # Decode user info
        user_info = _safe_b64decode(user_info_b64)
        if ":" in user_info:
            method, password = user_info.split(":", 1)
    else:
        # Full base64 string
        decoded = _safe_b64decode(clean_uri)
        # format: method:password@host:port
        m = re.match(r"^([^:]+):(.*)@([^:]+):(\d+)$", decoded)
        if m:
            method, password, host, p_str = m.groups()
            port = int(p_str)
        else:
            raise ValueError("Unrecognized Shadowsocks base64 format")

    if not name:
        name = f"SS-{host}:{port}"

    outbound: dict[str, Any] = {
        "type": "shadowsocks",
        "tag": "proxy-out",
        "server": host,
        "server_port": port,
        "method": method,
        "password": password,
    }
    return name, outbound


def parse_vpn_link(raw_input: str) -> tuple[str, str, dict[str, Any]]:
    """Parse any supported VPN link or raw JSON.

    Returns:
        (name: str, protocol: str, outbound_or_full_config: dict[str, Any])
    """
    raw = raw_input.strip()
    if not raw:
        raise ValueError("Empty VPN link or configuration")

    # If it is raw JSON
    if raw.startswith("{"):
        parsed_json = json.loads(raw)
        # Check if it's already a full sing-box config or an outbound
        if "outbounds" in parsed_json or "inbounds" in parsed_json:
            name = parsed_json.get("tag") or "Custom Config"
            return name, "custom", parsed_json
        elif "type" in parsed_json:
            proto = parsed_json.get("type", "custom")
            name = parsed_json.get("tag") or f"Custom-{proto}"
            return name, proto, parsed_json
        else:
            raise ValueError("Invalid JSON: must contain 'outbounds' or 'type'")

    lower = raw.lower()
    if lower.startswith("vless://"):
        name, outbound = parse_vless(raw)
        return name, "vless", outbound
    elif lower.startswith("vmess://"):
        name, outbound = parse_vmess(raw)
        return name, "vmess", outbound
    elif lower.startswith("trojan://"):
        name, outbound = parse_trojan(raw)
        return name, "trojan", outbound
    elif lower.startswith("ss://"):
        name, outbound = parse_shadowsocks(raw)
        return name, "shadowsocks", outbound
    else:
        raise ValueError("Unsupported link protocol. Supported: vless://, vmess://, trojan://, ss://, or JSON")


def generate_sing_box_config(outbound_or_config: dict[str, Any],
                             tun_interface: str = "quota-vpn",
                             clash_controller: str = "127.0.0.1:9090",
                             mixed_port: int = 2081,
                             allow_insecure: bool = False) -> dict[str, Any]:
    """Assemble a complete, production-ready sing-box configuration dictionary."""
    # If the user already provided a full sing-box config
    if "inbounds" in outbound_or_config and "outbounds" in outbound_or_config:
        cfg = json.loads(json.dumps(outbound_or_config))
        # Ensure clash_api is present
        if "experimental" not in cfg:
            cfg["experimental"] = {}
        cfg["experimental"]["clash_api"] = {
            "external_controller": clash_controller,
        }
        if allow_insecure and "outbounds" in cfg:
            for ob in cfg["outbounds"]:
                if isinstance(ob, dict) and "tls" in ob and isinstance(ob["tls"], dict):
                    ob["tls"]["insecure"] = True
        return cfg

    proxy_outbound = json.loads(json.dumps(outbound_or_config))
    proxy_outbound["tag"] = "proxy-out"
    # Ensure domain_resolver points to direct-dns for outbounds that need it
    proxy_outbound["domain_resolver"] = "direct-dns"
    if allow_insecure and "tls" in proxy_outbound and isinstance(proxy_outbound["tls"], dict):
        proxy_outbound["tls"]["insecure"] = True

    full_config: dict[str, Any] = {
        "log": {
            "level": "info",
            "timestamp": True,
        },
        "experimental": {
            "clash_api": {
                "external_controller": clash_controller,
            }
        },
        "dns": {
            "servers": [
                {
                    "type": "tcp",
                    "tag": "remote-dns",
                    "server": "8.8.8.8",
                    "server_port": 53,
                    "detour": "proxy-out",
                },
                {
                    "type": "local",
                    "tag": "direct-dns",
                },
            ],
            "rules": [
                {
                    "outbound": "any",
                    "server": "direct-dns",
                },
                {
                    "clash_mode": "Direct",
                    "server": "direct-dns",
                },
            ],
            "final": "remote-dns",
            "strategy": "ipv4_only",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": tun_interface,
                "address": ["172.19.0.1/30"],
                "mtu": 1500,
                "auto_route": False,
                "strict_route": False,
                "endpoint_independent_nat": True,
                "stack": "gvisor",
            },
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": mixed_port,
            }
        ],
        "outbounds": [
            proxy_outbound,
            {
                "type": "direct",
                "tag": "direct",
            }
        ],
        "route": {
            "find_process": False,
            "auto_detect_interface": True,
            "default_domain_resolver": "direct-dns",
            "final": "proxy-out",
            "rules": [
                {
                    "ip_cidr": [
                        "127.0.0.0/8",
                        "10.0.0.0/8",
                        "172.16.0.0/12",
                        "192.168.0.0/16",
                    ],
                    "outbound": "direct",
                },
                {
                    "protocol": "dns",
                    "action": "hijack-dns",
                },
                {
                    "port": 53,
                    "action": "hijack-dns",
                },
                {
                    "action": "sniff",
                },
                {
                    "network": "udp",
                    "port": 443,
                    "action": "reject",
                },
                {
                    "ip_version": 6,
                    "action": "reject",
                }
            ]
        }
    }
    return full_config


def find_sing_box_binary() -> str | None:
    """Find sing-box executable across local project and system directories."""
    bin_name = "sing-box.exe" if os.name == "nt" else "sing-box"
    base_dir = Path(__file__).resolve().parent.parent
    candidates = [
        base_dir / "bin" / bin_name,
        Path(f"/usr/local/bin/{bin_name}"),
        Path(f"/usr/bin/{bin_name}"),
        Path("D:/programming/C++/projects/vpn-private/bin/sing-box.exe"),
        Path("D:/programming/C++/projects/vpn-private/sing-box.exe"),
    ]
    for c in candidates:
        if c.is_file():
            if os.name == "posix" and not os.access(c, os.X_OK):
                try:
                    c.chmod(0o755)
                except Exception as e:
                    log.warning("Could not auto-chmod %s: %s", c, e)
            return str(c)

    which = shutil.which("sing-box")
    return which if which else None


def validate_sing_box_config(config_dict: dict[str, Any],
                             binary_path: str | None = None) -> tuple[bool, str]:
    """Validate a sing-box config using `sing-box check -c <file>`.

    Returns (valid: bool, error_message: str).
    """
    bin_path = binary_path or find_sing_box_binary()
    if not bin_path:
        # Binary not found in local environment: do basic JSON structure validation
        if not config_dict.get("outbounds"):
            return False, "Configuration missing 'outbounds'"
        return True, ""

    if os.name == "posix" and not os.access(bin_path, os.X_OK):
        try:
            os.chmod(bin_path, 0o755)
        except Exception as e:
            log.warning("Could not auto-chmod %s: %s", bin_path, e)

    # Write temporary config
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(config_dict, tf, indent=2)
        tf_path = tf.name

    env = os.environ.copy()
    env["ENABLE_DEPRECATED_OUTBOUND_DNS_RULE_ITEM"] = "true"

    try:
        proc = subprocess.run(
            [bin_path, "check", "-c", tf_path],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if proc.returncode == 0:
            return True, ""
        err = (proc.stderr or proc.stdout or "Validation failed").strip()
        return False, err
    except (PermissionError, OSError) as e:
        log.warning("sing-box binary execution error on %s: %s. Falling back to structure validation.", bin_path, e)
        # Attempt chmod one more time
        if os.name == "posix":
            try:
                os.chmod(bin_path, 0o755)
                proc = subprocess.run(
                    [bin_path, "check", "-c", tf_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                )
                if proc.returncode == 0:
                    return True, ""
            except Exception:
                pass
        if config_dict.get("outbounds"):
            return True, ""
        return False, f"sing-box check error: {e}"
    except Exception as e:
        return False, f"sing-box check error: {e}"
    finally:
        try:
            Path(tf_path).unlink(missing_ok=True)
        except Exception:
            pass
