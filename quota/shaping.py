"""Kernel-side speed shaping (``tc``) for the Linux gateway.

Mirrors the interface of the nftables engine: the caller (run.py maintenance
loop) pushes the desired state every ~15 s and this class reconciles the
kernel's traffic-control rules — no Python in the packet path.

Topology: ONE NIC carries the uplink (e.g. 192.168.1.110) and the client-
subnet alias (192.168.2.1); clients on 192.168.2.0/24 are masqueraded out the
uplink. The NAT changes which address is visible at each shaping point, so the
two directions use two different HTB trees:

* **Upload** (client -> internet): at NIC *ingress* the source is still the
  client IP (pre-NAT), so we redirect client-subnet ingress into an ``ifb``
  device (``mirred egress redirect``) and shape there by ``ip src``.
* **Download** (internet -> client): at NIC *egress* conntrack has already
  un-NAT'd the destination back to the client IP, so we shape directly on
  egress by ``ip dst`` (no second ifb needed).

Both trees are HTB (hierarchical token bucket) with **fq_codel on every leaf**.
The class hierarchy enforces:
* the **total-link cap** — the root caps at the LAN link rate
  (``shaping.lan_rate_mbps``) while every WAN class is capped at the configured
  line speed, so the internet queue forms at the tc layer (where fq_codel can
  drain it fairly) instead of in the modem's buffer ("bufferbloat": one heavy
  uploader/downloader no longer inflates everyone's ping);
* the **per-user aggregate** — a user's device leaves sit under their user
  class, which is capped at the user's configured total;
* the **per-device cap** — each device leaf ``rate = ceil = eff`` (hard cap).

LAN traffic gets a **pass-through**: client<->uplink-subnet traffic (NAS,
router admin, LAN transfers) AND client<->the box itself (dashboard, SSH,
file shares like RustDisk) are not internet — a prio-1 ``1:99`` class under
the root carries them at the full LAN link rate so LAN transfers never pay the
WAN cap. The uplink subnet resolves exactly like the nftables engine
(``engine.uplink_subnet`` wins, else derived from the dhcp block — the LAN
snapshot, else the box's own NIC addresses as a last resort); the box's own
addresses on the shaping NIC are always added (the kernel's address table, no
config keys). The root's headroom matters only for that class — internet
classes stay capped at the line rate.

Devices with no cap on either axis go to the default class (still capped at the
direction total + fq_codel, so untracked devices cannot flood the line).

Class ids are deterministic (recomputed each reconcile); device trees live on
separate qdiscs (``$IF`` / ``ifb0``) so ids may repeat across directions:
root qdisc ``handle 1: htb default 2``; root class ``1:1`` (rate = LAN link
rate); default ``1:2``; LAN pass-through ``1:99``; download aggregate ``1:100``;
user classes ``1:<0x300+uid>``; device leaves ``1:<0x8000+devid>``.

The tree is rebuilt only when a **signature** of (enabled, totals, aqm, sorted
device entries) changes — same idempotent-reconcile pattern as the
``_last_blocked_ips`` cache in :mod:`quota.nftables`. ``start()`` always tears
down + rebuilds; ``stop()`` leaves rules in place (conservative, like nftables
— limits keep applying if the service dies; a reboot clears all qdiscs).

Nftables accounting is unaffected: the forward hook runs once, after the ifb
re-injection, with pre-NAT src / post-NAT dst intact, and blocked devices are
dropped in ``forward`` before they ever reach a shaper qdisc.

Graceful degradation mirrors :class:`NftablesEngine`: missing ``tc``/``ip``/
root, ``modprobe ifb`` failure, or an unresolvable interface ⇒ ``available``
becomes False, logged once, dashboard unaffected.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Callable

from quota.nftables import resolve_local_networks

log = logging.getLogger("quota.shaping")

#: argv -> (returncode, output). Tests inject a fake; the default shells out
#: to the real binaries. Same contract as quota/nftables._default_run_command.
RunCommand = Callable[[list[str]], tuple[int, str]]


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def _user_class(uid: int) -> str:
    """HTB classid for a user's aggregate class (uid -> 1:0x301, 1:0x302 …)."""
    return f"1:0x{0x300 + int(uid):x}"


