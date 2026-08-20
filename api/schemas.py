"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

import ipaddress
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _cap_field() -> Field:
    """Optional speed cap in Mbps (0/None = unlimited), never negative."""
    return Field(None, ge=0)


class DeviceCreate(BaseModel):
    mac: str = Field(..., description="MAC address, e.g. aa:bb:cc:dd:ee:ff")
    name: str = ""
    quota_mode: str = "auto"  # 'fixed' | 'auto'
    fixed_gb: Optional[float] = None
    #: Owning user; None => auto-create a new user for this device.
    user_id: Optional[int] = None
    #: Name for the auto-created user (defaults to the device name).
    user_name: Optional[str] = None
    #: Per-device internet speed caps in Mbps (0 = unlimited). Shaped via tc.
    limit_down_mbps: Optional[float] = _cap_field()
    limit_up_mbps: Optional[float] = _cap_field()


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    quota_mode: Optional[str] = None
    fixed_gb: Optional[float] = None
    block: Optional[bool] = None  # true => admin_off, false => clear manual block
    #: Reassign the device to another user.
    user_id: Optional[int] = None
    #: Per-device override: exempt this device from its user's quota block.
    bypass: Optional[bool] = None
    limit_down_mbps: Optional[float] = _cap_field()
    limit_up_mbps: Optional[float] = _cap_field()


class UserCreate(BaseModel):
    name: str = ""
    quota_mode: str = "auto"  # 'fixed' | 'auto'
    fixed_gb: Optional[float] = None
    #: Per-user aggregate internet speed caps in Mbps (0 = unlimited): all of
    #: this user's devices together cannot exceed these.
    limit_down_mbps: Optional[float] = _cap_field()
    limit_up_mbps: Optional[float] = _cap_field()
    #: Exempt this user from quota (never quota-blocked; admin cuts still apply).
    exempt_quota: Optional[bool] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    quota_mode: Optional[str] = None
    fixed_gb: Optional[float] = None
    block: Optional[bool] = None  # true => admin_off (cut all devices), false => clear
    limit_down_mbps: Optional[float] = _cap_field()
    limit_up_mbps: Optional[float] = _cap_field()
    #: Per-user DNS-history retention in days; None = global default. 0 = keep
    #: nothing (history is effectively off for this user).
    history_days: Optional[int] = Field(None, ge=0, le=365)
    #: Exempt this user from quota — never quota-blocked, however much they
    #: use. A manual admin cut (user/device level) still applies.
    exempt_quota: Optional[bool] = None


class NetworkUpdate(BaseModel):
    """Speed-limit settings for the whole gateway (Network tab)."""

    #: Master switch for speed shaping. Off => tc tree removed, caps unused.
    enabled: Optional[bool] = None
    #: Total DOWNLOAD line speed in Mbps — set to the REAL line rate so the
    #: queue forms at the tc layer where fq_codel can keep pings low under load.
    total_down_mbps: Optional[float] = _cap_field()
    #: Total UPLOAD line speed in Mbps (same reasoning as total_down).
    total_up_mbps: Optional[float] = _cap_field()
    #: LAN pass-through rate in Mbps — the speed limit for LAN (client
    #: <-> uplink-subnet) traffic. 0/empty = the 1000 Mbps default; the WAN
    #: caps never apply to LAN transfers.
    lan_rate_mbps: Optional[float] = _cap_field()
    #: Bufferbloat avoidance (fq_codel on every queue). Default on.
    aqm: Optional[bool] = None
    #: "VPN share" master switch: route the whole client subnet through the
    #: box's VPN tunnel (quota/vpnshare.py policy routing). None = leave the
    #: current switch untouched (only the shaping fields changed).
    vpn_share: Optional[bool] = None
    #: Refuse brand-new devices with a randomized (locally-administered) MAC.
    #: Registered + immediately admin-blocked on first sight.
    decline_random_macs: Optional[bool] = None
    #: One-shot flag riding alongside ``decline_random_macs=true``: also
    #: admin-block every device that ALREADY joined with a random MAC. Reset
    #: after the sweep (the setting itself only gates new registrations).
    decline_random_macs_existing: Optional[bool] = None


class TopUpRequest(BaseModel):
    extra_gb: float = Field(..., gt=0)


class DeviceAccessUpdate(BaseModel):
    """Manual router-side access label for a device card ("WiFi · MyNet",
    "LAN1", ...). Empty string clears the pin — the passive WiFi probe's
    auto label takes over again. Capped: it is written verbatim into the DB
    and rendered in the UI (no newlines / silly lengths)."""

    override: str = Field("", max_length=80)


