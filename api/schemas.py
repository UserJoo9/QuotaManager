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


class UserUpdate(BaseModel):
    name: Optional[str] = None
    quota_mode: Optional[str] = None
    fixed_gb: Optional[float] = None
    block: Optional[bool] = None  # true => admin_off (cut all devices), false => clear
    limit_down_mbps: Optional[float] = _cap_field()
    limit_up_mbps: Optional[float] = _cap_field()


class NetworkUpdate(BaseModel):
    """Speed-limit settings for the whole gateway (Network tab)."""

    #: Master switch for speed shaping. Off => tc tree removed, caps unused.
    enabled: Optional[bool] = None
    #: Total DOWNLOAD line speed in Mbps — set to the REAL line rate so the
    #: queue forms at the tc layer where fq_codel can keep pings low under load.
    total_down_mbps: Optional[float] = _cap_field()
    #: Total UPLOAD line speed in Mbps (same reasoning as total_down).
    total_up_mbps: Optional[float] = _cap_field()
    #: Bufferbloat avoidance (fq_codel on every queue). Default on.
    aqm: Optional[bool] = None


class TopUpRequest(BaseModel):
    extra_gb: float = Field(..., gt=0)


class BundleUpdate(BaseModel):
    total_gb: Optional[float] = Field(None, gt=0)
    #: 0 => never auto-reset (bundle is recharged manually mid-month).
    reset_day: Optional[int] = Field(None, ge=0, le=28)
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


class PasswordUpdate(BaseModel):
    current: str
    new: str = Field(..., min_length=4)


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
    reset_day: Optional[int] = Field(None, ge=0, le=28)
    #: Required to change the password (wrong value => HTTP 400).
    current_password: Optional[str] = None
    #: New admin password (4+ chars). Omit to keep the current one.
    new_password: Optional[str] = Field(None, min_length=4)


class MilestoneNotify(BaseModel):
    """Milestone-page acknowledge: mark a crossed threshold as notified.

    Public (no admin session) — the milestone page is for the household's own
    devices on the LAN. The service validates ``milestone`` ∈ {50, 75, 100}.
    """

    user_id: int
    milestone: int


class LoginRequest(BaseModel):
    password: str


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
    ``domain_rules`` rows tagged ``source='preset:<preset_id>'``; disabling
    removes exactly those rows."""

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
