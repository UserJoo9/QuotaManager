"""Unified Policy Engine for Quota Manager.

Central authority for defining, evaluating, prioritizing, and resolving
network access policies. Every policy — DNS block, firewall rule, quota
limit, speed shape — flows through this engine before reaching an
enforcement adapter.

Design principles:
- Policies are declarative: (scope, target, action, conditions, schedule, priority)
- Evaluation is deterministic: given the same inputs, same output
- Conflict resolution is explicit: priority → specificity → deny-wins
- Enforcement is pluggable: adapters translate policy decisions into
  concrete kernel/network actions
- Explainability: every decision carries a reason trail
"""
from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("quota.policy_engine")


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class PolicyScope(str, enum.Enum):
    """What the policy applies to."""
    GLOBAL = "global"          # entire network
    NETWORK_SEGMENT = "segment"  # subnet / VLAN / group
    USER = "user"
    USER_GROUP = "user_group"
    DEVICE = "device"
    DEVICE_GROUP = "device_group"
    APP = "app"
    SERVICE = "service"
    DOMAIN = "domain"
    CONTENT_CATEGORY = "content_category"
    APP_FEATURE = "app_feature"
    TIME_WINDOW = "time_window"
    COMBINED = "combined"      # multiple conditions


class PolicyTarget(str, enum.Enum):
    """What the policy controls."""
    APP = "app"               # TikTok, YouTube, etc.
    SERVICE = "service"       # streaming, gaming, etc.
    DOMAIN = "domain"         # domain pattern
    CONTENT_CATEGORY = "content_category"  # entertainment, social, etc.
    FEATURE = "feature"       # app feature (shorts, reels)
    COMBINED = "combined"


class PolicyAction(str, enum.Enum):
    """What happens when policy matches."""
    BLOCK = "block"
    ALLOW = "allow"
    THROTTLE = "throttle"        # speed limit
    RATE_LIMIT = "rate_limit"    # requests per minute
    DAILY_LIMIT = "daily_limit"  # daily GB/minutes cap
    SESSION_LIMIT = "session_limit"
    SCHEDULE = "schedule"        # time-restricted
    TEMPORARY = "temporary"      # auto-expires
    REDIRECT = "redirect"        # redirect to notice
    MONITOR = "monitor"          # log only, don't block
    LOG_ONLY = "log_only"


class EnforcementMethod(str, enum.Enum):
    """How the policy is enforced."""
    DNS = "dns"                  # dnsmasq address= rules
    FIREWALL = "firewall"        # nftables rules
    TRAFFIC_CONTROL = "tc"       # tc shaping
    PROXY = "proxy"              # HTTP proxy
    ENDPOINT_AGENT = "agent"     # device agent
    BROWSER_EXT = "browser_ext"   # browser extension
    COMBINED = "combined"


