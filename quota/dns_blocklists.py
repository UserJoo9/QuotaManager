"""Safe retrieval and parsing for operator-managed DNS blocklist feeds."""

from __future__ import annotations

import http.client
import ipaddress
import asyncio
import logging
import socket
import ssl
import time
import urllib.parse
from typing import Any, Callable, Mapping

import aiosqlite

from quota import dns_rules as _dns_rules

log = logging.getLogger("quota.dns_blocklists")

FETCH_TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_DOMAINS_PER_FEED = 250_000
MAX_ACTIVE_DOMAINS = 750_000
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
MAX_CONCURRENT_FETCHES = 2


class BlocklistError(Exception):
    """A safe, stable error category that can be returned by the API."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _resolve_addresses(host: str) -> set[str]:
    """Resolve a hostname once; callers pin a chosen result for HTTPS."""
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BlocklistError("fetch_failed") from exc
    addresses = {record[4][0].split("%", 1)[0] for record in records}
    if not addresses:
        raise BlocklistError("unsafe_destination")
    return addresses


def _canonical_url(url: str) -> tuple[str, str, int]:
    if (not isinstance(url, str) or not url.strip()
            or any(ord(char) < 32 or char.isspace() for char in url)):
        raise BlocklistError("invalid_url")
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        if (parsed.scheme.lower() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise BlocklistError("invalid_url")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port if parsed.port is not None else 443
    except (UnicodeError, ValueError) as exc:
        raise BlocklistError("invalid_url") from exc
    if (not host or port < 1 or port > 65535 or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))):
        raise BlocklistError("unsafe_destination")
    path = parsed.path or "/"
    netloc_host = f"[{host}]" if ":" in host else host
    netloc = netloc_host if port == 443 else f"{netloc_host}:{port}"
    canonical = urllib.parse.urlunsplit(("https", netloc, path, parsed.query, ""))
    return canonical, host, port


def validate_blocklist_url(url: str) -> str:
    """Validate HTTPS and require every DNS answer to be a public address."""
    canonical, host, _port = _canonical_url(url)
    try:
        address = ipaddress.ip_address(host)
        addresses = {str(address)}
    except ValueError:
        addresses = _resolve_addresses(host)
    try:
        public = all(ipaddress.ip_address(item).is_global for item in addresses)
    except ValueError as exc:
        raise BlocklistError("unsafe_destination") from exc
    if not addresses or not public:
        raise BlocklistError("unsafe_destination")
    return canonical


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a pinned IP with the original host's TLS identity."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(
                raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _fetch_response(url: str, address: str
                    ) -> tuple[int, Mapping[str, str], bytes]:
    """Fetch one HTTPS response without following redirects automatically."""
    canonical, host, port = _canonical_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection = _PinnedHTTPSConnection(
        host, port, address, FETCH_TIMEOUT_SECONDS)
    try:
        connection.request(
            "GET", path,
            headers={"User-Agent": "QuotaManager/0.5", "Accept": "text/plain,*/*;q=0.5",
                     "Accept-Encoding": "identity"})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status in (301, 302, 303, 307, 308):
            return response.status, headers, b""
        if response.status != 200:
            raise BlocklistError("fetch_failed")
        content_length = headers.get("content-length", "")
        try:
            if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                raise BlocklistError("response_too_large")
        except ValueError:
            pass
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise BlocklistError("response_too_large")
        return response.status, headers, body
    except BlocklistError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise BlocklistError("fetch_failed") from exc
    finally:
        connection.close()


def _validated_addresses(url: str) -> tuple[str, list[str]]:
    canonical = validate_blocklist_url(url)
    _canonical, host, _port = _canonical_url(canonical)
    try:
        address = ipaddress.ip_address(host)
        addresses = [str(address)]
    except ValueError:
        addresses = sorted(_resolve_addresses(host))
    if not addresses or any(
            not ipaddress.ip_address(item).is_global for item in addresses):
        raise BlocklistError("unsafe_destination")
    return canonical, addresses


def fetch_blocklist_domains(url: str) -> set[str]:
    """Fetch a bounded hosts/AdBlock Plus feed and return normalized domains."""
    current_url = url
    redirects = 0
    while True:
        canonical, addresses = _validated_addresses(current_url)
        # Every address returned by DNS was checked, and this request uses a
        # pinned address so a second DNS lookup cannot redirect to a private IP.
        status, headers, body = _fetch_response(canonical, addresses[0])
        if status in (301, 302, 303, 307, 308):
            location = headers.get("location")
            if not location:
                raise BlocklistError("fetch_failed")
            if redirects >= MAX_REDIRECTS:
                raise BlocklistError("too_many_redirects")
            current_url = urllib.parse.urljoin(canonical, location)
            redirects += 1
            continue
        if status != 200:
            raise BlocklistError("fetch_failed")
        if len(body) > MAX_RESPONSE_BYTES:
            raise BlocklistError("response_too_large")
        text = body.decode("utf-8-sig", errors="replace")
        prefix = text.lstrip()[:256].lower()
        if prefix.startswith(("<!doctype html", "<html", "<head", "<body")):
            raise BlocklistError("unsupported_feed")
        parsed = _dns_rules.compile_source_text(text, "auto")
        domains = set()
        for raw_domain in parsed:
            try:
                domains.add(_dns_rules.normalize_pattern(raw_domain))
            except ValueError:
                continue
        if not domains:
            raise BlocklistError("empty_feed")
        if len(domains) > MAX_DOMAINS_PER_FEED:
            raise BlocklistError("too_many_domains")
        return domains


class DnsBlocklistManager:
    """Own subscription lifecycle and coordinate bounded feed refreshes."""

    def __init__(self, database: Any,
                 fetcher: Callable[[str], set[str]] = fetch_blocklist_domains):
        self.database = database
        self.fetcher = fetcher
        self._fetch_slots = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        self._capacity_lock = asyncio.Lock()
        self._feed_locks: dict[int, asyncio.Lock] = {}

    def _feed_lock(self, blocklist_id: int) -> asyncio.Lock:
        lock = self._feed_locks.get(blocklist_id)
        if lock is None:
            lock = asyncio.Lock()
            self._feed_locks[blocklist_id] = lock
        return lock

    async def list(self) -> list[dict[str, Any]]:
        return await self.database.list_dns_blocklists()

    async def _validate_url(self, url: str) -> str:
        return await asyncio.to_thread(validate_blocklist_url, url)

    async def _fetch_domains(self, url: str) -> set[str]:
        try:
            async with self._fetch_slots:
                raw_domains = await asyncio.to_thread(self.fetcher, url)
        except BlocklistError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert transport/parser details
            raise BlocklistError("fetch_failed") from exc
        domains = set()
        for raw in raw_domains:
            try:
                domains.add(_dns_rules.normalize_pattern(raw))
            except (TypeError, ValueError):
                continue
        if not domains:
            raise BlocklistError("empty_feed")
        if len(domains) > MAX_DOMAINS_PER_FEED:
            raise BlocklistError("too_many_domains")
        return domains

    async def _ensure_capacity(self, domains: set[str],
                               excluding_id: int | None = None) -> None:
        active = await self.database.count_enabled_dns_blocklist_domains(
            excluding_id=excluding_id)
        additional = await self.database.count_new_enabled_dns_blocklist_domains(
            domains, excluding_id=excluding_id)
        if active + additional > MAX_ACTIVE_DOMAINS:
            raise BlocklistError("capacity_exceeded")

    async def add(self, name: str, url: str
                  ) -> tuple[dict[str, Any], bool]:
        canonical_url = await self._validate_url(url)
        if any(item["url"] == canonical_url for item in await self.list()):
            raise BlocklistError("duplicate_url")
        domains = await self._fetch_domains(canonical_url)
        async with self._capacity_lock:
            await self._ensure_capacity(domains)
            before = await self.database.count_enabled_dns_blocklist_domains()
            display_name = name.strip() if name else ""
            if not display_name:
                display_name = (
                    urllib.parse.urlsplit(canonical_url).hostname or canonical_url)
            try:
                item = await self.database.create_dns_blocklist(
                    display_name, canonical_url, sorted(domains), enabled=True)
            except aiosqlite.IntegrityError as exc:
                raise BlocklistError("duplicate_url") from exc
            after = await self.database.count_enabled_dns_blocklist_domains()
        return item, after != before

    async def refresh(self, blocklist_id: int
                      ) -> tuple[dict[str, Any], bool]:
        async with self._feed_lock(blocklist_id):
            source = await self.database.get_dns_blocklist(blocklist_id)
            if source is None:
                raise BlocklistError("not_found")
            attempted_at = time.time()
            try:
                domains = await self._fetch_domains(source["url"])
                async with self._capacity_lock:
                    current = await self.database.get_dns_blocklist(blocklist_id)
                    if current is None:
                        raise BlocklistError("not_found")
                    if current["enabled"]:
                        await self._ensure_capacity(domains, excluding_id=blocklist_id)
                    changed = await self.database.replace_dns_blocklist_domains(
                        blocklist_id, sorted(domains), attempted_at)
                metadata = await self.database.get_dns_blocklist(blocklist_id)
                if metadata is None:
                    raise BlocklistError("not_found")
                return metadata, changed
            except BlocklistError as exc:
                await self.database.record_dns_blocklist_failure(
                    blocklist_id, attempted_at, exc.code)
                raise
            except Exception as exc:  # noqa: BLE001 - don't expose internal details
                await self.database.record_dns_blocklist_failure(
                    blocklist_id, attempted_at, "fetch_failed")
                raise BlocklistError("fetch_failed") from exc

    async def set_enabled(self, blocklist_id: int, enabled: bool) -> bool:
        async with self._capacity_lock:
            source = await self.database.get_dns_blocklist(blocklist_id)
            if source is None:
                raise BlocklistError("not_found")
            if enabled and not source["enabled"]:
                rows = await self.database.conn.execute_fetchall(
                    "SELECT domain FROM dns_blocklist_domains WHERE blocklist_id=?",
                    (blocklist_id,))
                domains = {row["domain"] for row in rows}
                await self._ensure_capacity(domains, excluding_id=blocklist_id)
            return await self.database.set_dns_blocklist_enabled(
                blocklist_id, enabled)

    async def remove(self, blocklist_id: int) -> bool:
        if await self.database.get_dns_blocklist(blocklist_id) is None:
            raise BlocklistError("not_found")
        return await self.database.delete_dns_blocklist(blocklist_id)

    async def refresh_due(self, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        due = await self.database.list_due_dns_blocklists(
            current_time, REFRESH_INTERVAL_SECONDS)
        if not due:
            return False

        async def refresh_one(source: dict[str, Any]) -> bool:
            try:
                _metadata, changed = await self.refresh(source["id"])
                return changed
            except BlocklistError as exc:
                log.warning("DNS blocklist refresh failed (id=%s, code=%s)",
                            source["id"], exc.code)
                return False

        results: list[bool] = []
        for start in range(0, len(due), MAX_CONCURRENT_FETCHES):
            batch = due[start:start + MAX_CONCURRENT_FETCHES]
            results.extend(await asyncio.gather(
                *(refresh_one(source) for source in batch)))
        return any(results)
