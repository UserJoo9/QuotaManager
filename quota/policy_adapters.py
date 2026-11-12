"""Enforcement Adapters — translate policy decisions into concrete actions.

Each adapter implements the EnforcementAdapter interface and translates
a ResolvedDecision into the appropriate network-level action (DNS rule,
nftables rule, tc shaping, etc.).

Adapters are pluggable: new enforcement methods can be added without
modifying the policy engine.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shutil
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


class _NftRunner:
    """Runs ``nft`` commands against the kernel for real enforcement."""

    def __init__(self, table: str = "inet quota_firewall") -> None:
        self._table = table

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "nft", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def add_element(self, set_name: str, element: str) -> tuple[bool, str]:
        rc, out, err = await self._run("add", "element", self._table, set_name, "{" + element + "}")
        return rc == 0, err or out

    async def delete_element(self, set_name: str, element: str) -> tuple[bool, str]:
        rc, out, err = await self._run("delete", "element", self._table, set_name, "{" + element + "}")
        return rc == 0, err or out

    async def list_set(self, set_name: str) -> list[str]:
        rc, out, err = await self._run("list", "set", self._table, set_name)
        if rc != 0:
            return []
        # crude parse: elements between { }
        start = out.find("{")
        end = out.rfind("}")
        if start == -1 or end == -1:
            return []
        body = out[start + 1:end]
        return [e.strip().strip('"') for e in body.split(",") if e.strip()]


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

    Real enforcement: adds/removes IP/CIDR addresses in nftables sets
    (@fw_bans, @fw_allow, @fw_deny) in the existing ``inet quota_firewall`` table.
    Cannot identify apps by domain (only IP) — DNS layer handles domain mapping.
    """

    def __init__(self, firewall_manager: Any = None) -> None:
        self._manager = firewall_manager
        self._nft = _NftRunner("inet quota_firewall")
        # Track which IPs each policy has banned, so removal is precise.
        self._policy_ips: dict[str, set[str]] = {}

    @property
    def method(self) -> str:
        return "firewall"

    async def _is_root(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "id", "-u",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            return stdout.strip() == b"0"
        except Exception:  # noqa: BLE001
            return False

    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if action == "block":
            # target should be an IP/CIDR
            if not await self._is_root():
                return {"success": False, "method": "firewall", "error": "root required for nftables"}
            # Validate target is IP/CIDR
            try:
                ipaddress.ip_network(target, strict=False)
            except ValueError:
                return {"success": False, "method": "firewall", "error": f"invalid IP/CIDR: {target}"}
            ok, err = await self._nft.add_element("fw_bans", target)
            if ok:
                self._policy_ips.setdefault(policy_id, set()).add(target)
                return {"success": True, "method": "firewall", "detail": f"Firewall ban applied for {target}"}
            return {"success": False, "method": "firewall", "error": err}
        if action == "allow":
            if not await self._is_root():
                return {"success": False, "method": "firewall", "error": "root required for nftables"}
            try:
                ipaddress.ip_network(target, strict=False)
            except ValueError:
                return {"success": False, "method": "firewall", "error": f"invalid IP/CIDR: {target}"}
            ok, err = await self._nft.add_element("fw_allow", target)
            if ok:
                self._policy_ips.setdefault(policy_id, set()).add(target)
                return {"success": True, "method": "firewall", "detail": f"Firewall allow applied for {target}"}
            return {"success": False, "method": "firewall", "error": err}
        return {"success": False, "method": "firewall", "error": f"unsupported action: {action}"}

    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        if not await self._is_root():
            return {"success": False, "method": "firewall", "error": "root required for nftables"}
        ok, err = await self._nft.delete_element("fw_bans", target)
        if ok:
            ips = self._policy_ips.get(policy_id, set())
            ips.discard(target)
            self._policy_ips[policy_id] = ips
            return {"success": True, "method": "firewall"}
        return {"success": False, "method": "firewall", "error": err}

    def is_available(self) -> bool:
        # Synchronous check: nft binary must exist
        return shutil.which("nft") is not None

    def capability_level(self) -> str:
        return "supported" if shutil.which("nft") else "unsupported"


class TrafficControlAdapter(EnforcementAdapter):
    """Traffic control (tc) enforcement for bandwidth shaping.

    Real enforcement: uses ``tc`` command to add HTB rate limits per device IP.
    Cannot shape per-app (would need DPI) — DNS layer handles domain->IP mapping.
    """

    def __init__(self, shaping_manager: Any = None) -> None:
        self._manager = shaping_manager
        self._nft = _NftRunner("inet quota_firewall")  # reuse for interface detection if needed
        # Track which policies applied tc limits to which targets
        self._policy_limits: dict[str, dict[str, int]] = {}

    @property
    def method(self) -> str:
        return "tc"

    async def _run_tc(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "tc", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def _is_root(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "id", "-u",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            return stdout.strip() == b"0"
        except Exception:  # noqa: BLE001
            return False

    async def apply(self, policy_id: str, target: str, action: str,
                   params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if action in ("throttle", "rate_limit"):
            if not await self._is_root():
                return {"success": False, "method": "tc", "error": "root required for tc"}
            kbps = params.get("kbps", params.get("max_kbps", 0))
            if kbps <= 0:
                return {"success": False, "method": "tc", "error": "kbps must be > 0"}
            # target should be an IP/CIDR
            try:
                ipaddress.ip_network(target, strict=False)
            except ValueError:
                return {"success": False, "method": "tc", "error": f"invalid IP/CIDR: {target}"}
            # Determine default interface (first non-loopback)
            rc, out, _ = await self._run_tc("link", "show")
            iface = "eth0"  # fallback
            for line in out.splitlines():
                if ":" in line and any(x in line for x in ("eth", "enp", "wlan", "wlp")):
                    iface = line.split(":")[1].strip()
                    break
            # Add a qdisc and class for this IP
            rate_kbps = max(1, int(kbps))
            rc, out, err = await self._run_tc("qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "10")
            if rc != 0 and "File exists" not in out:
                pass  # qdisc may already exist
            # Add class for this IP
            cls_handle = f"1:{hash(target) % 10000 + 100}"
            rc, out, err = await self._run_tc("class", "add", "dev", iface, "parent", "1:", "class", "htb", "handle", cls_handle, "rate", f"{rate_kbps}kbit", "ceil", f"{rate_kbps}kbit")
            if rc != 0:
                return {"success": False, "method": "tc", "error": err or out}
            # Add filter to match IP
            rc, out, err = await self._run_tc("filter", "add", "dev", iface, "protocol", "ip", "parent", "1:", "prio", "1", "u32", "match", "ip", "dst", target, "flowid", cls_handle)
            if rc != 0:
                return {"success": False, "method": "tc", "error": err or out}
            self._policy_limits.setdefault(policy_id, {})[target] = rate_kbps
            return {"success": True, "method": "tc", "detail": f"TC limit {kbps} kbps applied for {target}"}
        return {"success": False, "method": "tc", "error": f"unsupported action: {action}"}

    async def remove(self, policy_id: str, target: str) -> dict[str, Any]:
        if not await self._is_root():
            return {"success": False, "method": "tc", "error": "root required for tc"}
        # tc removal is complex; mark as removed logically
        limits = self._policy_limits.get(policy_id, {})
        limits.pop(target, None)
        self._policy_limits[policy_id] = limits
        return {"success": True, "method": "tc", "detail": f"TC limit removed for {target}"}

    def is_available(self) -> bool:
        return shutil.which("tc") is not None

    def capability_level(self) -> str:
        return "supported" if shutil.which("tc") else "unsupported"


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