class BundleUpdate(BaseModel):
    total_gb: Optional[float] = Field(None, gt=0)
    #: 0 => never auto-reset (bundle is recharged manually mid-month).
    reset_day: Optional[int] = Field(None, ge=0, le=31)
    #: "renew_day" = reset on the configured reset_day; "end_of_month" = the
    #: ISP's month-end bill — the configured day drives the reset too (0 = the
    #: calendar end, 1st of next month).
    period_type: Optional[str] = None
    #: When set, adds GB to the current bundle without rolling the period.
    add_gb: Optional[float] = Field(None, gt=0)
    #: Escape hatch: "config" returns bundle ownership to config.yaml so it is
    #: re-applied on the next restart (a dashboard edit sets this to "dashboard").
    bundle_source: Optional[str] = None


class GuestUpdate(BaseModel):
    #: Turn guest mode on/off (new devices become guests while on).
    enabled: Optional[bool] = None
    #: Allowance for each guest (GB). Applies to existing guests immediately.
    quota_gb: Optional[float] = Field(None, gt=0, le=100000)
    #: Maximum number of guest accounts (stops MAC-spoofing spam).
    limit: Optional[int] = Field(None, ge=1, le=10000)
    #: Default speed cap (Mbps) for each guest's aggregate bandwidth
    #: (0 = unlimited). Applies to existing guests immediately.
    speed_limit_mbps: Optional[float] = Field(None, ge=0, le=100000)
    #: Refuse brand-new devices while allowing already-registered ones.
    stop_new: Optional[bool] = None


class MacListsUpdate(BaseModel):
    """Replace one or both MAC lists (whitelist/blacklist). Each key is
    optional — only the provided lists are replaced, the other stays."""

    #: Whitelist: these MACs are never quota-blocked, whatever their usage.
    allow: Optional[list[str]] = None
    #: Blacklist: these MACs are always blocked, even when their user is fine.
    deny: Optional[list[str]] = None


class PasswordUpdate(BaseModel):
    current: str
    #: Password policy (core/passwords.py) is enforced in the handler — this
    #: schema-level floor keeps obvious garbage out of the PBKDF2 path.
    new: str = Field(..., min_length=12)


class SetupComplete(BaseModel):
    """First-run welcome panel submission.

    Every field is optional: the admin can confirm the bundle, change the
    password, both, or neither (an all-empty submit just dismisses the panel).
    ``current_password`` is required only when ``new_password`` is present.
    """

    #: Confirm/replace the bundle size (GB). Only applied when present, so a
    #: password-only save never takes bundle ownership from config.yaml.
    total_gb: Optional[float] = Field(None, gt=0)
    #: 0 => never auto-reset (bundle is recharged manually mid-month).
    reset_day: Optional[int] = Field(None, ge=0, le=31)
    #: "renew_day" / "end_of_month" — see BundleUpdate.
    period_type: Optional[str] = None
    #: Required to change the password (wrong value => HTTP 400).
    current_password: Optional[str] = None
    #: New admin password (policy-enforced, see core/passwords.py). Omit to
    #: keep the current one.
    new_password: Optional[str] = Field(None, min_length=12)


class MilestoneNotify(BaseModel):
    """Milestone-page acknowledge: mark a crossed threshold as notified.

    Public (no admin session) — the milestone page is for the household's own
    devices on the LAN. The service validates ``milestone`` ∈ {50, 75, 100}.
    """

    user_id: int
    milestone: int


class LoginRequest(BaseModel):
    password: str
    #: Optional TOTP code (quota/totp.py). Required when 2FA is enabled; the
    #: flow is two-stage: POST without a code verifies the password and returns
    #: ``{"totp": true}``, then the client re-POSTs WITH the code to log in.
    code: Optional[str] = None


class TotpEnableRequest(BaseModel):
    """Verify the enrollment code (from the authenticator app) and switch on."""
    code: str = Field(..., min_length=6, max_length=8)


class TotpDisableRequest(BaseModel):
    """Turn 2FA off. A valid session is the only requirement (an attacker
    without one can't reach this route at all)."""
    pass


