"""Browsing History Categorization and Visual Analytics.

Classifies DNS queries into:
- Popular Apps & Services (YouTube, Facebook, WhatsApp, TikTok, Netflix, Gaming, etc.)
- Registrable Websites and Domains
- Time-series activity distribution for interactive graphs
"""

from __future__ import annotations

import collections
import datetime as _dt
from typing import Any

from quota.db import Database

# Service definitions: (Pattern list, Display Name, Category, Brand Color, Icon identifier)
KNOWN_SERVICES: list[tuple[list[str], str, str, str, str]] = [
    (
        ["youtube.com", "googlevideo.com", "youtu.be", "ytimg.com"],
        "YouTube", "Video & Streaming", "#FF0000", "youtube"
    ),
    (
        ["facebook.com", "fbcdn.net", "fbsbx.com", "fb.com", "facebook.net", "meta.com"],
        "Facebook", "Social Media", "#1877F2", "facebook"
    ),
    (
        ["whatsapp.com", "whatsapp.net"],
        "WhatsApp", "Messaging & Calls", "#25D366", "whatsapp"
    ),
    (
        ["instagram.com", "cdninstagram.com"],
        "Instagram", "Social Media", "#E4405F", "instagram"
    ),
    (
        ["tiktok.com", "tiktokv.com", "tiktokcdn.com", "byteoversea.com", "ibytedtos.com", "musical.ly"],
        "TikTok", "Short Video", "#00F2FE", "tiktok"
    ),
    (
        ["netflix.com", "nflxvideo.net", "nflximg.net", "nflxext.com"],
        "Netflix", "Video & Streaming", "#E50914", "netflix"
    ),
    (
        ["telegram.org", "t.me", "telesco.pe"],
        "Telegram", "Messaging", "#229ED9", "telegram"
    ),
    (
        ["twitter.com", "x.com", "twimg.com", "t.co"],
        "X (Twitter)", "Social Media", "#1DA1F2", "twitter"
    ),
    (
        ["discord.com", "discord.gg", "discordapp.com", "discordapp.net"],
        "Discord", "Communication", "#5865F2", "discord"
    ),
    (
        ["steampowered.com", "steamcommunity.com", "steamstatic.com", "steamcontent.com"],
        "Steam", "Gaming", "#171A21", "steam"
    ),
    (
        ["riotgames.com", "leagueoflegends.com", "pvp.net"],
        "Riot Games", "Gaming", "#D32936", "riot"
    ),
    (
        ["roblox.com", "rbxcdn.com"],
        "Roblox", "Gaming", "#000000", "roblox"
    ),
    (
        ["spotify.com", "scdn.co", "spoti.fi"],
        "Spotify", "Music & Audio", "#1DB954", "spotify"
    ),
    (
        ["apple.com", "icloud.com", "mzstatic.com", "aaplimg.com"],
        "Apple Services", "Cloud & Devices", "#A2AAAD", "apple"
    ),
    (
        ["microsoft.com", "live.com", "office.com", "office365.com", "windowsupdate.com", "msftncsi.com", "bing.com"],
        "Microsoft", "Cloud & Software", "#00A4EF", "microsoft"
    ),
    (
        ["google.com", "google.com.eg", "googleapis.com", "gstatic.com", "1e100.net", "googleusercontent.com"],
        "Google Services", "Search & Web", "#4285F4", "google"
    ),
    (
        ["chatgpt.com", "openai.com", "oaistatic.com", "oaiusercontent.com"],
        "ChatGPT / AI", "AI & Tools", "#10A37F", "chatgpt"
    ),
    (
        ["snapchat.com", "sc-cdn.net"],
        "Snapchat", "Social Media", "#FFFC00", "snapchat"
    ),
    (
        ["amazon.com", "aws.amazon.com", "amazonaws.com", "cloudfront.net"],
        "Amazon & AWS", "Shopping & Cloud", "#FF9900", "amazon"
    ),
    (
        ["cloudflare.com", "cloudflare-dns.com"],
        "Cloudflare", "CDN & Infrastructure", "#F38020", "cloudflare"
    ),
    (
        ["twitch.tv", "ttvnw.net"],
        "Twitch", "Live Streaming", "#9146FF", "twitch"
    ),
    (
        ["reddit.com", "redd.it", "redditstatic.com"],
        "Reddit", "Community", "#FF4500", "reddit"
    ),
    (
        ["github.com", "githubusercontent.com"],
        "GitHub", "Developer Tools", "#24292E", "github"
    ),
]


