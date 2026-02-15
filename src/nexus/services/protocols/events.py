"""Events service protocol (Issue #1287: Extract domain services).

Defines the contract for file watching and advisory locking operations.
Existing implementation: ``nexus.core.nexus_fs_events.NexusFSEventsMixin``.

Dual-track support:
- Layer 1 (Same-box): OS-native file watching (inotify/FSEvents)
- Layer 2 (Distributed): Redis Pub/Sub events + distributed locks

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class EventsProtocol(Protocol):
    """Service contract for file watching and advisory locking.

    Provides:
    - ``wait_for_changes``: Long-poll for file system changes
    - ``lock`` / ``extend_lock`` / ``unlock``: Advisory lock lifecycle
    """

    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any] | None: ...

    async def lock(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        _context: OperationContext | None = None,
    ) -> str | None: ...

    async def extend_lock(
        self,
        lock_id: str,
        path: str,
        ttl: float = 30.0,
        _context: OperationContext | None = None,
    ) -> bool: ...

    async def unlock(
        self,
        lock_id: str,
        path: str | None = None,
        _context: OperationContext | None = None,
    ) -> bool: ...
