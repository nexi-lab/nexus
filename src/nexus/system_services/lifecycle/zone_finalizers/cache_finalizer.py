"""Cache zone finalizer — evicts zone-scoped cache entries (Issue #2061).

Delegates to ``FileCache.delete_zone()`` for L1 cache and
``CacheStoreABC.delete_by_pattern()`` for optional L2 (Dragonfly) cache.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.storage.file_cache import FileContentCache

logger = logging.getLogger(__name__)


class CacheZoneFinalizer:
    """Finalizer that cleans L1 file cache and optional L2 distributed cache."""

    def __init__(self, file_cache: "FileContentCache | Any", l2_cache: Any | None = None) -> None:
        self._file_cache = file_cache
        self._l2_cache = l2_cache

    @property
    def finalizer_key(self) -> str:
        return "nexus.core/cache"

    async def finalize_zone(self, zone_id: str) -> None:
        """Delete all cached entries for *zone_id*."""
        # L1: local file cache
        deleted = self._file_cache.delete_zone(zone_id)
        logger.info(
            "[CacheFinalizer] Deleted %d L1 cache entries for zone %s",
            deleted,
            zone_id,
        )

        # L2: distributed cache (optional)
        if self._l2_cache is not None:
            l2_deleted = await self._l2_cache.delete_by_pattern(f"zone:{zone_id}:*")
            logger.info(
                "[CacheFinalizer] Deleted %d L2 cache entries for zone %s",
                l2_deleted,
                zone_id,
            )
