"""Startup phase tracker for Kubernetes-style health probes (#2168).

Thread-safe, zero-I/O tracker that records which lifespan phases have
completed.  Used by the ``/healthz/*`` probe endpoints to distinguish
between "process alive", "can serve traffic", and "fully booted".
"""

import logging
import threading
import time
from enum import StrEnum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phases — one per lifespan initializer, in boot order
# ---------------------------------------------------------------------------


class StartupPhase(StrEnum):
    """Lifespan startup phases (matches lifespan/__init__.py order)."""

    OBSERVABILITY = "observability"
    FEATURES = "features"
    PERMISSIONS = "permissions"
    REALTIME = "realtime"
    SEARCH = "search"
    SERVICES = "services"
    UPLOADS = "uploads"
    GRPC = "grpc"


_ALL_PHASES: frozenset[StartupPhase] = frozenset(StartupPhase)

# Minimum phases required before the server should accept traffic.
_REQUIRED_FOR_READY: frozenset[StartupPhase] = frozenset(
    {
        StartupPhase.OBSERVABILITY,
        StartupPhase.FEATURES,
        StartupPhase.PERMISSIONS,
        StartupPhase.SERVICES,
        StartupPhase.GRPC,
    }
)

# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class StartupTracker:
    """Thread-safe, in-memory startup phase tracker.

    >>> tracker = StartupTracker()
    >>> tracker.complete(StartupPhase.OBSERVABILITY)
    >>> tracker.is_ready
    False
    """

    __slots__ = ("_completed", "_lock", "_start_time")

    def __init__(self) -> None:
        self._completed: set[StartupPhase] = set()
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    # -- mutators ----------------------------------------------------------

    def complete(self, phase: StartupPhase) -> None:
        """Mark *phase* as completed (idempotent)."""
        with self._lock:
            self._completed.add(phase)
        logger.info("[STARTUP] Phase completed: %s", phase.value)

    # -- queries -----------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """All phases finished."""
        with self._lock:
            return self._completed >= _ALL_PHASES

    @property
    def is_ready(self) -> bool:
        """Required phases finished — server can accept traffic."""
        with self._lock:
            return self._completed >= _REQUIRED_FOR_READY

    @property
    def completed_phases(self) -> frozenset[StartupPhase]:
        with self._lock:
            return frozenset(self._completed)

    @property
    def pending_phases(self) -> frozenset[StartupPhase]:
        with self._lock:
            return _ALL_PHASES - self._completed

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time
