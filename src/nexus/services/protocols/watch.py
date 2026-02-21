"""Watch service protocol — file change notification (ops-scenario-matrix §2.2.2, S8).

Defines the contract for long-polling file system changes.
Split from the former ``EventsProtocol`` which bundled watching (pub/sub)
and advisory locking (mutex) — fundamentally different subsystems.

Linux analogy: ``inotify(7)`` — read-only, pub/sub observation.

Existing implementation: ``nexus.core.nexus_fs_events.NexusFSEventsMixin``.

References:
    - docs/architecture/ops-scenario-matrix.md §2.2.2 (S8)
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class WatchProtocol(Protocol):
    """Service contract for file change notification (S8).

    Provides long-poll observation of file system changes.
    """

    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
        _context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None: ...
