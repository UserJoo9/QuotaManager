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
* the **total-link cap** — root class rate = the configured line speed, so the
  queue forms at the tc layer (where fq_codel can drain it fairly) instead of
  in the modem's buffer ("bufferbloat": one heavy uploader/downloader no longer
  inflates everyone's ping);
* the **per-user aggregate** — a user's device leaves sit under their user
  class, which is capped at the user's configured total;
* the **per-device cap** — each device leaf ``rate = ceil = eff`` (hard cap).

Devices with no cap on either axis go to the default class (still capped at the
direction total + fq_codel, so untracked devices cannot flood the line).

Class ids are deterministic (recomputed each reconcile); device trees live on
separate qdiscs (``$IF`` / ``ifb0``) so ids may repeat across directions:
root qdisc ``handle 1: htb default 2``; root class ``1:1`` (rate = direction
total); default ``1:2``; download aggregate ``1:100``; user classes
``1:<0x300+uid>``; device leaves ``1:<0x8000+devid>``.

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
            log.info("tc shaper ready: iface %s, client subnet %s, ifb %s",
                     self.iface, self.client_subnet or "(unset)", self.ifb)

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
                     total_down: float, total_up: float, aqm: bool) -> None:
        """Reconcile the kernel's tc tree with the desired shaping state."""
        self._rate_map = sorted(rate_map or [], key=lambda e: str(e.get("ip", "")))
        self._enabled = bool(enabled)
        self._total_down = max(0.0, float(total_down or 0.0))
        self._total_up = max(0.0, float(total_up or 0.0))
        self._aqm = bool(aqm)
        if not self.available:
            return
        if not (self._enabled and self._total_down > 0 and self._total_up > 0):
            # Off (or totals not set): remove the tree, forget the signature so
            # re-enabling rebuilds next tick.
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
                round(self._total_up, 3), self._aqm, entries)

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
        cmds: list[list[str]] = [
            # Upload direction: redirect client-subnet ingress into ifb0
            # (pre-NAT src is still the client IP), then shape there by src.
            ["tc", "qdisc", "add", "dev", self.iface, "handle", "ffff:", "ingress"],
            ["tc", "filter", "add", "dev", self.iface, "parent", "ffff:",
             "protocol", "ip", "u32", "match", "ip", "src", self.client_subnet,
             "action", "mirred", "egress", "redirect", "dev", self.ifb],
        ]
        # Download tree on $IF egress (post-NAT dst = client IP).
        cmds += self._tree_cmds(self.iface, self._total_down, "dst")
        # Upload tree on ifb0 egress (pre-NAT src = client IP).
        cmds += self._tree_cmds(self.ifb, self._total_up, "src")
        return cmds

    def _tree_cmds(self, dev: str, total: float, match_field: str) -> list[list[str]]:
        """HTB + fq_codel commands for one direction's tree."""
        base = _rate(total)
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
             "1:1", "htb", "rate", base, *base_burst],
            # Default class: everything without a device leaf (unlimited
            # devices AND the box's own traffic). Capped at the direction
            # total so no traffic escapes the line-rate ceiling that makes
            # fq_codel effective (a 1000mbit pass-through would let one
            # unlimited downloader flood the modem buffer and inflate pings).
            ["tc", "class", "add", "dev", dev, "parent", "1:1", "classid",
             "1:2", "htb", "rate", base, "ceil", base, *base_burst],
            # Aggregate class under which all user/device classes live.
            ["tc", "class", "add", "dev", dev, "parent", "1:1", "classid",
             "1:100", "htb", "rate", base, "ceil", base, *base_burst],
        ]
        if self._aqm:
            cmds.append(["tc", "qdisc", "add", "dev", dev, "parent", "1:2",
                         "handle", "2:", "fq_codel"])

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
                             "protocol", "ip", "prio", "1", "u32", "match",
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
