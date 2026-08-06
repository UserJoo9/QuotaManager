"""Non-blocking logging.

Disk I/O must never block the packet engine thread or the asyncio event loop,
so all log records are pushed onto a queue that a single writer thread drains
into a rotating file (and the console). Handlers are attached once; subsequent
calls are no-ops so module reloads don't stack duplicate writers.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
from pathlib import Path

_LOGGER_WRITER_THREAD: threading.Thread | None = None

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that never blocks: drops records when the queue is full.

    A blocking ``put()`` would stall the asyncio event loop the moment a burst
    of DEBUG/INFO records filled the
    5000-record queue — the exact failure the non-blocking design exists to
    prevent. Under pressure it is better to drop a log line than to drop
    packets or freeze the dashboard.
    """

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass  # best-effort: drop rather than block


def setup_logging(
    level: int = logging.INFO,
    log_file: str | Path | None = "logs/quota.log",
    console_level: int = logging.INFO,
) -> queue.Queue[logging.LogRecord]:
    """Install a queue-based, non-blocking root logger.

    Returns the queue. Call once at startup.
    """
    global _LOGGER_WRITER_THREAD
    root = logging.getLogger()
    # Idempotent: if we already installed a queue handler, don't stack a second.
    if any(isinstance(h, logging.handlers.QueueHandler) for h in root.handlers):
        for h in root.handlers:
            if isinstance(h, logging.handlers.QueueHandler):
                return h.queue  # type: ignore[no-any-return]

    log_q: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=5000)
    qh = _NonBlockingQueueHandler(log_q)
    root.addHandler(qh)
    root.setLevel(level)

    if _LOGGER_WRITER_THREAD is None or not _LOGGER_WRITER_THREAD.is_alive():
        target = _writer_loop
        _LOGGER_WRITER_THREAD = threading.Thread(
            target=target, args=(log_q, log_file, console_level), name="quota-log", daemon=True
        )
        _LOGGER_WRITER_THREAD.start()

    return log_q


def _writer_loop(
    log_q: queue.Queue[logging.LogRecord],
    log_file: str | Path | None,
    console_level: int,
) -> None:
    """Drain the queue into rotating file + console handlers."""
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    console_errors = logging.StreamHandler()
    console_errors.setLevel(logging.ERROR)
    console_errors.setFormatter(logging.Formatter(LOG_FORMAT))

    # Simpler: one rotating file handler (if configured) + one console handler.
    file_handler = None
    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        except OSError as exc:
            # An unwritable log dir (run as non-root while logs/ is owned by
            # root, or a read-only FS) must NOT kill the writer thread — that
            # would silently stop ALL logging. Fall back to console-only and
            # say so. Emit straight to the console handler to avoid re-queuing
            # this record (the root QueueHandler would loop it back here).
            console.emit(logging.LogRecord(
                "quota.logging", logging.ERROR, __file__, 0,
                f"cannot open log file {log_file}: {exc} — logging to console "
                "only", (), None))

    while True:
        try:
            record = log_q.get(timeout=0.5)
        except queue.Empty:
            continue
        # Route the record to the writer's own handlers (the queue handler on
        # the root is intentionally skipped to avoid recursion).
        if file_handler is not None and record.levelno >= file_handler.level:
            file_handler.emit(record)
        if record.levelno >= console.level:
            console.emit(record)
        if record.levelno >= console_errors.level:
            console_errors.emit(record)
