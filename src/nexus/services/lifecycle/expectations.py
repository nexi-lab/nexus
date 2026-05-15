"""Expectations tracker for safe async convergence (Issue #2067).

Kubernetes-inspired ``ControllerExpectations`` pattern: atomic counters + TTL
expiration to prevent duplicate mount/unmount operations during the race window
between "action requested" and "action observed in next snapshot."

Internal utility for the reconciler.

References:
    - Issue #2067: Agent warm-up phase / expectations tracker
    - kubernetes/kubernetes pkg/controller/controller_utils.go
"""

import logging
import threading
import time
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExpectationEntry:
    """Immutable snapshot of pending expectations for a single brick.

    Attributes:
        key: Brick name (registry key).
        pending_mounts: Number of expected but unobserved mount operations.
        pending_unmounts: Number of expected but unobserved unmount operations.
        created_at: Monotonic timestamp when this entry was created/updated.
    """

    key: str
    pending_mounts: int = 0
    pending_unmounts: int = 0
    created_at: float = 0.0


class Expectations:
    """Thread-safe expectations tracker with lazy TTL expiration.

    Prevents duplicate reconciler actions by tracking pending operations
    and gating per-brick processing until expectations are satisfied
    (all observed) or expired (TTL).

    Thread safety: single ``threading.Lock`` guards all mutations.
    Safe for both sync and async callers (PEP 703 future-proof).
    """

    _DEFAULT_TTL: float = 300.0  # 5 minutes

    def __init__(self, *, ttl: float = _DEFAULT_TTL) -> None:
        self._store: dict[str, ExpectationEntry] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    # ------------------------------------------------------------------
    # Expect operations
    # ------------------------------------------------------------------

    def expect_mount(self, key: str) -> None:
        """Record that a mount operation has been requested for *key*."""
        with self._lock:
            self._store[key] = ExpectationEntry(
                key=key,
                pending_mounts=1,
                pending_unmounts=0,
                created_at=time.monotonic(),
            )

    def expect_unmount(self, key: str) -> None:
        """Record that an unmount operation has been requested for *key*."""
        with self._lock:
            self._store[key] = ExpectationEntry(
                key=key,
                pending_mounts=0,
                pending_unmounts=1,
                created_at=time.monotonic(),
            )

    # ------------------------------------------------------------------
    # Observe operations
    # ------------------------------------------------------------------

    def mount_observed(self, key: str) -> None:
        """Record that a previously expected mount has completed for *key*."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return  # No expectation — noop
            new_mounts = max(0, entry.pending_mounts - 1)
            if new_mounts == 0 and entry.pending_unmounts <= 0:
                del self._store[key]
            else:
                self._store[key] = replace(entry, pending_mounts=new_mounts)

    def unmount_observed(self, key: str) -> None:
        """Record that a previously expected unmount has completed for *key*."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return  # No expectation — noop
            new_unmounts = max(0, entry.pending_unmounts - 1)
            if entry.pending_mounts <= 0 and new_unmounts == 0:
                del self._store[key]
            else:
                self._store[key] = replace(entry, pending_unmounts=new_unmounts)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def satisfied(self, key: str) -> bool:
        """Check whether expectations for *key* are satisfied.

        Returns ``True`` if:
        - No expectations exist for *key*, OR
        - All pending counts are <= 0, OR
        - The entry has expired (TTL exceeded) — logs a warning.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return True
            if entry.pending_mounts <= 0 and entry.pending_unmounts <= 0:
                return True
            # Lazy TTL check
            if time.monotonic() - entry.created_at > self._ttl:
                logger.warning(
                    "[EXPECTATIONS] TTL expired for %r (pending_mounts=%d, pending_unmounts=%d)",
                    key,
                    entry.pending_mounts,
                    entry.pending_unmounts,
                )
                del self._store[key]
                return True
            return False

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def expire_stale(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            stale_keys = [k for k, v in self._store.items() if now - v.created_at > self._ttl]
            for k in stale_keys:
                del self._store[k]
                removed += 1
        return removed

    @property
    def pending_keys(self) -> frozenset[str]:
        """Return snapshot of keys with pending expectations (for observation scan)."""
        with self._lock:
            return frozenset(self._store)

    def __len__(self) -> int:
        """Number of tracked entries."""
        with self._lock:
            return len(self._store)
