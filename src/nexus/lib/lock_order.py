"""Debug-only lock ordering assertions (Issue #3392).

**DEBUG TOOL ONLY** — for detecting potential deadlocks during development/CI.
Production: ALWAYS disabled (zero overhead). Enable only for debugging.

Tracks lock acquisition per-task (asyncio) or per-thread and asserts
that the global lock ordering (L1 → L2 → L3 → L4) is never violated.

Inspired by DFUSE (arXiv:2503.18191) §4.2: deadlock from reversed lock
ordering in distributed filesystem I/O. Correct lock design avoids
deadlock; this module is an additional safety net for verification.

Activation:
    NEXUS_DEBUG_LOCK_ORDER=1  — enable lock ordering assertions
    (Default: disabled — zero overhead in production)

Lock layers:
    L1 = VFS I/O locks      (Rust kernel LockManager, blocking_acquire/do_release)
    L2 = Advisory/Raft locks (Rust kernel LockManager, sys_lock/sys_unlock)
    L3 = asyncio primitives  (pipes, streams)
    L4 = threading locks     (semaphore._mu)

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

LOCK_DEBUG_ENABLED: bool = os.environ.get("NEXUS_DEBUG_LOCK_ORDER", "").lower() in (
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

# ContextVar tracks lock layer *counts* held by the current asyncio task.
# dict[layer, count] — supports multiple acquisitions of the same layer.
# Falls back to thread-local for synchronous callers.
_held_layers: ContextVar[dict[int, int]] = ContextVar("_held_layers")

# Observer depth counter: incremented on enter, decremented on exit.
# Positive value means we're inside an observer callback. Using a counter
# instead of a boolean makes nested observer dispatch safe.
_observer_depth: ContextVar[int] = ContextVar("_observer_depth", default=0)

# Thread-local fallback for synchronous code paths.
# Using a dict directly instead of threading.local() for proper typing.
_thread_held: dict[int, dict[int, int]] = {}  # thread_id -> layer counts


def _get_held() -> dict[int, int]:
    """Get the lock layer counts held by the current task/thread."""
    try:
        return _held_layers.get()
    except LookupError:
        # No asyncio task context — use thread-local via thread id.
        tid = threading.get_ident()
        if tid not in _thread_held:
            _thread_held[tid] = {}
        return _thread_held[tid]


def _set_held(layers: dict[int, int]) -> None:
    """Set the held layers for the current task/thread."""
    try:
        _held_layers.set(layers)
    except LookupError:
        _thread_held[threading.get_ident()] = layers


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
    if _observer_depth.get(0) > 0 and layer <= L2_ADVISORY:
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

    Increments the hold count for *layer* (supports multiple acquisitions).
    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    held = _get_held()
    held[layer] = held.get(layer, 0) + 1
    _set_held(held)


def mark_released(layer: int) -> None:
    """Record that the current task/thread released one hold of *layer*.

    Decrements the hold count; only removes the layer when count reaches zero.
    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    held = _get_held()
    count = held.get(layer, 0)
    if count <= 1:
        held.pop(layer, None)
    else:
        held[layer] = count - 1
    _set_held(held)


def enter_observer_context() -> None:
    """Tag the current task as running inside an observer callback.

    Uses a depth counter so nested observer dispatch is safe — each
    ``enter`` must be paired with an ``exit``.

    Called by ``KernelDispatch.notify()`` before dispatching to observers.
    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    _observer_depth.set(_observer_depth.get(0) + 1)


def exit_observer_context() -> None:
    """Decrement the observer depth counter for the current task.

    Only clears the observer state when the outermost callback exits.
    No-op when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return
    depth = _observer_depth.get(0)
    _observer_depth.set(max(0, depth - 1))


def is_observer_context() -> bool:
    """Return True if the current task is inside an observer callback.

    Always returns False when ``LOCK_DEBUG_ENABLED`` is False.
    """
    if not LOCK_DEBUG_ENABLED:
        return False
    return _observer_depth.get(0) > 0
