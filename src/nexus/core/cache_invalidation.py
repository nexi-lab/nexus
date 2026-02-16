"""Cache invalidation observer — decouples kernel from ReadSetAwareCache.

The kernel calls the observer on mutations (write/delete/rename) and reads
(to register cache dependencies). The concrete ``ReadSetCacheObserver``
bridges these calls to ``ReadSetAwareCache``, following the same pattern
used by ``_write_observer`` / ``_notify_observer`` in the kernel.

Issue #1169 / #1519.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadata

logger = logging.getLogger(__name__)


@runtime_checkable
class CacheInvalidationObserver(Protocol):
    """Observer for kernel mutation events that require cache invalidation.

    Injected into the kernel via ``KernelServices.cache_observer``.
    The kernel calls these methods; the observer handles cache orchestration.
    """

    def on_write(self, path: str, revision: int, zone_id: str) -> None: ...

    def on_delete(self, path: str, revision: int, zone_id: str) -> None: ...

    def on_rename(self, old_path: str, new_path: str, revision: int, zone_id: str) -> None: ...

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

    Wraps the existing ``ReadSetAwareCache`` and ``ReadSetRegistry``,
    translating kernel observer calls into the cache's native API.
    """

    def __init__(self, read_set_cache: Any) -> None:
        self._cache = read_set_cache

    def on_write(self, path: str, revision: int, zone_id: str) -> None:
        self._cache.invalidate_for_write(path, revision, zone_id=zone_id)

    def on_delete(self, path: str, revision: int, zone_id: str) -> None:
        self._cache.invalidate_for_write(path, revision, zone_id=zone_id)

    def on_rename(self, old_path: str, new_path: str, revision: int, zone_id: str) -> None:
        self._cache.invalidate_for_write(old_path, revision, zone_id=zone_id)
        self._cache.invalidate_for_write(new_path, revision, zone_id=zone_id)

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

        from nexus.core.read_set import ReadSet

        rs = ReadSet(query_id=f"cache:{path}", zone_id=zone_id)
        rs.record_read(resource_type, path, revision=revision)
        self._cache.put_path(path, metadata, read_set=rs, zone_revision=revision)
