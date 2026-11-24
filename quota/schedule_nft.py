"""Kernel side of internet schedules: the ``inet quota_schedule`` table.

Why a table of its own (and not more sets in ``quota_gateway``):

* ``NftablesEngine.start`` flushes ``quota_gateway`` on every service start.
  This table is NEVER flushed as a whole, so a device that is off by schedule
  stays off while the service restarts, and the "allowed sites" addresses that
  dnsmasq collected survive too (dnsmasq only adds an address when it forwards
  a query, not when it answers from its cache).
* Every scheduled device is added with an nftables **timeout** equal to the end
  of its cut. The kernel itself lets the device back online at the right time,
  even if the service is stopped or crashed.
* Its forward chain runs at priority -5, before ``quota_gateway``'s counters
  (priority 0), so dropped packets are never counted as usage.

Sets
----
``devs4`` / ``devs6`` / ``macs``  scheduled devices (IPv4 lease, tracked IPv6
                                  addresses, and the MAC for lease-less devices),
                                  ``flags timeout``.
``allow4`` / ``allow6``           addresses of the "always allowed" sites, filled
                                  by dnsmasq's ``nftset=`` (see
                                  ``dns_rules.render_schedule_sites``).

A scheduled device keeps LAN access (printer, NAS, the dashboard) exactly like
a quota cut: the drops exclude the local networks. DNS to the box itself goes
through the input hook, so name lookups keep working.

Every change is ONE ``nft -f -`` transaction (flush set + re-add in the same
batch), so a membership change never opens an unblock window (golden rule 2).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

FAMILY = "inet"
TABLE = "quota_schedule"
CHAIN_PRIORITY = -5
#: extra seconds on every kernel timeout, so the kernel never frees a device a
#: moment before the service's own end-of-cut tick does.
TIMEOUT_SLACK_S = 5

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

RunScript = Callable[[str], bool]


def _default_run(script: str) -> bool:
    nft = shutil.which("nft") or "/usr/sbin/nft"
    try:
        proc = subprocess.run([nft, "-f", "-"], input=script, text=True,
                              capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("schedules: nft unavailable: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning("schedules: nft rejected the batch: %s",
                    (proc.stderr or "").strip()[:500])
        return False
    return True


def _exclusions(key: str, networks: Iterable[str]) -> str:
    return "".join(f" {key} != {n}" for n in networks)


def _valid_net(net: str) -> bool:
    try:
        ipaddress.ip_network(net, strict=False)
        return True
    except ValueError:
        return False


class ScheduleFirewall:
    """Programs ``inet quota_schedule``. All methods are blocking (call them
    from a thread) and never raise."""

    def __init__(self, local_networks: Iterable[str] = (),
                 local_networks6: Iterable[str] = (),
                 run_script: Optional[RunScript] = None) -> None:
        self.local4 = [n for n in local_networks if _valid_net(n) and ":" not in n]
        self.local6 = [n for n in local_networks6 if _valid_net(n) and ":" in n]
        self._run = run_script or _default_run
        self._ready = False
        #: last committed membership (None = unknown -> rewrite on next sync)
        self._last: Optional[tuple] = None

    # -- base ---------------------------------------------------------------

    def base_script(self) -> str:
        t = f"{FAMILY} {TABLE}"
        lines = [
            f"add table {t}",
            f"add set {t} devs4 {{ type ipv4_addr; flags timeout; }}",
            f"add set {t} devs6 {{ type ipv6_addr; flags timeout; }}",
            f"add set {t} macs {{ type ether_addr; flags timeout; }}",
            f"add set {t} allow4 {{ type ipv4_addr; }}",
            f"add set {t} allow6 {{ type ipv6_addr; }}",
            f"add chain {t} forward {{ type filter hook forward priority "
            f"{CHAIN_PRIORITY}; policy accept; }}",
            f"flush chain {t} forward",
        ]
        ex4d, ex4s = _exclusions("ip daddr", self.local4), _exclusions("ip saddr", self.local4)
        ex6d, ex6s = _exclusions("ip6 daddr", self.local6), _exclusions("ip6 saddr", self.local6)
        rules = [
            f"ip saddr @devs4 ip daddr != @allow4{ex4d} drop",
            f"ip daddr @devs4 ip saddr != @allow4{ex4s} drop",
            f"ip6 saddr @devs6 ip6 daddr != @allow6{ex6d} drop",
            f"ip6 daddr @devs6 ip6 saddr != @allow6{ex6s} drop",
            # lease-less / static-IP devices: matched by their own MAC
            f"ether saddr @macs ip daddr != @allow4{ex4d} drop",
            f"ether saddr @macs ip6 daddr != @allow6{ex6d} drop",
        ]
        lines += [f"add rule {t} forward {r}" for r in rules]
        return "\n".join(lines) + "\n"

    def ensure(self) -> bool:
        """Create the table/sets (kept if present) and rewrite the chain."""
        if self._ready:
            return True
        self._ready = self._run(self.base_script())
        if self._ready:
            log.info("schedules: nft table %s.%s ready", FAMILY, TABLE)
        return self._ready

    # -- membership -----------------------------------------------------------

    @staticmethod
    def desired(cuts: dict[str, float], ip_to_mac: dict[str, str]
                ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        """(v4, v6, macs) -> end timestamp, validated (golden rule 3)."""
        macs: dict[str, int] = {}
        for mac, until in cuts.items():
            m = str(mac).lower()
            if _MAC_RE.match(m) and m != "00:00:00:00:00:00":
                macs[m] = int(until)
        v4: dict[str, int] = {}
        v6: dict[str, int] = {}
        for addr, mac in ip_to_mac.items():
            m = str(mac).lower()
            if m not in macs:
                continue
            try:
                ip = ipaddress.ip_address(str(addr))
            except ValueError:
                continue
            (v6 if ip.version == 6 else v4)[str(ip)] = macs[m]
        return v4, v6, macs

    def sync(self, cuts: dict[str, float], ip_to_mac: dict[str, str],
             now_ts: float) -> bool:
        """Make the kernel sets hold exactly ``cuts`` (MAC -> end timestamp)."""
        v4, v6, macs = self.desired(cuts, ip_to_mac)
        sig = (tuple(sorted(v4.items())), tuple(sorted(v6.items())),
               tuple(sorted(macs.items())))
        if sig == self._last:
            return True
        if not self.ensure():
            return False
        t = f"{FAMILY} {TABLE}"
        lines = [f"flush set {t} devs4", f"flush set {t} devs6", f"flush set {t} macs"]
        for name, members in (("devs4", v4), ("devs6", v6), ("macs", macs)):
            elems = []
            for addr, until in sorted(members.items()):
                left = int(until - now_ts) + TIMEOUT_SLACK_S
                if left <= TIMEOUT_SLACK_S:
                    continue  # already over
                elems.append(f"{addr} timeout {left}s")
            if elems:
                lines.append(f"add element {t} {name} {{ {', '.join(elems)} }}")
        self._last = None
        if not self._run("\n".join(lines) + "\n"):
            # The table may be gone (someone ran `nft flush ruleset`): rebuild
            # it on the next try.
            self._ready = False
            return False
        self._last = sig
        if macs:
            log.info("schedules: %d device(s) off by schedule", len(macs))
        return True

    def reset_allowed(self) -> bool:
        """Forget the collected allowed-site addresses (the site list changed;
        dnsmasq is restarted with the new list and refills them)."""
        if not self.ensure():
            return False
        t = f"{FAMILY} {TABLE}"
        return self._run(f"flush set {t} allow4\nflush set {t} allow6\n")