def _device_class(devid: int) -> str:
    """HTB classid for a device leaf (devid -> 1:0x8001, 1:0x8002 …)."""
    return f"1:0x{0x8000 + int(devid):x}"


def _device_qdisc(devid: int) -> str:
    """fq_codel qdisc handle for a device leaf (matches its class minor)."""
    return f"0x{0x8000 + int(devid):x}:"


def _rate(mbps: float) -> str:
    """tc rate string (e.g. 12.5 -> '12.5mbit', 100.0 -> '100mbit')."""
    return f"{mbps:g}mbit"


def _burst(mbps: float) -> list[str]:
    """tc ``burst``/``cburst`` args for an HTB class at ``mbps``.

    HTB's default token bucket is ~1 second of traffic at the class rate
    (``buffer = rate.rate`` when unset), so a class can transmit at full line
    speed for up to a second before settling at ``rate`` — a short speed test
    then reads ~1.5x the configured cap ("2 Mbps cap shows ~3 Mbps"). The
    bucket only needs to hold ``rate/HZ`` to sustain the rate; 50 ms of
    traffic (``rate/20``) keeps a 2 s test within a few percent of the cap
    while leaving a wide margin over the scheduler tick (HZ is 250 on modern
    kernels, rarely as low as 100) so the class is never starved.
    """
    burst = max(1500, round(mbps * 1_000_000 / 8 / 20))
    return ["burst", str(burst), "cburst", str(burst)]


def _effective(dev_cap: float, user_cap: float, total: float) -> float | None:
    """Effective per-device cap: min(device cap, user cap), clamped to the
    direction total. ``0`` means unlimited; None => no cap -> default class."""
    caps = [c for c in (dev_cap, user_cap) if c and c > 0]
    if not caps:
        return None
    return max(0.0, min(min(caps), total))


def _find_interface_for(gateway_ip: str, run_command: RunCommand) -> str:
    """Find the interface whose subnet contains ``gateway_ip`` (the client
    alias) by parsing ``ip -o -4 addr show``. Returns '' when not found."""
    if not gateway_ip:
        return ""
    code, out = run_command(["ip", "-o", "-4", "addr", "show"])
    if code != 0:
        return ""
    try:
        target = ipaddress.ip_address(gateway_ip)
    except ValueError:
        return ""
    for line in out.splitlines():
        parts = line.split()
        # e.g. "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 …"
        if len(parts) < 4 or parts[2] != "inet":
            continue
        addr, _, prefix = parts[3].partition("/")
        if not prefix:
            continue
        try:
            net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
        except ValueError:
            continue
        if target in net:
            return parts[1]
    return ""


def _derive_client_subnet(gateway_ip: str, mask: str) -> str:
    """192.168.2.1 + 255.255.255.0 -> '192.168.2.0/24'."""
    if not gateway_ip:
        return ""
    try:
        return str(ipaddress.ip_network(f"{gateway_ip}/{mask or '24'}",
                                        strict=False))
    except ValueError:
        return ""


def _find_uplink_subnet(iface: str, client_subnet: str,
                        run_command: RunCommand) -> str:
    """Derive the LAN's uplink subnet from the box's OWN addresses (last-resort
    fallback when the config can't resolve it).

    ``ip -o -4 addr show dev <iface>`` lists every directly-connected IPv4
    subnet on the shaping NIC (e.g. eth0 carries ``192.168.1.110/24`` uplink +
    ``192.168.2.1/24`` client alias); the first subnet that is NOT the client
    subnet is the uplink LAN. This is what the pass-through excludes, so it
    works even when ``engine.uplink_subnet`` / the router snapshot keys are
    missing or stale — the kernel always knows its own addresses. Returns ''
    when the NIC has only the client subnet (nothing LAN-cross to pass through).
    """
    if not iface or not client_subnet:
        return ""
    code, out = run_command(["ip", "-o", "-4", "addr", "show", "dev", iface])
    if code != 0:
        return ""
    try:
        client = str(ipaddress.ip_network(client_subnet, strict=False))
    except ValueError:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        addr, _, prefix = parts[3].partition("/")
        if not prefix:
            continue
        try:
            net = str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
        except ValueError:
            continue
        if net != client:
            return net
    return ""


