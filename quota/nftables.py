"""nftables-backed packet engine for the Linux gateway.

Mirrors the shared engine interface (``start`` / ``stop`` / ``update_state`` /
``flush``, using :class:`quota.engine.EngineSnapshot`) but instead of diverting
packets in a userspace thread it programs the kernel:

* one **named counter per device per direction** — ``q_up_<ip>`` / ``q_down_<ip>``
  (dots -> underscores) — matching rules in the ``forward`` hook. Clients live
  on their own subnet (``192.168.2.0/24``) that the kernel masquerades out the
  uplink, so the forward chain sees the whole byte flow and the box's own
  traffic (DNS, DHCP, the dashboard) is naturally excluded.
* **LOCAL (LAN) traffic never counts** against the metered bundle. Same-subnet
  client<->client is L2 and never forwards, but client<->uplink-subnet hosts
  (the router's admin UI, a NAS, the router as DNS) DO cross the forward hook —
  without an exclusion those bytes would be charged. The counter rules carry
  ``ip daddr/saddr != <local-net>`` matches for the client subnet and the uplink
  subnet (from ``engine.client_subnet`` / ``engine.uplink_subnet``, derived
  from the dhcp block when unset), so LAN traffic is simply never counted.
* a **``blocked`` set** that two drop rules reference — `ip saddr @blocked drop`
  + `ip daddr @blocked drop`. The kernel drops a blocked device's internet
  packets at line rate, no Python in the path. The drop rules carry the same
  local-net exclusions, so a quota-blocked device keeps LAN access (printer,
  NAS, router admin) while its internet is cut.

Counters are read back with ``nft -j list counters`` (JSON — far cheaper than
walking the ruleset text). ``flush()`` returns the **delta since the previous
flush**, which is exactly what the maintenance loop writes to ``usage_daily``.

Rule lifecycle
--------------
* ``start()`` — idempotent, best-effort: (re)builds the table base. It flushes
  the table first so a restart never inherits stale device rules, then zeroes
  any named counters that survived the flush (``nft reset counters``, best
  effort). ``update_state`` re-seeds the delta baseline from a counter that
  carried its cumulative total over a restart, so that total is never drained
  as new usage.
* ``update_state(ip_to_mac, blocked)`` — **add-only** for device counter rules
  (new IPs get a counter pair; departed IPs' rules are left in place but never
  reported, because ``flush()`` only surfaces IPs in the current map). The
  ``blocked`` set is rebuilt from scratch each call so drops always match the
  service's latest decision.
* ``flush()`` — reads counters, subtracts the engine's last-seen values,
  returns an :class:`EngineSnapshot` limited to known device IPs.

Graceful degradation
--------------------
If ``nft`` is missing, the caller lacks root, or a command fails, the engine
marks itself unavailable and ``flush()`` returns empty snapshots — the rest of
the app (dashboard, DB usage) keeps working.

The command runner is injected (``run_command``) so tests can drive a fake
``nft`` binary and assert the exact ruleset programmed.
"""

from __future__ import annotations

import json
import logging
import time
from ipaddress import ip_address, ip_network
from typing import Any, Callable

from quota.engine import EngineCounters, EngineSnapshot, GATEWAY_MAC

log = logging.getLogger("quota.nftables")

#: argv -> (returncode, output). Tests inject a fake; the default shells out
#: to the real ``nft`` binary.
RunCommand = Callable[[list[str]], tuple[int, str]]

#: Table all rules live in. ``inet`` so an operator's IPv6 rules coexist.
FAMILY = "inet"

#: ``arp``-family table for the ARP gateway-lock's interception chain.
#: ARP is link-layer, so the lock uses its own table (an inet chain cannot
#: match ARP frames).
ARP_TABLE = "quota_arp_lock"


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, "nft: not found"
    except subprocess.TimeoutExpired:
        return 124, "nft: timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def _counter_name(ip: str, direction: str) -> str:
    """nft identifier for a device's counter (dots are not allowed)."""
    return f"q_{direction}_{ip.replace('.', '_')}"


def _cidr(value: str) -> str | None:
    """Normalize an IPv4 CIDR string (``192.168.2.0/24``); None when invalid."""
    try:
        return str(ip_network(value, strict=False))
    except ValueError:
        return None


def _derive_subnet(host: str, netmask: str) -> str | None:
    """Derive a network from a host IP + netmask (dotted, ``24``, or ``/24``)."""
    if not host:
        return None
    mask = str(netmask or "").lstrip("/")
    return _cidr(f"{host}/{mask}") if mask else None


