"""Where is each device's traffic going? (per-destination accounting from conntrack)

DNS history only says what a device *looked up*; a phone uploading a video over an
already-open connection does no lookups at all, so the Live-traffic view could not say
where the bytes went. The kernel knows: with ``nf_conntrack_acct`` on, every tracked
flow carries byte counters for both directions. Sampling them each tick and diffing
gives, per client address, how many bytes went to / came from each remote IP in the
window — real traffic, not a guess.

Remote IPs are shown by name when we can: first from dnsmasq's own ``reply <name> is
<ip>`` log lines (the exact name the device asked for), then from a reverse-DNS lookup
done off-thread and cached; otherwise the bare IP is shown (still a usable "link").
When neither knows the IP, the IP registry (RDAP) is asked — off-thread, cached per
network range and persisted — who owns the address (e.g. "Telegram Messenger Inc"): an
honest "who runs this server", never an app guess. Set ``history.ip_owner_lookup: false``
to keep destination IPs from ever leaving the box.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import queue
import re
import socket
import subprocess
import threading
import urllib.request
import time
from pathlib import Path
from typing import Callable, NamedTuple

log = logging.getLogger("quota.flows")

PROC_CONNTRACK = Path("/proc/net/nf_conntrack")
ACCT_SYSCTL = Path("/proc/sys/net/netfilter/nf_conntrack_acct")

#: Destinations moving less than this in a window are noise, not "where it goes".
MIN_DEST_BYTES = 4096
#: Entries kept in the IP -> name caches.
_MAX_NAMES = 20000
_PROTOS = {"tcp", "udp", "udplite", "sctp", "dccp"}

RunCommand = Callable[[list[str]], tuple[int, str]]


class Flow(NamedTuple):
    proto: str
    client: str       # original-direction source (the LAN device)
    server: str       # original-direction destination (the remote end)
    sport: int
    dport: int
    up: int           # bytes client -> server
    down: int         # bytes server -> client


def enable_conntrack_accounting(path: Path = ACCT_SYSCTL) -> bool:
    """Turn on per-flow byte counters (off by default in the kernel). Best effort."""
    try:
        if path.read_text(encoding="utf-8").strip() == "1":
            return True
        path.write_text("1\n", encoding="utf-8")
        log.info("conntrack byte accounting enabled")
        return True
    except OSError as exc:
        log.warning("cannot enable conntrack accounting (%s) — the Live-traffic "
                    "destination list falls back to DNS lookups", exc)
        return False


def parse_conntrack_line(line: str) -> Flow | None:
    """One ``/proc/net/nf_conntrack`` (or ``conntrack -L``) line -> :class:`Flow`.

    ``... tcp 6 431999 ESTABLISHED src=A dst=B sport=1 dport=2 packets=3 bytes=4
    src=B dst=NAT sport=2 dport=1 packets=5 bytes=6 [ASSURED] ...`` — the first
    tuple is the original direction (client -> server), the second the reply.
    Lines without ``bytes=`` (accounting off) or non-TCP/UDP return ``None``.
    """
    if " src=" not in line:
        return None
    tokens = line.split()
    proto = next((t for t in tokens[:4] if t in _PROTOS), "")
    if not proto:
        return None
    src: list[str] = []
    dst: list[str] = []
    sport: list[int] = []
    dport: list[int] = []
    nbytes: list[int] = []
    for tok in tokens:
        key, sep, val = tok.partition("=")
        if not sep:
            continue
        try:
            if key == "src":
                src.append(val)
            elif key == "dst":
                dst.append(val)
            elif key == "sport":
                sport.append(int(val))
            elif key == "dport":
                dport.append(int(val))
            elif key == "bytes":
                nbytes.append(int(val))
        except ValueError:
            return None
    if not (src and dst and sport and dport and nbytes):
        return None
    return Flow(proto, src[0], dst[0], sport[0], dport[0], nbytes[0],
                nbytes[1] if len(nbytes) > 1 else 0)


_REPLY_RE = re.compile(r"(?:reply|cached)\s+(\S+)\s+is\s+([0-9A-Fa-f:.]+)\s*$")


def parse_reply_line(line: str) -> tuple[str, str] | None:
    """dnsmasq ``reply <name> is <ip>`` / ``cached <name> is <ip>`` -> (name, ip)."""
    if " is " not in line:
        return None
    m = _REPLY_RE.search(line.strip())
    if not m:
        return None
    name, ip = m.group(1).rstrip(".").lower(), m.group(2)
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    return (name, ip) if "." in name else None


def _is_remote(ip: str) -> bool:
    """A real internet destination (not LAN / ULA / link-local / multicast)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local
                or a.is_multicast or a.is_unspecified or a.is_reserved)


