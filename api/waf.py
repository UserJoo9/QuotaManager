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

import re

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


class WafRateState:
    """Per-source-IP request buckets + auto-ban hit counters (in-memory)."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}      # ip -> WAF-block timestamps
        self._buckets: dict[tuple[str, str], tuple[int, float]] = {}
        # (ip, path-prefix) -> (count, window_start)

    def rate_limited(self, ip: str, path: str, limits: dict[str, list[int]],
                     now: float) -> bool:
        """True when ``ip`` exceeded the per-path cap for ``path``."""
        prefix = next((p for p in limits if path.startswith(p)), None)
        if not prefix:
            return False
        max_reqs, window = limits[prefix]
        key = (ip, prefix)
        count, start = self._buckets.get(key, (0, now))
        if now - start > window:
            count, start = 0, now
        count += 1
        self._buckets[key] = (count, start)
        return count > max_reqs

    def register_block(self, ip: str, now: float) -> None:
        """Record a WAF block for ``ip`` (feeds the auto-ban counter)."""
        stamps = self._hits.setdefault(ip, [])
        stamps.append(now)

    def blocks_in_window(self, ip: str, window: float, now: float) -> int:
        stamps = [t for t in self._hits.get(ip, []) if now - t <= window]
        self._hits[ip] = stamps
        return len(stamps)