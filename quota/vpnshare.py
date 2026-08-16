""""VPN share" — route the whole client subnet through the box's VPN.

The product's gateway keeps DHCP + DNS on the box and counts/blocks every
client byte in the kernel (nftables forward chain, see quota/nftables.py).
"VPN share" adds ONE more policy-routing layer on top: when enabled, every
managed client's INTERNET traffic is sent into the box's VPN tunnel
(interface W), so the whole household shares the VPN connection.

How it works
------------
The VPN client runs in TUN mode on the box itself (sing-box/xray `tun`
inbound, WireGuard, or tun2socks bridging any local SOCKS/HTTP proxy —
the same stack v2rayN-style clients use). The manager never touches the
VPN client; it only adds/removes a policy-routing shortcut:

* ``ip rule add from <client_subnet> lookup <table> pref <pref>`` — client
  packets are diverted BEFORE the main table is consulted.
* in that table: ``default dev <tun>`` (+ ``via`` when the tunnel has a peer
  address, e.g. OpenVPN), plus direct routes for the LOCAL subnets (client +
  uplink) dev <lan_if> so LAN traffic (router admin, NAS, the router as DNS)
  NEVER enters the tunnel, mirroring the nftables ``resolve_local_networks``
  local-net exclusions.

Why the kernel accounting/blocking still works: the forward-chain counters
and the ``blocked`` set run on the client subnet BEFORE the VPN device is
reached (client -> PREROUTING -> routing -> FORWARD -> tunnel), and speed
shaping (tc) matches the same pre/post-NAT client IPs — so quota blocks,
counting, and per-device speed caps all keep applying to traffic that exits
through the VPN. The box's own input/output metering keeps working normally
while relaying: relay traffic is never double-charged to the "Gateway" user,
and if the Gateway user's internet is cut (gw_blocked), ONLY the VPN-server
endpoint(s) stay reachable via the engine's gw_allowed whitelist — so the
household's tunnel survives a cut box (see NftablesEngine.set_gateway_allowed).

Honest limits
-------------
* DNS queries keep going through dnsmasq's normal upstream (usually the
  router/ISP resolver) — this feature tunnels DATA, not the box's DNS path.
* A client using DNS-over-HTTPS/TLS straight to a resolver bypasses this,
  exactly as today; it does not change the nftables/tc layers at all.
* The tunnel is auto-detected by kernel type — no app can conjure a VPN;
  the VPN client must be running and UP on the box first.

Graceful degradation: no `ip` binary / not root => the manager reports an
`error` state and the dashboard keeps working; if the tunnel interface
vanishes while enabled, the policy rule is removed so clients fall back to
the direct uplink (they must never be blackholed by a dead VPN).

The command runner is injected (``run_command``) so tests drive a fake
``ip`` binary and assert the exact routing programmed.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("quota.vpnshare")

#: ARPHRD_NONE — the kernel link type of TUN devices (sing-box/xray utun,
#: OpenVPN tun, WireGuard wg). TAP is ARPHRD_ETHER (1); ppp is 512; these
#: are deliberately NOT candidates.
ARPHRD_NONE = 65534

RunCommand = Callable[[list[str]], tuple[int, str]]

STATE_OFF = "off"
STATE_ON = "on"
STATE_NO_INTERFACE = "no-interface"
STATE_ERROR = "error"


@dataclass
class VpnShareStatus:
    """What the dashboard shows for the VPN-share switch."""

    state: str = STATE_OFF   # one of STATE_*
    interface: str = ""      # the tunnel interface traffic is routed into
    peer: str = ""           # tunnel peer IP when the tunnel carries one
    candidates: list[str] = field(default_factory=list)
    message: str = ""


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def _derive_subnet(host: str, netmask: str) -> str:
    mask = str(netmask or "").split("/")[-1].strip()
    try:
        return str(ipaddress.ip_network(f"{host}/{mask}", strict=False))
    except ValueError:
        return ""


class VpnShareManager:
    """Programs/removes the client-subnet policy routing into the VPN.

    Pure syscalls/subprocesses, no threads, no sockets — the maintenance
    loop calls :meth:`reconcile` from a worker thread. Everything is
    idempotent and reconciles toward the DESIRED state each call, so a
    crash, a reboot, or a VPN client restart self-heals on the next tick.
    """

    def __init__(self, cfg: object, run_command: RunCommand | None = None,
                 sysfs_root: str | Path = "/sys/class/net") -> None:
        self._run_command = run_command or _default_run_command
        self.sysfs_root = Path(sysfs_root)
        vs = getattr(cfg, "vpn_share", None)
        engine = getattr(cfg, "engine", None)
        dhcp = getattr(cfg, "dhcp", None)
        self.table = int(getattr(vs, "route_table", 200) or 200)
        self.rule_pref = int(getattr(vs, "rule_pref", 1000) or 1000)
        self._cfg_iface = (getattr(vs, "interface", "") or "").strip()
        gw_ip = (getattr(dhcp, "gateway_ip", "") if dhcp is not None else "") or ""
        mask = (getattr(dhcp, "subnet", "") if dhcp is not None else "") or ""
        self.client_subnet = ((getattr(engine, "client_subnet", "") or "").strip()
                              or _derive_subnet(gw_ip, mask))
        self.uplink_subnet = ((getattr(engine, "uplink_subnet", "") or "").strip()
                              or _derive_subnet(
                                  (getattr(dhcp, "router_ip", "")
                                   if dhcp is not None else ""), mask))
        #: LAN-side interface candidates for the LOCAL routes in the VPN
        #: table (never route LAN traffic into the tunnel). Deliberately NOT
        #: ``vpn_share.interface`` — that key is the TUN pin, never a LAN NIC.
        self._iface_override = ((getattr(getattr(cfg, "shaping", None),
                                         "interface", "") or "").strip()
                                or (getattr(dhcp, "interface", "")
                                    if dhcp is not None else ""))
        #: cached tunnel peer per interface (`ip -o -4 addr show` parse)
        self._peers: dict[str, str] = {}
        #: last tunnel interface actually programmed into the kernel
        #: (None = nothing routed into any tunnel).
        self._iface: str | None = None
        #: whether the kernel currently holds our policy rule
        self._applied = False
        #: whether a leftover-rule probe ran (self-heal scan at boot when
        #: the DB says off; only one `ip rule show` per transition).
        self._clean_checked = False

    # ------------------------------------------------------------- queries

    def detect_interfaces(self) -> list[str]:
        """TUN-ish interfaces on the box, best first.

        A candidate is an interface whose kernel link type is ARPHRD_NONE
        (65534 — tun/utun/wireguard). Order: named tun*/utun*/wg* first,
        then the rest; an interface carrying an IPv4 address beats a bare
        one. ``ip`` is never consulted here (sysfs only), so detection also
        runs chrooted/root-free.
        """
        root = self.sysfs_root
        if not root.is_dir():
            return []
        candidates: list[str] = []
        for dev in root.iterdir():
            if not dev.is_dir():
                continue
            try:
                link_type = (dev / "type").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if link_type == str(ARPHRD_NONE):
                candidates.append(dev.name)
        if not candidates:
            return []

        def rank(name: str) -> tuple[int, str]:
            p = 1 if re.search(r"(tun|utun|wg|vpn)\d*$", name) else 2
            return (0 if self._has_ipv4(name) else 1, p,
                    "0" if re.match(r"^wg", name) else (
                        "1" if re.match(r"^tun", name) else (
                            "2" if re.match(r"^xray", name) else "3")), name)

        return sorted(candidates, key=rank)

    def _iface_exists(self, iface: str) -> bool:
        """Does the tunnel device exist right now? Sysfs check (no
        subprocess). A pinned tunnel that went away must never be routed
        into — the VPN client may have restarted as a new tun index."""
        return (self.sysfs_root / iface).is_dir()

    def _ensure_link_up(self, iface: str) -> bool:
        """Best-effort: bring the tunnel device's link UP so the kernel
        accepts a route through it.

        A freshly spawned tun2socks device can lag behind the process; this
        is the one deterministic point where the link state matters, so the
        routing manager makes sure it is up right before programming the
        default route (never raises, never fatal)."""
        try:
            code, out = self._run_command(
                ["ip", "link", "set", "dev", iface, "up"])
        except Exception:  # noqa: BLE001
            code = 1
            out = ""
        if code != 0:
            log.info("vpn share: could not bring %s up: %s",
                     iface, (out or "").strip())
        return code == 0

    def _link_state(self, iface: str) -> str:
        """A compact ``ip``-style description of the interface so a routing
        failure is diagnosable (does it exist? is the link UP? does it carry
        an address?) instead of a bare "Device for nexthop is not up"."""
        code, out = self._run_command(
            ["ip", "-o", "link", "show", "dev", iface])
        if code == 0 and (out or "").strip():
            return (out or "").strip().splitlines()[0]
        code, out = self._run_command(
            ["ip", "-o", "-4", "addr", "show", "dev", iface])
        if code == 0 and (out or "").strip():
            return (out or "").strip().splitlines()[0]
        return "interface missing or ip(8) failed"

    def _has_ipv4(self, iface: str) -> bool:
        code, out = self._run_command(["ip", "-o", "-4", "addr", "show",
                                       "dev", iface])
        return code == 0 and re.search(r"inet \d", out or "")

    def peer_ip(self, iface: str) -> str:
        """The tunnel's point-to-point peer address, if it has one.

        OpenVPN-style tunnels carry ``inet <local> peer <peer>/32``;
        sing-box/xray TUN and plain WireGuard addresses usually don't. The
        result is cached per interface (``ip`` runs once per transition).
        """
        if iface in self._peers:
            return self._peers[iface]
        peer = ""
        code, out = self._run_command(["ip", "-o", "-4", "addr", "show",
                                       "dev", iface])
        if code == 0:
            # ip -o -4 addr show: `inet <local> peer <peer>/32 scope ...`
            m = re.search(r"inet \S+ peer (\d+\.\d+\.\d+\.\d+)/", out or "")
            if m:
                peer = m.group(1)
        self._peers[iface] = peer
        return peer

    def lan_interface(self) -> str:
        """The interface holding the client gateway IP (LAN routes in the
        VPN table need a real device). Falls back to shaping/dhcp interface
        config; empty => the LOCAL routes are skipped (the main table still
        answers them)."""
        if self._iface_override:
            return self._iface_override
        code, out = self._run_command(["ip", "-o", "-4", "addr", "show"])
        if code == 0 and self.client_subnet:
            for line in (out or "").splitlines():
                # 2: eth0    inet 192.168.2.1/24 brd ... scope global eth0
                m = re.match(r"^\d+:\s+([^\s:]+).*?inet (\d+\.\d+\.\d+\.\d+)/", line)
                if m:
                    name, ip = m.group(1), m.group(2)
                    try:
                        if ipaddress.ip_address(ip) in ipaddress.ip_network(
                                self.client_subnet, strict=False):
                            return name
                    except ValueError:
                        continue
        return ""

    # -------------------------------------------------------------- actions

    def is_rule_installed(self) -> bool:
        """Is our client-subnet policy rule present in the kernel?

        One ``ip rule show`` subprocess per call; used at boot (leftover
        self-heal) and after removals. The rule string is matched loosely
        (``from <client> lookup <table>``) so formatting differences
        between iproute2 versions cannot hide it.
        """
        code, out = self._run_command(["ip", "rule", "show"])
        needle = f"from {self.client_subnet} lookup {self.table}"
        return code == 0 and needle in (out or "")

    def apply(self, iface: str = "") -> VpnShareStatus:
        """Program (idempotently) the client-subnet policy routing into the
        tunnel ``iface``. Returns the resulting status; a failure leaves
        ``self._applied`` False so the next tick retries. A missing tunnel
        device is NEVER routed into (that would blackhole the subnet), so
        the rule is only added after the interface's existence is confirmed
        via sysfs."""
        iface = iface or self._cfg_iface
        if not iface:
            return VpnShareStatus(STATE_NO_INTERFACE, message=(
                "no VPN tunnel interface found — start the VPN client "
                "(TUN mode: sing-box / xray / WireGuard / tun2socks)"))
        if iface and not self._iface_exists(iface):
            return VpnShareStatus(STATE_NO_INTERFACE, interface=iface,
                                  message=(f"tunnel {iface} is not present — "
                                           "is the VPN client running?"))
        # A tunnel with no IPv4 is a dead/junk device (the live-box "evice"):
        # routing into it would blackhole the whole subnet. A freshly spawned
        # tun2socks can lag behind the process, so wait within a short settle
        # window for the address to land; a device that never gains one is
        # reported as no-interface, never routed into.
        if iface:
            deadline = time.monotonic() + 2.0
            while not self._has_ipv4(iface) and time.monotonic() < deadline:
                time.sleep(0.5)
            if not self._has_ipv4(iface):
                return VpnShareStatus(
                    STATE_NO_INTERFACE, interface=iface,
                    message=(f"tunnel {iface} carries no IPv4 address — "
                             "is the VPN client running?"))
        st = VpnShareStatus(STATE_ON, interface=iface, peer=self.peer_ip(iface))

        def ok(argv: list[str], tolerate: tuple[str, ...] = ()) -> bool:
            code, out = self._run_command(argv)
            if code == 0:
                st.state = STATE_ON
                st.message = ""
                return True
            out_l = (out or "").lower()
            if any(t in out_l for t in tolerate):
                return True
            st.state = STATE_ERROR
            st.message = f"{argv[0]} {' '.join(argv[1:])}: {out.strip()}"
            log.error("vpn share: %s => %s", argv, out.strip())
            return False

        def best_effort(argv: list[str]) -> None:
            """A non-critical step (the LAN direct routes) — the main table
            still answers those destinations, so a failure only warns."""
            code, out = self._run_command(argv)
            if code != 0:
                log.warning("vpn share: %s => %s", argv, (out or "").strip())

        # 1. Divert client-subnet sources BEFORE the main table. Re-adding an
        #    identical rule errors "File exists" — that IS the idempotent case.
        if not ok(["ip", "rule", "add", "from", self.client_subnet,
                   "lookup", str(self.table), "pref", str(self.rule_pref)],
                  tolerate=("file exists", "already exists")):
            return st
        # 2. LOCAL routes in the VPN table: client + uplink subnets never
        #    enter the tunnel (mirror the nftables local-net exclusions).
        lan = self.lan_interface()
        if lan:
            best_effort(["ip", "route", "replace", "table", str(self.table),
                         self.client_subnet, "dev", lan])
            if self.uplink_subnet and self.uplink_subnet != self.client_subnet:
                best_effort(["ip", "route", "replace", "table", str(self.table),
                             self.uplink_subnet, "dev", lan])
        # 3. The default route into the tunnel: via its peer when it carries
        #    one, else dev-only (plain `scope link` fallback for devices that
        #    reject a bare dev route). A tunnel must be UP for the kernel to
        #    accept a route through it, so ensure the link is up first and
        #    retry — a freshly spawned tun2socks device can lag behind the
        #    process. On final failure, report the link's REAL state (the
        #    kernel's bare "Device for nexthop is not up" hides whether the
        #    device is missing, down, or unaddressed).
        if st.peer:
            route = ["ip", "route", "replace", "table", str(self.table),
                     "default", "via", st.peer, "dev", iface]
        else:
            route = ["ip", "route", "replace", "table", str(self.table),
                     "default", "dev", iface]
        for _ in range(3):
            self._ensure_link_up(iface)
            if ok(route):
                break
            if not st.peer and ok(route + ["scope", "link"]):
                break
            time.sleep(0.5)
        else:
            st.message = f"{' '.join(route)}: {self._link_state(iface)}"
            log.error("vpn share: default route into %s failed; "
                      "link state: %s", iface, self._link_state(iface))
        if st.state != STATE_ON:
            return st
        self._iface = iface
        self._applied = True
        self._clean_checked = True
        log.info("vpn share: client subnet %s routed through %s "
                 "(table %d)",
                 self.client_subnet, iface, self.table)
        return st

    def remove(self) -> None:
        """Remove the policy rule + empty the VPN table. Idempotent;
        tolerates a rule/route that is already gone."""
        code, out = self._run_command(
            ["ip", "rule", "del", "from", self.client_subnet,
             "lookup", str(self.table), "pref", str(self.rule_pref)])
        if code != 0:
            log.info("vpn share: rule del: %s", (out or "").strip())
        code, out = self._run_command(
            ["ip", "route", "flush", "table", str(self.table)])
        if code != 0 and "nothing" not in (out or "").lower():
            log.warning("vpn share: route flush: %s", (out or "").strip())
        self._applied = False
        self._iface = None
        log.info("vpn share: policy routing removed")

    # --------------------------------------------------------------- sync

    def reconcile(self, enabled: bool, interface_pin: str = "") -> VpnShareStatus:
        """Make the kernel match the DESIRED state (dashboard switch).

        * enabled + rule missing  -> apply (idempotent, self-heals a reboot
          or a VPN-client restart re-creating the tunnel).
        * enabled + tunnel GONE   -> remove the rule, report no-interface
          (a dead VPN must never blackhole the household: without the rule
          the clients ride the direct uplink).
        * disabled + rule present -> remove it (including leftovers from a
          crashed previous run, probed once on the first disabled call).
        """
        if enabled:
            iface = self._cfg_iface or interface_pin
            # A pinned tunnel that vanished is treated like no pin at all:
            # re-detect (the VPN client may have restarted as a new tun
            # index) rather than route into a dead device. Same for a pinned
            # device that still EXISTS but carries no IPv4 — a stale/junk
            # ARPHRD_NONE device (the live-box "evice") is present in sysfs
            # yet routes nothing; honoring it would blackhole the subnet.
            if iface and (not self._iface_exists(iface)
                          or not self._has_ipv4(iface)):
                log.warning("vpn share: pinned tunnel %s is gone or carries "
                            "no IPv4 — re-detecting", iface)
                iface = ""
            if not iface:
                cands = self.detect_interfaces()
                iface = cands[0] if cands else ""
            if not iface:
                if self._applied:
                    self.remove()
                return VpnShareStatus(STATE_NO_INTERFACE,
                                      candidates=self.detect_interfaces(),
                                      message=("no VPN tunnel interface "
                                               "found — start the VPN "
                                               "client (TUN mode)"))
            return self.apply(iface)
        if self._applied:
            self.remove()
            return VpnShareStatus(STATE_OFF)
        if not self._clean_checked and self.is_rule_installed():
            self.remove()
            self._clean_checked = True
            return VpnShareStatus(STATE_OFF)
        self._clean_checked = True
        return VpnShareStatus(STATE_OFF)