"""Zone lifecycle protocol and data models (Issue #2061).

Defines the contract for zone deprovisioning — a Kubernetes-inspired
finalizer pattern where zones enter ``Terminating`` phase, registered
finalizers run ordered cleanup, and the zone enters ``Terminated``
when all finalizers complete.

Storage Affinity: ``phase`` + ``finalizers`` columns on ``ZoneModel``.

References:
    - services/protocols/brick_lifecycle.py (frozen dataclass models, StrEnum)
    - LEGO §3.3 (Protocol for brick interfaces)
    - Issue #2061: Zone finalizer protocol for ordered cleanup
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Well-known finalizer keys (used for ordering in ZoneLifecycleService)
REBAC_FINALIZER_KEY: str = "nexus.core/rebac"


class ZonePhase(StrEnum):
    """Zone lifecycle phase.

    Transition diagram::

        Active ──► Terminating ──► Terminated
    """

    ACTIVE = "Active"
    TERMINATING = "Terminating"
    TERMINATED = "Terminated"


@runtime_checkable
class ZoneFinalizerProtocol(Protocol):
    """A service that must clean up before zone deletion completes.

    Each finalizer has a unique ``finalizer_key`` (e.g. ``nexus.core/cache``)
    and a ``finalize_zone()`` coroutine that performs cleanup.  Raises on
    failure so the orchestrator can record the error and retry later.
    """

    @property
    def finalizer_key(self) -> str:
        """Unique key identifying this finalizer (e.g. ``nexus.core/cache``)."""
        ...

    async def finalize_zone(self, zone_id: str) -> None:
        """Clean up all resources owned by *zone_id*.

        MUST be idempotent: calling this method multiple times with the
        same *zone_id* must be safe (e.g. ``DELETE ... WHERE zone_id = ?``
        is inherently idempotent).  The orchestrator retries failed
        finalizers on subsequent deprovision attempts.

        Raises:
            Exception: On failure — orchestrator records error, retries later.
        """
        ...


@dataclass(frozen=True, slots=True)
class ZoneLifecycleStatus:
    """Snapshot of a zone's lifecycle state during deprovisioning."""

    zone_id: str
    phase: ZonePhase
    finalizers: tuple[str, ...]
    errors: dict[str, str]  # finalizer_key → error message


@dataclass(frozen=True, slots=True)
class ZoneDeprovisionResult:
    """Result of a zone deprovision request."""

    zone_id: str
    phase: ZonePhase
    finalizers_completed: tuple[str, ...]
    finalizers_pending: tuple[str, ...]
    finalizers_failed: dict[str, str]  # finalizer_key → error message
