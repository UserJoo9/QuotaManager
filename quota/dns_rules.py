"""Domain-level DNS filtering: blacklists, allow-list exceptions, custom host
redirects, curated blocklist presets, and per-user/per-device upstream
DNS-server overrides.

Why this rides on dnsmasq instead of adding a new component
-------------------------------------------------------------
dnsmasq already owns DHCP + DNS on the gateway (see ``quota/db.py``'s module
docstring and ``scripts/setup_gateway_kali.sh``). It already natively
supports everything this feature needs:

- ``address=/domain/target`` blackholes or redirects a domain AND every
  subdomain of it — that IS the "wildcard" behaviour most people mean by
  domain blocking; dnsmasq has no syntax for a glob in the MIDDLE of a
  label, so a pattern like ``*.ads.*.example.com`` cannot be expressed here
  (see :func:`normalize_pattern`, which rejects it rather than silently
  accepting something that would never actually block anything).
- ``server=upstream$tag`` sends one client's queries to a specific resolver
  — the per-client DNS-server feature, for free.
- DHCP tags (``dhcp-host=mac,set:tag``) are already how the box could bind a
  MAC to DHCP options; they double as the "is this rule for THIS device"
  selector, so nothing about the nftables (packet accounting/blocking) or tc
  (speed shaping) subsystems changes.

This module never touches nftables or tc. It only renders two extra files
inside dnsmasq's already-active ``conf-dir`` (``/etc/dnsmasq.d`` by default)
and restarts the dnsmasq unit when they actually change. A restart — not a
SIGHUP — is required for dnsmasq to notice new ``address=``/``server=``/
``dhcp-host=`` lines, which costs every client a brief (roughly one second)
DNS blip on a rule change; see ``DnsRuleManager.apply``.

Known, honest limitation: this is DNS-layer filtering. A client using
DNS-over-HTTPS/TLS to a resolver outside the box (Android Private DNS,
browser "secure DNS", etc.) or one that hardcodes a destination IP bypasses
it entirely, the same way it already bypasses the box's regular DNS. Nothing
here changes that; it is called out so it is not sold as airtight.
"""

from __future__ import annotations

import logging
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("quota.dns_rules")

SCOPE_GLOBAL = "global"
SCOPE_USER = "user"
SCOPE_DEVICE = "device"

ACTION_BLOCK = "block"
ACTION_ALLOW = "allow"
ACTION_REDIRECT = "redirect"

_SCOPE_ORDER = {SCOPE_GLOBAL: 0, SCOPE_USER: 1, SCOPE_DEVICE: 2}


# ---------------------------------------------------------------------------
# Built-in presets — curated, ready-to-use blocklists
# ---------------------------------------------------------------------------
# Sources are HOSTS-format ("0.0.0.0 domain" / "127.0.0.1 domain") or
# AdBlock-Plus-format ("||domain^") text; both are reduced to a flat domain
# set by compile_source_text. Only network-address-shaped ABP rules survive
# the conversion — element-hiding/path/regex rules have no DNS-layer
# equivalent and are dropped (see parse_adblock_plus).

@dataclass
class Preset:
    id: str
    name: str
    description: str
    urls: list[str] = field(default_factory=list)
    format: str = "auto"  # "hosts" | "adblock" | "auto" (sniff per source)


PRESETS: dict[str, Preset] = {}


def _register(p: Preset) -> None:
    PRESETS[p.id] = p


_register(Preset(
    id="ads-tracking",
    name="Ads & tracking",
    description="General-purpose ads + tracking hosts blocklist (StevenBlack/hosts).",
    urls=["https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"],
    format="hosts",
))
_register(Preset(
    id="social-media",
    name="Social media",
    description=("Facebook/Instagram/TikTok/Twitter/Discord and friends "
                 "(cyb3rko/social-media-hosts-blocklists)."),
    urls=[
        "https://raw.githubusercontent.com/cyb3rko/social-media-hosts-blocklists/main/facebookhosts.txt",
        "https://raw.githubusercontent.com/cyb3rko/social-media-hosts-blocklists/main/instagramhosts.txt",
        "https://raw.githubusercontent.com/cyb3rko/social-media-hosts-blocklists/main/tiktokhosts.txt",
        "https://raw.githubusercontent.com/cyb3rko/social-media-hosts-blocklists/main/twitterhosts.txt",
        "https://raw.githubusercontent.com/cyb3rko/social-media-hosts-blocklists/main/discordhosts.txt",
    ],
    format="hosts",
))
_register(Preset(
    id="streaming",
    name="Streaming & video",
    description=("Common video-streaming platforms (Netflix, YouTube, Twitch, "
                 "Prime/Disney+/Hulu/HBO Max, Spotify) — curated inline, see "
                 "STREAMING_DOMAINS below (no single well-maintained upstream "
                 "list exists for this category)."),
    urls=[],
    format="hosts",
))
_register(Preset(
    id="porn",
    name="Adult content (Porn)",
    description="Blocks adult websites and pornographic content (StevenBlack/hosts alternates).",
    urls=["https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn-only/hosts"],
    format="hosts",
))
_register(Preset(
    id="gambling",
    name="Gambling",
    description="Online gambling / betting sites (StevenBlack/hosts alternates).",
    urls=["https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/gambling-only/hosts"],
    format="hosts",
))

