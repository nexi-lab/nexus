"""Debug-mode lock ordering assertions (Issue #3392).

Tracks lock acquisition per-task (asyncio) or per-thread and asserts
that the global lock ordering (L1 → L2 → L3 → L4) is never violated.

Inspired by DFUSE (arXiv:2503.18191) §4.2: deadlock from reversed lock
ordering in distributed filesystem I/O. This module detects violations
at runtime so they surface during development/CI instead of production.

Activation:
    NEXUS_LOCK_DEBUG=1  — enable lock ordering assertions
    (Default: disabled — zero overhead in production)

Lock layers:
    L1 = VFS I/O locks      (core/lock_fast.py)
    L2 = Advisory/Raft locks (lib/distributed_lock.py, raft/lock_manager.py)
    L3 = asyncio primitives  (pipes, streams)
    L4 = threading locks     (file_watcher._waiters_lock, semaphore._mu)

Rules enforced:
    1. A task holding L2 must not acquire L1.
    2. A task tagged as observer must not acquire L1 or L2.

See: docs/architecture/LOCK-ORDERING.md
"""

from __future__ import annotations

import logging
import os
import threading
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Activation gate
# ---------------------------------------------------------------------------

LOCK_DEBUG_ENABLED: bool = os.environ.get("NEXUS_LOCK_DEBUG", "").lower() in (
    "1",
    "true",
    "yes",
)

# ---------------------------------------------------------------------------
# Lock layer constants
# ---------------------------------------------------------------------------

L1_VFS = 1  # VFS I/O locks
L2_ADVISORY = 2  # Advisory / Raft locks
L3_ASYNCIO = 3  # asyncio primitives
L4_THREADING = 4  # threading locks

_LAYER_NAMES = {
    L1_VFS: "L1:VFS",
    L2_ADVISORY: "L2:Advisory",
    L3_ASYNCIO: "L3:asyncio",
    L4_THREADING: "L4:threading",
}

# ---------------------------------------------------------------------------
# Per-task / per-thread state
# ---------------------------------------------------------------------------

# ContextVar tracks lock layers held by the current asyncio task.
# Falls back to thread-local for synchronous callers.
_held_layers: ContextVar[set[int]] = ContextVar("_held_layers")

# Observer tagging: set to True in tasks spawned by KernelDispatch.notify().
_in_observer: ContextVar[bool] = ContextVar("_in_observer", default=False)

# Thread-local fallback for synchronous code paths.
_thread_state = threading.local()


def _get_held() -> set[int]:
    """Get the set of lock layers held by the current task/thread."""
    try:
        return _held_layers.get()
    except LookupError:
        # No asyncio task context — use thread-local.
        if not hasattr(_thread_state, "held"):
            _thread_state.held = set()
        return _thread_state.held


def _set_held(layers: set[int]) -> None:
    """Set the held layers for the current task/thread."""
    try:
        _held_layers.set(layers)
    except LookupError:
        _thread_state.held = layers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class LockOrderError(RuntimeError):
    """Raised when lock ordering is violated in debug mode."""


def assert_can_acquire(layer: int) -> None:
    """Assert that acquiring *layer* does not violate ordering.

    Rules:
        1. Must not acquire a lower-numbered layer while holding a higher one.
           (e.g. acquiring L1 while holding L2 is forbidden)
        2. Observer tasks must not acquire L1 or L2.

    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return

    # Rule 2: observer context check.
    if _in_observer.get(False) and layer <= L2_ADVISORY:
        layer_name = _LAYER_NAMES.get(layer, f"L{layer}")
        raise LockOrderError(
            f"Lock ordering violation: observer task attempted to acquire "
            f"{layer_name}. Observers (OBSERVE phase) must not acquire VFS "
            f"or advisory locks — this would create the DFUSE deadlock pattern. "
            f"See docs/architecture/LOCK-ORDERING.md §3.2."
        )

    # Rule 1: layer ordering check.
    held = _get_held()
    for h in held:
        if h > layer:
            held_name = _LAYER_NAMES.get(h, f"L{h}")
            layer_name = _LAYER_NAMES.get(layer, f"L{layer}")
            raise LockOrderError(
                f"Lock ordering violation: attempted to acquire {layer_name} "
                f"while holding {held_name}. Global ordering requires "
                f"L1 → L2 → L3 → L4 (never reverse). "
                f"See docs/architecture/LOCK-ORDERING.md §3."
            )


def mark_acquired(layer: int) -> None:
    """Record that the current task/thread now holds *layer*.

    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    held = _get_held()
    new = held | {layer}
    _set_held(new)


def mark_released(layer: int) -> None:
    """Record that the current task/thread released *layer*.

    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    held = _get_held()
    new = held - {layer}
    _set_held(new)


def enter_observer_context() -> None:
    """Tag the current task as running inside an observer callback.

    Called by ``KernelDispatch.notify()`` before dispatching to observers.
    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    _in_observer.set(True)


def exit_observer_context() -> None:
    """Clear the observer tag for the current task.

    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    _in_observer.set(False)


def is_observer_context() -> bool:
    """Return True if the current task is tagged as an observer.

    Always returns False when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return False
    return _in_observer.get(False)
