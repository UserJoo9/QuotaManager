"""Shared keyed attempt state used by authentication and voucher throttles."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field


@dataclass
class AttemptBucket:
    failures: int = 0
    timestamps: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0


class AttemptBuckets:
    """LRU storage for per-key limiter state; policy stays with each caller."""

    def __init__(self, max_entries: int | None = None) -> None:
        self._max_entries = max_entries
        self._states: OrderedDict[str, AttemptBucket] = OrderedDict()

    def get(self, key: str, *, create: bool = False) -> AttemptBucket | None:
        state = self._states.get(key)
        if state is None and create:
            state = AttemptBucket()
            self._states[key] = state
            if self._max_entries is not None:
                while len(self._states) > max(1, self._max_entries):
                    self._states.popitem(last=False)
        elif state is not None:
            self._states.move_to_end(key)
        return state

    def discard(self, key: str) -> None:
        self._states.pop(key, None)

    def count(self, key: str) -> int:
        state = self._states.get(key)
        return state.failures if state is not None else 0