class WanUpdate(BaseModel):
    """WAN-mode apply (the dashboard WAN tab, v19).

    ``topology`` = "lan" (default: the box sits behind the router, clients on
    their own subnet) or "wan" (strong mode: the box terminates the PPPoE line
    itself and the router is a pure bridge/AP). Unlike v18 (which only persisted
    a preference for the next restart), a submit now APPLIES the topology live:
    rewrites ``config.yaml`` + the DB setting together, runs the runtime
    applier (NIC + dnsmasq + PPPoE dial), and schedules a detached restart.
    ``pppoe_user`` / ``pppoe_password`` are the ISP credentials for WAN mode;
    ``wan_if`` is the optional second NIC that reaches the ONT/modem (two-NIC
    layout). Credentials travel to the applier via the environment, never argv.
    """

    topology: Optional[str] = None
    pppoe_user: Optional[str] = None
    pppoe_password: Optional[str] = None
    wan_if: Optional[str] = None


class DnsServerUpdate(BaseModel):
    """Per-user or per-device upstream DNS-server override.

    Rendered as a tag-restricted dnsmasq ``server=`` line (see
    quota/dns_rules.py). Empty string clears the override (falls back to the
    user's override, then the gateway's default upstreams). Must be a bare
    IPv4/IPv6 address — this string is written directly into generated
    dnsmasq config, so anything else (in particular embedded whitespace/
    newlines) is rejected rather than risk a config-injection line.
    """

    dns_server: str = Field("", max_length=64)

    @field_validator("dns_server")
    @classmethod
    def _validate_ip_or_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return v
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(
                f"dns_server must be a bare IP address, got {v!r}") from None
        return v


class DomainRuleCreate(BaseModel):
    """One domain-filtering rule. ``scope``/``scope_id`` together decide who
    it applies to: ``global`` (scope_id ignored), ``user`` (scope_id = a
    user id — fanned out to every device that user owns), or ``device``
    (scope_id = a device id). ``domain`` accepts an optional leading
    ``*.`` (stripped — dnsmasq's match already includes every subdomain);
    any other ``*`` is rejected as unenforceable. ``target_ip`` is only used
    when ``action == "redirect"`` and must be a bare IP — like
    ``dns_server``, it is written directly into generated dnsmasq config.
    """

    scope: str = Field("global", pattern="^(global|user|device)$")
    scope_id: Optional[int] = None
    action: str = Field("block", pattern="^(block|allow|redirect)$")
    domain: str = Field(..., min_length=1, max_length=253)
    target_ip: Optional[str] = None
    enabled: bool = True

    @field_validator("target_ip")
    @classmethod
    def _validate_target_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(
                f"target_ip must be a bare IP address, got {v!r}") from None
        return v


class DomainRuleUpdate(BaseModel):
    enabled: Optional[bool] = None
    target_ip: Optional[str] = None

    @field_validator("target_ip")
    @classmethod
    def _validate_target_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return v  # None = "leave unchanged" for a PATCH; see api/app.py
        v = v.strip()
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(
                f"target_ip must be a bare IP address, got {v!r}") from None
        return v


class DnsPresetEnable(BaseModel):
    """Turn a built-in blocklist preset on/off for a scope (default: the
    whole household). Enabling fetches + compiles the preset's source(s)
    (or uses the curated inline list for presets with none) into
    ``domain_rules`` rows tagged ``source='preset:<preset_id>:<scope>:
    <scope_id>'``; disabling — or re-enabling at a DIFFERENT scope — removes
    exactly those rows (see api/app.py's enable_dns_preset)."""

    scope: str = Field("global", pattern="^(global|user|device)$")
    scope_id: Optional[int] = None


class DnsImportRequest(BaseModel):
    """Paste raw hosts-format or AdBlock-Plus-format blocklist text and turn
    it into domain_rules rows (source='import'). ``format`` picks the
    parser; "auto" tries hosts-format first, then AdBlock-Plus."""

    text: str = Field(..., min_length=1)
    format: str = Field("auto", pattern="^(auto|hosts|adblock)$")
    scope: str = Field("global", pattern="^(global|user|device)$")
    scope_id: Optional[int] = None
    action: str = Field("block", pattern="^(block|allow)$")


class DnsQuickRule(BaseModel):
    """One-click block/allow from the browsing-history view: pick a domain
    a device has actually queried and file a rule for it without leaving
    the History tab. ``scope`` is deliberately narrowed to just
    ``global``/``device`` (not ``user``) — the two choices the History tab
    actually offers ("this device only" / "everyone")."""

    domain: str = Field(..., min_length=1, max_length=253)
    action: str = Field(..., pattern="^(block|allow)$")
    scope: str = Field(..., pattern="^(global|device)$")
    device_id: Optional[int] = None