def classify_domain(domain: str) -> tuple[str, str, str, str] | None:
    """Classify a domain into a known app/service.

    Returns:
        (DisplayName, Category, HexColor, IconName) or None if unclassified.
    """
    clean = domain.strip().lower()
    for patterns, name, category, color, icon in KNOWN_SERVICES:
        for p in patterns:
            if clean == p or clean.endswith("." + p):
                return name, category, color, icon
    return None


def get_base_domain(domain: str) -> str:
    """Extract registrable root domain (e.g. news.bbc.co.uk -> bbc.co.uk)."""
    parts = domain.strip().lower().split(".")
    if len(parts) <= 2:
        return domain
    # Common 2-part ccTLDs: .co.uk, .com.eg, .org.eg, .com.sa, etc.
    second_levels = {"co", "com", "org", "net", "gov", "edu"}
    if len(parts) >= 3 and parts[-2] in second_levels and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def get_history_analytics(db: Database,
                                device_id: int | None = None,
                                hours: int = 24,
                                precomputed_raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile rich visual analytics from DNS history."""
    now = _dt.datetime.now().astimezone()
    if precomputed_raw is not None:
        raw_history = precomputed_raw
    else:
        since = now - _dt.timedelta(hours=hours)
        since_minute = since.strftime("%Y-%m-%d %H:%M")
        raw_history = await db.get_dns_history(device_id=device_id, since_minute=since_minute, limit=500)

    top_domains = raw_history.get("top_domains", [])
    total_queries = raw_history.get("total", 0)
    raw_activity = raw_history.get("activity", [])

    # Aggregate by App/Service
    app_counts: dict[str, dict[str, Any]] = {}
    website_counts: dict[str, int] = collections.defaultdict(int)

    for item in top_domains:
        dom = item["domain"]
        hits = item["hits"]
        svc = classify_domain(dom)

        if svc:
            name, category, color, icon = svc
            if name not in app_counts:
                app_counts[name] = {
                    "name": name,
                    "category": category,
                    "color": color,
                    "icon": icon,
                    "queries": 0,
                    "domains": [],
                }
            app_counts[name]["queries"] += hits
            if dom not in app_counts[name]["domains"]:
                app_counts[name]["domains"].append(dom)
        else:
            base_site = get_base_domain(dom)
            website_counts[base_site] += hits

    # Format Apps list
    top_apps = list(app_counts.values())
    top_apps.sort(key=lambda a: a["queries"], reverse=True)
    for app in top_apps:
        app["percentage"] = round((app["queries"] / max(1, total_queries)) * 100, 1)

    # Format Websites list
    top_websites = [
        {
            "domain": site,
            "queries": count,
            "percentage": round((count / max(1, total_queries)) * 100, 1),
        }
        for site, count in website_counts.items()
    ]
    top_websites.sort(key=lambda w: w["queries"], reverse=True)

    # Roll activity into hourly buckets for smooth chart display
    hourly_activity: dict[str, int] = collections.defaultdict(int)
    for act in raw_activity:
        # act["minute"] is "YYYY-MM-DD HH:MM" -> bucket to "YYYY-MM-DD HH:00"
        hour_key = act["minute"][:13] + ":00"
        hourly_activity[hour_key] += act["hits"]

    # Fill in chronological hours if sparse
    timeline = []
    for h in range(min(hours, 72)):
        slot = (now - _dt.timedelta(hours=hours - 1 - h)).strftime("%Y-%m-%d %H:00")
        label = (now - _dt.timedelta(hours=hours - 1 - h)).strftime("%H:00")
        timeline.append({
            "slot": slot,
            "label": label,
            "queries": hourly_activity.get(slot, 0),
        })

    return {
        "device_id": device_id,
        "hours": hours,
        "total_queries": total_queries,
        "top_apps": top_apps[:15],
        "top_websites": top_websites[:20],
        "timeline": timeline,
        "unclassified_queries": sum(website_counts.values()),
        "app_queries": sum(a["queries"] for a in top_apps),
    }