def resolve_local_networks(engine_cfg: Any, dhcp_cfg: Any,
                           *, wan_client_only: bool = False) -> list[str]:
    """Which LAN subnets are LOCAL — never counted or dropped by the engine.

    Explicit ``engine.client_subnet`` / ``engine.uplink_subnet`` values win;
    otherwise each is derived from the dhcp block (``gateway_ip`` -> client
    subnet, ``router_ip`` -> uplink subnet), the uplink falling back to the LAN
    snapshot (``dhcp.uplink_ip`` + ``dhcp.lan_cidr``) when the router key is
    empty. An invalid explicit value warns and falls back to derivation.
    Returns a deduped, sorted list; empty when nothing resolves (fall back to
    counting every forwarded packet — the pre-LAN-aware behaviour).

    Under ``engine.topology == "wan"`` the box terminates the WAN itself (dials
    PPPoE on ppp0), but it KEEPS the old uplink IP as a secondary alias so
    clients can reach the router's admin page (192.168.1.1) through it — that
    uplink subnet is LOCAL too. An explicit ``engine.uplink_subnet`` is honored;
    otherwise it derives from the LAN snapshot (``dhcp.uplink_ip`` +
    ``dhcp.lan_cidr``), falling back to ``dhcp.router_ip`` + ``dhcp.subnet``.

    ``wan_client_only`` (rogue scanner only): under WAN topology return just the
    client subnet. The uplink subnet then holds only the box's admin alias and
    the bridged router's own management IP, and only ``$CLIENT_NET`` is
    masqueraded — so a static host there can never reach the internet, and
    probing it would just flag the router as a false rogue. Ignored in LAN mode.
    """

    def one(field: str, host_attr: str, netmask_attr: str = "subnet") -> str | None:
        raw = (getattr(engine_cfg, field, "") or "").strip()
        if raw:
            net = _cidr(raw)
            if net is not None:
                return net
            log.warning("engine.%s=%r is not a valid IPv4 CIDR — deriving it "
                        "from the dhcp block instead", field, raw)
        host = getattr(dhcp_cfg, host_attr, "") if dhcp_cfg is not None else ""
        netmask = getattr(dhcp_cfg, netmask_attr, "") if dhcp_cfg is not None else ""
        return _derive_subnet(host, netmask)

    if (getattr(engine_cfg, "topology", "") or "").strip().lower() == "wan":
        # The box keeps the uplink IP as a router-admin alias, so the uplink
        # subnet is local in WAN mode too. Explicit uplink_subnet wins; else the
        # LAN snapshot (uplink_ip + lan_cidr) survives from LAN mode; else the
        # active router_ip. A static-IP bypasser is still stopped — the NAT
        # masquerade only covers the client subnet, so an uplink-subnet source
        # is never NATed out ppp0.
        client = one("client_subnet", "gateway_ip")
        if wan_client_only:
            return [client] if client else []
        uplink = (one("uplink_subnet", "uplink_ip", "lan_cidr")
                  or one("uplink_subnet", "router_ip"))
        return sorted({n for n in (client, uplink) if n})

    return sorted({n for n in (one("client_subnet", "gateway_ip"),
                               one("uplink_subnet", "router_ip")
                               or one("uplink_subnet", "uplink_ip", "lan_cidr"))
                   if n})


def _match(seed: str, negated_key: str, networks: list[str]) -> str:
    """Build an nftables match expression with LAN exclusions.

    ``seed`` is the primary match (e.g. ``ip saddr 192.168.2.111``); for each
    LOCAL network the ``negated_key`` (``ip daddr`` / ``ip saddr``) is negated
    against it, so a packet whose other endpoint is on the LAN never matches
    the rule. With no local networks it returns the seed unchanged.
    """
    if not networks:
        return seed
    return " ".join([seed] + [f"{negated_key} != {n}" for n in networks])


def _gateway_exclusions(negated_key: str, networks: list[str]) -> str:
    """Local-net exclusions for a gateway hook rule (no device IP to anchor).

    ``_match`` needs a seed expression; the box's own rules have none — they
    match any IP except the LOCAL networks. Emits just the negated key
    (``ip daddr != <net> ip daddr != <net>``), or ``""`` when no local network
    resolves (count/drop everything the box sends or receives).
    """
    if not networks:
        return ""
    return " ".join(f"{negated_key} != {n}" for n in networks)


def _which_network(networks: list[str], host: str) -> str | None:
    """The network in ``networks`` that contains ``host`` (CIDR string)."""
    for net_str in networks:
        try:
            if ip_address(host) in ip_network(net_str, strict=False):
                return net_str
        except ValueError:
            continue
    return None