class WanTest(BaseModel):
    """PPPoE connection test (the dashboard WAN tab, v19.1).

    Dials the line with the entered credentials on a throwaway ``ppp200``
    interface and reports whether the ISP accepts them and an internet
    connection comes up. Deliberately does NOT change the running topology —
    no config.yaml write, no DB write, no ``ppp0``. ``wan_if`` is the optional
    second NIC that reaches the ONT/modem (two-NIC layout).
    """

    pppoe_user: Optional[str] = None
    pppoe_password: Optional[str] = None
    wan_if: Optional[str] = None


class WanRenewConfig(BaseModel):
    """WAN public-IP auto-renew schedule (the dashboard WAN tab).

    ``enabled`` arms the periodic PPPoE re-dial (``quota-wan-ppp`` restart);
    ``minutes`` is the interval, clamped to a 5-minute floor in the service —
    every renewal drops internet for a few seconds, so a lower bound keeps a
    typo from hammering the line (no upper bound: any longer interval works).
    """

    enabled: bool
    minutes: int


class FirewallRule(BaseModel):
    """One ordered custom firewall rule (Firewall tab).

    ``chain`` ``"input"`` guards the box itself (the dashboard), ``"forward"``
    the forwarded path. ``action`` ``"deny"`` drops, ``"allow"`` accepts.
    Empty ``src``/``dst``/``protocol``/ports mean "any". A deny rule whose
    source/dest covers the client subnet or the box's own IPs is refused
    server-side (the admin can never lock themself out).
    """

    name: str = ""
    chain: str = Field("forward", pattern="^(input|forward)$")
    action: str = Field("deny", pattern="^(allow|deny)$")
    src: str = ""
    dst: str = ""
    protocol: str = Field("", pattern="^(|tcp|udp|icmp)$")
    src_port: int = Field(0, ge=0, le=65535)
    dst_port: int = Field(0, ge=0, le=65535)
    log: bool = True


class FirewallService(BaseModel):
    """A box service exposed on the internet under WAN mode."""

    name: str = ""
    protocol: str = Field("tcp", pattern="^(tcp|udp)$")
    port: int = Field(..., ge=1, le=65535)
    source: str = "0.0.0.0/0"


class FirewallPortForward(BaseModel):
    """WAN-mode inbound port forward (dnat to an internal host)."""

    name: str = ""
    protocol: str = Field("tcp", pattern="^(tcp|udp)$")
    source_port: int = Field(..., ge=1, le=65535)
    target_ip: str = ""
    target_port: int = Field(..., ge=1, le=65535)

    @field_validator("target_ip")
    @classmethod
    def _validate_target_ip(cls, v: str) -> str:
        v = v.strip()
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(f"target_ip must be a bare IP address, got {v!r}") from None
        return v


class FirewallConfigUpdate(BaseModel):
    """Full firewall config replacement (Firewall tab "Apply").

    Mirrors the ``firewall:`` YAML schema; every field optional so a partial
    payload merges over the current config. Applied through the safe-apply
    path (sanitize -> snapshot -> program -> watchdog auto-revert).
    """

    enabled: Optional[bool] = None
    watchdog_seconds: Optional[int] = Field(None, ge=1, le=3600)
    probe_ip: Optional[str] = None
    services: Optional[list[FirewallService]] = None
    port_forwards: Optional[list[FirewallPortForward]] = None
    dmz: Optional[str] = None
    rules: Optional[list[FirewallRule]] = None
    allow_cidrs: Optional[list[str]] = None
    deny_cidrs: Optional[list[str]] = None
    syn_flood: Optional[dict] = None
    brute_force: Optional[dict] = None
    scan_detect: Optional[dict] = None
    geo_block: Optional[bool] = None
    wan_confirmed: Optional[bool] = None


class FirewallBanRequest(BaseModel):
    """Manual kernel ban (auto-expiring)."""

    ip: str = ""
    seconds: int = Field(1800, ge=60, le=604800)
    reason: str = "manual"


class FirewallUnbanRequest(BaseModel):
    ip: str = ""


class FirewallGeoUpdate(BaseModel):
    """Country -> CIDR map for geo-blocking, e.g. {"CN": ["1.0.1.0/24"]}.
    Stored in the ``firewall_geo`` DB setting; inert while ``geo_block`` is
    off. Maintained externally (the module never refreshes geo databases)."""

    mapping: dict = Field(default_factory=dict)


class UpdateSettings(BaseModel):
    """Self-update preferences (the dashboard Admin tab).

    ``enabled`` arms the 24 h GitHub release check (the "update available"
    notification); ``auto_install`` makes a found update download + install
    itself without the admin pressing the button. Both persist in settings;
    neither field is required (a partial save leaves the other untouched).
    """

    enabled: Optional[bool] = None
    auto_install: Optional[bool] = None
