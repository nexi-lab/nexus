"""Lock service protocol — advisory file locking (ops-scenario-matrix §2.2.2, S9).

Defines the contract for advisory lock lifecycle.
Split from the former ``EventsProtocol`` which bundled watching (pub/sub)
and advisory locking (mutex) — fundamentally different subsystems.

Linux analogy: ``flock(2)`` — write/mutex coordination.

Existing implementation: ``nexus.core.nexus_fs_events.NexusFSEventsMixin``.

References:
    - docs/architecture/ops-scenario-matrix.md §2.2.2 (S9)
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class LockProtocol(Protocol):
    """Service contract for advisory file locking (S9).

    Provides advisory lock lifecycle: acquire, extend, release.
    """

    async def lock(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        _context: "OperationContext | None" = None,
    ) -> str | None: ...

    async def extend_lock(
        self,
        lock_id: str,
        path: str,
        ttl: float = 30.0,
        _context: "OperationContext | None" = None,
    ) -> bool: ...

    async def unlock(
        self,
        lock_id: str,
        path: str | None = None,
        _context: "OperationContext | None" = None,
    ) -> bool: ...
