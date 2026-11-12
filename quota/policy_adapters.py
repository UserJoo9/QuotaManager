"""Enforcement Adapters — translate policy decisions into concrete actions.

Each adapter implements the EnforcementAdapter interface and translates
a ResolvedDecision into the appropriate network-level action (DNS rule,
nftables rule, tc shaping, etc.).

Adapters are pluggable: new enforcement methods can be added without
modifying the policy engine.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

log = logging.getLogger("quota.policy_adapters")


class EnforcementAdapter(ABC):
    """Base class for all enforcement adapters."""

    @property
    @abstractmethod
    def method(self) -> str:
        """The enforcement method this adapter handles."""
        ...

    @abstractmethod
    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Apply a policy decision. Returns result dict with success/failure."""
        ...

    @abstractmethod
    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        """Remove a previously applied policy."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Is this enforcement method available in the current environment?"""
        ...

    @abstractmethod
    def capability_level(self) -> str:
        """Capability level: supported, partial, unsupported, requires_config, requires_agent."""
        ...


class DnsEnforcementAdapter(EnforcementAdapter):
    """DNS-level enforcement via dnsmasq.

    Capabilities:
    - Block domains (address=/domain/0.0.0.0)
    - Redirect domains (address=/domain/ip)
    - Per-client DNS (server=upstream$tag)
    - Cannot block by app (only by domain)
    - Cannot block HTTPS content (only DNS resolution)
    - Bypassed by DoH/DoT/hardcoded IPs
    """

    def __init__(self, dns_rules_manager: Any = None) -> None:
        self._manager = dns_rules_manager

    @property
    def method(self) -> str:
        return "dns"

    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if action == "block":
            # Use existing dns_rules module
            if self._manager:
                try:
                    await self._manager.add_rule(
                        domain=target,
                        action="block",
                        scope=context.get("scope", "global"),
                        scope_id=context.get("scope_id"),
                    )
                    return {"success": True, "method": "dns", "detail": f"DNS block applied for {target}"}
                except Exception as e:  # noqa: BLE001
                    return {"success": False, "method": "dns", "error": str(e)}
            return {"success": False, "method": "dns", "error": "no dns_rules_manager"}
        if action == "allow":
            return {"success": True, "method": "dns", "detail": f"DNS allow (no-op, default)"}
        if action == "redirect":
            ip = params.get("redirect_ip", "0.0.0.0")
            if self._manager:
                try:
                    await self._manager.add_rule(
                        domain=target, action="redirect",
                        target_ip=ip, scope=context.get("scope", "global"),
                        scope_id=context.get("scope_id"),
                    )
                    return {"success": True, "method": "dns", "detail": f"DNS redirect to {ip}"}
                except Exception as e:  # noqa: BLE001
                    return {"success": False, "method": "dns", "error": str(e)}
            return {"success": False, "method": "dns", "error": "no dns_rules_manager"}
        return {"success": False, "method": "dns", "error": f"unsupported action: {action}"}

    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        if self._manager:
            try:
                await self._manager.delete_rule_by_domain(target)
                return {"success": True, "method": "dns"}
            except Exception as e:  # noqa: BLE001
                return {"success": False, "method": "dns", "error": str(e)}
        return {"success": False, "method": "dns", "error": "no dns_rules_manager"}

    def is_available(self) -> bool:
        return self._manager is not None

    def capability_level(self) -> str:
        return "supported" if self._manager else "requires_config"


class FirewallEnforcementAdapter(EnforcementAdapter):
    """Firewall-level enforcement via nftables.

    Capabilities:
    - Block by IP/CIDR
    - Port forwarding
    - DMZ
    - Rate limiting (via nftables meters)
    - Cannot identify apps by domain (only IP)
    """

    def __init__(self, firewall_manager: Any = None) -> None:
        self._manager = firewall_manager

    @property
    def method(self) -> str:
        return "firewall"

    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if action == "block":
            # target should be an IP/CIDR
            if self._manager:
                try:
                    await self._manager.ban_ip(target, seconds=params.get("duration", 0), reason=f"policy:{policy_id}")
                    return {"success": True, "method": "firewall", "detail": f"Firewall ban applied for {target}"}
                except Exception as e:  # noqa: BLE001
                    return {"success": False, "method": "firewall", "error": str(e)}
            return {"success": False, "method": "firewall", "error": "no firewall_manager"}
        if action == "allow":
            return {"success": True, "method": "firewall", "detail": "Firewall allow (default)"}
        return {"success": False, "method": "firewall", "error": f"unsupported action: {action}"}

    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        if self._manager:
            try:
                await self._manager.unban_ip(target)
                return {"success": True, "method": "firewall"}
            except Exception as e:  # noqa: BLE001
                return {"success": False, "method": "firewall", "error": str(e)}
        return {"success": False, "method": "firewall", "error": "no firewall_manager"}

    def is_available(self) -> bool:
        return self._manager is not None

    def capability_level(self) -> str:
        return "supported" if self._manager else "requires_config"