class _NameCache:
    """IP -> hostname, fed by dnsmasq replies (thread-safe)."""

    def __init__(self) -> None:
        self._d: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, ip: str, name: str) -> None:
        with self._lock:
            self._d[ip] = (name, time.time())
            if len(self._d) > _MAX_NAMES:
                for k, _ in sorted(self._d.items(), key=lambda kv: kv[1][1])[:_MAX_NAMES // 5]:
                    self._d.pop(k, None)

    def get(self, ip: str, max_age: float = 86400.0) -> str | None:
        with self._lock:
            hit = self._d.get(ip)
        return hit[0] if hit and time.time() - hit[1] <= max_age else None


#: shared by the dnsmasq tailer thread (writer) and the sampler (reader)
DNS_NAMES = _NameCache()


def record_dns_reply(line: str) -> None:
    """Feed one dnsmasq log line to the IP -> name cache (no-op if not a reply)."""
    parsed = parse_reply_line(line)
    if parsed:
        DNS_NAMES.put(parsed[1], parsed[0])


class _ReverseResolver:
    """Off-thread reverse DNS with a cache (positive + negative). Never blocks callers."""

    def __init__(self, resolve: Callable[[str], str | None] | None = None) -> None:
        self._resolve = resolve or self._gethostbyaddr
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._q: queue.Queue[str] = queue.Queue(maxsize=64)
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _gethostbyaddr(ip: str) -> str | None:
        try:
            return socket.gethostbyaddr(ip)[0].rstrip(".").lower()
        except (OSError, UnicodeError):
            return None

    def lookup(self, ip: str) -> str | None:
        with self._lock:
            hit = self._cache.get(ip)
        if hit and time.time() - hit[1] < (86400 if hit[0] else 3600):
            return hit[0]
        self._request(ip)
        return None

    def _request(self, ip: str) -> None:
        with self._lock:
            if ip in self._pending:
                return
            self._pending.add(ip)
        try:
            self._q.put_nowait(ip)
        except queue.Full:
            with self._lock:
                self._pending.discard(ip)
            return
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="flows-rdns", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                ip = self._q.get(timeout=30)
            except queue.Empty:
                return
            name = self._resolve(ip)
            with self._lock:
                self._cache[ip] = (name, time.time())
                self._pending.discard(ip)
                if len(self._cache) > _MAX_NAMES:
                    for k in list(self._cache)[: _MAX_NAMES // 5]:
                        self._cache.pop(k, None)



# ---------------------------------------------------------------- IP owner (RDAP)

RDAP_URL = "https://rdap.org/ip/{ip}"


def owner_from_rdap(data: dict, ip: str) -> tuple[str | None, "ipaddress._BaseNetwork | None"]:
    """RDAP ``ip network`` object -> (owner name, the network range it covers)."""
    owner: str | None = None
    for ent in data.get("entities") or []:
        roles = ent.get("roles") or []
        vcard = (ent.get("vcardArray") or [None, []])[1]
        for item in vcard or []:
            if isinstance(item, list) and len(item) >= 4 and item[0] == "fn" and item[3]:
                if "registrant" in roles or owner is None:
                    owner = str(item[3])
                if "registrant" in roles:
                    break
    if not owner and data.get("name"):
        owner = str(data["name"])
    if owner:
        owner = re.sub(r"\s+", " ", owner).strip()[:40] or None
    net = None
    try:
        addr = ipaddress.ip_address(ip)
        cidrs = data.get("cidr0_cidrs") or []
        for c in cidrs:
            pref = c.get("v4prefix") or c.get("v6prefix")
            if pref:
                cand = ipaddress.ip_network(f"{pref}/{c['length']}", strict=False)
                if addr in cand:
                    net = cand
                    break
        if net is None and data.get("startAddress") and data.get("endAddress"):
            for cand in ipaddress.summarize_address_range(
                    ipaddress.ip_address(data["startAddress"]),
                    ipaddress.ip_address(data["endAddress"])):
                if addr in cand:
                    net = cand
                    break
    except (ValueError, KeyError, TypeError):
        net = None
    return owner, net


def _fetch_rdap(ip: str) -> dict | None:
    try:
        req = urllib.request.Request(RDAP_URL.format(ip=ip),
                                     headers={"Accept": "application/rdap+json",
                                              "User-Agent": "QuotaManager/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:   # follows the RIR redirect
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — offline / rate limited / bad JSON: just no name
        return None


class _OwnerResolver:
    """Who owns this IP? One RDAP query per network RANGE (not per address), done on a
    daemon thread at most ~1/s, cached in memory and in a small JSON file."""

    def __init__(self, fetch: Callable[[str], dict | None] | None = None,
                 cache_path: Path | None = None, min_interval: float = 1.0) -> None:
        self._fetch = fetch or _fetch_rdap
        self._path = cache_path
        self._min_interval = min_interval
        self._ranges: list[tuple[ipaddress._BaseNetwork, str]] = []
        self._neg: dict[str, float] = {}
        self._q: queue.Queue[str] = queue.Queue(maxsize=32)
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_save = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path:
            return
        try:
            for net, owner in json.loads(self._path.read_text(encoding="utf-8")):
                self._ranges.append((ipaddress.ip_network(net), str(owner)))
        except (OSError, ValueError, TypeError):
            self._ranges = []

    def _save(self) -> None:
        if not self._path or time.time() - self._last_save < 30:
            return
        self._last_save = time.time()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([[str(n), o] for n, o in self._ranges[-5000:]]),
                                  encoding="utf-8")
        except OSError:
            pass

    def lookup(self, ip: str) -> str | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        with self._lock:
            for net, owner in self._ranges:
                if addr in net:
                    return owner
            if time.time() - self._neg.get(ip, 0) < 3600:
                return None
            if ip in self._pending:
                return None
            self._pending.add(ip)
        try:
            self._q.put_nowait(ip)
        except queue.Full:
            with self._lock:
                self._pending.discard(ip)
            return None
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="flows-rdap", daemon=True)
            self._thread.start()
        return None

    def _run(self) -> None:
        while True:
            try:
                ip = self._q.get(timeout=60)
            except queue.Empty:
                return
            data = self._fetch(ip)
            owner, net = owner_from_rdap(data, ip) if data else (None, None)
            with self._lock:
                self._pending.discard(ip)
                if owner:
                    self._ranges.append((net or ipaddress.ip_network(ip), owner))
                else:
                    self._neg[ip] = time.time()
            if owner:
                self._save()
            time.sleep(self._min_interval)


def _base_domain(name: str) -> str:
    from quota.history_analytics import get_base_domain
    return get_base_domain(name) or name


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, p.stdout or ""


class FlowSampler:
    """Diffs conntrack byte counters between ticks -> per-client, per-remote-IP bytes."""

    def __init__(self, *, proc_path: Path = PROC_CONNTRACK, run: RunCommand | None = None,
                 resolver: _ReverseResolver | None = None,
                 owner_resolver: "_OwnerResolver | None" = None,
                 owner_lookup: bool = True, owner_cache: Path | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._proc = proc_path
        self._run = run or _default_run
        self._rdns = resolver or _ReverseResolver()
        self._owners = owner_resolver if owner_resolver is not None else (
            _OwnerResolver(cache_path=owner_cache) if owner_lookup else None)
        self._clock = clock
        self._prev: dict[tuple, tuple[int, int]] = {}
        self._first = True
        self._last_t: float | None = None
        #: seconds the latest ``bytes`` cover
        self.window_s: float = 15.0
        #: client address -> remote ip -> [up_bytes, down_bytes] in the last window
        self.latest: dict[str, dict[str, list[int]]] = {}
        #: False when the kernel exposes no readable/accounted conntrack table
        self.available: bool = True

    def _read(self) -> list[str]:
        try:
            return self._proc.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            code, out = self._run(["conntrack", "-L"])
            return out.splitlines() if code == 0 else []

    def sample(self, clients: set[str] | None = None) -> None:
        """Read the table once and refresh :attr:`latest`. Blocking — call off-loop."""
        now = self._clock()
        lines = self._read()
        flows = [f for f in (parse_conntrack_line(l) for l in lines) if f]
        self.available = bool(flows) or not lines
        if self._last_t is not None:
            self.window_s = min(120.0, max(1.0, now - self._last_t))
        self._last_t = now
        new_prev: dict[tuple, tuple[int, int]] = {}
        out: dict[str, dict[str, list[int]]] = {}
        for f in flows:
            if clients is not None and f.client not in clients:
                continue
            key = (f.proto, f.client, f.sport, f.server, f.dport)
            new_prev[key] = (f.up, f.down)
            prev = self._prev.get(key)
            if prev is None:
                # first ever sample: only a baseline; later: a new flow began inside the window
                du, dd = (0, 0) if self._first else (f.up, f.down)
            else:
                du = f.up - prev[0] if f.up >= prev[0] else f.up
                dd = f.down - prev[1] if f.down >= prev[1] else f.down
            if (du or dd) and _is_remote(f.server):
                slot = out.setdefault(f.client, {}).setdefault(f.server, [0, 0])
                slot[0] += du
                slot[1] += dd
        self._prev = new_prev
        self._first = False
        self.latest = out

    def label_for(self, ip: str) -> tuple[str, str]:
        """(display label, kind) for a remote IP. kind: ``name`` (dnsmasq / reverse DNS),
        ``owner`` (IP-registry owner of the network) or ``ip`` (nothing known yet)."""
        name = DNS_NAMES.get(ip) or self._rdns.lookup(ip)
        if name and "." in name:
            return _base_domain(name), "name"
        owner = self._owners.lookup(ip) if self._owners is not None else None
        if owner:
            return owner, "owner"
        return ip, "ip"

    def top_destinations(self, addrs: set[str], limit: int = 4) -> list[dict]:
        """Where the given device addresses sent/received the most in the last window."""
        merged: dict[str, list] = {}
        for a in addrs:
            for ip, (u, d) in (self.latest.get(a) or {}).items():
                if u + d < MIN_DEST_BYTES:
                    continue
                label, kind = self.label_for(ip)
                slot = merged.setdefault(label, [0, 0, ip, kind])
                slot[0] += u
                slot[1] += d
        ranked = sorted(merged.items(), key=lambda kv: kv[1][0] + kv[1][1], reverse=True)
        total = sum(v[0] + v[1] for _, v in ranked) or 1
        win = self.window_s or 15.0
        return [{
            "domain": label, "ip": v[2], "kind": v[3], "resolved": v[3] != "ip",
            "up_bps": int(v[0] * 8 / win), "down_bps": int(v[1] * 8 / win),
            "share_pct": round((v[0] + v[1]) * 100 / total, 1),
        } for label, v in ranked[:limit]]


def seed_dns_names_from_log(path: str, max_bytes: int = 4 * 1024 * 1024) -> int:
    """Warm the IP -> name cache from the END of dnsmasq's query log, so destinations
    of connections opened before a restart are named from the first tick."""
    n = 0
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    for line in data.splitlines():
        if " is " in line and ("reply" in line or "cached" in line):
            parsed = parse_reply_line(line)
            if parsed:
                DNS_NAMES.put(parsed[1], parsed[0])
                n += 1
    return n
