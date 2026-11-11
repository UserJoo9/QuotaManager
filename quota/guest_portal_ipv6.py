"""IPv6 pending-guest address selection and captive redirect policy."""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Mapping

from quota.ip_addresses import split_ip_addresses

log = logging.getLogger("quota.guest_portal_ipv6")

BestEffortNft = Callable[[list[str]], bool]


def split_guest_addresses(addresses: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return canonical IPv4 and IPv6 guest addresses, ignoring malformed input."""
    return split_ip_addresses(addresses)


def blocked_guest_addresses(
    state: Mapping[str, Mapping[str, object]],
    ip_to_mac: Mapping[str, str],
) -> set[str]:
    """Collect each blocked guest's IPv4 lease and tracked IPv6 addresses."""
    guest_macs = {
        mac.lower() for mac, device in state.items()
        if device.get("guest") and device.get("blocked")
    }
    if not guest_macs:
        return set()

    addresses = [
        device["ip"] for mac, device in state.items()
        if mac.lower() in guest_macs and isinstance(device.get("ip"), str)
    ]
    addresses.extend(
        ip for ip, mac in ip_to_mac.items()
        if mac.lower() in guest_macs
    )
    ipv4, ipv6 = split_guest_addresses(addresses)
    return set(ipv4 + ipv6)


class IPv6PendingGuestPortal:
    """Own the IPv6 captive redirect set and its cache/health state."""

    def __init__(self, table: str, web_port: int) -> None:
        self.table = table
        self.web_port = int(web_port)
        self._redirect_configured = False
        self._last_addresses: list[str] | None = None
        self._last_error = ""

    def reset(self) -> None:
        """Forget kernel state before the engine rebuilds its base table."""
        self._redirect_configured = False
        self._last_addresses = None
        self._last_error = ""

    def install(self, run_best_effort: BestEffortNft) -> bool:
        """Install the set, shared prerouting chain, and HTTP redirect rule."""
        commands = (
            ["add", "set", f"inet {self.table} pending_guests6",
             "{ type ipv6_addr; }"],
            ["add", "chain", f"inet {self.table} prerouting",
             "{ type nat hook prerouting priority -50; policy accept; }"],
            ["add", "rule", f"inet {self.table} prerouting",
             f"ip6 saddr @pending_guests6 tcp dport 80 redirect to :{self.web_port}"],
        )
        ok = True
        for command in commands:
            if not run_best_effort(command):
                ok = False
                # A failed prerequisite must not be followed by `add rule`.
                # The next tick may retry set/chain setup without duplicating
                # the redirect in the shared prerouting chain.
                break
        self._redirect_configured = ok
        if ok:
            self._last_error = ""
        else:
            self._last_error = "IPv6 captive portal redirect rules are unavailable"
        return ok

    def sync(self, addresses: Iterable[str],
             run_best_effort: BestEffortNft) -> None:
        """Reconcile IPv6 set membership and leave failures retryable."""
        if not self._redirect_configured and not self.install(run_best_effort):
            return

        _, desired = split_guest_addresses(addresses)
        if desired == self._last_addresses:
            return

        self._last_addresses = None
        if not run_best_effort(
                ["flush", "set", f"inet {self.table} pending_guests6"]):
            self._last_error = "IPv6 pending guest set could not be flushed"
            return
        for address in desired:
            if not run_best_effort([
                    "add", "element", f"inet {self.table} pending_guests6",
                    f"{{ {address} }}"]):
                self._last_error = "IPv6 pending guest set could not be updated"
                return

        self._last_addresses = desired
        self._last_error = ""

    def status(self, engine_available: bool) -> dict[str, object]:
        """Report configured redirect state separately from applied membership."""
        return {
            "available": bool(engine_available and self._redirect_configured),
            "redirect_configured": self._redirect_configured,
            "pending_set_applied": bool(
                engine_available and self._redirect_configured and
                self._last_addresses is not None),
            "last_error": self._last_error,
        }