#: Curated inline list for the "streaming" preset — reused whenever a preset
#: has no ``urls`` (see :func:`fetch_preset`).
STREAMING_DOMAINS: set[str] = {
    "netflix.com", "nflxvideo.net", "nflximg.net", "nflxso.net", "nflxext.com",
    "youtube.com", "googlevideo.com", "ytimg.com", "youtu.be",
    "twitch.tv", "ttvnw.net", "jtvnw.net",
    "primevideo.com", "amazonvideo.com",
    "disneyplus.com", "dssott.com",
    "hulu.com", "hbomax.com", "max.com",
    "spotify.com", "scdn.co",
}


# ---------------------------------------------------------------------------
# Parsing: hosts-format and AdBlock-Plus-format -> a flat domain set
# ---------------------------------------------------------------------------

_HOSTS_LINE_RE = re.compile(
    r"^\s*(?:0\.0\.0\.0|127\.0\.0\.1|::1)\s+([A-Za-z0-9.\-_]+)")
_ABP_DOMAIN_RE = re.compile(r"^\|\|([A-Za-z0-9.\-_]+)\^")
_HOSTS_SKIP = {
    "localhost", "localhost.localdomain", "local", "broadcasthost",
    "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
    "ip6-allnodes", "ip6-allrouters", "ip6-allhosts",
}


def parse_hosts_format(text: str) -> set[str]:
    """Extract blocked domains from an ``/etc/hosts``-style blocklist.

    Recognises ``0.0.0.0 domain`` / ``127.0.0.1 domain`` / ``::1 domain``
    lines. Comments (``#``) and blank lines are ignored, and the handful of
    loopback aliases every hosts file carries are skipped so they never end
    up as a generated dnsmasq blackhole.
    """
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _HOSTS_LINE_RE.match(line)
        if not m:
            continue
        domain = m.group(1).lower().strip(".")
        if domain and domain not in _HOSTS_SKIP:
            out.add(domain)
    return out


def parse_adblock_plus(text: str) -> set[str]:
    """Extract the network-address-shaped rules from an AdBlock-Plus list.

    Only ``||domain^`` (optionally followed by ``$options``) rules translate
    to DNS-layer blocking. Element-hiding (``##``/``#@#``), path/regex rules,
    and exception rules (``@@``) have no DNS-layer equivalent and are
    dropped — this recovers a subset of what the list author intended, which
    is the honest ceiling of what a DNS blocker can enforce from an ABP list.
    """
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("!", "[")):
            continue
        if line.startswith("@@"):
            continue  # exception rule — never something to BLOCK
        m = _ABP_DOMAIN_RE.match(line)
        if not m:
            continue
        domain = m.group(1).lower().strip(".")
        if domain:
            out.add(domain)
    return out


def compile_source_text(text: str, fmt: str = "auto") -> set[str]:
    """Parse one fetched/pasted blocklist, sniffing the format if needed."""
    if fmt == "hosts":
        return parse_hosts_format(text)
    if fmt == "adblock":
        return parse_adblock_plus(text)
    domains = parse_hosts_format(text)
    if domains:
        return domains
    return parse_adblock_plus(text)


_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?"
                        r"(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)+$")


def normalize_pattern(pattern: str) -> str:
    """Normalize a user-entered domain pattern to what dnsmasq can enforce.

    dnsmasq's ``address=/domain/`` match already covers the domain AND every
    subdomain of it — that IS the "wildcard" support dnsmasq has. A leading
    ``*.`` is therefore just stripped (dnsmasq cannot narrow a match to
    "subdomains only, not the apex" — there is no syntax for that). Any
    OTHER ``*`` (mid-label, e.g. ``*.ads.*.example.com``) is rejected rather
    than silently accepted and never actually blocking anything.
    """
    p = pattern.strip().lower().rstrip(".")
    if p.startswith("*."):
        p = p[2:]
    if not p or "*" in p or not _DOMAIN_RE.match(p):
        raise ValueError(
            f"unsupported domain pattern {pattern!r} — dnsmasq can only "
            "match a plain domain (the match already includes every "
            "subdomain); mid-label wildcards are not enforceable at the "
            "DNS layer")
    return p


