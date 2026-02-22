"""Cache invalidation observer — decouples kernel from ReadSetAwareCache.

Mutation-side: implements ``VFSObserver`` and is registered via
``KernelDispatch.register_observe()``.  The kernel fires a frozen
``MutationEvent`` after each write/delete/rename (OBSERVE phase);
this observer handles cache invalidation.

Read-side: ``on_read()`` is still called directly by the kernel (reads
are not mutations and have no observer notification).

Issue #1169 / #1519 / #625 / #900.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata
    from nexus.contracts.vfs_hooks import MutationEvent

logger = logging.getLogger(__name__)


@runtime_checkable
class CacheInvalidationObserver(Protocol):
    """Observer for kernel cache invalidation.

    Injected into the kernel via ``KernelServices.cache_observer``.

    ``on_mutation()`` is called via ``KernelDispatch.notify()`` (OBSERVE
    phase) after each write/delete/rename.
    ``on_read()`` is called directly (reads are not mutations).
    """

    def on_mutation(self, event: MutationEvent) -> None: ...

    def on_read(
        self,
        path: str,
        metadata: FileMetadata | None,
        revision: int,
        zone_id: str,
        resource_type: str = "file",
    ) -> None: ...


class ReadSetCacheObserver:
    """Bridges kernel mutation/read events to ReadSetAwareCache.

    Implements ``VFSObserver`` for mutations and direct ``on_read``
    for read-side cache population.
    """

    def __init__(self, read_set_cache: Any) -> None:
        self._cache = read_set_cache

    def on_mutation(self, event: MutationEvent) -> None:
        """Handle VFS mutations (OBSERVE phase)."""
        from nexus.contracts.vfs_hooks import MutationOp

        if event.operation is MutationOp.RENAME and event.new_path is not None:
            self._cache.invalidate_for_write(event.path, event.revision, zone_id=event.zone_id)
            self._cache.invalidate_for_write(event.new_path, event.revision, zone_id=event.zone_id)
        else:
            # write or delete
            self._cache.invalidate_for_write(event.path, event.revision, zone_id=event.zone_id)

    def on_read(
        self,
        path: str,
        metadata: FileMetadata | None,
        revision: int,
        zone_id: str,
        resource_type: str = "file",
    ) -> None:
        if metadata is None:
            return

        from nexus.storage.read_set import ReadSet

        rs = ReadSet(query_id=f"cache:{path}", zone_id=zone_id)
        rs.record_read(resource_type, path, revision=revision)
        self._cache.put_path(path, metadata, read_set=rs, zone_revision=revision)
