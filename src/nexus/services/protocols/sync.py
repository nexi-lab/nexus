"""SyncService protocol (Issue #696).

Defines the contract for mount synchronisation.

Existing implementation: ``nexus.services.sync_service.SyncService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

# Type alias matching the service's ProgressCallback
ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True, slots=True)
class SyncContext:
    """Immutable value object describing a sync request."""

    mount_point: str | None
    path: str | None = None
    recursive: bool = True
    dry_run: bool = False
    sync_content: bool = True
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    generate_embeddings: bool = False
    context: OperationContext | None = None
    progress_callback: ProgressCallback | None = None
    full_sync: bool = False


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Immutable value object describing the outcome of a sync."""

    files_scanned: int = 0
    files_created: int = 0
    files_updated: int = 0
    files_deleted: int = 0
    files_skipped: int = 0
    cache_synced: int = 0
    cache_bytes: int = 0
    cache_skipped: int = 0
    embeddings_generated: int = 0
    errors: list[str] = field(default_factory=list)
    mounts_synced: int = 0
    mounts_skipped: int = 0


@runtime_checkable
class SyncServiceProtocol(Protocol):
    """Service contract for mount synchronisation."""

    def sync_mount(self, ctx: SyncContext) -> SyncResult: ...