def fetch_url(url: str, timeout: float = 20.0) -> str:
    """Best-effort fetch of a preset source. Raises on failure — the caller
    decides whether that should keep a previously cached list."""
    _assert_safe_preset_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "QuotaManager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _assert_safe_preset_url(url: str) -> None:
    """SSRF guard: a preset source must be https on the blocklist hosts.

    Every preset ``urls`` entry is a curated GitHub raw URL — nothing else is
    a legitimate source. Refuses other hosts/credentials/plain-http before any
    connection is made, so a tampered preset can never be used to reach an
    internal service.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https",):
        raise ValueError(f"refusing non-https preset URL: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError("refusing preset URL with embedded credentials")
    host = (parsed.hostname or "").lower()
    if host not in ("raw.githubusercontent.com", "github.com"):
        raise ValueError(f"preset host {host!r} not in allowlist")


def fetch_preset(preset: Preset) -> set[str]:
    """Fetch + parse every source URL of a preset into one domain set.

    A preset with no ``urls`` (currently only "streaming") uses its curated
    inline list instead. One failing source is logged and skipped rather
    than failing the whole preset.
    """
    if not preset.urls:
        if preset.id == "streaming":
            return set(STREAMING_DOMAINS)
        return set()
    domains: set[str] = set()
    for url in preset.urls:
        try:
            text = fetch_url(url)
        except Exception:  # noqa: BLE001 — one bad source must not kill the rest
            log.warning("failed to fetch preset source %s", url, exc_info=True)
            continue
        domains |= compile_source_text(text, preset.format)
    return domains


# ---------------------------------------------------------------------------
# Rendering: DB rows -> dnsmasq config text
# ---------------------------------------------------------------------------

def device_tag(device_id: int) -> str:
    """The dnsmasq DHCP tag a device is bound to (stable per device id)."""
    return f"qmdev{device_id}"


def render_tags(devices: Iterable[Any]) -> str:
    """Bind every known MAC to its own DHCP tag (``qmdev<id>``).

    This is what makes per-device rules possible at all: dnsmasq selects
    config lines by tag, and a tag is assigned per-MAC via ``dhcp-host``.
    Written to its own file (kept separate from :func:`render_rules`'s
    output) so a domain-rule edit — which happens far more often than a
    device being added — never rewrites this file too.
    """
    lines = [
        "# Quota Manager — generated, do not edit by hand.",
        "# Binds every known device MAC to its own dnsmasq tag so domain",
        "# rules / DNS-server overrides can be scoped per device or per user.",
    ]
    for dev in devices:
        mac = getattr(dev, "mac", None)
        dev_id = getattr(dev, "id", None)
        if not mac or dev_id is None or not _is_safe_config_token(mac):
            continue
        lines.append(f"dhcp-host={mac},set:{device_tag(dev_id)}")
    return "\n".join(lines) + "\n"


def _tags_for(scope: str, scope_id: Optional[int],
             device_ids_by_user: dict[int, list[int]]) -> list[Optional[str]]:
    """Every dnsmasq tag a rule/server override at this scope expands to.

    ``global`` has no tag (applies to everyone). ``device`` is exactly one
    tag. ``user`` expands to one tag PER device that user currently owns —
    dnsmasq only understands per-device tags, so a user-level rule/override
    is fanned out to every device belonging to that user at render time
    (mirrors how ``service.resolve_device_state`` fans a user-level admin
    cut out to devices, just at the DNS layer instead of nftables).
    """
    if scope == SCOPE_GLOBAL:
        return [None]
    if scope == SCOPE_DEVICE:
        return [device_tag(scope_id)] if scope_id is not None else []
    if scope == SCOPE_USER:
        return [device_tag(d) for d in device_ids_by_user.get(scope_id, [])]
    return []


def _is_safe_config_token(value: str) -> bool:
    """True if ``value`` cannot break out of a single generated config line.

    Defense in depth: the API layer (api/schemas.py) already validates
    ``target_ip``/``dns_server`` as bare IP addresses before they ever reach
    here, but the renderer must never trust that as the ONLY gate — a future
    caller of render_rules/render_tags that skips the API (a script, a
    future non-HTTP path) must not be able to smuggle a newline into a
    dnsmasq directive. Rejects any whitespace/control character.
    """
    return bool(value) and value == value.strip() and "\n" not in value \
        and "\r" not in value and not any(c.isspace() for c in value)


def render_rules(rules: Iterable[Any], dns_servers: Iterable[tuple[str, Optional[int], str]],
                 device_ids_by_user: dict[int, list[int]]) -> str:
    """Render domain rules (block/allow/redirect) + per-client DNS-server
    overrides into one dnsmasq config file.

    ``rules`` are DomainRule-like objects (``scope``/``scope_id``/``action``/
    ``domain``/``target_ip``/``enabled``). ``dns_servers`` are
    ``(scope, scope_id, ip)`` triples for the per-user/per-device
    upstream-DNS-server feature.

    Ordering: rules are rendered global-first, then user-scope, then
    device-scope, and within the same scope, block/redirect before allow.
    For an EXACT-same-domain-string conflict, dnsmasq's own behaviour is
    "the later directive for that tag wins", so this ordering lets a
    narrower scope (or an allow rule) override a broader one. It does NOT
    matter for the common case of allowing a more specific subdomain of a
    blocked parent domain — dnsmasq's longest-match already picks the more
    specific rule regardless of file order.
    """
    lines = [
        "# Quota Manager — generated, do not edit by hand.",
        "# Domain rules (block/allow/redirect) + per-client DNS-server overrides.",
    ]

    ordered = sorted(
        rules,
        key=lambda r: (
            _SCOPE_ORDER.get(getattr(r, "scope", SCOPE_GLOBAL), 0),
            1 if getattr(r, "action", ACTION_BLOCK) == ACTION_ALLOW else 0,
        ),
    )

    for rule in ordered:
        if not getattr(rule, "enabled", True):
            continue
        action = getattr(rule, "action", ACTION_BLOCK)
        domain = getattr(rule, "domain", "")
        if not domain or not _is_safe_config_token(domain):
            log.warning("skipping domain rule with unsafe/empty domain %r", domain)
            continue
        scope = getattr(rule, "scope", SCOPE_GLOBAL)
        scope_id = getattr(rule, "scope_id", None)
        if action == ACTION_ALLOW:
            target: Optional[str] = None  # no override -> resolves normally
        elif action == ACTION_REDIRECT:
            target = getattr(rule, "target_ip", None) or "0.0.0.0"
            if not _is_safe_config_token(target):
                log.warning("skipping redirect rule with unsafe target_ip %r "
                          "for domain %s", target, domain)
                continue
        else:  # block
            target = "0.0.0.0"
        for tag in _tags_for(scope, scope_id, device_ids_by_user):
            prefix = f"tag:{tag} " if tag else ""
            if target is None:
                # Re-point the domain at the normal upstream chain for this
                # tag scope, clearing any broader-scope blackhole for it.
                lines.append(f"{prefix}server=/{domain}/#")
            else:
                lines.append(f"{prefix}address=/{domain}/{target}")

    for scope, scope_id, ip in dns_servers:
        if not ip or not _is_safe_config_token(ip):
            if ip:
                log.warning("skipping DNS-server override with unsafe value %r", ip)
            continue
        for tag in _tags_for(scope, scope_id, device_ids_by_user):
            prefix = f"tag:{tag} " if tag else ""
            lines.append(f"{prefix}server={ip}")

    return "\n".join(lines) + "\n"


def resolve_domain_status(domain: str, rules: Iterable[Any],
                          device_id: Optional[int] = None,
                          user_id: Optional[int] = None
                          ) -> tuple[str, Optional[Any]]:
    """What would actually happen to a lookup for ``domain`` right now, for
    ``device_id`` (optionally owned by ``user_id``) — or, with both left
    ``None``, considering only global-scope rules (the aggregate "all
    devices" history view, where no single device's rules apply).

    Mirrors dnsmasq's own matching exactly, so this reports the SAME answer
    the live config would give, not an approximation:

    * longest-domain-match wins first (a rule for ``ads.example.com`` beats
      one for ``example.com`` regardless of scope) — this is dnsmasq's own
      matching rule, not an ordering choice made here;
    * among rules tied on domain length, the same scope/action ordering
      :func:`render_rules` renders in decides it (global < user < device;
      allow after block within a scope) — because that ordering is exactly
      "last directive for this tag wins", the last-sorted candidate here IS
      the one dnsmasq would actually apply.

    Returns ``(status, rule)`` where ``status`` is one of ``"blocked"``,
    ``"allowed"``, ``"redirected"``, or ``"none"`` (no rule reaches this
    domain for this device), and ``rule`` is the winning DomainRule (or
    ``None`` for ``"none"``).
    """
    candidates = []
    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue
        rule_domain = getattr(rule, "domain", "")
        if not rule_domain:
            continue
        if domain != rule_domain and not domain.endswith("." + rule_domain):
            continue  # dnsmasq's own domain match: exact, or a subdomain
        scope = getattr(rule, "scope", SCOPE_GLOBAL)
        scope_id = getattr(rule, "scope_id", None)
        if scope == SCOPE_GLOBAL:
            applies = True
        elif scope == SCOPE_DEVICE:
            applies = device_id is not None and scope_id == device_id
        elif scope == SCOPE_USER:
            applies = user_id is not None and scope_id == user_id
        else:
            applies = False
        if applies:
            candidates.append(rule)
    if not candidates:
        return "none", None
    # Longest domain match first (dnsmasq's real rule), then the same
    # scope/action tiebreak render_rules sorts by — "last directive wins"
    # for equally-specific rules, so the LAST item after this sort is the
    # one actually in effect.
    candidates.sort(key=lambda r: (
        len(getattr(r, "domain", "")),
        _SCOPE_ORDER.get(getattr(r, "scope", SCOPE_GLOBAL), 0),
        1 if getattr(r, "action", ACTION_BLOCK) == ACTION_ALLOW else 0,
    ))
    winner = candidates[-1]
    action = getattr(winner, "action", ACTION_BLOCK)
    status = {"block": "blocked", "allow": "allowed",
             "redirect": "redirected"}.get(action, "none")
    return status, winner


# ---------------------------------------------------------------------------
# The manager: writes the two files, reloads dnsmasq only when something
# actually changed (same signature-gated pattern as the nftables blocked set
# and the tc tree — see Structure_README.md "Key design decisions").
# ---------------------------------------------------------------------------

@dataclass
class DnsRuleManager:
    conf_dir: str = "/etc/dnsmasq.d"
    tags_file: str = "quota-tags.conf"
    rules_file: str = "quota-domains.conf"
    reload_dnsmasq: bool = True

    def _write_if_changed(self, path: Path, content: str) -> bool:
        try:
            if path.exists() and path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            log.error("could not write %s: %s (run as root on the gateway?)",
                      path, exc)
            return False
        return True

    def apply(self, devices: Iterable[Any], rules: Iterable[Any],
             dns_servers: Iterable[tuple[str, Optional[int], str]],
             device_ids_by_user: dict[int, list[int]]) -> bool:
        """Render + write both files. Reloads dnsmasq only if a file's
        content actually changed. Returns True if a reload was triggered."""
        tags_text = render_tags(devices)
        rules_text = render_rules(rules, dns_servers, device_ids_by_user)
        tags_path = Path(self.conf_dir) / self.tags_file
        rules_path = Path(self.conf_dir) / self.rules_file
        tags_changed = self._write_if_changed(tags_path, tags_text)
        rules_changed = self._write_if_changed(rules_path, rules_text)
        changed = tags_changed or rules_changed
        if changed and self.reload_dnsmasq:
            self._reload()
        return changed

    def _reload(self) -> None:
        # `dnsmasq --test` validates the WHOLE effective config (base file +
        # every conf-dir file), the same check the setup script runs after
        # writing quota-gateway.conf. Everything this module writes comes
        # from normalize_pattern-validated input, so a failure here would
        # mean a bug — but a bad config must never be allowed to take DHCP +
        # DNS down for the whole household, so this is a hard gate.
        try:
            probe = subprocess.run(
                ["dnsmasq", "--test"], capture_output=True, text=True, timeout=10)
            if probe.returncode != 0:
                log.error("generated dnsmasq config failed validation — NOT "
                          "reloading: %s", probe.stderr.strip())
                return
        except FileNotFoundError:
            log.warning("`dnsmasq` binary not found — skipping the pre-reload "
                       "validation check (reloading anyway)")
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not run `dnsmasq --test` (%s) — reloading anyway", exc)
        try:
            subprocess.run(["systemctl", "restart", "dnsmasq"],
                           capture_output=True, text=True, timeout=15, check=True)
            log.info("dnsmasq restarted to apply DNS filtering rule changes")
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("failed to restart dnsmasq after a DNS rule change: %s", exc)
