"""Embedded request-level WAF for the dashboard web app (2026-08-19).

The kernel firewall (``quota/firewall.py``) sees IPs/ports/rates — it cannot
see *inside* an HTTP request. This module is the application-layer inspector
embedded in the FastAPI app (a Starlette middleware in front of every route),
catching attacks that arrive on an already-allowed port over perfectly valid
TCP.

The classification logic here is pure and fully unit-testable: given a
request's method / path / query / body / headers it returns the first rule
that fires (or None). The middleware in ``api/app.py`` owns the app plumbing
(config wiring, mode resolution, DB events, firewall auto-ban, HTTP
responses) and delegates the actual inspection to these functions.

Rule set is adapted from the OWASP Core Rule Set's spirit (not a wholesale
copy — hand-rolled regexes are routinely bypassed, so the *signals* are kept
narrow enough to be low-false-positive while still catching the scripted
exploit kits: sqlmap, nikto, mass scanners, classic stored-XSS dumps).

Mode semantics (``WafConfig.mode``): "auto" = strict in WAN, log-only in LAN;
"strict" blocks; "log" records + passes; "off" skips everything. ``fail_mode``
= "closed" (WAN: an internal error makes the dashboard unreachable rather than
silently unprotected) | "open" (log + pass through).
"""

from __future__ import annotations

import ipaddress
import re
from collections import deque

from starlette.types import Message, Receive


class InspectionBody:
    """Buffer a bounded HTTP body for inspection, then replay the same bytes."""

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._prefix: bytearray | None = None
        self._more_body = True
        self._pending: Message | None = None

    async def inspect(self, limit: int) -> str | None:
        """Return decoded text, or None at the first chunk that exceeds the cap."""
        prefix = self._prefix = bytearray()
        while self._more_body:
            message = await self._receive()
            if message["type"] == "http.disconnect":
                self._pending = message
                return ""
            chunk = message.get("body", b"")
            if len(chunk) > limit - len(prefix):
                # Log mode still needs to pass this event on. Keep the original
                # message without copying its bytes into the inspection buffer.
                self._pending = message
                return None
            prefix.extend(chunk)
            self._more_body = message.get("more_body", False)
        return prefix.decode("utf-8", "replace")

    async def receive(self) -> Message:
        if self._prefix is not None:
            prefix, self._prefix = self._prefix, None
            return {"type": "http.request", "body": bytes(prefix), "more_body": self._more_body}
        if self._pending is not None:
            message, self._pending = self._pending, None
            return message
        return await self._receive()


#: HTTP verbs the dashboard actually uses; everything else is rejected
#: outright (TRACE/CONNECT and unused verbs are attack surface, not features).
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE",
                             "HEAD", "OPTIONS"})

#: Known scanner / exploit-tool fingerprints in the User-Agent header.
SCANNER_UA_RE = re.compile(
    r"sqlmap|nikto|nmap|masscan|wpscan|dirb|dirbuster|gobuster|joomscan|"
    r"acunetix|nessus|openvas|burp|arachni|zap|xenu|webreaver|fimap|"
    r"watobo|w3af|havij|pwn|metasploit|hydra|hydra1|ncrack|patator|"
    r"medusa|aircrack|skypebot|spybot|libwhisker|wikto|blackwidow|"
    r"paros|webinspect|appscan|qualys|probing|cybercop|vulscan|"
    r"python-requests|go-http-client|libwww-perl|wget/|curl/|lwp-",
    re.IGNORECASE)

#: (rule_id, category, compiled regex) — the request-signature ruleset.
_RULES: list[tuple[str, str, re.Pattern[str]]] = []


def _rule(rule_id: str, category: str, pattern: str) -> None:
    _RULES.append((rule_id, category, re.compile(pattern, re.IGNORECASE)))


# -- SQL injection ----------------------------------------------------------
_rule("sqli-1", "sql-injection",
      r"(?:\bunion\b.{0,60}\bselect\b)|(?:\bselect\b.{0,60}\bfrom\b)")