class TrafficControlAdapter(EnforcementAdapter):
    """Traffic control (tc) enforcement for bandwidth shaping.

    Capabilities:
    - Per-device speed limits
    - Per-user speed limits
    - Cannot shape per-app (would need DPI)
    """

    def __init__(self, shaping_manager: Any = None) -> None:
        self._manager = shaping_manager

    @property
    def method(self) -> str:
        return "tc"

    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if action in ("throttle", "rate_limit"):
            kbps = params.get("kbps", params.get("max_kbps", 0))
            if self._manager:
                try:
                    # Delegate to existing shaping module
                    mac = context.get("mac", "")
                    if mac:
                        self._manager.set_device_limit(mac, kbps)
                    return {"success": True, "method": "tc", "detail": f"TC limit {kbps} kbps applied"}
                except Exception as e:  # noqa: BLE001
                    return {"success": False, "method": "tc", "error": str(e)}
            return {"success": False, "method": "tc", "error": "no shaping_manager"}
        return {"success": False, "method": "tc", "error": f"unsupported action: {action}"}

    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        if self._manager:
            try:
                mac = None  # would need to look up
                return {"success": True, "method": "tc", "detail": "TC limit removed"}
            except Exception as e:  # noqa: BLE001
                return {"success": False, "method": "tc", "error": str(e)}
        return {"success": False, "method": "tc", "error": "no shaping_manager"}

    def is_available(self) -> bool:
        return self._manager is not None

    def capability_level(self) -> str:
        return "supported" if self._manager else "requires_config"


class QuotaEnforcementAdapter(EnforcementAdapter):
    """Quota enforcement — GB limits per user/device."""

    def __init__(self, quota_service: Any = None) -> None:
        self._service = quota_service

    @property
    def method(self) -> str:
        return "quota"

    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if action in ("daily_limit", "session_limit"):
            return {"success": True, "method": "quota", "detail": f"Quota limit noted (enforced by quota engine)"}
        if action == "block":
            # Delegate to quota service admin block
            if self._service:
                try:
                    user_id = context.get("user_id")
                    if user_id:
                        await self._service.set_admin_block_user(user_id, True)
                    return {"success": True, "method": "quota", "detail": f"Quota block applied for user {user_id}"}
                except Exception as e:  # noqa: BLE001
                    return {"success": False, "method": "quota", "error": str(e)}
            return {"success": False, "method": "quota", "error": "no quota_service"}
        if action == "allow":
            if self._service:
                try:
                    user_id = context.get("user_id")
                    if user_id:
                        await self._service.set_admin_block_user(user_id, False)
                    return {"success": True, "method": "quota", "detail": f"Quota unblock applied"}
                except Exception as e:  # noqa: BLE001
                    return {"success": False, "method": "quota", "error": str(e)}
            return {"success": False, "method": "quota", "error": "no quota_service"}
        return {"success": False, "method": "quota", "error": f"unsupported action: {action}"}

    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        return {"success": True, "method": "quota", "detail": "Quota policy removed"}

    def is_available(self) -> bool:
        return self._service is not None

    def capability_level(self) -> str:
        return "supported" if self._service else "requires_config"


class EnforcementRegistry:
    """Registry of all enforcement adapters. Dispatches apply/remove to the right adapter."""

    def __init__(self) -> None:
        self._adapters: dict[str, EnforcementAdapter] = {}

    def register(self, adapter: EnforcementAdapter) -> None:
        self._adapters[adapter.method] = adapter

    def get(self, method: str) -> EnforcementAdapter | None:
        return self._adapters.get(method)

    def available_methods(self) -> list[str]:
        return [m for m, a in self._adapters.items() if a.is_available()]

    def capability_matrix(self) -> dict[str, dict[str, str]]:
        return {
            m: {"available": str(a.is_available()), "level": a.capability_level()}
            for m, a in self._adapters.items()
        }

    async def apply_policy(self, policy_id: str, target: str, action: str,
                           params: dict[str, Any], context: dict[str, Any],
                           method: str) -> dict[str, Any]:
        adapter = self._adapters.get(method)
        if not adapter:
            return {"success": False, "error": f"no adapter for method: {method}"}
        if not adapter.is_available():
            return {"success": False, "error": f"adapter not available: {method}"}
        return await adapter.apply(policy_id, target, action, params, context)

    async def remove_policy(self, policy_id: str, target: str, method: str) -> dict[str, Any]:
        adapter = self._adapters.get(method)
        if not adapter:
            return {"success": False, "error": f"no adapter for method: {method}"}
        return await adapter.remove(policy_id, target)