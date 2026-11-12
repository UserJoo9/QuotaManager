"""Application & Service Catalog for Quota Manager.

Defines known applications, services, content categories, and their
features for policy targeting. Each app has:
- ID, name, category
- Domain patterns (for DNS-level identification)
- Features (for feature-level control)
- Capability requirements (what enforcement methods can identify it)
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("quota.app_catalog")

# --------------------------------------------------------------------------- #
# Data Models
# --------------------------------------------------------------------------- #

@dataclass
class AppFeature:
    """A controllable feature within an application."""
    id: str
    name: str
    description: str = ""
    # Can this feature be independently controlled?
    controllable: bool = False
    # What enforcement methods can detect this feature?
    detection_methods: list[str] = field(default_factory=list)
    # Notes about technical limitations
    notes: str = ""


@dataclass
class AppDefinition:
    """Definition of an application or service."""
    id: str
    name: str
    category: str
    # Domain patterns for DNS-level identification (wildcard supported)
    domains: list[str] = field(default_factory=list)
    # Content category
    content_category: str = ""
    # Features (feature-level control)
    features: list[AppFeature] = field(default_factory=list)
    # Capability: what enforcement methods can identify this app?
    dns_identifiable: bool = True
    firewall_identifiable: bool = False
    proxy_identifiable: bool = False
    # Notes
    notes: str = ""
    # Icon identifier
    icon: str = ""


# --------------------------------------------------------------------------- #
# Content Categories
# --------------------------------------------------------------------------- #

CONTENT_CATEGORIES = {
    "social_media": "Social Media",
    "video_streaming": "Video Streaming",
    "music_streaming": "Music & Audio",
    "gaming": "Gaming",
    "messaging": "Messaging & Chat",
    "news": "News & Information",
    "shopping": "Shopping & E-commerce",
    "cloud_services": "Cloud & Productivity",
    "education": "Education",
    "entertainment": "Entertainment",
    "file_sharing": "File Sharing & P2P",
    "vpn_proxy": "VPN & Proxy Services",
    "ads_trackers": "Advertisements & Trackers",
    "telemetry": "Telemetry & Analytics",
    "unknown": "Unclassified",
}


# --------------------------------------------------------------------------- #
# Application Catalog
# --------------------------------------------------------------------------- #

APP_CATALOG: dict[str, AppDefinition] = {}


def _register(app: AppDefinition) -> None:
    APP_CATALOG[app.id] = app


# --- Social Media ---
_register(AppDefinition(
    id="tiktok", name="TikTok", category="social_media",
    domains=["tiktok.com", "tiktokv.com", "tiktokcdn.com", "byteoversea.com", "ibytedtos.com", "musical.ly", "snssdk.com"],
    content_category="social_media",
    features=[
        AppFeature("shorts", "Short Videos", "TikTok short-form video feed", False, ["proxy"], "Requires HTTP proxy/SSL inspection to distinguish from regular content"),
        AppFeature("live", "Live Streaming", "TikTok live streams", False, ["proxy"], "Requires proxy inspection"),
        AppFeature("messaging", "Direct Messages", "TikTok DMs", False, ["proxy"], "Requires proxy inspection"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works. Feature-level control requires proxy.",
    icon="tiktok",
))

_register(AppDefinition(
    id="instagram", name="Instagram", category="social_media",
    domains=["instagram.com", "cdninstagram.com", "igcdn.com", "ig.me"],
    content_category="social_media",
    features=[
        AppFeature("reels", "Reels", "Instagram short-form video", False, ["proxy"], "Same domains as main app; requires proxy/SSL inspection"),
        AppFeature("explore", "Explore", "Instagram Explore tab", False, ["proxy"], "Requires proxy inspection"),
        AppFeature("stories", "Stories", "Instagram Stories", False, ["proxy"], "Requires proxy inspection"),
        AppFeature("messaging", "Direct Messages", "Instagram DMs", False, ["proxy"], "Requires proxy inspection"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works. Feature-level (Reels) requires proxy.",
    icon="instagram",
))

_register(AppDefinition(
    id="facebook", name="Facebook", category="social_media",
    domains=["facebook.com", "fbcdn.net", "fbsbx.com", "fb.com", "facebook.net", "meta.com"],
    content_category="social_media",
    features=[
        AppFeature("reels", "Reels", "Facebook Reels", False, ["proxy"], "Same domains; requires proxy"),
        AppFeature("watch", "Watch", "Facebook Watch videos", False, ["proxy"], "Requires proxy"),
        AppFeature("messaging", "Messenger", "Facebook Messenger", False, ["proxy"], "Requires proxy"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works. Feature-level requires proxy.",
    icon="facebook",
))

_register(AppDefinition(
    id="youtube", name="YouTube", category="video_streaming",
    domains=["youtube.com", "googlevideo.com", "youtu.be", "ytimg.com", "youtube-nocookie.com"],
    content_category="video_streaming",
    features=[
        AppFeature("shorts", "Shorts", "YouTube Shorts", False, ["proxy"], "Same domains as regular video; requires proxy/SSL inspection to distinguish"),
        AppFeature("live", "Live", "YouTube Live streams", False, ["proxy"], "Requires proxy"),
        AppFeature("music", "YouTube Music", "YouTube Music", False, ["dns", "proxy"], "Separate domain: music.youtube.com"),
        AppFeature("comments", "Comments", "Video comments", False, ["proxy"], "Requires proxy"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works. Shorts vs regular video requires proxy (same domains). YouTube Music has separate domain.",
    icon="youtube",
))

_register(AppDefinition(
    id="snapchat", name="Snapchat", category="social_media",
    domains=["snapchat.com", "sc-cdn.net", "snap.com"],
    content_category="social_media",
    features=[
        AppFeature("stories", "Stories", "Snapchat Stories", False, ["proxy"], "Requires proxy"),
        AppFeature("spotlight", "Spotlight", "Snapchat Spotlight (short video)", False, ["proxy"], "Requires proxy"),
        AppFeature("chat", "Chat", "Snapchat Chat", False, ["proxy"], "Requires proxy"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works. Feature-level requires proxy.",
    icon="snapchat",
))

_register(AppDefinition(
    id="twitter", name="X (Twitter)", category="social_media",
    domains=["twitter.com", "x.com", "twimg.com", "t.co"],
    content_category="social_media",
    features=[
        AppFeature("spaces", "Spaces", "Twitter Spaces (audio)", False, ["proxy"], "Requires proxy"),
        AppFeature("fleets", "Fleets", "Twitter Fleets", False, ["proxy"], "Requires proxy"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="twitter",
))

_register(AppDefinition(
    id="reddit", name="Reddit", category="social_media",
    domains=["reddit.com", "redd.it", "redditstatic.com", "redditmedia.com"],
    content_category="social_media",
    features=[
        AppFeature("nsfw", "NSFW Content", "Not Safe For Work content", False, ["proxy"], "Requires proxy + content classification"),
    ],
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="reddit",
))

_register(AppDefinition(
    id="discord", name="Discord", category="messaging",
    domains=["discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media"],
    content_category="messaging",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="discord",
))

_register(AppDefinition(
    id="telegram", name="Telegram", category="messaging",
    domains=["telegram.org", "t.me", "telesco.pe", "telegram.me"],
    content_category="messaging",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works. Telegram uses MTProto (not HTTPS) so may be easier to identify at network level.",
    icon="telegram",
))

_register(AppDefinition(
    id="twitch", name="Twitch", category="video_streaming",
    domains=["twitch.tv", "ttvnw.net", "jtvnw.net"],
    content_category="video_streaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="twitch",
))

# --- Streaming ---
_register(AppDefinition(
    id="netflix", name="Netflix", category="video_streaming",
    domains=["netflix.com", "nflxvideo.net", "nflximg.net", "nflxext.com", "nflxso.net"],
    content_category="video_streaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="netflix",
))

_register(AppDefinition(
    id="spotify", name="Spotify", category="music_streaming",
    domains=["spotify.com", "scdn.co", "spoti.fi", "spotifycdn.com"],
    content_category="music_streaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="spotify",
))

# --- Gaming ---
_register(AppDefinition(
    id="steam", name="Steam", category="gaming",
    domains=["steampowered.com", "steamcommunity.com", "steamstatic.com", "steamcontent.com", "steamcdn-a.akamaihd.net"],
    content_category="gaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="steam",
))

_register(AppDefinition(
    id="epic", name="Epic Games", category="gaming",
    domains=["epicgames.com", "unrealengine.com", "epicgames-download1.akamaized.net"],
    content_category="gaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="epic",
))

_register(AppDefinition(
    id="playstation", name="PlayStation", category="gaming",
    domains=["playstation.com", "sonyentertainmentnetwork.com", "playstation.net"],
    content_category="gaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="playstation",
))

_register(AppDefinition(
    id="xbox", name="Xbox", category="gaming",
    domains=["xbox.com", "xboxlive.com", "xboxservices.com"],
    content_category="gaming",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works.",
    icon="xbox",
))

# --- Productivity/Cloud ---
_register(AppDefinition(
    id="google_services", name="Google Services", category="cloud_services",
    domains=["google.com", "googleapis.com", "gstatic.com", "1e100.net", "googleusercontent.com", "youtube.com"],
    content_category="cloud_services",
    dns_identifiable=True, proxy_identifiable=True,
    notes="Broad category; blocking affects many services.",
    icon="google",
))

_register(AppDefinition(
    id="microsoft", name="Microsoft", category="cloud_services",
    domains=["microsoft.com", "live.com", "office.com", "office365.com", "windowsupdate.com", "msftncsi.com", "bing.com"],
    content_category="cloud_services",
    dns_identifiable=True, proxy_identifiable=True,
    notes="Broad category; blocking affects many services.",
    icon="microsoft",
))

_register(AppDefinition(
    id="apple", name="Apple Services", category="cloud_services",
    domains=["apple.com", "icloud.com", "mzstatic.com", "aaplimg.com"],
    content_category="cloud_services",
    dns_identifiable=True, proxy_identifiable=True,
    notes="Broad category.",
    icon="apple",
))

# --- Ads & Trackers ---
_register(AppDefinition(
    id="ads_trackers", name="Advertisements & Trackers", category="ads_trackers",
    domains=["doubleclick.net", "googleadservices.com", "googlesyndication.com", "facebook.com/tr", "analytics.google.com"],
    content_category="ads_trackers",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works for known ad domains.",
    icon="ads",
))

# --- VPN/Proxy ---
_register(AppDefinition(
    id="vpn_services", name="VPN & Proxy Services", category="vpn_proxy",
    domains=["nordvpn.com", "expressvpn.com", "surfshark.com", "protonvpn.com", "mullvad.net"],
    content_category="vpn_proxy",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works but easily bypassed by hardcoded IPs.",
    icon="vpn",
))

# --- File Sharing ---
_register(AppDefinition(
    id="torrent_p2p", name="Torrent / P2P", category="file_sharing",
    domains=["bittorrent.com", "utorrent.com", "thepiratebay.org", "1337x.to"],
    content_category="file_sharing",
    dns_identifiable=True, proxy_identifiable=True,
    notes="DNS-level blocking works but easily bypassed.",
    icon="download",
))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def get_app(app_id: str) -> AppDefinition | None:
    return APP_CATALOG.get(app_id)


def list_apps(category: str | None = None) -> list[AppDefinition]:
    apps = list(APP_CATALOG.values())
    if category:
        apps = [a for a in apps if a.category == category]
    return sorted(apps, key=lambda a: a.name)


def list_categories() -> list[str]:
    return sorted(set(a.category for a in APP_CATALOG.values()))


def get_app_domains(app_id: str) -> list[str]:
    app = APP_CATALOG.get(app_id)
    return app.domains if app else []


def get_app_features(app_id: str) -> list[AppFeature]:
    app = APP_CATALOG.get(app_id)
    return app.features if app else []


def is_feature_controllable(app_id: str, feature_id: str) -> bool:
    app = APP_CATALOG.get(app_id)
    if not app:
        return False
    for f in app.features:
        if f.id == feature_id:
            return f.controllable
    return False


def get_capability_matrix() -> dict[str, Any]:
    """Return capability matrix: what enforcement methods can identify each app."""
    matrix = {}
    for app_id, app in APP_CATALOG.items():
        matrix[app_id] = {
            "name": app.name,
            "category": app.category,
            "dns_identifiable": app.dns_identifiable,
            "proxy_identifiable": app.proxy_identifiable,
            "feature_control": any(f.controllable for f in app.features),
            "controllable_features": [f.id for f in app.features if f.controllable],
        }
    return matrix


def to_dict() -> dict[str, Any]:
    return {
        "apps": {aid: asdict(a) for aid, a in APP_CATALOG.items()},
        "categories": CONTENT_CATEGORIES,
        "count": len(APP_CATALOG),
    }