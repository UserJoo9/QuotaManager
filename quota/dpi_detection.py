"""DPI / Proxy application detection for Quota Manager.

Detects applications (TikTok, YouTube, Instagram, etc.) and proxy/VPN
services from network signals: DNS queries, TLS SNI, known ports/IP ranges.

This is a heuristic layer — it matches against known signatures and returns
confidence scores. It does not decrypt traffic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Application signatures
# --------------------------------------------------------------------------- #

@dataclass
class AppSignature:
    name: str
    category: str
    domains: list[str] = field(default_factory=list)
    snis: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    confidence: float = 0.7


APP_SIGNATURES: list[AppSignature] = [
    AppSignature("TikTok", "social",
                 domains=["tiktok.com", "musical.ly", "tiktokv.com"],
                 snis=["tiktok.com", "musical.ly", "tiktokv.com"],
                 ports=[443, 80]),
    AppSignature("Instagram", "social",
                 domains=["instagram.com", "cdninstagram.com", "fbcdn.net"],
                 snis=["instagram.com", "cdninstagram.com"],
                 ports=[443, 80]),
    AppSignature("Facebook", "social",
                 domains=["facebook.com", "fb.com", "fbcdn.net"],
                 snis=["facebook.com", "fb.com"],
                 ports=[443, 80]),
    AppSignature("YouTube", "video",
                 domains=["youtube.com", "youtu.be", "googlevideo.com"],
                 snis=["youtube.com", "youtu.be", "googlevideo.com"],
                 ports=[443, 80]),
    AppSignature("Snapchat", "social",
                 domains=["snapchat.com", "scdn.co"],
                 snis=["snapchat.com", "scdn.co"],
                 ports=[443, 80]),
    AppSignature("X (Twitter)", "social",
                 domains=["twitter.com", "x.com", "t.co"],
                 snis=["twitter.com", "x.com"],
                 ports=[443, 80]),
    AppSignature("Reddit", "social",
                 domains=["reddit.com", "redd.it"],
                 snis=["reddit.com"],
                 ports=[443, 80]),
    AppSignature("Discord", "social",
                 domains=["discord.com", "discord.gg", "discordapp.com"],
                 snis=["discord.com", "discord.gg"],
                 ports=[443, 80]),
    AppSignature("Telegram", "messaging",
                 domains=["telegram.org", "t.me", "telegram.me"],
                 snis=["telegram.org", "t.me"],
                 ports=[443, 80, 443]),
    AppSignature("Twitch", "video",
                 domains=["twitch.tv", "ttvnw.net"],
                 snis=["twitch.tv"],
                 ports=[443, 80]),
    AppSignature("Netflix", "video",
                 domains=["netflix.com", "nflxvideo.net", "nflxext.com"],
                 snis=["netflix.com", "nflxvideo.net"],
                 ports=[443, 80]),
    AppSignature("Spotify", "music",
                 domains=["spotify.com", "scdn.co", "spotifycdn.com"],
                 snis=["spotify.com"],
                 ports=[443, 80]),
    AppSignature("Steam", "gaming",
                 domains=["steampowered.com", "steamstatic.com", "steamgames.com"],
                 snis=["steampowered.com"],
                 ports=[443, 80, 27015]),
    AppSignature("PlayStation", "gaming",
                 domains=["playstation.com", "sonyentertainmentnetwork.com"],
                 snis=["playstation.com"],
                 ports=[443, 80]),
    AppSignature("Xbox", "gaming",
                 domains=["xboxlive.com", "xbox.com"],
                 snis=["xboxlive.com"],
                 ports=[443, 80]),
]

# --------------------------------------------------------------------------- #
# Proxy / VPN signatures
# --------------------------------------------------------------------------- #

PROXY_SIGNATURES: list[AppSignature] = [
    AppSignature("Tailscale", "vpn",
                 domains=["tailscale.com", "tailscale.io"],
                 snis=["tailscale.com", "tailscale.io"],
                 ports=[443, 41641]),
    AppSignature("WireGuard", "vpn",
                 domains=[],
                 snis=[],
                 ports=[51820]),
    AppSignature("OpenVPN", "vpn",
                 domains=[],
                 snis=[],
                 ports=[1194, 443]),
    AppSignature("Tor", "proxy",
                 domains=["torproject.org"],
                 snis=["torproject.org"],
                 ports=[9001, 9030, 9050]),
    AppSignature("Shadowsocks", "proxy",
                 domains=[],
                 snis=[],
                 ports=[8388, 8389, 1080]),
    AppSignature("V2Ray", "proxy",
                 domains=[],
                 snis=[],
                 ports=[8080, 443, 1080]),
    AppSignature("Trojan", "proxy",
                 domains=[],
                 snis=[],
                 ports=[443]),
]


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _match_domain(host: str, sig: AppSignature) -> bool:
    host = host.lower().rstrip(".")
    for d in sig.domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return True
    return False


def _match_sni(sni: str, sig: AppSignature) -> bool:
    sni = sni.lower().rstrip(".")
    for s in sig.snis:
        s = s.lower()
        if sni == s or sni.endswith("." + s):
            return True
    return False


def detect_app(dns_query: str | None = None, tls_sni: str | None = None,
               dst_port: int | None = None) -> list[dict[str, Any]]:
    """Detect applications from network signals. Returns list of matches."""
    results: list[dict[str, Any]] = []
    for sig in APP_SIGNATURES:
        score = 0.0
        matched_by: list[str] = []

        if dns_query and _match_domain(dns_query, sig):
            score += 0.4
            matched_by.append("dns")
        if tls_sni and _match_sni(tls_sni, sig):
            score += 0.4
            matched_by.append("sni")
        if dst_port and dst_port in sig.ports:
            score += 0.2
            matched_by.append(f"port:{dst_port}")

        if score > 0:
            conf = min(score * sig.confidence, 1.0)
            results.append({
                "app": sig.name,
                "category": sig.category,
                "confidence": round(conf, 2),
                "matched_by": matched_by,
            })

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results


def detect_proxy(dns_query: str | None = None, tls_sni: str | None = None,
                 dst_port: int | None = None) -> list[dict[str, Any]]:
    """Detect proxy/VPN services from network signals."""
    results: list[dict[str, Any]] = []
    for sig in PROXY_SIGNATURES:
        score = 0.0
        matched_by: list[str] = []

        if dns_query and _match_domain(dns_query, sig):
            score += 0.4
            matched_by.append("dns")
        if tls_sni and _match_sni(tls_sni, sig):
            score += 0.4
            matched_by.append("sni")
        if dst_port and dst_port in sig.ports:
            score += 0.2
            matched_by.append(f"port:{dst_port}")

        if score > 0:
            conf = min(score * sig.confidence, 1.0)
            results.append({
                "service": sig.name,
                "category": sig.category,
                "confidence": round(conf, 2),
                "matched_by": matched_by,
            })

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results


def list_apps() -> list[dict[str, str]]:
    """Return the catalog of detectable applications."""
    return [{"name": s.name, "category": s.category} for s in APP_SIGNATURES]


def list_proxies() -> list[dict[str, str]]:
    """Return the catalog of detectable proxy/VPN services."""
    return [{"name": s.name, "category": s.category} for s in PROXY_SIGNATURES]