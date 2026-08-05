"""nftables-backed packet engine for the Linux gateway.

Mirrors the public interface of :class:`quota.engine.PacketEngine`
(``start`` / ``stop`` / ``update_state`` / ``flush``) but instead of diverting
packets in a userspace thread it programs the kernel:

* one **named counter per device per direction** — ``q_up_<ip>`` / ``q_down_<ip>``
  (dots -> underscores) — matching rules in the ``forward`` hook. Clients live
  on their own subnet (``192.168.2.0/24``) that the kernel masquerades out the
  uplink, so the forward chain sees the whole byte flow and the box's own
  traffic (DNS, DHCP, the dashboard) is naturally excluded.
* a **``blocked`` set** that two drop rules at the tail of the chain
  reference — `ip saddr @blocked drop` + `ip daddr @blocked drop`. The kernel
  drops a blocked device's packets at line rate, no Python in the path.

Counters are read back with ``nft -j list counters`` (JSON — far cheaper than
walking the ruleset text). ``flush()`` returns the **delta since the previous
flush**, which is exactly what the maintenance loop writes to ``usage_daily``.

Rule lifecycle
--------------
* ``start()`` — idempotent, best-effort: (re)builds the table base. It flushes
  the table first so a restart never inherits stale device rules.
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
the app (dashboard, DB usage) keeps working, exactly like a missing pydivert
on Windows.

The command runner is injected (``run_command``) so tests can drive a fake
``nft`` binary and assert the exact ruleset programmed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from quota.engine import EngineCounters, EngineSnapshot

log = logging.getLogger("quota.nftables")

#: argv -> (returncode, output). Tests inject a fake; the default shells out
#: to the real ``nft`` binary.
RunCommand = Callable[[list[str]], tuple[int, str]]

#: Table all rules live in. ``inet`` so an operator's IPv6 rules coexist.
FAMILY = "inet"


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


class NftablesEngine:
    """Linux (nftables) accounting + hard-block engine.

    Threads are not used: the kernel counts, this class only reconciles rules
    and reads counters back on demand. ``is_blocked_cb`` is accepted for
    interface parity with :class:`PacketEngine` but unused (the maintenance
    loop drives enforcement via :meth:`update_state`).
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

        self.available = True
        self._warned = False
        self._ip_to_mac: dict[str, str] = {}
        self._blocked: dict[str, bool] = {}
        #: IPs most recently programmed into the kernel `blocked` set.
        self._last_blocked_ips: list[str] = []
        #: IPs whose counter rules exist in the kernel.
        self._installed: set[str] = set()
        #: last-seen kernel byte totals per IP (for delta computation).
        self._last: dict[str, EngineCounters] = {}

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
        self._run(["add", "table", f"{FAMILY} {self.table}"])
        self._run(["flush", "table", f"{FAMILY} {self.table}"])
        self._run(["add", "chain", f"{FAMILY} {self.table} forward",
                   "{ type filter hook forward priority 0; policy accept; }"])
        self._run(["add", "set", f"{FAMILY} {self.table} blocked",
                   "{ type ipv4_addr; }"])
        self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                   "ip saddr @blocked drop"])
        self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                   "ip daddr @blocked drop"])
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
        for ip in sorted(set(ip_to_mac) - self._installed):
            self._add_device(ip)

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

        return EngineSnapshot(
            by_ip=by_ip,
            ip_to_mac=dict(self._ip_to_mac),
            blocked=dict(self._blocked),
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
        self._fail(f"nft {args[0]} failed: {out.strip()}")
        return False

    def _add_device(self, ip: str) -> None:
        up = _counter_name(ip, "up")
        down = _counter_name(ip, "down")
        ok = self._run(["add", "counter", f"{FAMILY} {self.table}", up])
        ok &= self._run(["add", "counter", f"{FAMILY} {self.table}", down])
        ok &= self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                         f"ip saddr {ip} counter name {up}"])
        ok &= self._run(["add", "rule", f"{FAMILY} {self.table} forward",
                         f"ip daddr {ip} counter name {down}"])
        if ok:
            self._installed.add(ip)
            log.info("nftables: watching device %s", ip)

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

    def _fail(self, reason: str) -> None:
        self.available = False
        if not self._warned:
            log.error("nftables engine unavailable: %s — no per-packet "
                      "accounting on this host (the dashboard still shows "
                      "DB usage). Run as root and check `nft --version`.",
                      reason)
            self._warned = True