def _find_own_addresses(iface: str, run_command: RunCommand) -> list[str]:
    """The box's OWN IPv4 addresses on ``iface`` (e.g. ``["192.168.2.1"]``).

    Client->box traffic (dashboard, SSH, RustDisk to the gateway's own IP) is
    redirected into ifb0 by the client-subnet ingress rule, so the LAN
    pass-through must also exempt the box's own addresses — otherwise that
    traffic is shaped as internet upload and throttled by the WAN cap. The
    kernel's ``ip -o -4 addr show`` is the ground truth; no config keys.
    Returns [] when the probe fails or the interface carries no IPv4.
    """
    if not iface:
        return []
    code, out = run_command(["ip", "-o", "-4", "addr", "show", "dev", iface])
    if code != 0:
        return []
    addrs: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        addr, _, _ = parts[3].partition("/")
        if addr:
            addrs.append(addr)
    return addrs


class TcShaper:
    """Linux (tc/HTB/fq_codel) speed-shaping engine.

    The maintenance loop feeds :meth:`update_state` a ``rate_map`` (one entry
    per device with a live IP) plus the shaping settings; this class programs
    the kernel's traffic control only when something actually changed.
    """

    def __init__(self, cfg: Any, run_command: RunCommand | None = None) -> None:
        self._run_command = run_command or _default_run_command
        sc = getattr(cfg, "shaping", None)
        self.ifb = getattr(sc, "ifb", "") or "ifb0"

        # LAN interface + client subnet: config override, else auto-detect.
        self.iface = getattr(sc, "interface", "") or ""
        self.client_subnet = getattr(sc, "client_subnet", "") or ""
        if not self.iface:
            self.iface = _find_interface_for(
                getattr(getattr(cfg, "dhcp", None), "gateway_ip", ""),
                self._run_command)
        if not self.client_subnet:
            dhcp = getattr(cfg, "dhcp", None)
            self.client_subnet = _derive_client_subnet(
                getattr(dhcp, "gateway_ip", ""), getattr(dhcp, "subnet", ""))

        # LAN link rate + the uplink subnet whose traffic must NOT be shaped.
        # The subnet resolves exactly like the nftables engine (explicit
        # engine.uplink_subnet wins, else derived from the dhcp block — the
        # LAN snapshot in LAN or WAN mode, else the box's own NIC addresses);
        # LAN-cross traffic rides a pass-through class at the full LAN rate
        # instead of the WAN cap.
        self._lan_rate_mbps = max(0.0, float(getattr(sc, "lan_rate_mbps", 0) or 0))
        self.uplink_subnet = ""
        if self.client_subnet:
            self.uplink_subnet = next(
                (n for n in resolve_local_networks(
                    getattr(cfg, "engine", None), getattr(cfg, "dhcp", None))
                 if n != self.client_subnet), "")
        # Last-resort: the config keys are missing/stale (e.g. an older
        # core/config.py that drops the LAN snapshot, or a hand-emptied
        # router_ip) — derive the uplink subnet from the box's own NIC
        # addresses instead, so the LAN pass-through still programs.
        if not self.uplink_subnet:
            self.uplink_subnet = _find_uplink_subnet(
                self.iface, self.client_subnet, self._run_command)

        # The box's own addresses on the shaping NIC: client->box / box->client
        # traffic (dashboard, SSH, RustDisk to the gateway IP) is LAN, not
        # internet — the pass-through must exempt it too.
        self.own_addresses = _find_own_addresses(self.iface, self._run_command)

        self.available = True
        self._warned = False
        #: last-applied state signature (None = not applied / needs rebuild).
        self._last_signature: Any = None
        self._rate_map: list[dict[str, Any]] = []
        self._enabled = False
        self._total_down = 0.0
        self._total_up = 0.0
        self._aqm = True

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Probe tc, load ifb0, clear stale rules. The first ``update_state``
        (next maintenance tick) programs the tree from the DB settings."""
        if not self.available:
            return
        if not self.iface:
            self._fail("no LAN interface (auto-detect found none) — set "
                       "shaping.interface in the config")
            return
        code, _ = self._run_command(["tc", "-V"])
        if code != 0:
            self._fail("tc binary missing or unusable (install iproute2 / run "
                       "as root)")
            return
        if not self._ensure_ifb():
            return  # _ensure_ifb logged the reason and set available=False
        self._teardown()  # no stale qdisc from a previous process
        self._last_signature = None
        if self.available:
            log.info("tc shaper ready: iface %s, client subnet %s, uplink %s "
                     "(LAN pass-through %s), ifb %s",
                     self.iface, self.client_subnet or "(unset)",
                     self.uplink_subnet or "(none)",
                     "on" if self.uplink_subnet else "off", self.ifb)

    def stop(self) -> None:
        """Stop accepting work. Rules are left in place on purpose — like the
        nftables engine, a device that was capped stays capped if the service
        dies; a reboot clears all qdiscs and start() rebuilds fresh."""
        self.available = False

    def _ensure_ifb(self) -> bool:
        """Load ifb so ``self.ifb`` (ifb0) exists and is up; False otherwise.

        ``modprobe ifb numifbs=1`` silently no-ops when the module is ALREADY
        loaded — kmod returns success without re-creating the netdevs — so a
        kernel that loaded ifb with a different ``numifbs`` never gets an ifb0.
        The old code ran that no-op at apply time and the following
        ``ip link set ifb0 up`` failed, permanently degrading the shaper with
        a generic error. We verify the device actually appeared and, when it
        did not, force a clean reload (unload + reload with the right
        numifbs); nothing can be using ifb0 then, since it does not exist.
        """
        def exists() -> bool:
            code, _ = self._run_command(["ip", "link", "show", "dev", self.ifb])
            return code == 0

        if not exists():
            code, out = self._run_command(["modprobe", "ifb", "numifbs=1"])
            if code != 0 or not exists():
                self._run_best_effort(["modprobe", "-r", "ifb"])
                code, out = self._run_command(["modprobe", "ifb", "numifbs=1"])
            if code != 0:
                self._fail(f"modprobe ifb failed: {out.strip()}")
                return False
            if not exists():
                self._fail(f"{self.ifb} still missing after modprobe ifb — "
                           "the ifb module is unavailable on this kernel")
                return False
        code, out = self._run_command(["ip", "link", "set", "dev", self.ifb, "up"])
        if code != 0:
            self._fail(f"ip link set {self.ifb} up failed: {out.strip()}")
            return False
        return True

    def update_state(self, rate_map: list[dict[str, Any]], enabled: bool,
                     total_down: float, total_up: float, aqm: bool,
                     lan_rate_mbps: float | None = None) -> None:
        """Reconcile the kernel's tc tree with the desired shaping state."""
        self._rate_map = sorted(rate_map or [], key=lambda e: str(e.get("ip", "")))
        self._enabled = bool(enabled)
        self._total_down = max(0.0, float(total_down or 0.0))
        self._total_up = max(0.0, float(total_up or 0.0))
        self._aqm = bool(aqm)
        if lan_rate_mbps is not None:
            # The LAN pass-through rate is a live setting (Network-tab "LAN
            # speed"), not just a boot-time config value: keep it in sync so
            # the signature diff picks up an edit.
            self._lan_rate_mbps = max(0.0, float(lan_rate_mbps or 0.0))
        if not self.available:
            return
        if not (self._enabled and
                (self._total_down > 0 or self._total_up > 0)):
            # Off: remove the tree, forget the signature so re-enabling rebuilds
            # next tick. A direction whose total is 0 is UNLIMITED (0 means no
            # cap), so "0" for one direction must NOT tear down the other
            # direction's tree — see _build_cmds for the per-direction build.
            self._teardown()
            self._last_signature = None
            return
        sig = self._state_signature()
        if sig == self._last_signature:
            return  # nothing changed — leave the kernel alone
        self._teardown()
        if not self._apply():
            self._last_signature = None
            return
        self._last_signature = sig

    @property
    def applied(self) -> bool:
        """True when the kernel tree matches the state last fed to update_state.

        The Network preview shows this: after a save the API schedules an
        immediate re-sync; until the (off-loop) rebuild commits, ``applied``
        is False and the panel can say "applying…" instead of silently showing
        stale numbers. Off (or failed) trees count as applied only when they
        were intentionally torn down.
        """
        if not self.available:
            return False
        if not (self._enabled and (self._total_down > 0 or self._total_up > 0)):
            return self._last_signature is None
        return self._last_signature == self._state_signature()

    # ---------------------------------------------------------------- internals

    def _state_signature(self) -> tuple[Any, ...]:
        entries = tuple(
            (e.get("ip", ""), e.get("device_id"), e.get("user_id"),
             round(float(e.get("down") or 0.0), 3),
             round(float(e.get("up") or 0.0), 3),
             round(float(e.get("user_down") or 0.0), 3),
             round(float(e.get("user_up") or 0.0), 3))
            for e in self._rate_map)
        return (self._enabled, round(self._total_down, 3),
                round(self._total_up, 3), self._aqm, self._lan_rate_mbps,
                self.uplink_subnet, tuple(sorted(self.own_addresses)), entries)

    def _apply(self) -> bool:
        """Program the full tree from the stored state. On any failure, tear
        down so no half-built tree lingers, then degrade."""
        for argv in self._build_cmds():
            if not self._run(argv):
                self._teardown()
                return False
        return True

    def _build_cmds(self) -> list[list[str]]:
        """The complete tc argv sequence that programs shaping.

        ifb0 is brought up once by :meth:`start` and stays up (teardown only
        removes qdiscs, never the device), so the apply does not re-modprobe —
        a no-op ``modprobe ifb numifbs=1`` at apply time is what killed the
        shaper on a box whose ifb was already loaded without an ifb0.
        """
        cmds: list[list[str]] = []
        # Download tree on $IF egress (post-NAT dst = client IP). Built only
        # when the down total is set — a 0 down total is "unlimited" (no tree,
        # no cap), and it must not take the upload shaping down with it.
        if self._total_down > 0:
            cmds += self._tree_cmds(self.iface, self._total_down, "dst")
        if self._total_up > 0:
            # Upload direction: redirect client-subnet ingress into ifb0
            # (pre-NAT src is still the client IP), then shape there by src.
            cmds += [
                ["tc", "qdisc", "add", "dev", self.iface, "handle", "ffff:",
                 "ingress"],
                ["tc", "filter", "add", "dev", self.iface, "parent", "ffff:",
                 "protocol", "ip", "u32", "match", "ip", "src",
                 self.client_subnet, "action", "mirred", "egress", "redirect",
                 "dev", self.ifb],
                *self._tree_cmds(self.ifb, self._total_up, "src"),
            ]
        return cmds

    def _tree_cmds(self, dev: str, total: float, match_field: str) -> list[list[str]]:
        """HTB + fq_codel commands for one direction's tree."""
        base = _rate(total)
        # The root caps at the LAN link rate so the pass-through class can
        # exceed the WAN line; every WAN class below stays capped at ``total``
        # so fq_codel keeps draining the internet queue at the line rate.
        if self._lan_rate_mbps > 0:
            lan = self._lan_rate_mbps
        else:
            # A stale config/loader left the LAN rate at 0. NEVER fall back to
            # the WAN total here — that would silently throttle LAN traffic to
            # the line cap, defeating the pass-through. Use the documented
            # default instead and say so.
            lan = 1000.0
            log.warning(
                "shaping.lan_rate_mbps is unset/0 — LAN pass-through capped at "
                "1000 Mbps (set it in config.yaml to change)"
            )
        lan_rate = _rate(lan)
        # Group the rate-map by owner user (a user's devices share their class).
        by_user: dict[int | None, dict[str, Any]] = {}
        for e in self._rate_map:
            uid = e.get("user_id")
            if uid is None:
                continue  # orphaned device — cannot shape (no user class)
            by_user.setdefault(uid, {
                "cap_down": float(e.get("user_down") or 0.0),
                "cap_up": float(e.get("user_up") or 0.0), "devs": []})
            by_user[uid]["devs"].append(e)

        base_burst = _burst(total)
        cmds: list[list[str]] = [
            ["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:",
             "htb", "default", "2"],
            ["tc", "class", "add", "dev", dev, "parent", "1:", "classid",
             "1:1", "htb", "rate", lan_rate, *_burst(lan)],
            # Default class: everything without a device leaf (unlimited
            # devices AND the box's own traffic). Capped at the direction
            # total so no traffic escapes the line-rate ceiling that makes
            # fq_codel effective (a fast unlimited downloader cannot flood
            # the modem buffer and inflate everyone's ping).
            ["tc", "class", "add", "dev", dev, "parent", "1:1", "classid",
             "1:2", "htb", "rate", base, "ceil", base, *base_burst],
            # Aggregate class under which all user/device classes live.
            ["tc", "class", "add", "dev", dev, "parent", "1:1", "classid",
             "1:100", "htb", "rate", base, "ceil", base, *base_burst],
        ]
        if self._aqm:
            cmds.append(["tc", "qdisc", "add", "dev", dev, "parent", "1:2",
                         "handle", "2:", "fq_codel"])

        # LAN pass-through: client<->uplink-subnet traffic (NAS, router admin,
        # LAN transfers) AND client<->the box itself (dashboard, SSH, file
        # shares like RustDisk) is not internet — it must run at full LAN speed,
        # not the WAN cap. A prio-1 class 1:99 under the root carries it
        # (prio-1 beats every prio-2 device filter, so a LAN-cross packet can
        # never be stolen by a device's cap). Priorities are deliberately
        # non-zero: tc treats an explicit ``prio 0`` filter as "no priority"
        # and auto-assigns it AFTER all real priorities, so the pass-through
        # would silently lose to the device caps (the live-box "LAN still
        # throttled" bug):
        #   upload tree (ifb0, pre-NAT src = client): match ip dst <uplink> AND
        #     ip dst <box's own addresses> — the client-subnet ingress redirect
        #     catches client->box traffic too, and without its own address it
        #     would fall to the default class (throttled by total_up: the
        #     live-box RustDisk report);
        #   download tree (egress): match ip src <uplink> (LAN downloads to
        #     clients + the box's own egress) AND ip src <box's own addresses>
        #     (box->client LAN responses, never capped by the device leaves) AND
        #     ip dst <uplink> (re-injected LAN uploads already shaped at ifb0 —
        #     without this they would be re-capped by the default class on their
        #     way out).
        # The class programs whenever there is an uplink subnet OR the box has
        # own addresses to exempt (a NIC with only the client alias still needs
        # to pass client->box traffic through).
        if (self.uplink_subnet and self.uplink_subnet != self.client_subnet) \
                or self.own_addresses:
            cmds.append(["tc", "class", "add", "dev", dev, "parent", "1:1",
                         "classid", "1:99", "htb", "rate", lan_rate,
                         "ceil", lan_rate, *_burst(lan)])
            if self._aqm:
                cmds.append(["tc", "qdisc", "add", "dev", dev, "parent",
                             "1:99", "handle", "0x99:", "fq_codel"])
            if match_field == "src":
                if self.uplink_subnet and self.uplink_subnet != self.client_subnet:
                    cmds.append(["tc", "filter", "add", "dev", dev, "parent",
                                 "1:", "protocol", "ip", "prio", "1", "u32",
                                 "match", "ip", "dst", self.uplink_subnet,
                                 "flowid", "1:99"])
                for addr in sorted(self.own_addresses):
                    cmds.append(["tc", "filter", "add", "dev", dev, "parent",
                                 "1:", "protocol", "ip", "prio", "1", "u32",
                                 "match", "ip", "dst", addr, "flowid", "1:99"])
            else:
                if self.uplink_subnet and self.uplink_subnet != self.client_subnet:
                    cmds.append(["tc", "filter", "add", "dev", dev, "parent",
                                 "1:", "protocol", "ip", "prio", "1", "u32",
                                 "match", "ip", "src", self.uplink_subnet,
                                 "flowid", "1:99"])
                    cmds.append(["tc", "filter", "add", "dev", dev, "parent",
                                 "1:", "protocol", "ip", "prio", "1", "u32",
                                 "match", "ip", "dst", self.uplink_subnet,
                                 "flowid", "1:99"])
                for addr in sorted(self.own_addresses):
                    cmds.append(["tc", "filter", "add", "dev", dev, "parent",
                                 "1:", "protocol", "ip", "prio", "1", "u32",
                                 "match", "ip", "src", addr, "flowid", "1:99"])

        for uid in sorted(by_user):
            grp = by_user[uid]
            cap = (grp["cap_down"] if match_field == "dst" else grp["cap_up"])
            # A device belongs in THIS tree only if ITS cap in this direction
            # (or its user's aggregate) is non-zero — the leaf's own cap must
            # match the direction, or an up-only device (up>0, down=0) with no
            # user upload aggregate was dropped from the upload tree and its
            # upload limit silently ignored.
            dev_attr = "down" if match_field == "dst" else "up"
            leaves = [e for e in grp["devs"]
                      if _effective(float(e.get(dev_attr) or 0.0),
                                    cap, total) is not None]
            if not leaves:
                continue  # every one of this user's devices is unlimited
            user_rate = min(cap, total) if cap > 0 else total
            user_cid = _user_class(uid)
            cmds.append(["tc", "class", "add", "dev", dev, "parent", "1:100",
                         "classid", user_cid, "htb", "rate", _rate(user_rate),
                         "ceil", _rate(user_rate), *_burst(user_rate)])
            for e in sorted(leaves, key=lambda x: str(x.get("ip", ""))):
                dev_cap = float(e.get("down") or 0.0) if match_field == "dst" \
                    else float(e.get("up") or 0.0)
                eff = _effective(dev_cap, cap, total)
                if eff is None:
                    continue
                dev_cid = _device_class(int(e["device_id"]))
                cmds.append(["tc", "class", "add", "dev", dev, "parent",
                             user_cid, "classid", dev_cid, "htb",
                             "rate", _rate(eff), "ceil", _rate(eff),
                             *_burst(eff)])
                if self._aqm:
                    cmds.append(["tc", "qdisc", "add", "dev", dev, "parent",
                                 dev_cid, "handle", _device_qdisc(int(e["device_id"])),
                                 "fq_codel"])
                cmds.append(["tc", "filter", "add", "dev", dev, "parent", "1:",
                             "protocol", "ip", "prio", "2", "u32", "match",
                             "ip", match_field, str(e["ip"]), "flowid", dev_cid])
        return cmds

    def _teardown(self) -> None:
        """Remove the trees we own (best-effort — del errors when absent)."""
        if not self.iface:
            return
        for argv in ([["tc", "qdisc", "del", "dev", self.iface, "root"],
                      ["tc", "qdisc", "del", "dev", self.iface, "ingress"],
                      ["tc", "qdisc", "del", "dev", self.ifb, "root"]]):
            self._run_best_effort(argv)

    def _run(self, argv: list[str]) -> bool:
        """Run an apply command; a failure degrades the shaper."""
        if not self.available:
            return False
        code, out = self._run_command(argv)
        if code == 0:
            return True
        self._fail(f"{argv[0]} failed: {out.strip()}")
        return False

    def _run_best_effort(self, argv: list[str]) -> None:
        """Run a teardown/probe command; a failure only logs."""
        code, out = self._run_command(argv)
        if code != 0:
            log.warning("tc %s failed (best-effort, ignoring): %s",
                        argv[0], out.strip() or code)

    def _fail(self, reason: str) -> None:
        self.available = False
        if not self._warned:
            log.error("tc shaper unavailable: %s — speed limits + low-latency "
                      "queues are off; quota blocks and accounting still work.",
                      reason)
            self._warned = True
