"""Browsing History Categorization and Visual Analytics.

Classifies DNS queries into:
- Popular Apps & Services (YouTube, Facebook, WhatsApp, TikTok, Netflix, Gaming, etc.)
- Registrable Websites and Domains
- Time-series activity distribution for interactive graphs
"""

from __future__ import annotations

import collections
import datetime as _dt
import time
from typing import Any

from quota.db import Database

#: Below this combined LAN throughput the whole network is "idle": a lone
#: device at 40 Kbps would otherwise show a misleading ~98 % "share" simply
#: because it is the only one making noise. The share is a *relative* figure
#: (device ÷ everyone's current traffic) — it is only meaningful when there is
#: real traffic to divide.
LIVE_IDLE_NET_BPS = 100_000

#: How many recently-contacted sites each Live-traffic row/hero lists.
LIVE_TOP_SITES = 4


def _is_displayable_site(name: str) -> bool:
    """A real, user-meaningful site: has a dot, is not reverse-DNS / local noise
    / an IP literal (e.g. a bare ``direct`` or ``1.0.168.192.in-addr.arpa``)."""
    n = (name or "").strip().lower().rstrip(".")
    if "." not in n:
        return False
    if n.endswith((".arpa", ".local", ".lan", ".localdomain", ".internal", ".home.arpa")):
        return False
    return not n.replace(".", "").isdigit()

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
    (
        ["zoom.us", "zoom.com"],
        "Zoom", "Video Conferencing", "#2D8CFF", "video"
    ),
    (
        ["epicgames.com", "unrealengine.com"],
        "Epic Games", "Gaming", "#313131", "gamepad"
    ),
    (
        ["playstation.com", "sonyentertainmentnetwork.com", "playstation.net"],
        "PlayStation", "Gaming", "#003791", "gamepad"
    ),
    (
        ["xbox.com", "xboxlive.com"],
        "Xbox", "Gaming", "#107C10", "gamepad"
    ),
    (
        ["shahid.net", "mbc.net"],
        "Shahid VIP", "Video & Streaming", "#00A859", "video"
    ),
    (
        ["watchit.com"],
        "Watch It", "Video & Streaming", "#E50914", "video"
    ),
    (
        ["pinterest.com", "pinimg.com"],
        "Pinterest", "Social Media", "#E60023", "image"
    ),
    (
        ["linkedin.com", "licdn.com"],
        "LinkedIn", "Professional", "#0A66C2", "briefcase"
    ),
    (
        ["speedtest.net", "ookla.com"],
        "Speedtest", "Network Tools", "#141526", "gauge"
    ),
    (
        ["bittorrent.com", "utorrent.com"],
        "Torrent / P2P", "File Sharing", "#38BDF8", "download"
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


async def get_live_traffic_analytics(
    db: Database,
    devices: list[Any],
    leases: dict[str, str],
    live: Any,
    users: dict[int, str] | None = None,
    selected_device_id: int | None = None,
    ip6_by_mac: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compile real-time bandwidth consumption ranking, top consumer, and active apps per device."""
    now_ts = time.time()
    users_map = users or {}

    # The counters in ``live`` are the bytes moved since the PREVIOUS flush, so
    # the speed is bytes / the real window between flushes (not a fixed 15 s).
    # A snapshot older than a few windows is stale (maintenance loop stuck) —
    # report zero rather than a frozen, made-up speed.
    window_s = float(getattr(live, "interval_s", 15.0) or 15.0)
    window_s = min(120.0, max(1.0, window_s))
    live_ts = float(getattr(live, "ts", 0.0) or 0.0)
    snapshot_stale = bool(live_ts) and (now_ts - live_ts) > max(45.0, 3 * window_s)

    target_devices = [
        d for d in devices
        if selected_device_id is None or d.id == selected_device_id
    ]

    device_results = []
    for dev in target_devices:
        ip = leases.get(dev.mac, "")
        # Read live counters from EngineSnapshot
        live_c = getattr(live, "by_ip", {}).get(ip) if ip else None
        live_up = getattr(live_c, "up", 0) if live_c else 0
        live_down = getattr(live_c, "down", 0) if live_c else 0
        # dual-stack: add the device's IPv6 counters (snapshot keys by address)
        for ip6, mac6 in (getattr(live, "ip_to_mac", None) or {}).items():
            if ":" in ip6 and mac6 == dev.mac:
                c6 = getattr(live, "by_ip", {}).get(ip6)
                if c6:
                    live_up += getattr(c6, "up", 0)
                    live_down += getattr(c6, "down", 0)
        # LOCAL (LAN) traffic of this device in the same window (monitoring only)
        lan_c = (getattr(live, "lan_by_ip", None) or {}).get(ip) if ip else None
        lan_up = getattr(lan_c, "up", 0) if lan_c else 0
        lan_down = getattr(lan_c, "down", 0) if lan_c else 0
        if snapshot_stale:
            live_up = live_down = 0
            lan_up = lan_down = 0

        # Average speed over the real flush window
        down_bps = int((live_down * 8) / window_s) if live_down else 0
        up_bps = int((live_up * 8) / window_s) if live_up else 0
        total_bps = down_bps + up_bps

        # Determine what this device is currently accessing ("ساحبه ف ايه")
        # Strict time-decay filter: only consider DNS queries within the last 5 minutes
        now_dt = _dt.datetime.now().astimezone()
        live_cutoff = (now_dt - _dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")

        recent_rows = await db.conn.execute_fetchall(
            """
            SELECT domain, SUM(count) as hits, MAX(bucket_minute) as last_seen
            FROM dns_history
            WHERE device_id = ? AND bucket_minute >= ?
            GROUP BY domain
            ORDER BY hits DESC, last_seen DESC
            LIMIT 40
            """,
            (dev.id, live_cutoff)
        )

        # Sites this device RECENTLY looked up (DNS, last 5 min): collapsed to the
        # base domain, hits summed, most-queried first, top LIVE_TOP_SITES kept.
        # Deliberately NOT presented as "the app using the bandwidth": DNS shows
        # what was contacted, not where the bytes went, so no app name is guessed
        # and the UI shows several sites (a device often runs more than one service).
        site_hits: dict[str, int] = {}
        for r in recent_rows:
            base = get_base_domain(r["domain"]) or r["domain"]
            if not _is_displayable_site(base):
                continue
            site_hits[base] = site_hits.get(base, 0) + int(r["hits"] or 0)
        ranked_sites = sorted(site_hits.items(), key=lambda kv: kv[1], reverse=True)
        recent_sites = [{"domain": d, "hits": n} for d, n in ranked_sites[:LIVE_TOP_SITES]]
        recent_domains = [x["domain"] for x in recent_sites]

        device_results.append({
            "id": dev.id,
            "name": dev.name,
            "ip": ip,
            "ip6": (ip6_by_mac or {}).get((dev.mac or "").lower(), ""),
            "mac": dev.mac,
            "user_id": dev.user_id,
            "user_name": users_map.get(dev.user_id, ""),
            "live_down_bytes": live_down,
            "live_up_bytes": live_up,
            "down_bps": down_bps,
            "up_bps": up_bps,
            "total_bps": total_bps,
            "lan_down_bps": int((lan_down * 8) / window_s) if lan_down else 0,
            "lan_up_bps": int((lan_up * 8) / window_s) if lan_up else 0,
            "recent_domains": recent_domains,
            "recent_sites": recent_sites,
            # where the bytes actually went in the last window (conntrack); [] when
            # unavailable or the device is quiet — the UI then falls back to DNS sites
            "top_destinations": ([] if snapshot_stale else list(
                (getattr(live, "flows_by_mac", None) or {}).get(dev.mac, []))),
        })

    # Sort devices by total bandwidth descending
    device_results.sort(
        key=lambda d: (d["total_bps"], d["live_down_bytes"] + d["live_up_bytes"]),
        reverse=True,
    )

    total_down_bps = sum(d["down_bps"] for d in device_results)
    total_up_bps = sum(d["up_bps"] for d in device_results)
    net_total_bps = total_down_bps + total_up_bps
    is_idle = net_total_bps < LIVE_IDLE_NET_BPS

    # Bandwidth share % — a RELATIVE figure (this device ÷ all devices right
    # now). On an idle line it is noise, so it is reported as 0 and flagged.
    for d in device_results:
        d["share_pct"] = (
            round((d["total_bps"] / max(1, net_total_bps)) * 100, 1)
            if (net_total_bps > 0 and not is_idle)
            else 0.0
        )
        d["is_idle"] = is_idle

    # Top consumer is the #1 active device
    top_consumer = (
        device_results[0]
        if (device_results and (device_results[0]["total_bps"] > 0 or device_results[0]["live_down_bytes"] > 0))
        else (device_results[0] if device_results else None)
    )

    return {
        "timestamp": now_ts,
        "total_down_bps": total_down_bps,
        "total_up_bps": total_up_bps,
        "total_lan_down_bps": sum(d["lan_down_bps"] for d in device_results),
        "total_lan_up_bps": sum(d["lan_up_bps"] for d in device_results),
        "net_total_bps": net_total_bps,
        "is_idle": is_idle,
        "idle_threshold_bps": LIVE_IDLE_NET_BPS,
        "window_s": window_s,
        "top_consumer": top_consumer,
        "devices": device_results,
    }