_rule("sqli-2", "sql-injection",
      r"\b(?:insert\s+into|delete\s+from|drop\s+table|alter\s+table|"
      r"truncate\s+table|create\s+table|xp_cmdshell|information_schema)\b")
_rule("sqli-3", "sql-injection", r"\b(?:or|and)\s+1\s*=\s*1\b")
_rule("sqli-4", "sql-injection", r"['\"](?:\s*or\s*['\"]?[\w\d]*)|\bupdate\s+\w+\s+set\b")
_rule("sqli-5", "sql-injection",
      r"\bsleep\s*\(\s*\d+\s*\)|\bbenchmark\s*\(|/\*.*?\*/")
_rule("sqli-6", "sql-injection", r"\b(?:or|union)\b[^\s]*\(|'\s*(?:or|and)\s+'")

# -- Cross-Site Scripting (stored + reflected) ------------------------------
_rule("xss-1", "xss", r"<\s*(?:script|iframe|object|embed|svg|math)\b")
_rule("xss-2", "xss", r"<\s*/\s*script\s*>")
_rule("xss-3", "xss", r"javascript\s*:")
_rule("xss-4", "xss",
      r"\bon(?:error|load|click|mouseover|focus|blur|change)\s*=\s*")
_rule("xss-5", "xss", r"(?:document\.cookie|alert\s*\(|prompt\s*\(|"
      r"eval\s*\(|fromCharCode\s*\()")
_rule("xss-6", "xss", r"&#x?[0-9a-fA-F]{2,8};")
_rule("xss-7", "xss", r"data\s*:\s*text/html|vbscript\s*:")

# -- Command injection ------------------------------------------------------
_rule("cmdi-1", "command-injection", r"(?:\$\s*\(|\$\(\s*|\`|\|\||&&)")
_rule("cmdi-2", "command-injection",
      r"\b(?:cat|ls|whoami|wget|curl|nc|netcat|chmod|rm\s+-rf|"
      r"sh\s+-c|bash\s+-c|python[23]?\s+-c|perl\s+-e)\b[^,}\"]*")
_rule("cmdi-3", "command-injection", r";\s*(?:cat|ls|whoami|wget|curl|"
      r"nc|netcat|chmod|sh|bash|python|perl|rm)\b")

# -- Path traversal / file access -------------------------------------------
_rule("path-1", "path-traversal", r"\.\./|\.\.%2f|%2e%2e%2f|%252e|"
      r"\.\.\\|%2e%2e%5c")
_rule("path-2", "path-traversal", r"%00|\\\\?\u0000|/etc/passwd|"
      r"C:\\\\windows|\\\\proc\\\\self")
_rule("path-3", "path-traversal", r"\.\.(?:/|\\|$)")


def classify(method: str, path: str, query: str, body_text: str,
             headers: dict[str, str]) -> tuple[str, str] | None:
    """Return ``(rule_id, category)`` for the first rule that fires, else None.

    The order of checks mirrors the middleware: cheap whole-request checks
    (method/size) first, then signatures over path + query + body.
    """
    if method not in ALLOWED_METHODS:
        return ("method", "method-not-allowed")
    # header-count / size checks are middleware-side (they need the raw list);
    # here we only reject a missing Host (a well-formed HTTP/1.1 request has
    # one and several attack toolkits omit it).
    if not headers.get("host") and method in ("GET", "POST"):
        return ("host-1", "missing-host")
    if "\x00" in path or "%00" in path:
        return ("path-2", "path-traversal")
    for rule_id, category, rx in _RULES:
        for target in (path, query, body_text):
            if rx.search(target):
                return (rule_id, category)
    return None


def classify_ua(user_agent: str) -> bool:
    """True when the User-Agent matches a known scanner/exploit-tool."""
    return bool(user_agent and SCANNER_UA_RE.search(user_agent))


