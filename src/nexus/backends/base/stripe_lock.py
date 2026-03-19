"""Stripe lock — lightweight in-memory coordination for CAS metadata updates.

A fixed-size array of threading.Lock objects indexed by content hash.
Provides per-hash coordination for metadata read-modify-write cycles
without any disk I/O. Much cheaper than FileLock (~us vs ~ms).

Used by CASAddressingEngine and CASLocalBackend for ref_count updates.
"""

from __future__ import annotations

import threading

_NUM_STRIPES = 64  # power of 2 for fast modulo


class _StripeLock:
    """Fixed-size array of threading.Lock objects indexed by hash.

    Provides per-hash coordination for metadata read-modify-write cycles
    without any disk I/O. Much cheaper than FileLock (~μs vs ~ms).
    """

    __slots__ = ("_contention_count", "_locks")

    def __init__(self, num_stripes: int = _NUM_STRIPES) -> None:
        self._locks = [threading.Lock() for _ in range(num_stripes)]
        self._contention_count = 0

    def acquire_for(self, content_hash: str) -> "threading.Lock":
        """Return the stripe lock for a given content hash (not acquired)."""
        # Use last 4 hex chars for even distribution
        idx = int(content_hash[-4:], 16) % len(self._locks)
        return self._locks[idx]

    @property
    def contention_count(self) -> int:
        """Number of times a stripe lock was already held on acquire attempt."""
        return self._contention_count

    def acquire_with_contention_tracking(self, content_hash: str) -> "threading.Lock":
        """Acquire stripe lock and track contention (Issue #1752)."""
        lock = self.acquire_for(content_hash)
        if not lock.acquire(blocking=False):
            self._contention_count += 1
            lock.acquire()
        return lock
