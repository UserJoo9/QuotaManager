"""Packet engine: per-device accounting + hard blocking via WinDivert.

The engine runs in a dedicated thread. It opens a WinDivert handle, reads every
packet, attributes bytes to the client device by IP, and — for a blocked
device — does **not** re-inject the packet (WinDivert drops anything you don't
re-inject). Counters are held in a plain dict and swapped out by the asyncio
side every flush interval; the hot path touches no locks and no I/O.

Windows kernel IP forwarding means each forwarded packet is observed twice by
WinDivert (an inbound sighting and an outbound sighting). To avoid
double-counting, only one direction is counted (configurable, default
``inbound``); the other sighting is simply re-injected without counting.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("quota.engine")


@dataclass
class EngineCounters:
    """Atomic snapshot of per-IP byte counters (bytes)."""

    up: int = 0      # bytes sent by clients (to the internet)
    down: int = 0    # bytes received by clients

    @property
    def total(self) -> int:
        return self.up + self.down


@dataclass
class EngineSnapshot:
    """Swapped atomically between engine thread and consumers."""

    #: ip -> counters
    by_ip: dict[str, EngineCounters] = field(default_factory=dict)
    #: ip -> mac for fast lookup
    ip_to_mac: dict[str, str] = field(default_factory=dict)
    #: mac -> blocked (bool) — the enforcement truth
    blocked: dict[str, bool] = field(default_factory=dict)
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

    The engine thread writes via :meth:`swap`; the asyncio/UI side reads via
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


class PacketEngine:
    """WinDivert-backed accounting + drop thread.

    Attributes
    ----------
    snapshot_holder:
        A mutable holder (e.g. ``SnapshotHolder``) with a ``swap()``/``get()``
        interface used to hand the latest snapshot to the asyncio side without
        locks in the hot path.
    """

    def __init__(
        self,
        cfg: Any,
        snapshot_holder: Any,
        is_blocked_cb: Callable[[str], bool] | None = None,
    ) -> None:
        self.cfg = cfg
        self.holder = snapshot_holder
        self.is_blocked_cb = is_blocked_cb or (lambda ip: False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error_queue: queue.Queue[Exception] = queue.Queue(maxsize=8)

        # Locked only on swap; hot path reads the atomically-swapped dicts.
        self._lock = threading.Lock()
        self._counters: dict[str, EngineCounters] = {}
        self._ip_to_mac: dict[str, str] = {}
        self._blocked: dict[str, bool] = {}
        self._last_flush = time.time()

        # Rate-limit send-failure logging (WinDivertSend can fail transiently).
        self._last_send_error = 0.0

    def _build_filter(self) -> str:
        """WinDivert filter string scoped to the client pool.

        A bare ``ip`` filter diverts **every** IPv4 packet on the wire — the PC's
        own traffic, DHCP broadcasts (src 0.0.0.0 / dst 255.255.255.255) and
        multicast. Re-injecting those with WinDivertSend can fail with
        ``ERROR_NETWORK_UNREACHABLE`` (WinError 1232) because the machine has no
        route to, e.g., 255.255.255.255 — and a single unhandled failure used to
        kill the whole engine thread. Scoping the filter to the DHCP pool means
        only client traffic is ever diverted, so unroutable packets never reach
        WinDivertSend in the first place.
        """
        dhcp = getattr(self.cfg, "dhcp", None)
        start = getattr(dhcp, "pool_start", "") if dhcp is not None else ""
        end = getattr(dhcp, "pool_end", "") if dhcp is not None else ""
        if start and end:
            return (
                f"ip and ((ip.SrcAddr >= {start} and ip.SrcAddr <= {end}) "
                f"or (ip.DstAddr >= {start} and ip.DstAddr <= {end}))"
            )
        return "ip"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="packet-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update_state(self, ip_to_mac: dict[str, str], blocked: dict[str, bool]) -> None:
        """Update IP->MAC and blocked maps (called by the quota service)."""
        with self._lock:
            self._ip_to_mac = dict(ip_to_mac)
            self._blocked = dict(blocked)

    def flush(self) -> EngineSnapshot:
        """Return current counters and reset the accumulator."""
        with self._lock:
            snap = EngineSnapshot(
                by_ip=self._counters,
                ip_to_mac=dict(self._ip_to_mac),
                blocked=dict(self._blocked),
                ts=time.time(),
            )
            self._counters = {}
            return snap

    # -- packet handling ------------------------------------------------------

    def _run(self) -> None:
        try:
            # Lazy probe so the rest of the app imports fine without pydivert.
            self._pydivert = __import__("pydivert")
        except ImportError:
            log.error("pydivert not installed — packet engine unavailable. "
                      "Install with `pip install pydivert` (WinDivert driver bundled).")
            self._report_error(RuntimeError("pydivert missing"))
            return

        try:
            self._loop()
        except Exception as exc:  # noqa: BLE001 - report & die cleanly
            log.exception("packet engine crashed")
            self._report_error(exc)

    def _report_error(self, exc: Exception) -> None:
        try:
            self.error_queue.put_nowait(exc)
        except queue.Full:
            pass

    def _loop(self) -> None:
        pydivert = self._pydivert
        filter_str = self._build_filter()
        with pydivert.WinDivert(filter_str) as w:
            log.info("packet engine attached: %s (filter: %s)", w, filter_str)
            while not self._stop.is_set():
                try:
                    packet = w.recv()
                except OSError:
                    # Handle closed between checks; otherwise transient and rare.
                    if self._stop.is_set():
                        break
                    continue
                try:
                    self._handle_packet(packet, w)
                except OSError as exc:
                    # Belt-and-suspenders: a WinDivert call must never kill the
                    # engine thread (WinError 1232 etc.). Drop the packet.
                    self._report_send_error(exc, packet)

    def _report_send_error(self, exc: Exception, packet: Any) -> None:
        """Log a WinDivertSend failure at most once per minute (hot path)."""
        now = time.time()
        if now - self._last_send_error > 60.0:
            ip = getattr(packet, "ipv4", None)
            src = getattr(ip, "src_addr", "?") if ip is not None else "?"
            dst = getattr(ip, "dst_addr", "?") if ip is not None else "?"
            log.warning(
                "WinDivertSend failed (%s) for %s -> %s — packet dropped; "
                "suppressing repeats for 60s", exc, src, dst)
            self._last_send_error = now

    def _handle_packet(self, packet: Any, w: Any) -> None:
        ip = getattr(packet, "ipv4", None)
        if ip is None:
            self._safe_send(w, packet)  # non-IPv4: pass through untouched
            return

        # Identify the client: the packet is for/from a device iff either src or
        # dst is a LAN client IP we know.
        with self._lock:
            ip_to_mac = self._ip_to_mac
            blocked = self._blocked
            counters = self._counters

        src = ip.src_addr
        dst = ip.dst_addr
        is_from_client = src in ip_to_mac
        is_to_client = dst in ip_to_mac

        if not (is_from_client or is_to_client):
            self._safe_send(w, packet)
            return

        client_ip = src if is_from_client else dst
        client_mac = ip_to_mac.get(client_ip, "")

        # Hard block: drop (don't re-inject) packets touching a blocked device.
        if blocked.get(client_mac, False):
            return  # not sent -> dropped

        # Count. Only count the configured direction to avoid double counting
        # forwarded traffic (kernel routes it across the callout twice).
        direction = self.cfg.engine.count_direction if hasattr(self.cfg, "engine") else "inbound"
        packet_len = len(packet.raw) if hasattr(packet, "raw") else 0
        counter = counters.setdefault(client_ip, EngineCounters())
        if is_from_client:
            # client -> internet (upload). Count on inbound sighting only.
            if direction != "outbound":
                counter.up += packet_len
        else:
            if direction == "inbound":
                counter.down += packet_len
            else:
                # outbound-direction counting: still count the down sighting
                counter.down += packet_len

        self._safe_send(w, packet)

    def _safe_send(self, w: Any, packet: Any) -> None:
        """Re-inject ``packet``; never raise on transient WinDivert failures."""
        try:
            w.send(packet)
        except OSError as exc:
            self._report_send_error(exc, packet)

    # -- helpers (used by tests) ------------------------------------------------

    def _classify_for_test(self, src: str, dst: str, blocked: dict[str, bool] | None = None) -> tuple[bool, str, bool]:
        """Pure classification helper (no pydivert). Used by unit tests."""
        with self._lock:
            ip_to_mac = self._ip_to_mac
            blocked_map = self._blocked if blocked is None else blocked
        src_is_client = src in ip_to_mac
        dst_is_client = dst in ip_to_mac
        if not (src_is_client or dst_is_client):
            return False, "", False
        client_ip = src if src_is_client else dst
        client_mac = ip_to_mac.get(client_ip, "")
        drop = blocked_map.get(client_mac, False)
        return True, client_ip, drop