def resolve_mode(configured: str, topology: str) -> str:
    """Resolve ``WafConfig.mode`` against the live topology."""
    mode = (configured or "auto").lower()
    if mode == "auto":
        return "strict" if topology == "wan" else "log"
    if mode in ("strict", "log", "off"):
        return mode
    return "log"  # unknown value: default to the safest LAN posture


def is_local_ip(ip: str, subnets: list[str]) -> bool:
    """True when *ip* belongs to any of the CIDR *subnets*.

    Used by the middleware to exempt local management traffic from WAF
    blocking — the panel must always be reachable from the LAN even in
    strict (WAN) mode.  Invalid IPs or empty subnet lists are handled
    gracefully (returns False).
    """
    if not ip or not subnets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in subnets:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def is_exempt(exceptions: list[dict], rule_id: str, path: str,
              source_ip: str) -> bool:
    """True when a rule exception permits ``rule_id`` for this request.

    ``exceptions`` entries: ``{rule_id, path?, source_ip?}`` — a matching
    ``rule_id`` is bypassed when every provided filter also matches. A rule
    with no path/source filters is bypassed globally.
    """
    for exc in exceptions or []:
        if exc.get("rule_id") != rule_id:
            continue
        if exc.get("path") and not path.startswith(str(exc["path"])):
            continue
        if exc.get("source_ip") and exc.get("source_ip") != source_ip:
            continue
        return True
    return False


RATE_STATE_SWEEP_SECONDS = 5


class WafRateState:
    """Bounded endpoint buckets and rolling auto-ban history."""

    def __init__(self, *, max_entries: int = 4096, ban_threshold: int = 8,
                 ban_window_seconds: float = 300) -> None:
        self._max_entries = max_entries
        self._ban_threshold = ban_threshold
        self._ban_window = ban_window_seconds
        self._buckets: dict[tuple[str, str], tuple[int, float]] = {}
        self._hits: dict[str, deque[float]] = {}
        self._next_sweep = 0.0

    def expire(self, now: float) -> None:
        """Remove expired entries even when their source never returns."""
        self._next_sweep = now + RATE_STATE_SWEEP_SECONDS
        for key, (_, deadline) in list(self._buckets.items()):
            if now > deadline:
                del self._buckets[key]
        for ip, stamps in list(self._hits.items()):
            self._expire_hits(stamps, now)
            if not stamps:
                del self._hits[ip]

    def _expire_if_due(self, now: float) -> None:
        if now >= self._next_sweep:
            self.expire(now)

    def rate_limited(self, ip: str, path: str, limits: dict[str, list[int]],
                     now: float) -> bool:
        """Check the per-path cap without displacing an active bucket."""
        prefix = next((p for p in limits if path.startswith(p)), None)
        if prefix is None:
            return False
        self._expire_if_due(now)
        max_reqs, window = limits[prefix]
        if max_reqs <= 0:
            return True
        key = (ip, prefix)
        bucket = self._buckets.get(key)
        if bucket is not None and now <= bucket[1]:
            count, deadline = bucket
            if count >= max_reqs:
                return True
            self._buckets[key] = (count + 1, deadline)
        else:
            if bucket is None and len(self._buckets) >= self._max_entries:
                return True
            self._buckets[key] = (1, now + window)
        return False

    def _expire_hits(self, stamps: deque[float], now: float) -> None:
        cutoff = now - self._ban_window
        while stamps and stamps[0] < cutoff:
            stamps.popleft()

    def register_block(self, ip: str, now: float) -> bool:
        """Record a block and report whether this IP has met the ban threshold."""
        if self._ban_threshold <= 0:
            return False
        self._expire_if_due(now)
        stamps = self._hits.get(ip)
        if stamps is None:
            if len(self._hits) >= self._max_entries:
                # The request is still blocked by the WAF. Keep the existing
                # histories so a new source can't erase another source's hits.
                return False
            stamps = self._hits[ip] = deque(maxlen=self._ban_threshold)
        self._expire_hits(stamps, now)
        stamps.append(now)
        return len(stamps) >= self._ban_threshold
