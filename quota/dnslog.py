"""Per-device DNS browsing history — dnsmasq query-log tailer (Linux).

Every client resolves through the box's dnsmasq (``dhcp-option=6`` = the box),
so ``log-queries=extra`` captures one line per query with the requestor IP on
it. The verbose extra form stamps the client's ip/port after the serial::

    Aug 10 00:00:54 dnsmasq[862442]: 1 192.168.2.186/16773 query[A] example.com from 192.168.2.186

(simple ``query[A] example.com from 192.168.2.100`` lines are accepted too).

The setup script installs an app-owned dnsmasq fragment
(/etc/dnsmasq.d/quota-dnslog.conf) pointing ``log-facility`` at a dedicated
file, and logrotate bounds that file. This module tails it on a DEDICATED
thread — never the event loop — and pushes parsed ``(minute, ip, domain)``
events onto a bounded queue the maintenance loop drains in batches into
``dns_history``.

Why the thread exists:

* File I/O (stat + read) must not touch the asyncio loop — a slow disk or a
  logrotate cycle must never stall the 5 s dashboard push or DNS itself.
* The queue is bounded and drops on overflow (``put_nowait`` /
  ``except queue.Full``), the same shape as the app's own logging
  (``core/logging_setup.py``) and dnsmasq's ``log-async`` queue — on extreme
  load lines are dropped, never does a slow consumer block DNS.
* ``copytruncate`` + ``log-async`` can leave a sparse NUL hole: dnsmasq keeps
  its own write offset after a truncate, so the tailer strips ``\\x00`` and
  caps the partial-line buffer so a hole never reads as one giant line.
* First start with no persisted state seeks to EOF — pre-feature lines are
  never attributed to a device (upgrade-safe).

Rotation handling: an inode change (logrotate ``create``/rename mode) or a
size shrink (``copytruncate``) both reset the cursor to 0 and keep reading —
the tailer survives a logrotate cycle without double-counting or wedging.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import queue
import re
import threading
from typing import Callable, NamedTuple

log = logging.getLogger("quota.dnslog")

#: Local minute bucket (lexicographic == chronological, so the TTL prune and
#: the "since this minute" queries both work as plain string comparisons).
MINUTE_FMT = "%Y-%m-%d %H:%M"
#: A copytruncate NUL hole reads as one giant line; cap the partial-line buffer
#: so a hole can't balloon memory. Reset, not kept, once exceeded.
MAX_PARTIAL_LINE = 1024 * 1024


class ParsedQuery(NamedTuple):
    """One dnsmasq query line, stamped with the minute bucket it belongs to."""

    minute: str  # local "%Y-%m-%d %H:%M"
    ip: str      # requestor IP (the lease-joined device at drain time)
    domain: str  # lowercased hostname (reverse-pointer names filtered out)


# dnsmasq log-queries=extra: the line may carry an optional syslog-style
# "dnsmasq[pid]:" prefix and/or a leading serial number (newer dnsmasq) and,
# in the verbose "extra" form, the client's own ip/port between the serial
# and `query[` (e.g. "1 192.168.2.186/16773 query[A] ..."). Then
# `query[TYPE] name from <ip>`. File-mode lines (log-facility) carry no
# timestamp — the minute is stamped at read time. Tolerant to all shapes.
_QUERY_RE = re.compile(
    r"^(?:(?:(?:[A-Za-z]{3}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2})\s+)?"
    r"(?:dnsmasq\[\d+\]:\s*)?)?"
    r"(?:(?:\d+\s+)?(?:[0-9A-Fa-f:.]+/\d+\s+)?)?query\[([A-Z0-9]+)\]\s+"
    r"([^\s]+)\s+from\s+([0-9A-Fa-f:.]+)\s*$")
#: Reverse-pointer names are DNS housekeeping (IP -> name lookups), not
#: browsing — filtered out so the history stays readable.
_PTR_RE = re.compile(r"\.(?:in-addr|ip6)\.arpa\.?$", re.IGNORECASE)


def parse_dnslog_line(line: str,
                      now_fn: Callable[[], _dt.datetime] | None = None
                      ) -> ParsedQuery | None:
    """Parse one dnsmasq query-log line into a ``(minute, ip, domain)`` event.

    Returns ``None`` for anything that is not a ``query[...] ... from <ip>``
    line (forwarded/reply/cached/config/DHCP/overflow — those never start with
    ``query[``, so the regex simply does not match) and for reverse-pointer
    lookups. ``minute`` is stamped from ``now_fn`` (injectable for tests)
    because file-mode lines carry no timestamp of their own.
    """
    m = _QUERY_RE.match(line.strip())
    if not m:
        return None
    domain = m.group(2).rstrip(".").lower()
    if _PTR_RE.search(domain):
        return None
    now = now_fn() if now_fn else _dt.datetime.now().astimezone()
    return ParsedQuery(minute=now.strftime(MINUTE_FMT), ip=m.group(3),
                       domain=domain)


class DnslogTailer:
    """Background thread that tails dnsmasq's query log into a bounded queue.

    Construction is cheap and opens nothing; call :meth:`start` once after the
    DB is up and :meth:`stop` on shutdown. The maintenance loop calls
    :meth:`drain_events` each tick (non-blocking, empty when nothing new) and
    :meth:`state_snapshot` to persist the read cursor for restart-resume.
    A missing log file is not an error — the thread keeps polling (the app
    degrades gracefully when the dnsmasq fragment is not installed yet).
    """

    #: poll interval between read passes (seconds)
    POLL_INTERVAL = 0.5
    #: bounded queue — drops on overflow (never blocks the thread or DNS)
    QUEUE_SIZE = 2000

    def __init__(self, path: str, resume: dict[str, object] | None = None,
                 now_fn: Callable[[], _dt.datetime] | None = None) -> None:
        self.path = path
        #: persisted read cursor {inode, offset}; a first start without one
        #: seeks to EOF so pre-feature lines are never attributed.
        self._resume = resume or {}
        self._now_fn = now_fn
        self.q: queue.Queue[ParsedQuery] = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: live read cursor — updated each pass, exposed via state_snapshot()
        self._inode: int | None = None
        self._offset: int = 0
        #: bytes read but not yet split on a newline (kept across passes)
        self._partial = ""
        #: True while the poll loop is alive (so the tick skips a stopped
        #: or never-started tailer)
        self.running = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="dnslog-tailer",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        self.running = True
        try:
            while not self._stop.is_set():
                try:
                    self._read_pass()
                except OSError as exc:
                    # Missing/unreadable log file: keep polling. Silent while no
                    # cursor was ever established (the dnsmasq fragment may not
                    # be installed yet); warn once a previously-working tail
                    # breaks (e.g. the file was unlinked out from under us).
                    if self._inode is not None:
                        log.warning("dnslog tail: cannot read %s: %s",
                                    self.path, exc)
                self._stop.wait(self.POLL_INTERVAL)
        finally:
            self.running = False

    def _read_pass(self) -> None:
        """One read: stat, reconcile rotation, read new bytes, parse lines."""
        st = os.stat(self.path)
        if self._inode is None:
            # No cursor yet — establish the starting point.
            if self._resume:
                # Resume from the persisted cursor (never past the current
                # EOF). If the file rotated while the app was stopped, the old
                # cursor is meaningless but a fresh file starts empty anyway.
                self._inode = int(self._resume.get("inode") or 0)
                self._offset = int(self._resume.get("offset") or 0)
                if self._inode != st.st_ino:
                    log.info("dnslog tail: %s rotated while stopped", self.path)
                    self._inode = st.st_ino
                    self._offset = 0
                self._offset = min(self._offset, st.st_size)
            else:
                # Tail semantics: pre-feature lines are never attributed.
                self._inode = st.st_ino
                self._offset = st.st_size
                return
        elif self._inode != st.st_ino:
            # logrotate create/rename mode: a fresh file — restart from 0.
            log.info("dnslog tail: %s rotated (new inode %s); restarting from 0",
                     self.path, st.st_ino)
            self._inode = st.st_ino
            self._offset = 0
            self._partial = ""
        elif self._offset > st.st_size:
            # copytruncate: the file shrank under us — restart from 0.
            log.info("dnslog tail: %s truncated (%s -> %s); restarting from 0",
                     self.path, self._offset, st.st_size)
            self._offset = 0
            self._partial = ""

        if self._offset >= st.st_size:
            return  # nothing new since the last pass

        with open(self.path, "rb") as fh:
            fh.seek(self._offset)
            raw = fh.read()
            self._offset = fh.tell()

        # copytruncate + log-async can leave a sparse NUL hole at the old
        # write offset; strip it so a hole can't join two lines into one.
        text = raw.decode("utf-8", errors="replace").replace("\x00", "")
        self._partial += text
        if len(self._partial) > MAX_PARTIAL_LINE:
            log.warning("dnslog tail: partial-line buffer exceeded %s B — "
                        "discarding (possible NUL hole)", MAX_PARTIAL_LINE)
            self._partial = ""
            return
        lines = self._partial.split("\n")
        self._partial = lines.pop()  # trailing partial line, kept for next pass
        for line in lines:
            ev = parse_dnslog_line(line, self._now_fn)
            if ev is None:
                continue
            try:
                self.q.put_nowait(ev)
            except queue.Full:
                log.warning("dnslog tail: queue full — dropping DNS history "
                            "events (bounded loss, DNS unaffected)")
                return

    # -- consumption --------------------------------------------------------

    def drain_events(self) -> list[ParsedQuery]:
        """Non-blocking drain of every event queued since the last call."""
        out: list[ParsedQuery] = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def state_snapshot(self) -> dict[str, object]:
        """The read cursor, for persisting between restarts."""
        return {"inode": self._inode, "offset": self._offset}
