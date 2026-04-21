"""VFS Counting Semaphore with Rust acceleration.

Provides name-addressed counting semaphore with holder tracking, SSOT
max_holders enforcement, TTL expiry, and UUID holder IDs.

Semantics:
    - holder IDs are UUID4 strings
    - first acquirer sets ``max_holders`` (SSOT); mismatch → ``ValueError``
    - TTL: lazy expiry on acquire (evict expired before capacity check)
    - acquire() returns ``str | None`` (holder_id or None on timeout)

Fallback chain:
    1. Rust ``VFSSemaphore`` (via ``nexus_kernel``) — ~200ns per acquire
    2. Python ``PythonVFSSemaphore`` (threading-based) — ~500ns-1us

References:
    - docs/architecture/lock-architecture.md §3.2
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, cast

from nexus.contracts.protocols.semaphore import VFSSemaphoreProtocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal data classes
# ---------------------------------------------------------------------------


class _HolderEntry:
    __slots__ = ("holder_id", "acquired_at_ns", "expires_at_ns")

    def __init__(self, holder_id: str, acquired_at_ns: int, expires_at_ns: int) -> None:
        self.holder_id = holder_id
        self.acquired_at_ns = acquired_at_ns
        self.expires_at_ns = expires_at_ns


class _SemaphoreState:
    __slots__ = ("max_holders", "holders")

    def __init__(self, max_holders: int) -> None:
        self.max_holders = max_holders
        self.holders: dict[str, _HolderEntry] = {}

    def is_empty(self) -> bool:
        return len(self.holders) == 0


# ---------------------------------------------------------------------------
# Python fallback
# ---------------------------------------------------------------------------


class PythonVFSSemaphore:
    """Pure-Python counting semaphore using ``threading.RLock`` + dict."""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._semaphores: dict[str, _SemaphoreState] = {}

        # Metrics
        self._acquire_count = 0
        self._release_count = 0
        self._timeout_count = 0

    # -- helpers -----------------------------------------------------------

    def _evict_expired(self, state: _SemaphoreState, now_ns: int) -> None:
        """Remove holders whose TTL has expired (lazy expiry)."""
        expired = [hid for hid, entry in state.holders.items() if entry.expires_at_ns <= now_ns]
        for hid in expired:
            del state.holders[hid]

    def _try_acquire_once(self, name: str, max_holders: int, ttl_ms: int) -> str | None:
        """Non-blocking single attempt.  Returns holder_id or None."""
        now_ns = time.monotonic_ns()

        with self._mu:
            state = self._semaphores.get(name)

            if state is not None:
                # SSOT: max_holders must match
                if state.max_holders != max_holders:
                    raise ValueError(
                        f"Semaphore {name!r}: max_holders mismatch — "
                        f"existing={state.max_holders}, requested={max_holders}"
                    )
                # Lazy TTL expiry
                self._evict_expired(state, now_ns)
                # Clean up empty
                if state.is_empty():
                    del self._semaphores[name]
                    state = None

            if state is None:
                state = _SemaphoreState(max_holders)
                self._semaphores[name] = state

            # Capacity check
            if len(state.holders) >= state.max_holders:
                return None

            holder_id = str(uuid.uuid4())
            expires_at_ns = now_ns + ttl_ms * 1_000_000
            state.holders[holder_id] = _HolderEntry(holder_id, now_ns, expires_at_ns)
            return holder_id

    # -- public API --------------------------------------------------------

    def acquire(
        self,
        name: str,
        max_holders: int,
        timeout_ms: int = 0,
        ttl_ms: int = 30_000,
    ) -> str | None:
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        holder_id = self._try_acquire_once(name, max_holders, ttl_ms)
        if holder_id is not None:
            self._acquire_count += 1
            return holder_id

        if timeout_ms == 0:
            self._timeout_count += 1
            return None

        deadline = time.monotonic() + timeout_ms / 1000.0
        backoff_s = 0.00005  # 50us

        while True:
            time.sleep(backoff_s)

            holder_id = self._try_acquire_once(name, max_holders, ttl_ms)
            if holder_id is not None:
                self._acquire_count += 1
                return holder_id

            if time.monotonic() >= deadline:
                self._timeout_count += 1
                return None

            backoff_s = min(backoff_s * 2, 0.005)  # cap at 5ms

    def release(self, name: str, holder_id: str) -> bool:
        with self._mu:
            state = self._semaphores.get(name)
            if state is None:
                return False

            if holder_id not in state.holders:
                return False

            del state.holders[holder_id]

            # Clean up empty
            if state.is_empty():
                del self._semaphores[name]

            self._release_count += 1
            return True

    def extend(self, name: str, holder_id: str, ttl_ms: int = 30_000) -> bool:
        now_ns = time.monotonic_ns()
        with self._mu:
            state = self._semaphores.get(name)
            if state is None:
                return False

            entry = state.holders.get(holder_id)
            if entry is None:
                return False

            entry.expires_at_ns = now_ns + ttl_ms * 1_000_000
            return True

    def info(self, name: str) -> dict | None:
        now_ns = time.monotonic_ns()
        with self._mu:
            state = self._semaphores.get(name)
            if state is None:
                return None

            self._evict_expired(state, now_ns)
            if state.is_empty():
                del self._semaphores[name]
                return None

            return {
                "name": name,
                "max_holders": state.max_holders,
                "active_count": len(state.holders),
                "holders": [
                    {
                        "holder_id": e.holder_id,
                        "acquired_at_ns": e.acquired_at_ns,
                        "expires_at_ns": e.expires_at_ns,
                    }
                    for e in state.holders.values()
                ],
            }

    def force_release(self, name: str) -> bool:
        with self._mu:
            state = self._semaphores.get(name)
            if state is None:
                return False

            count = len(state.holders)
            del self._semaphores[name]
            self._release_count += count
            return True

    def stats(self) -> dict:
        with self._mu:
            active_semaphores = len(self._semaphores)
            active_holders = sum(len(s.holders) for s in self._semaphores.values())
        return {
            "acquire_count": self._acquire_count,
            "release_count": self._release_count,
            "timeout_count": self._timeout_count,
            "active_semaphores": active_semaphores,
            "active_holders": active_holders,
        }

    @property
    def active_semaphores(self) -> int:
        with self._mu:
            return len(self._semaphores)


# ---------------------------------------------------------------------------
# Rust wrapper
# ---------------------------------------------------------------------------


# RUST_FALLBACK: VFSSemaphore
class RustVFSSemaphore:
    """Thin wrapper around ``nexus_kernel.VFSSemaphore``."""

    def __init__(self) -> None:
        from nexus_kernel import VFSSemaphore

        self._inner: Any = VFSSemaphore()

    def acquire(
        self,
        name: str,
        max_holders: int,
        timeout_ms: int = 0,
        ttl_ms: int = 30_000,
    ) -> str | None:
        return cast("str | None", self._inner.acquire(name, max_holders, timeout_ms, ttl_ms))

    def release(self, name: str, holder_id: str) -> bool:
        return cast(bool, self._inner.release(name, holder_id))

    def extend(self, name: str, holder_id: str, ttl_ms: int = 30_000) -> bool:
        return cast(bool, self._inner.extend(name, holder_id, ttl_ms))

    def info(self, name: str) -> dict | None:
        return cast("dict | None", self._inner.info(name))

    def force_release(self, name: str) -> bool:
        return cast(bool, self._inner.force_release(name))

    def stats(self) -> dict:
        return cast(dict, self._inner.stats())

    @property
    def active_semaphores(self) -> int:
        return cast(int, self._inner.active_semaphores)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_vfs_semaphore() -> VFSSemaphoreProtocol:
    """Return the best available VFS semaphore.

    Prefers the Rust implementation; falls back to pure Python.
    """
    try:
        sem = RustVFSSemaphore()
        logger.debug("VFS semaphore: Rust (nexus_kernel)")
        return sem
    except (ImportError, Exception) as exc:
        logger.debug("Rust VFS semaphore unavailable (%s), using Python fallback", exc)
        return PythonVFSSemaphore()
