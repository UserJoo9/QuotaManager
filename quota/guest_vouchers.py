"""Guest voucher code generation and failed-redemption throttling."""

from __future__ import annotations

import math
import secrets
import time
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from core.attempt_limiter import AttemptBucket, AttemptBuckets

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = 4
_CODE_GROUP_LENGTH = 4


def generate_voucher_code() -> str:
    """Return a 16-character, 80-bit, human-readable voucher code."""
    chars = [secrets.choice(_CODE_ALPHABET)
             for _ in range(_CODE_GROUPS * _CODE_GROUP_LENGTH)]
    groups = ["".join(chars[i:i + _CODE_GROUP_LENGTH])
              for i in range(0, len(chars), _CODE_GROUP_LENGTH)]
    return "-".join(groups)


class VoucherAttemptLimiter:
    """Bounded rolling invalid-code window keyed by normalized device MAC."""

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 threshold: int = 5, window_seconds: int = 900,
                 cooldown_seconds: int = 900, max_devices: int = 4096) -> None:
        self._clock = clock or time.monotonic
        self._threshold = max(1, int(threshold))
        self._window_seconds = max(1, int(window_seconds))
        self._cooldown_seconds = max(1, int(cooldown_seconds))
        self._max_devices = max(1, int(max_devices))
        self._attempts = AttemptBuckets(self._max_devices)
        self._locks: dict[str, asyncio.Lock] = {}
        # Count current holders and waiters so LRU pressure can never replace a
        # lock between release and a queued same-MAC request acquiring it.
        self._lock_users: dict[str, int] = {}

    @asynccontextmanager
    async def serialize(self, mac: str) -> AsyncIterator[None]:
        """Serialize redemption decisions for one normalized device MAC."""
        key = str(mac or "").strip().lower()
        if not key:
            yield
            return
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._lock_users[key] - 1
            if remaining:
                self._lock_users[key] = remaining
            else:
                self._lock_users.pop(key, None)
                if self._locks.get(key) is lock:
                    self._locks.pop(key, None)

    def retry_after(self, mac: str) -> int:
        """Return remaining cooldown seconds, or zero when redemption may run."""
        key = str(mac or "").strip().lower()
        if not key:
            return 0
        state = self._attempts.get(key)
        if state is None:
            return 0
        now = self._clock()
        if state.blocked_until > now:
            self._attempts.get(key)
            return max(1, math.ceil(state.blocked_until - now))
        if state.blocked_until:
            state.blocked_until = 0.0
            state.timestamps.clear()
        self._prune(state, now)
        if not state.timestamps:
            self._attempts.discard(key)
        else:
            self._attempts.get(key)
        return 0

    def record_invalid(self, mac: str) -> None:
        """Record one invalid/expired/revoked/used-code submission."""
        key = str(mac or "").strip().lower()
        if not key:
            return
        now = self._clock()
        state = self._attempts.get(key, create=True)
        if state is None:
            return
        if state.blocked_until > now:
            return
        self._prune(state, now)
        state.timestamps.append(now)
        if len(state.timestamps) >= self._threshold:
            state.blocked_until = now + self._cooldown_seconds
            state.timestamps.clear()
        self._attempts.get(key)

    def _prune(self, state: AttemptBucket, now: float) -> None:
        cutoff = now - self._window_seconds
        while state.timestamps and state.timestamps[0] <= cutoff:
            state.timestamps.popleft()