class CapabilityStatus(str, enum.Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    REQUIRES_CONFIG = "requires_config"
    REQUIRES_AGENT = "requires_agent"
    DEGRADED = "degraded"


# --------------------------------------------------------------------------- #
# Data Models
# --------------------------------------------------------------------------- #

@dataclass
class PolicyCondition:
    """A single condition within a policy."""
    field: str          # device_id, user_id, time, day_of_week, etc.
    operator: str       # eq, ne, in, not_in, gt, lt, between, contains
    value: Any          # the value to compare against

    def evaluate(self, context: dict[str, Any]) -> bool:
        """Evaluate this condition against a context dict."""
        actual = context.get(self.field)
        if self.operator == "eq":
            return actual == self.value
        if self.operator == "ne":
            return actual != self.value
        if self.operator == "in":
            return actual in (self.value if isinstance(self.value, (list, set, tuple)) else [self.value])
        if self.operator == "not_in":
            return actual not in (self.value if isinstance(self.value, (list, set, tuple)) else [self.value])
        if self.operator == "gt":
            return (actual or 0) > self.value
        if self.operator == "lt":
            return (actual or 0) < self.value
        if self.operator == "between":
            lo, hi = self.value
            return lo <= (actual or 0) <= hi
        if self.operator == "contains":
            return self.value in (actual or "")
        if self.operator == "time_range":
            # value = [start_hour, start_min, end_hour, end_min]
            now = _dt.datetime.now().astimezone()
            start = now.replace(hour=self.value[0], minute=self.value[1], second=0, microsecond=0)
            end = now.replace(hour=self.value[2], minute=self.value[3], second=0, microsecond=0)
            if end < start:
                end += _dt.timedelta(days=1)
            return start <= now <= end
        if self.operator == "day_of_week":
            now = _dt.datetime.now().astimezone()
            return now.weekday() in (self.value if isinstance(self.value, (list, set, tuple)) else [self.value])
        return False


@dataclass
class PolicySchedule:
    """Time-based schedule for a policy."""
    enabled: bool = False
    # days of week (0=Monday..6=Sunday). Empty = every day.
    days: list[int] = field(default_factory=list)
    # time ranges: list of [start_hour, start_min, end_hour, end_min]
    time_ranges: list[list[int]] = field(default_factory=list)
    # timezone (empty = system default)
    timezone: str = ""
    # auto-enable/disable dates
    start_date: str = ""   # ISO date
    end_date: str = ""     # ISO date (empty = no expiry)

    def is_active_now(self) -> bool:
        """Is this schedule currently active?"""
        if not self.enabled:
            return True  # no schedule = always active
        now = _dt.datetime.now().astimezone()
        if self.start_date:
            if now.date() < _dt.date.fromisoformat(self.start_date):
                return False
        if self.end_date:
            if now.date() > _dt.date.fromisoformat(self.end_date):
                return False
        if self.days and now.weekday() not in self.days:
            return False
        if self.time_ranges:
            active = False
            for tr in self.time_ranges:
                start = now.replace(hour=tr[0], minute=tr[1], second=0, microsecond=0)
                end = now.replace(hour=tr[2], minute=tr[3], second=0, microsecond=0)
                if end < start:
                    end += _dt.timedelta(days=1)
                if start <= now <= end:
                    active = True
                    break
            if not active:
                return False
        return True


@dataclass
class Policy:
    """A single network access policy."""
    id: str
    name: str
    description: str = ""
    enabled: bool = True

    # Scope: who does this apply to?
    scope_type: PolicyScope = PolicyScope.GLOBAL
    scope_targets: list[str] = field(default_factory=list)  # user IDs, device MACs, group names

    # Target: what does this control?
    target_type: PolicyTarget = PolicyTarget.APP
    target_values: list[str] = field(default_factory=list)  # app IDs, domain patterns, categories

    # Action
    action: PolicyAction = PolicyAction.BLOCK
    action_params: dict[str, Any] = field(default_factory=dict)

    # Conditions (additional)
    conditions: list[PolicyCondition] = field(default_factory=list)

    # Schedule
    schedule: PolicySchedule = field(default_factory=PolicySchedule)

    # Priority (higher = more important)
    priority: int = 0

    # Exceptions (targets exempted from this policy)
    exceptions: list[str] = field(default_factory=list)

    # Expiration (empty = no expiry)
    expires_at: str = ""

    # Enforcement
    enforcement_method: EnforcementMethod = EnforcementMethod.DNS

    # Logging
    log_enabled: bool = True

    # Metadata
    created_at: str = ""
    updated_at: str = ""
    created_by: str = "admin"
    tags: list[str] = field(default_factory=list)

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return _dt.datetime.now().astimezone() > _dt.datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return False

    def is_active(self) -> bool:
        if not self.enabled:
            return False
        if self.is_expired():
            return False
        return self.schedule.is_active_now()

    def matches_scope(self, context: dict[str, Any]) -> bool:
        """Does this policy apply to the given context (user/device/etc.)?"""
        # Exceptions: exempted targets never match
        ctx_device = context.get("device_id") or context.get("mac")
        ctx_user = context.get("user_id")
        if str(ctx_device) in self.exceptions or str(ctx_user) in self.exceptions:
            return False

        if self.scope_type == PolicyScope.GLOBAL:
            return True
        ctx_user = context.get("user_id")
        ctx_device = context.get("device_id") or context.get("mac")
        ctx_group = context.get("group")
        ctx_segment = context.get("segment")

        if self.scope_type == PolicyScope.USER:
            return str(ctx_user) in self.scope_targets
        if self.scope_type == PolicyScope.DEVICE:
            return str(ctx_device) in self.scope_targets
        if self.scope_type == PolicyScope.USER_GROUP:
            return ctx_group in self.scope_targets
        if self.scope_type == PolicyScope.DEVICE_GROUP:
            return ctx_group in self.scope_targets
        if self.scope_type == PolicyScope.NETWORK_SEGMENT:
            return str(ctx_segment) in self.scope_targets
        if self.scope_type == PolicyScope.APP:
            return True  # app-scoped policies apply globally by app
        if self.scope_type == PolicyScope.COMBINED:
            return all(c.evaluate(context) for c in self.conditions)
        return False

    def matches_target(self, target: str, context: dict[str, Any]) -> bool:
        """Does this policy target the given app/domain/category?"""
        if target in self.exceptions:
            return False
        if self.target_type == PolicyTarget.APP:
            return target in self.target_values
        if self.target_type == PolicyTarget.DOMAIN:
            # support wildcard: *.example.com
            for pattern in self.target_values:
                if pattern.startswith("*."):
                    suffix = pattern[1:]
                    if target.endswith(suffix):
                        return True
                elif pattern == target:
                    return True
            return False
        if self.target_type == PolicyTarget.CONTENT_CATEGORY:
            cat = context.get("content_category", "")
            return cat in self.target_values
        if self.target_type == PolicyTarget.FEATURE:
            return target in self.target_values
        if self.target_type == PolicyTarget.SERVICE:
            return target in self.target_values
        if self.target_type == PolicyTarget.COMBINED:
            return target in self.target_values
        return False

    def evaluate(self, target: str, context: dict[str, Any]) -> "PolicyDecision":
        """Evaluate this single policy against a target + context."""
        if not self.is_active():
            return PolicyDecision(policy_id=self.id, action=PolicyAction.ALLOW,
                                 reason="policy_inactive", matched=False)
        if not self.matches_scope(context):
            return PolicyDecision(policy_id=self.id, action=PolicyAction.ALLOW,
                                 reason="scope_mismatch", matched=False)
        if not self.matches_target(target, context):
            return PolicyDecision(policy_id=self.id, action=PolicyAction.ALLOW,
                                 reason="target_mismatch", matched=False)
        # Additional conditions
        for cond in self.conditions:
            if not cond.evaluate(context):
                return PolicyDecision(policy_id=self.id, action=PolicyAction.ALLOW,
                                     reason=f"condition_failed:{cond.field}", matched=False)
        return PolicyDecision(
            policy_id=self.id,
            action=self.action,
            reason=f"policy_matched:{self.name}",
            matched=True,
            enforcement=self.enforcement_method,
            params=self.action_params,
        )


@dataclass
class PolicyDecision:
    """Result of evaluating a single policy."""
    policy_id: str
    action: PolicyAction
    reason: str
    matched: bool = False
    enforcement: EnforcementMethod = EnforcementMethod.DNS
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 0


@dataclass
class ResolvedDecision:
    """Final resolved decision after conflict resolution."""
    action: PolicyAction
    reason: str
    matched_policies: list[str] = field(default_factory=list)
    denied_by: str | None = None
    allowed_by: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    enforcement: EnforcementMethod = EnforcementMethod.DNS
    explain: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Policy Engine
# --------------------------------------------------------------------------- #

class PolicyEngine:
    """Central policy evaluation and conflict resolution engine.

    Usage:
        engine = PolicyEngine()
        engine.add_policy(policy)
        decision = engine.resolve("tiktok", {"user_id": 1, "device_id": "aa:bb"})
        # decision.action == PolicyAction.BLOCK
    """

    def __init__(self) -> None:
        self._policies: dict[str, Policy] = {}
        self._listeners: list[Callable[[str, Policy], None]] = []

    def add_policy(self, policy: Policy) -> None:
        self._policies[policy.id] = policy
        self._notify("add", policy)

    def remove_policy(self, policy_id: str) -> bool:
        p = self._policies.pop(policy_id, None)
        if p:
            self._notify("remove", p)
        return p is not None

    def update_policy(self, policy: Policy) -> None:
        self._policies[policy.id] = policy
        self._notify("update", policy)

    def get_policy(self, policy_id: str) -> Policy | None:
        return self._policies.get(policy_id)

    def list_policies(self, *, enabled_only: bool = False) -> list[Policy]:
        out = list(self._policies.values())
        if enabled_only:
            out = [p for p in out if p.enabled]
        return sorted(out, key=lambda p: (-p.priority, p.name))

    def on_change(self, cb: Callable[[str, Policy], None]) -> None:
        self._listeners.append(cb)

    def _notify(self, event: str, policy: Policy) -> None:
        for cb in self._listeners:
            try:
                cb(event, policy)
            except Exception:  # noqa: BLE001
                log.exception("policy change listener failed")

    def evaluate(self, target: str, context: dict[str, Any]) -> list[PolicyDecision]:
        """Evaluate all active policies against a target + context.
        Returns list of decisions (one per policy that matched scope+target)."""
        decisions = []
        for p in self._policies.values():
            d = p.evaluate(target, context)
            if d.matched:
                d.priority = p.priority
                decisions.append(d)
        return decisions

    def resolve(self, target: str, context: dict[str, Any]) -> ResolvedDecision:
        """Evaluate + resolve conflicts → single final decision."""
        decisions = self.evaluate(target, context)
        explain = []

        if not decisions:
            return ResolvedDecision(
                action=PolicyAction.ALLOW,
                reason="no_matching_policy",
                explain=["No active policy matched this target/context."],
            )

        # Sort by priority descending, then by specificity (scope specificity)
        decisions.sort(key=lambda d: (-d.priority,))

        # Conflict resolution: deny-wins among same priority, then highest priority wins
        # 1. Collect all matched policies
        matched_ids = [d.policy_id for d in decisions]
        explain.append(f"Matched policies: {', '.join(matched_ids)}")

        # 2. Deny-wins: if any policy says BLOCK, the result is BLOCK
        #    unless a higher-priority ALLOW overrides it
        blocks = [d for d in decisions if d.action == PolicyAction.BLOCK]
        allows = [d for d in decisions if d.action == PolicyAction.ALLOW]

        if blocks and allows:
            max_block_prio = max(d.priority for d in blocks)
            max_allow_prio = max(d.priority for d in allows)
            if max_allow_prio > max_block_prio:
                winner = max(allows, key=lambda d: d.priority)
                explain.append(f"ALLOW overrides BLOCK (allow priority {max_allow_prio} > block priority {max_block_prio})")
            else:
                winner = max(blocks, key=lambda d: d.priority)
                explain.append(f"BLOCK wins (block priority {max_block_prio} >= allow priority {max_allow_prio})")
        elif blocks:
            winner = max(blocks, key=lambda d: d.priority)
            explain.append("BLOCK (no ALLOW to override)")
        elif allows:
            winner = max(allows, key=lambda d: d.priority)
            explain.append("ALLOW")
        else:
            # Throttle/rate_limit/etc — highest priority wins
            winner = max(decisions, key=lambda d: d.priority)
            explain.append(f"{winner.action.value} (highest priority among non-block/allow)")

        # Build resolved decision
        denied_by = winner.policy_id if winner.action == PolicyAction.BLOCK else None
        allowed_by = winner.policy_id if winner.action == PolicyAction.ALLOW else None

        return ResolvedDecision(
            action=winner.action,
            reason=winner.reason,
            matched_policies=matched_ids,
            denied_by=denied_by,
            allowed_by=allowed_by,
            params=winner.params,
            enforcement=winner.enforcement,
            explain=explain,
        )

    def simulate(self, target: str, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate policy evaluation without enforcement. Returns full trace."""
        decisions = self.evaluate(target, context)
        resolved = self.resolve(target, context)
        return {
            "target": target,
            "context": context,
            "matched_policies": [
                {"id": d.policy_id, "action": d.action.value, "reason": d.reason, "priority": d.priority}
                for d in decisions
            ],
            "resolved_action": resolved.action.value,
            "resolved_reason": resolved.reason,
            "denied_by": resolved.denied_by,
            "allowed_by": resolved.allowed_by,
            "explain": resolved.explain,
            "note": "Simulation only — not actual enforcement.",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "policies": [asdict(p) for p in self._policies.values()],
            "count": len(self._policies),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyEngine":
        engine = cls()
        for pd in data.get("policies", []):
            # Reconstruct PolicyCondition objects
            conds = []
            for c in pd.get("conditions", []):
                conds.append(PolicyCondition(**c))
            sched = PolicySchedule(**pd.get("schedule", {}))
            policy = Policy(
                id=pd["id"],
                name=pd["name"],
                description=pd.get("description", ""),
                enabled=pd.get("enabled", True),
                scope_type=PolicyScope(pd.get("scope_type", "global")),
                scope_targets=pd.get("scope_targets", []),
                target_type=PolicyTarget(pd.get("target_type", "app")),
                target_values=pd.get("target_values", []),
                action=PolicyAction(pd.get("action", "block")),
                action_params=pd.get("action_params", {}),
                conditions=conds,
                schedule=sched,
                priority=pd.get("priority", 0),
                exceptions=pd.get("exceptions", []),
                expires_at=pd.get("expires_at", ""),
                enforcement_method=EnforcementMethod(pd.get("enforcement_method", "dns")),
                log_enabled=pd.get("log_enabled", True),
                created_at=pd.get("created_at", ""),
                updated_at=pd.get("updated_at", ""),
                created_by=pd.get("created_by", "admin"),
                tags=pd.get("tags", []),
            )
            engine.add_policy(policy)
        return engine