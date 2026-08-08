"""Shared engine snapshot types.

The Linux gateway counts and blocks in the kernel (nftables) — there is no
userspace packet hot path. This module holds only the small data types passed
between the nftables engine and the asyncio side: per-IP byte counters, an
atomically-swapped snapshot, and a thread-safe holder. The holder is what the
API / WebSocket push reads to show live per-device traffic.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: Sentinel MAC for the gateway box itself. The box's own internet consumption
#: is charged to a protected "Gateway" user under this MAC (see
#: db._seed_gateway); the nftables engine counts/blocked it via input/output
#: hooks, never via a forward-chain device rule.
GATEWAY_MAC = "00:00:00:00:00:00"


@dataclass
class EngineCounters:
    """Atomic snapshot of per-IP byte counters (bytes)."""

    up: int = 0      # bytes sent by clients (to the internet)
    down: int = 0    # bytes received by clients

    @property
    def total(self) -> int:
        return self.up + self.down


@dataclass
class RogueHost:
    """A host seen alive on the LAN that is NOT a known DHCP device.

    Rogues are typically devices with a static IP + the router as their gateway,
    which route around the quota box entirely. ``online`` is the last scan's view
    (raw-socket ARP probe / ``ip neigh``); ``vendor`` is resolved from the MAC.
    """

    ip: str = ""
    mac: str = ""
    vendor: str = ""
    online: bool = True


@dataclass
class EngineSnapshot:
    """Swapped atomically between engine thread and consumers."""

    #: ip -> counters
    by_ip: dict[str, EngineCounters] = field(default_factory=dict)
    #: ip -> mac for fast lookup
    ip_to_mac: dict[str, str] = field(default_factory=dict)
    #: mac -> blocked (bool) — the enforcement truth
    blocked: dict[str, bool] = field(default_factory=dict)
    #: hosts alive on the LAN that are NOT known DHCP devices (static-IP bypassers)
    rogue: list[RogueHost] = field(default_factory=list)
    #: live WAN-mode status ({topology, source, pending, ppp0, ppp_ip, ppp_peer});
    #: empty dict until the maintenance loop populates it (mirrors ``rogue``).
    wan_status: dict = field(default_factory=dict)
    #: the box's OWN internet bytes since the last flush (input/output hooks —
    #: the box's packets never cross the forward chain). Drained into the
    #: Gateway user's device usage by the maintenance loop.
    gateway: EngineCounters = field(default_factory=EngineCounters)
    #: last ``gw_blocked`` set membership the engine pushed to the kernel for
    #: the box itself (True = cut, False = free, None = never programmed). The
    #: maintenance loop copies it from the engine after ``set_gateway_blocked``
    #: so the dashboard can show when the UI toggle and the kernel disagree.
    gateway_blocked: bool | None = None
    #: is the packet engine running? False when it was never built (config
    #: ``engine.enabled=false``) or failed at start — nothing is counted or
    #: blocked in that state, so the dashboard must say so instead of implying
    #: enforcement is live.
    engine_available: bool = True
    ts: float = field(default_factory=time.time)

    def counters_for(self, mac: str) -> EngineCounters:
        """Aggregate live counters for a MAC (sum over its IPs)."""
        agg = EngineCounters()
        for ip, mac_ in self.ip_to_mac.items():
            if mac_ == mac:
                c = self.by_ip.get(ip)
                if c:
                    agg.up += c.up
                    agg.down += c.down
        return agg


class SnapshotHolder:
    """Thread-safe holder of the latest :class:`EngineSnapshot`.

    The engine writes via :meth:`swap`; the asyncio/UI side reads via
    :meth:`get`. No locks are taken in the packet hot path — the holder just
    hands over an immutable snapshot object.
    """

    def __init__(self) -> None:
        self._snapshot = EngineSnapshot()
        self._lock = threading.Lock()

    def swap(self, snapshot: EngineSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> EngineSnapshot:
        with self._lock:
            return self._snapshot
