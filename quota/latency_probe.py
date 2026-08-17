"""WiFi/LAN classification by round-trip latency (any hardware, any router).

Monitor-mode sniffing (quota/wifi_probe.py) needs a card whose driver supports
monitor mode — many (especially Windows-era or vendor-locked WiFi modules) do
not. This module answers the same question ("is this device on the ROUTER's
WiFi or wired") with plain ARP round-trip times, which work on ANY NIC and ANY
router firmware:

  * a wired device's ARP reply crosses the router's bridge — sub-millisecond;
  * a WiFi device's reply additionally pays airtime (802.11 frame exchange +
    contention), typically 1 ms and up, and the radio has no reliable way to
    look faster.

So the fastest observed RTT (the least affected by local scheduling noise) is
a reliable WiFi/LAN discriminant: ``min(rtts) >= threshold_ms`` => WiFi.

Probing backend (mirrors quota/arp_scan.py):
  1. raw AF_PACKET ARP requests with per-request timestamps (default);
  2. ``ping -c N`` output parsing (``time=...``) when raw sockets are absent
     (no root / non-Linux) — ICMP-blocking clients then go unclassified;
  3. empty result when probing is impossible — the box keeps the previous
     label (graceful degradation, same as the engine).

The classifier is deliberately simple (min over N samples, both limits
configurable) so a misclassification is one knob to turn, and run.py wraps it
in a consecutive-sweep guard to prevent flapping. The monitor-mode probe, when
present, still takes precedence and adds the exact SSID.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from typing import Callable

from quota.arp_scan import (RunCommand, _default_run_command,
                            arp_request_frame, parse_arp_reply, resolve_nic)
from quota.nftables import resolve_local_networks

log = logging.getLogger("quota.latency_probe")

ETH_P_ARP = 0x0806

#: classification result
WIFI = "wifi"
LAN = "lan"
UNKNOWN = "unknown"


def classify_rtts(rtts: list[float], threshold_ms: float,
                  min_samples: int = 3) -> str:
    """Classify a device as WiFi/LAN from its round-trip samples.

    Uses the MINIMUM sample: local scheduling noise only ever inflates RTTs,
    so the best-case delivery is the true path latency. Fewer than
    ``min_samples`` replies => UNKNOWN (the caller keeps the previous label).
    """
    if len(rtts) < min_samples:
        return UNKNOWN
    return WIFI if min(rtts) >= threshold_ms else LAN


class ArpRttProbe:
    """Measure per-target ARP round-trip times on the LAN wire.

    Constructed once at startup (no sockets opened yet). :meth:`probe` is
    blocking and must be called off the event loop (``asyncio.to_thread``);
    it degrades to a ping parse when raw sockets are unavailable and to an
    empty map when probing is impossible.
    """

    def __init__(self, cfg, run_command: RunCommand | None = None,
                 socket_factory: Callable[[], socket.socket] | None = None,
                 samples: int = 6, timeout_s: float = 0.5,
                 inter_round_s: float = 0.02) -> None:
        self._run = run_command or _default_run_command
        self._socket_factory = socket_factory
        self.samples = max(1, int(samples))
        self.timeout_s = timeout_s
        self.inter_round_s = inter_round_s
        dhcp = getattr(cfg, "dhcp", None)
        self._client_ip = getattr(dhcp, "gateway_ip", "") if dhcp else ""
        self._router_ip = getattr(dhcp, "router_ip", "") if dhcp else ""
        # Client subnets only (wan_client_only mirrors the rogue scanner; the
        # box's own addresses and the router are never probed).
        self._networks = resolve_local_networks(
            getattr(cfg, "engine", None), dhcp, wan_client_only=True)

    @property
    def enabled(self) -> bool:
        return bool(self._networks) and bool(self._client_ip)

    def _open_raw_socket(self) -> socket.socket | None:
        """Open the raw AF_PACKET socket (injected factory or real). None =>
        raw sockets unavailable (no root / non-Linux / a refused factory) and
        the caller falls back to ping parsing."""
        if self._socket_factory is not None:
            return self._socket_factory()
        try:
            return socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                 socket.htons(ETH_P_ARP))
        except (AttributeError, OSError):
            return None

    def _raw_probe(self, targets: list[str], iface: str, box_mac: str,
                   box_ips: set[str]) -> dict[str, list[float]] | None:
        """Burst ARP requests per target; time the replies. None => no raw
        sockets (caller falls back)."""
        try:
            sock = self._open_raw_socket()
        except (AttributeError, OSError):
            sock = None
        if sock is None:
            log.warning("cannot open a raw ARP socket (root?) — latency "
                        "probe falls back to ping parsing")
            return None
        rtts: dict[str, list[float]] = {ip: [] for ip in targets}
        try:
            sock.bind((iface, 0))
            pending: dict[str, list[float]] = {}
            # The box answers all ARPs with the NIC MAC regardless — the
            # sender IP just needs to be one of the box's own addresses.
            src = next(iter(box_ips), self._client_ip)
            sock.settimeout(self.timeout_s)
            # Interleaved send/drain rounds: a power-save device (sleeping
            # phone, NIC-sleeping PC) wakes on the FIRST request but its
            # replies land seconds later — a send-everything-then-listen sweep
            # closes its receive window before the device wakes. Round-robin
            # gives every target a fresh reply window across the whole sweep,
            # and the device's post-wake replies are its true path latency.
            for _ in range(self.samples):
                for ip in targets:
                    try:
                        sock.send(arp_request_frame(box_mac, src, ip))
                    except OSError:
                        continue
                    pending.setdefault(ip, []).append(time.monotonic())
                time.sleep(self.inter_round_s)
                self._drain(sock, pending, rtts)
        finally:
            sock.close()
        return {ip: ts for ip, ts in rtts.items() if ts}

    def _drain(self, sock: socket.socket, pending: dict[str, list[float]],
               rtts: dict[str, list[float]]) -> bool:
        """Read replies until one timeout; True when anything was received."""
        got = False
        while True:
            try:
                frame = sock.recv(2048)
            except socket.timeout:
                return got
            except OSError:
                return got
            parsed = parse_arp_reply(frame)
            if parsed and parsed[0] in pending and pending[parsed[0]]:
                sent_at = pending[parsed[0]].pop(0)
                rtts[parsed[0]].append(
                    (time.monotonic() - sent_at) * 1000.0)
                got = True

    def _ping_probe(self, targets: list[str]) -> dict[str, list[float]]:
        """Parse ``time=`` values out of ``ping`` — the no-raw-socket
        fallback (ICMP-blocking clients are simply not classified)."""
        rtts: dict[str, list[float]] = {}
        for ip in targets:
            code, out = self._run(
                ["ping", "-c", str(self.samples), "-W", "1", "-n", "-q", ip])
            if code != 0:
                continue
            values = [float(m) for m in re.findall(r"time=([0-9.]+)\s*ms", out)]
            if values:
                rtts[ip] = values
        return rtts

    def probe(self, targets: list[str]) -> dict[str, list[float]]:
        """Return ``{ip: [rtt_ms...]}`` for every probed, replying target."""
        if not targets or not self.enabled:
            return {}
        nic = resolve_nic(self._client_ip, self._run)
        if nic is None:
            return {}
        iface, box_mac, box_ips = nic
        found = self._raw_probe(targets, iface, box_mac, box_ips)
        if found is None:
            return self._ping_probe(targets)
        return found