class NftablesEngine:
    """Linux (nftables) accounting + hard-block engine.

    Threads are not used: the kernel counts, this class only reconciles rules
    and reads counters back on demand. ``is_blocked_cb`` is accepted for
    interface parity but unused (the maintenance loop drives enforcement via
    :meth:`update_state`).
    """

    def __init__(
        self,
        cfg: Any,
        snapshot_holder: Any,
        is_blocked_cb: Callable[[str], bool] | None = None,
        run_command: RunCommand | None = None,
    ) -> None:
        self.cfg = cfg
        self.holder = snapshot_holder
        self.is_blocked_cb = is_blocked_cb or (lambda ip: False)
        self._run_command = run_command or _default_run_command
        engine_cfg = getattr(cfg, "engine", None)
        self.table = getattr(engine_cfg, "table", "quota_gateway")
        self.name = "nftables"
        #: LAN subnets excluded from accounting + drops. LOCAL traffic never
        #: counts against the metered bundle, and a quota-blocked device keeps
        #: LAN access (see :func:`resolve_local_networks`).
        self._local_networks = resolve_local_networks(
            engine_cfg, getattr(cfg, "dhcp", None))

        dhcp_cfg = getattr(cfg, "dhcp", None)
        #: ARP gateway-lock (opt-in): deny internet to devices that bypass the
        #: box by using the ROUTER as their gateway (static-IP cheat). The lock
        #: needs the router IP + the client subnet to scope its ARP interception
        #: and its deny rule; without either it degrades to a no-op.
        self._arp_lock = bool(getattr(engine_cfg, "gateway_arp_lock", False))
        #: Deployment topology ("lan" | "wan"). In WAN mode the box dials PPPoE
        #: itself and there is no router on the client segment to lock against,
        #: so the ARP gateway-lock is forced off (config.yaml may still say true).
        self._topology = (getattr(engine_cfg, "topology", "") or "lan").strip().lower()
        if self._topology == "wan" and self._arp_lock:
            log.warning("ARP gateway-lock disabled in WAN mode (no router on the "
                        "client segment — the box terminates the WAN)")
            self._arp_lock = False
        self._router_ip = (getattr(dhcp_cfg, "router_ip", "")
                           if dhcp_cfg is not None else "")
        self._client_net = _which_network(self._local_networks,
                                          getattr(dhcp_cfg, "gateway_ip", "")
                                          if dhcp_cfg is not None else "")
        #: IPs most recently programmed into the `known_ips` set (locked devices
        #: must be IN it; everything else from the client subnet is dropped).
        self._last_known_ips: list[str] = []

        self.available = True
        self._warned = False
        self._ip_to_mac: dict[str, str] = {}
        self._blocked: dict[str, bool] = {}
        #: IPs most recently programmed into the kernel `blocked` set.
        self._last_blocked_ips: list[str] = []
        #: MACs most recently programmed into the kernel `blocked_macs` set
        #: (lease-less / static-IP devices are cut by ethernet address — see
        #: :meth:`_sync_blocked_macs`).
        self._last_blocked_macs: list[str] = []
        #: IPs whose counter rules exist in the kernel.
        self._installed: set[str] = set()
        #: last-seen kernel byte totals per IP (for delta computation).
        self._last: dict[str, EngineCounters] = {}
        #: Account the box's OWN internet (input/output hooks, q_gw_up/q_gw_down)
        #: against the protected "Gateway" user (engine.count_gateway, default on).
        self._count_gateway = bool(getattr(engine_cfg, "count_gateway", True))
        #: last-seen box-own byte totals (gateway delta baseline).
        self._last_gateway = EngineCounters()
        #: last-programmed ``gw_blocked`` membership (None = never programmed).
        self._gateway_blocked: bool | None = None
        #: last ``gw_allowed`` membership pushed to the kernel (None = never
        #: programmed). While "VPN share" relays the household through the box's
        #: tunnel, the VPN server endpoints live here — the ONE box-side egress
        #: that survives a Gateway cut (see :meth:`set_gateway_allowed`).
        self._gateway_allowed: tuple[str, ...] | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Ensure the table + base block hook exist. Idempotent, best-effort."""
        if not self.available:
            return
        # Missing binary / no root surfaces through the first _run() call
        # (code 127 / permission error), which flips self.available to False.
        # Rebuild the base from scratch: a restart must not inherit stale
        # device rules or a stale blocked set from a previous process.
        self._installed = set()
        self._last = {}
        self._last_blocked_ips = []
        self._last_blocked_macs = []
        self._run(["add", "table", f"{FAMILY} {self.table}"])
        self._run(["flush", "table", f"{FAMILY} {self.table}"])
        self._run(["add", "chain", f"{FAMILY} {self.table} forward",
                   "{ type filter hook forward priority 0; policy accept; }"])
        self._run(["add", "set", f"{FAMILY} {self.table} blocked",
                   "{ type ipv4_addr; }"])
        # MAC-keyed blocked set: drops devices whose IP is not in the lease
        # file (static-IP / lease-less quota-blocked devices). Matched by
        # ethernet address on both directions; LAN-excluded like the IP set.
        self._run(["add", "set", f"{FAMILY} {self.table} blocked_macs",
                   "{ type ether_addr; }"])
        # ARP gateway-lock (opt-in): the deny rule must be added FIRST in the
        # forward chain, before the blocked drops + counters, so an intercepted
        # bypasser's packets are dropped before any counter can see them.
        self._program_gateway_lock()
        # A blocked device is cut from the INTERNET. The drop rules carry the
        # same local-net exclusions as the counters, so its LAN traffic (printer,
        # NAS, router admin) still flows — the quota gates the internet, not the
        # LAN.
        saddr_drop = _match("ip saddr @blocked", "ip daddr",
                            self._local_networks) + " drop"
        daddr_drop = _match("ip daddr @blocked", "ip saddr",
                            self._local_networks) + " drop"
        self._run(["add", "rule", f"{FAMILY} {self.table} forward", saddr_drop])
        self._run(["add", "rule", f"{FAMILY} {self.table} forward", daddr_drop])
        # MAC-based drops: the same cut for devices the IP set cannot see
        # (lease-less / static-IP). LAN traffic stays excluded both ways, so a
        # MAC-cut device keeps its LAN access (printer, NAS) exactly like an
        # IP-cut one.
        mac_saddr_drop = _match("ether saddr @blocked_macs", "ip daddr",
                                self._local_networks) + " drop"
        mac_daddr_drop = _match("ether daddr @blocked_macs", "ip saddr",
                                self._local_networks) + " drop"
        self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                   mac_saddr_drop])
        self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                   mac_daddr_drop])
        # The box's own internet: input/output hooks with q_gw_up/q_gw_down
        # counters (count_gateway) + a gw_blocked set the admin toggles. Runs
        # BEFORE the counter reset below so a carried-over q_gw_* total is
        # captured as the baseline first (see _reseed_gateway_baseline).
        self._program_gateway()
        # `flush table` removes rules but NOT named counter objects. After a
        # restart the per-device counters keep their cumulative totals and the
        # in-memory delta baseline (_last) is gone, so the first drain would
        # count the whole carried-over total as NEW usage. Zero the surviving
        # counters so the fresh baseline is valid; if the kernel's nft is too
        # old for `reset counters` this is best-effort and update_state()
        # reseeds _last from the carried-over values instead (_add_counter).
        self._run_best_effort(["reset", "counters", "table", FAMILY, self.table])
        if self.available:
            log.info("nftables engine ready: table %s.%s (forward chain, "
                     "blocked set, per-device counters)", FAMILY, self.table)

    def stop(self) -> None:
        """Stop accepting work. Rules are left in place on purpose.

        The blocked set stays live after shutdown, so a device that was cut off
        stays cut off if the service dies — conservative for a 24/7 gateway.
        ``start()`` rebuilds the table on the next boot.
        """
        self.available = False

    def update_state(self, ip_to_mac: dict[str, str],
                     blocked: dict[str, bool]) -> None:
        """Reconcile per-device counters + the blocked set with the service."""
        self._ip_to_mac = dict(ip_to_mac)
        self._blocked = dict(blocked)
        if not self.available:
            return

        # Add counter rules for every new device IP (add-only, never remove).
        carried: list[str] = []
        for ip in sorted(set(ip_to_mac) - self._installed):
            if self._add_device(ip):
                carried.append(ip)
        if carried:
            self._reseed_baselines(carried)

        # Keep the known_ips set (ARP-lock deny allowlist) in step with the
        # leased client IPs. Done BEFORE the blocked-set early return so a new
        # lease is allowed even on ticks where the blocked set is unchanged.
        self._sync_known_ips(ip_to_mac)

        # Rebuild the blocked set from the current blocked MACs -> their IPs,
        # but only when the desired membership actually changed AND is not
        # empty. Re-flushing an identical set every ~15 s re-opens a small
        # unblock window for every blocked device on every tick (the chain's
        # policy is accept between the flush and the last re-add), and a
        # mid-rebuild nft failure leaves the later devices missing from the set
        # — both silent enforcement gaps. The empty case is cheap (one
        # subprocess, no devices affected) so it always runs to keep the kernel
        # authoritative.
        blocked_ips = sorted(
            ip for ip, mac in ip_to_mac.items() if blocked.get(mac))
        self._sync_blocked_ips(blocked_ips)
        # MAC-based drops are the SECOND enforcement channel, keyed by the
        # device's ethernet address instead of its lease: they cut devices
        # whose IP never entered ``ip_to_mac`` — a lease-less quota-blocked
        # device (DHCP never assigned / lease pruned) or a static-IP device
        # that routes through the box with no lease row. The IP set above
        # cannot see those; the ether set can (its traffic still crosses the
        # forward chain with its real saddr/daddr).
        self._sync_blocked_macs(blocked)

    def _sync_blocked_ips(self, blocked_ips: list[str]) -> None:
        """Program the kernel ``blocked`` set for leased, quota-blocked IPs."""
        if blocked_ips and blocked_ips == self._last_blocked_ips:
            return
        self._last_blocked_ips = None  # not yet committed; retry next tick if a step fails
        if self._run(["flush", "set", f"{FAMILY} {self.table} blocked"]):
            ok = True
            for ip in blocked_ips:
                if not self._run(["add", "element",
                                  f"{FAMILY} {self.table} blocked",
                                  f"{{ {ip} }}"]):
                    ok = False
                    break
            if ok:
                self._last_blocked_ips = blocked_ips

    def _sync_blocked_macs(self, blocked: dict[str, bool]) -> None:
        """Program the kernel ``blocked_macs`` set for every blocked device.

        The set is ``ether_addr``-typed; the forward chain's drop rules match
        ``ether saddr/daddr @blocked_macs`` (both directions, LAN-excluded).
        The box's own sentinel MAC is never programmed (its packets never
        cross the forward chain). Cache-gated exactly like the IP set: a
        same-set re-flush every tick would open a short unblock window.
        """
        blocked_macs = sorted(m for m, b in blocked.items()
                              if b and m != GATEWAY_MAC)
        if blocked_macs and blocked_macs == self._last_blocked_macs:
            return
        self._last_blocked_macs = None  # not yet committed; retry next tick
        if self._run(["flush", "set", f"{FAMILY} {self.table} blocked_macs"]):
            ok = True
            for mac in blocked_macs:
                if not self._run(["add", "element",
                                  f"{FAMILY} {self.table} blocked_macs",
                                  f"{{ {mac} }}"]):
                    ok = False
                    break
            if ok:
                self._last_blocked_macs = blocked_macs

    def _sync_known_ips(self, ip_to_mac: dict[str, str]) -> None:
        """Keep the kernel ``known_ips`` set == the currently leased client IPs.

        The ARP-lock deny rule drops any client-subnet source that is NOT in
        this set, so every managed DHCP device must be in it. The set is rebuilt
        only when its membership changes (mirror the blocked-set cache): a
        same-set re-flush every ~15 s would briefly deny every managed device
        between the flush and the last re-add.
        """
        if not self._arp_lock or not self._client_net:
            return
        known = sorted(ip_to_mac)
        if known and known == self._last_known_ips:
            return
        # A genuinely empty set is never programmed (see the guard above): the
        # kernel keeps its last non-empty membership, which errs toward ALLOWING
        # (never drops a device on stale data) and avoids a boot-time deny-all.
        self._last_known_ips = None  # not yet committed; retry next tick if a step fails
        if self._run(["flush", "set", f"{FAMILY} {self.table} known_ips"]):
            ok = True
            for ip in known:
                if not self._run(["add", "element",
                                  f"{FAMILY} {self.table} known_ips",
                                  f"{{ {ip} }}"]):
                    ok = False
                    break
            if ok:
                self._last_known_ips = known

    def _program_gateway_lock(self) -> None:
        """Install the ARP gateway-lock: capture + deny for static-IP bypassers.

        The router keeps WiFi + NAT and shares the client segment, so a device
        that sets its gateway to the ROUTER sends its frames straight to the
        router at Layer 2 — the box never sees a byte. Two rules close that:

        * an ``arp``-family chain drops the ROUTER's ARP *replies* to
          client-subnet hosts, so no client can learn the router's real MAC. The
          box's own ARP for the router is untouched (the router's reply to the
          box is addressed to the box's uplink IP, not the client subnet).
        * a ``known_ips`` set + forward deny: any forwarded packet from the
          client subnet whose source IP is not a leased DHCP address is dropped
          — that is an intercepted bypasser's traffic, blackholed. The
          continuous responder in quota/arp_lock.py makes the bypasser resolve
          the router's IP to the box's MAC, so its frames arrive here to be
          dropped. Managed DHCP clients are in ``known_ips`` and pass.

        Both are scoped to the client subnet + router IP; without either the
        lock degrades to a no-op (the scanner still reports the rogue).
        """
        if not self._arp_lock or not self._router_ip or not self._client_net:
            if self._arp_lock:
                log.warning("ARP gateway-lock requested but the router IP / "
                            "client subnet are unresolved — lock disabled")
            return
        self._run(["add", "set", f"{FAMILY} {self.table} known_ips",
                   "{ type ipv4_addr; }"])
        self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                   f"ip saddr {self._client_net} ip saddr != @known_ips drop"])
        self._run(["add", "table", f"arp {ARP_TABLE}"])
        self._run(["flush", "table", f"arp {ARP_TABLE}"])
        self._run(["add", "chain", f"arp {ARP_TABLE} input",
                   "{ type filter hook input priority 0; policy accept; }"])
        self._run(["add", "rule", f"arp {ARP_TABLE} input",
                   f"arp operation 2 arp saddr ip {self._router_ip} "
                   f"arp daddr ip {self._client_net} drop"])
        log.info("nftables: ARP gateway-lock active (router %s, client subnet "
                 "%s)", self._router_ip, self._client_net)

    def _program_gateway(self) -> None:
        """Program the box's OWN internet accounting + block (input/output hooks).

        The box's packets never cross the ``forward`` chain (they originate and
        terminate on the box itself), so its own bundle consumption would be
        invisible and unbounded. Two hooked chains cover it:

        * ``output`` (hook output) — the box's uploads; ``input`` (hook input) —
          the box's downloads. When ``engine.count_gateway`` is on, a counter
          rule in each chain (``q_gw_up`` / ``q_gw_down``) counts non-LOCAL
          traffic, drained by the maintenance loop into the protected "Gateway"
          user's device usage.
        * a ``gw_blocked`` interval set + two drop rules toggle the box's own
          internet cut (see :meth:`set_gateway_blocked`).
        * a ``gw_allowed`` set + two accept rules keep ONE box-side egress
          alive under that cut: the VPN server connection(s) the "VPN share"
          relay rides (see :meth:`set_gateway_allowed`). Loopback is exempt
          from the cut structurally, so the box's own local services (and the
          tun2socks<->VPN-client hop) keep working.

        Rule order in each chain is: exemptions FIRST, allowed accepts NEXT,
        drops NEXT, counters LAST. The DNS-exemption accepts (udp 53) keep
        dnsmasq's upstream queries flowing for CLIENTS while the box itself is
        cut, and the DHCP-exemption accepts (udp 67/68) keep NEW clients able
        to complete the lease handshake — because they run before any counter,
        this relayed service traffic is never charged to the "Gateway" user
        (household DNS is not the box's own usage). The ``gw_allowed`` accepts
        sit before the drops AND the counters, so relay traffic is neither cut
        nor double-charged; the ``gw_blocked`` drops come before the counters
        too: a dropped packet terminates the chain, so a blocked box's
        attempted bytes are never counted (they never leave the box, so they
        consume nothing from the bundle). Only non-local, non-exempted traffic
        that survives the block reaches the counters. LAN traffic is excluded
        from every rule (dashboard/SSH from the LAN stay reachable). The rules
        are programmed once — only the sets' memberships change.
        """
        for hook in ("input", "output"):
            self._run(["add", "chain", f"{FAMILY} {self.table} {hook}",
                       f"{{ type filter hook {hook} priority 0; policy accept; }}"])
        self._run(["add", "set", f"{FAMILY} {self.table} gw_blocked",
                   "{ type ipv4_addr; flags interval; }"])
        self._run(["add", "set", f"{FAMILY} {self.table} gw_allowed",
                   "{ type ipv4_addr; flags interval; }"])
        # DNS exemptions FIRST: dnsmasq keeps resolving for clients even while
        # the box itself is cut. Accepted before any counter, so relayed client
        # DNS (a household's ~30 MB/day) is never charged to the "Gateway" user.
        self._run(["add", "rule", f"{FAMILY} {self.table} output",
                   "udp dport 53 accept"])
        self._run(["add", "rule", f"{FAMILY} {self.table} input",
                   "udp sport 53 accept"])
        # DHCP exemptions: a NEW client's DISCOVER/REQUEST has no IP yet
        # (saddr 0.0.0.0, not a local subnet) and the OFFER/ACK reply goes to
        # the broadcast 255.255.255.255 — both would match the gw_blocked drop
        # rules below and leave the device stuck on "Obtaining IP address"
        # while the box's own internet is cut. Input accepts client requests
        # (sport 68 -> dport 67); output accepts the server's replies (sport 67).
        self._run(["add", "rule", f"{FAMILY} {self.table} output",
                   "udp sport 67 accept"])
        self._run(["add", "rule", f"{FAMILY} {self.table} input",
                   "udp sport 68 udp dport 67 accept"])
        # gw_allowed accepts NEXT: the box's connections to the VPN server(s)
        # (the "VPN share" relay) survive the cut below — and, being accepted
        # before any counter, are never charged to the Gateway user either.
        # Membership is empty by default; set_gateway_allowed fills it.
        self._run(["add", "rule", f"{FAMILY} {self.table} output",
                   "ip daddr @gw_allowed accept"])
        self._run(["add", "rule", f"{FAMILY} {self.table} input",
                   "ip saddr @gw_allowed accept"])
        # gw_blocked drops BEFORE the counters: a dropped packet terminates the
        # chain, so a blocked box's attempted bytes are never counted (they
        # never leave the box — nothing is consumed from the bundle). Loopback
        # is exempt so the box's own local services (dashboard, tun2socks ->
        # VPN client) keep working while its internet is cut.
        gw_exclusions = self._local_networks + ["127.0.0.0/8"]
        out_drop = _match("ip daddr @gw_blocked", "ip daddr",
                          gw_exclusions) + " drop"
        in_drop = _match("ip saddr @gw_blocked", "ip saddr",
                         gw_exclusions) + " drop"
        self._run(["add", "rule", f"{FAMILY} {self.table} output", out_drop])
        self._run(["add", "rule", f"{FAMILY} {self.table} input", in_drop])
        # Counters LAST: only non-local, non-exempted traffic that survives the
        # block reaches them.
        if self._count_gateway:
            out_count = _gateway_exclusions("ip daddr", self._local_networks)
            in_count = _gateway_exclusions("ip saddr", self._local_networks)
            carried_up = self._add_counter("q_gw_up")
            carried_down = self._add_counter("q_gw_down")
            if carried_up or carried_down:
                # A restart kept the named counters (and their cumulative
                # totals); seed the delta baseline so the old total is never
                # drained as new usage.
                self._reseed_gateway_baseline()
            self._run(["add", "rule", f"{FAMILY} {self.table} output",
                       f"{out_count} counter name q_gw_up".strip()])
            self._run(["add", "rule", f"{FAMILY} {self.table} input",
                       f"{in_count} counter name q_gw_down".strip()])
        log.info("nftables: gateway box accounting + block programmed "
                 "(count_gateway=%s)", self._count_gateway)

    def set_gateway_blocked(self, blocked: bool) -> None:
        """Toggle the box's OWN internet cut (``gw_blocked`` set membership).

        Cache-gated on ``_gateway_blocked`` (mirror the ``blocked`` set — see
        :meth:`update_state`): re-flushing an identical set every ~15 s would
        re-open a small free window each tick. An empty set drops nothing (box
        free); ``{ 0.0.0.0/0 }`` drops every non-LOCAL packet the box sends or
        receives. Not gated on ``count_gateway`` — the admin's block toggle
        must work even when counting is off.
        """
        if not self.available:
            return
        if blocked == self._gateway_blocked:
            return
        self._gateway_blocked = None  # not yet committed; retry next tick on failure
        if not self._run(["flush", "set", f"{FAMILY} {self.table} gw_blocked"]):
            return
        if blocked and not self._run(
                ["add", "element", f"{FAMILY} {self.table} gw_blocked",
                 "{ 0.0.0.0/0 }"]):
            return
        self._gateway_blocked = blocked

    @property
    def gateway_blocked(self) -> bool | None:
        """Last ``gw_blocked`` membership pushed to the kernel (None = never).

        Exposed so the maintenance loop can copy it into the snapshot and the
        dashboard can show whether the box's own internet is ACTUALLY cut at
        the kernel — not just what the UI toggle says.
        """
        return self._gateway_blocked

    def set_gateway_allowed(self, ips: list[str]) -> None:
        """Program the ``gw_allowed`` set — the box egress that survives a cut.

        The box's OWN internet can be cut (``gw_blocked`` = ``0.0.0.0/0``)
        while "VPN share" relays the household through the box's tunnel. The
        relay rides the box's connection(s) to the VPN server(s) (clients ->
        tun -> VPN client on the box -> server out of the box), so those
        endpoints must stay reachable or the tunnel dies — the ONLY box-side
        egress that keeps working under the cut (DNS/DHCP and loopback are
        exempt structurally). Membership is fed by the maintenance loop from
        the VPN-client process's established sockets, plus any explicit
        ``engine.gateway_allow_ips``; empty clears the set.

        The accept rules sit in the input/output chains ABOVE the
        ``gw_blocked`` drops and the q_gw counters (programmed once in
        ``_program_gateway``), so allowed relay traffic is never dropped and
        never double-charged to the protected Gateway user — while everything
        else the box sends stays cut. Cache-gated like ``set_gateway_blocked``
        (mirror the ``blocked`` set — see :meth:`update_state`): re-flushing
        an identical set every ~15 s would re-open a small free window each
        tick. A failure leaves the desired state uncommitted so the
        maintenance tick retries.
        """
        if not self.available:
            return
        key = tuple(sorted({i for i in ips if i}))
        if key == self._gateway_allowed:
            return
        self._gateway_allowed = None  # not yet committed; retry next tick on failure
        if not self._run(["flush", "set", f"{FAMILY} {self.table} gw_allowed"]):
            return
        if key and not self._run(
                ["add", "element", f"{FAMILY} {self.table} gw_allowed",
                 "{ " + ", ".join(key) + " }"]):
            return
        self._gateway_allowed = key
        log.info("nftables: gw_allowed = %s", list(key))

    @property
    def gateway_allowed(self) -> tuple[str, ...] | None:
        """Last ``gw_allowed`` membership pushed to the kernel (None = never)."""
        return self._gateway_allowed

    def flush(self) -> EngineSnapshot:
        """Return byte deltas since the last flush, as an EngineSnapshot."""
        if not self.available:
            return EngineSnapshot(ts=time.time())
        code, out = self._run_command(["nft", "-j", "list", "counters"])
        if code != 0:
            self._fail(f"nft -j list counters failed: {out.strip()}")
            return EngineSnapshot(ts=time.time())
        try:
            raw = self._parse_counters(out)
        except ValueError as exc:
            self._fail(f"could not parse nft counter output: {exc}")
            return EngineSnapshot(ts=time.time())

        now = time.time()
        by_ip: dict[str, EngineCounters] = {}
        for ip in self._ip_to_mac:
            prev = self._last.get(ip, EngineCounters())
            up = raw.get(_counter_name(ip, "up"), 0)
            down = raw.get(_counter_name(ip, "down"), 0)
            cur = EngineCounters(
                up=max(0, up - prev.up),
                down=max(0, down - prev.down),
            )
            if cur.up or cur.down:
                by_ip[ip] = cur
            self._last[ip] = EngineCounters(up=up, down=down)

        # The box's OWN internet (input/output hooks, q_gw_* counters): delta
        # since the last flush, drained into the Gateway user's device usage by
        # the maintenance loop.
        gw_up = raw.get("q_gw_up", 0)
        gw_down = raw.get("q_gw_down", 0)
        gateway = EngineCounters(
            up=max(0, gw_up - self._last_gateway.up),
            down=max(0, gw_down - self._last_gateway.down),
        )
        self._last_gateway = EngineCounters(up=gw_up, down=gw_down)

        return EngineSnapshot(
            by_ip=by_ip,
            ip_to_mac=dict(self._ip_to_mac),
            blocked=dict(self._blocked),
            gateway=gateway,
            ts=now,
        )

    # -- internals ------------------------------------------------------------

    def _run(self, args: list[str]) -> bool:
        """Run ``nft <args>``; True if it succeeded (or already existed)."""
        if not self.available:
            return False
        code, out = self._run_command(["nft", *args])
        if code == 0:
            return True
        # nft is not idempotent: re-adding an existing table/chain/set/rule
        # errors "File exists" — that is the success case we tolerate.
        if args[0] == "add" and any(s in out for s in ("File exists",
                                                       "already exists")):
            return True
        self._fail(f"nft {args[0]} failed: {out.strip()}", command=args)
        return False

    def _run_best_effort(self, args: list[str]) -> None:
        """Run ``nft <args>``; a failure only logs, never disables the engine."""
        if not self.available:
            return
        code, out = self._run_command(["nft", *args])
        if code != 0:
            log.warning("nft %s failed (best-effort, ignoring): %s",
                        args[0], out.strip())

    def _add_counter(self, name: str) -> bool:
        """Create a named counter; report whether it already existed.

        ``flush table`` deletes rules but keeps named counter objects and their
        byte totals, so a counter re-created after a restart can carry over a
        cumulative total from the previous process. Returns True in that case so
        :meth:`_add_device` can seed ``_last`` from it instead of draining the
        old total as new usage. Returns False for a fresh create (or a failed
        command — the engine marks itself unavailable, matching ``_run``).
        """
        if not self.available:
            return False
        code, out = self._run_command(["nft", "add", "counter",
                                       f"{FAMILY} {self.table}", name])
        if code == 0:
            return False
        if any(s in out for s in ("File exists", "already exists")):
            return True
        self._fail(f"nft add counter failed: {out.strip()}",
                   command=["add", "counter", f"{FAMILY} {self.table}", name])
        return False

    def _add_device(self, ip: str) -> bool:
        """Install a device's counter pair + counting rules.

        Returns True when a counter object already existed — i.e. it carried its
        cumulative total over a restart — so the caller can re-seed the delta
        baseline from it (see :meth:`_reseed_baselines`). A freshly created
        counter starts at zero and needs no baseline.
        """
        up_name = _counter_name(ip, "up")
        down_name = _counter_name(ip, "down")
        carried_up = self._add_counter(up_name)
        carried_down = self._add_counter(down_name)
        carried = carried_up or carried_down
        if not self.available:
            return False
        # Count only INTERNET-bound traffic: a packet to/from a local LAN
        # network must not match (and so must not consume the metered bundle).
        up_match = _match(f"ip saddr {ip}", "ip daddr", self._local_networks)
        down_match = _match(f"ip daddr {ip}", "ip saddr", self._local_networks)
        ok = self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                        f"{up_match} counter name {up_name}"])
        ok &= self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                         f"{down_match} counter name {down_name}"])
        if not ok:
            return False
        self._installed.add(ip)
        log.info("nftables: watching device %s", ip)
        return carried

    def _reseed_baselines(self, ips: list[str]) -> None:
        """Point ``_last`` at carried-over counters so their totals never count
        as new usage (see :meth:`_add_counter`).

        Runs once per restart, right after the first ``update_state`` re-adds
        the per-device rules. Reads the counters back and stores their current
        values as the baseline, so the next ``flush()`` reports only bytes
        accumulated since this process started.
        """
        code, out = self._run_command(["nft", "-j", "list", "counters"])
        if code != 0:
            self._fail(f"nft -j list counters failed: {out.strip()}")
            return
        try:
            raw = self._parse_counters(out)
        except ValueError as exc:
            self._fail(f"could not parse nft counter output: {exc}")
            return
        for ip in ips:
            self._last[ip] = EngineCounters(
                up=raw.get(_counter_name(ip, "up"), 0),
                down=raw.get(_counter_name(ip, "down"), 0),
            )
        log.info("nftables: reseeded %d carried-over counter baseline(s) "
                 "after restart", len(ips))

    def _reseed_gateway_baseline(self) -> None:
        """Point ``_last_gateway`` at a carried-over ``q_gw_*`` restart total.

        Mirrors :meth:`_reseed_baselines` for the box's own counters: after a
        restart ``flush table`` keeps the named ``q_gw_up``/``q_gw_down``
        counters and their cumulative totals, so without a baseline the first
        drain would charge the whole carried-over total to the Gateway user as
        NEW usage.
        """
        code, out = self._run_command(["nft", "-j", "list", "counters"])
        if code != 0:
            self._fail(f"nft -j list counters failed: {out.strip()}")
            return
        try:
            raw = self._parse_counters(out)
        except ValueError as exc:
            self._fail(f"could not parse nft counter output: {exc}")
            return
        self._last_gateway = EngineCounters(
            up=raw.get("q_gw_up", 0),
            down=raw.get("q_gw_down", 0),
        )
        log.info("nftables: reseeded carried-over gateway counter baseline(s) "
                 "after restart")

    def _parse_counters(self, json_text: str) -> dict[str, int]:
        """Flatten ``nft -j list counters`` into {counter_name: bytes}."""
        data = json.loads(json_text)
        out: dict[str, int] = {}
        for entry in data.get("nftables", []):
            counter = entry.get("counter")
            if not counter:
                continue
            if counter.get("table") != self.table:
                continue
            name = counter.get("name", "")
            if not name.startswith("q_"):
                continue
            try:
                out[name] = int(counter.get("bytes") or 0)
            except (TypeError, ValueError):
                raise ValueError(f"bad bytes value in counter {name!r}")
        return out

    def _fail(self, reason: str, command: list[str] | None = None) -> None:
        self.available = False
        if not self._warned:
            argv = f"nft {' '.join(command)}" if command else "nft <unknown>"
            log.error("nftables engine unavailable: %s — no per-packet "
                      "accounting on this host (the dashboard still shows "
                      "DB usage). Run as root and check `nft --version`.",
                      f"{reason} [{argv}]")
            self._warned = True
