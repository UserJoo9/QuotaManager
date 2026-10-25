"""ISP IPv6 reachability probe and dynamic dual-stack state.

Decides whether the ISP actually delivers IPv6 right now:

1. Looks for a Global Unicast Address (2000::/3) on the WAN/uplink interface.
2. Actively probes external IPv6 targets (Cloudflare 2606:4700:4700::1111,
   Google 2001:4860:4860::8888) with a TCP connect (ICMPv6 ping as fallback).

The verdict drives the nftables ``v6gate``: while the ISP has no IPv6 every
client IPv6 packet bound for the internet is answered with ICMPv6 "no route",
so apps fall back to IPv4 *instantly* (instead of waiting out a black hole);
the moment the ISP delivers IPv6 the gate opens and the box NATs the clients'
ULA addresses out (NAT66). LAN-local IPv6 (ULA / link-local) always works.

Cadence: on boot, then every ``check_interval_sec`` (default 10 min) — and on
demand from the dashboard. Activation is immediate on the first success;
deactivation needs ``fail_threshold`` consecutive failures so one lost probe
never flaps every client's IPv6.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import platform
import subprocess
import time
from typing import Any, Callable, Optional

log = logging.getLogger("quota.ipv6_probe")

RunCommand = Callable[[list[str]], tuple[int, str]]


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return proc.returncode, (proc.stdout or proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    except Exception as exc:
        return 1, str(exc)


_ULA_NET = ipaddress.IPv6Network("fc00::/7")


def is_global_unicast_ipv6(addr_str: str) -> bool:
    """Check if address is a routable Global Unicast Address (2000::/3).

    Excludes loopback (::1), link-local (fe80::/10), and ULA (fc00::/7 / fd00::/8).
    """
    clean = addr_str.strip().split("/")[0].split("%")[0].strip("[]")
    if not clean:
        return False
    try:
        ip = ipaddress.IPv6Address(clean)
        if ip.is_loopback or ip.is_link_local or ip.is_multicast:
            return False
        if ip in _ULA_NET:
            return False
        # 2000::/3 check (first three bits are 001)
        return (int(ip) >> 125) == 1
    except ValueError:
        return False


class Ipv6Probe:
    """Periodic ISP IPv6 reachability tester and dual-stack status reporter."""

    DEFAULT_TARGETS = [
        "2606:4700:4700::1111",  # Cloudflare IPv6 DNS
        "2001:4860:4860::8888",  # Google IPv6 DNS
    ]

    def __init__(
        self,
        cfg: Any = None,
        run_command: Optional[RunCommand] = None,
        probe_targets: Optional[list[str]] = None,
        check_interval_sec: float = 600.0,
        fail_threshold: int = 2,
        on_status_change: Optional[Callable[[bool, dict[str, Any]], Any]] = None,
    ) -> None:
        self.cfg = cfg
        self._run_command = run_command or _default_run_command
        self.probe_targets = list(probe_targets or self.DEFAULT_TARGETS)
        self.check_interval_sec = max(60.0, float(check_interval_sec))
        self.fail_threshold = max(1, int(fail_threshold))
        self._fail_streak = 0
        self.on_status_change = on_status_change

        self.wan_ipv6_active: bool = False
        self.wan_ipv6_address: str = ""
        self.last_probe_time: float = 0.0
        self.last_probe_status: str = "not_checked"
        self.last_probe_details: str = ""

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def detect_wan_global_ipv6(self, wan_iface: str = "") -> str:
        """Scan system interfaces for a valid Global Unicast IPv6 address."""
        cmd = ["ip", "-6", "-o", "addr", "show", "scope", "global"]
        if wan_iface:
            cmd.extend(["dev", wan_iface])
        code, out = self._run_command(cmd)
        if code == 0 and out:
            for line in out.splitlines():
                parts = line.split()
                # format: <num>: <ifname> inet6 <addr>/<cidr> scope global ...
                if len(parts) >= 4 and parts[2] == "inet6":
                    cand = parts[3].split("/")[0]
                    if is_global_unicast_ipv6(cand):
                        return cand
        return ""

    def detect_default_ipv6_route(self) -> bool:
        """Check whether the kernel has a default IPv6 gateway route."""
        code, out = self._run_command(["ip", "-6", "route", "show", "default"])
        if code == 0 and "default" in out:
            return True
        return False

    async def check_reachability(self, timeout_sec: float = 3.0) -> tuple[bool, str]:
        """Probe external IPv6 targets via direct async TCP connect to port 53.

        Falls back to ping -6 if socket connection cannot be formed.
        """
        for target in self.probe_targets:
            # 1. Direct TCP connect to port 53 (reliable, unprivileged)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(target, 53),
                    timeout=timeout_sec,
                )
                writer.close()
                await writer.wait_closed()
                return True, f"TCP connect succeeded to {target}:53"
            except Exception as e:
                log.debug("IPv6 socket probe to %s failed: %s", target, e)

            # 2. ICMPv6 ping fallback
            try:
                if platform.system().lower() == "windows":
                    ping_cmd = ["ping", "-6", "-n", "1", "-w", str(int(timeout_sec * 1000)), target]
                else:
                    ping_cmd = ["ping", "-6", "-c", "1", "-W", str(int(timeout_sec)), target]
                code, out = await asyncio.to_thread(self._run_command, ping_cmd)
                if code == 0:
                    return True, f"ping -6 succeeded to {target}"
            except Exception as e:
                log.debug("IPv6 ping fallback to %s failed: %s", target, e)

        return False, "All IPv6 external probe targets unreachable"

    async def probe_now(self) -> dict[str, Any]:
        """Execute a full probe of WAN IPv6 connectivity."""
        wan_iface = ""
        engine = getattr(self.cfg, "engine", None) if self.cfg else None
        if engine and getattr(engine, "topology", "") == "wan":
            wan_iface = "ppp0"

        # 1. Address detection
        global_ip = await asyncio.to_thread(self.detect_wan_global_ipv6, wan_iface)
        self.wan_ipv6_address = global_ip

        # 2. Reachability probe
        reachable, reason = await self.check_reachability()

        old_active = self.wan_ipv6_active
        self.last_probe_time = time.time()
        self.last_probe_details = reason
        if reachable:
            self._fail_streak = 0
            self.wan_ipv6_active = True
        else:
            self._fail_streak += 1
            # one lost probe must not flap every client's IPv6: stay active
            # until ``fail_threshold`` probes in a row have failed
            if not (old_active and self._fail_streak < self.fail_threshold):
                self.wan_ipv6_active = False

        if reachable:
            self.last_probe_status = "active"
        elif old_active and self.wan_ipv6_active:
            self.last_probe_status = "active_degraded"
        elif global_ip:
            self.last_probe_status = "has_ip_unreachable"
        else:
            self.last_probe_status = "isp_no_ipv6"

        log.info(
            "IPv6 Probe completed: status=%s, global_ip=%s, active=%s, reason=%s",
            self.last_probe_status,
            global_ip or "none",
            self.wan_ipv6_active,
            reason,
        )

        status = self.get_status()
        if old_active != self.wan_ipv6_active and self.on_status_change is not None:
            try:
                res = self.on_status_change(self.wan_ipv6_active, status)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                log.exception("Error in IPv6 on_status_change hook: %s", exc)

        return status

    def get_status(self) -> dict[str, Any]:
        """Return a dictionary representing current IPv6 dual-stack status."""
        dhcp = getattr(self.cfg, "dhcp", None) if self.cfg else None
        prefix = getattr(dhcp, "ipv6_prefix", "fd00:da7a:2::/64") if dhcp else "fd00:da7a:2::/64"
        gateway = getattr(dhcp, "ipv6_gateway", "fd00:da7a:2::1") if dhcp else "fd00:da7a:2::1"
        return {
            "lan_ipv6_enabled": True,
            "lan_prefix": prefix,
            "lan_gateway": gateway,
            "lan_mode": "SLAAC + DHCPv6 (RFC 4862)",
            "check_interval_sec": self.check_interval_sec,
            "wan_ipv6_active": self.wan_ipv6_active,
            "wan_ipv6_address": self.wan_ipv6_address,
            "last_probe_time": self.last_probe_time,
            "last_probe_status": self.last_probe_status,
            "last_probe_details": self.last_probe_details,
        }

    async def start(self) -> None:
        """Start the background periodic probe task."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._probe_loop())
        log.info("IPv6 probe background loop started (interval: %.0f min)", self.check_interval_sec / 60.0)

    async def stop(self) -> None:
        """Stop the background probe task."""
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("IPv6 probe background loop stopped")

    async def _probe_loop(self) -> None:
        """Run initial probe on startup, then probe periodically."""
        # Initial check right after boot
        try:
            await self.probe_now()
        except Exception as e:
            log.warning("Initial IPv6 probe encountered error: %s", e)

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.check_interval_sec)
                break
            except asyncio.TimeoutError:
                try:
                    await self.probe_now()
                except Exception as e:
                    log.warning("Periodic IPv6 probe encountered error: %s", e)
