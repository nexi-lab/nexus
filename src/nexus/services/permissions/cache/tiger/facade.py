"""Tiger Cache Facade — Unified API for Tiger Cache operations.

Extracts the 8 tiger_* delegation methods from EnhancedReBACManager
into a standalone facade that coordinates TigerCache and TigerCacheUpdater.

External callers (background_tasks, nexus_fs, nexus_fs_rebac) access
Tiger via EnhancedReBACManager's thin delegation methods, which now
route through this facade.

Related: Issue #1459 Phase 12, Issue #682 (Tiger Cache)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.services.permissions.cache.tiger.bitmap_cache import TigerCache
    from nexus.services.permissions.cache.tiger.updater import TigerCacheUpdater

logger = logging.getLogger(__name__)


class TigerFacade:
    """Facade for Tiger Cache operations.

    Wraps TigerCache (bitmap lookups/mutations) and TigerCacheUpdater
    (background queue processing) behind a single, tuple-based API
    that matches the ReBAC Manager's calling convention.

    All methods accept subject as ``(type, id)`` tuples and handle
    the ``None`` guard (cache disabled) internally.
    """

    def __init__(
        self,
        tiger_cache: TigerCache | None = None,
        tiger_updater: TigerCacheUpdater | None = None,
    ) -> None:
        self._cache = tiger_cache
        self._updater = tiger_updater

    # -- Properties for external inspection --------------------------------

    @property
    def cache(self) -> TigerCache | None:
        """Access the underlying TigerCache (or None if disabled)."""
        return self._cache

    @property
    def updater(self) -> TigerCacheUpdater | None:
        """Access the underlying TigerCacheUpdater (or None if disabled)."""
        return self._updater

    @property
    def enabled(self) -> bool:
        """Whether Tiger Cache is active."""
        return self._cache is not None

    # -- Read operations ---------------------------------------------------

    def check_access(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],  # noqa: A002
    ) -> bool | None:
        """Check permission using Tiger Cache (O(1) bitmap lookup).

        Returns:
            True if allowed, False if denied, None if not in cache.
        """
        if not self._cache:
            return None

        return self._cache.check_access(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=object[0],
            resource_id=object[1],
        )

    def get_accessible_resources(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> set[int]:
        """Get all resources accessible by subject using Tiger Cache.

        Returns:
            Set of integer resource IDs.
        """
        if not self._cache:
            return set()

        return self._cache.get_accessible_resources(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
        )

    # -- Write-through operations ------------------------------------------

    def persist_grant(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> bool:
        """Write-through: persist a single permission grant to Tiger Cache.

        Returns:
            True if persisted successfully, False on error.
        """
        if not self._cache:
            return False

        return self._cache.persist_single_grant(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            resource_id=resource_id,
            zone_id=zone_id,
        )

    def persist_revoke(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> bool:
        """Write-through: persist a single permission revocation to Tiger Cache.

        Returns:
            True if persisted successfully, False on error.
        """
        if not self._cache:
            return False

        return self._cache.persist_single_revoke(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            resource_id=resource_id,
            zone_id=zone_id,
        )

    # -- Queue operations --------------------------------------------------

    def queue_update(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        zone_id: str,
        priority: int = 100,
    ) -> int | None:
        """Queue a Tiger Cache update for background processing.

        Returns:
            Queue entry ID, or None if Tiger Cache is disabled.
        """
        if not self._updater:
            return None

        return self._updater.queue_update(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
            priority=priority,
        )

    def process_queue(self, batch_size: int = 100) -> int:
        """Process pending Tiger Cache update queue.

        Returns:
            Number of entries processed.
        """
        if not self._updater:
            logger.warning("[TIGER] process_queue: _tiger_updater is None")
            return 0

        if logger.isEnabledFor(logging.INFO):
            logger.info("[TIGER] process_queue: calling updater (batch=%d)", batch_size)
        result = self._updater.process_queue(batch_size=batch_size)
        if logger.isEnabledFor(logging.INFO):
            logger.info("[TIGER] process_queue: result=%d", result)
        return result

    # -- Cache management --------------------------------------------------

    def invalidate_cache(
        self,
        subject: tuple[str, str] | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger Cache entries.

        Returns:
            Number of entries invalidated.
        """
        if not self._cache:
            return 0

        subject_type = subject[0] if subject else None
        subject_id = subject[1] if subject else None

        return self._cache.invalidate(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
        )

    def register_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> int:
        """Register a resource in the Tiger resource map.

        Returns:
            Integer ID for use in bitmaps, or -1 if disabled.
        """
        if not self._cache:
            return -1

        return self._cache._resource_map.get_or_create_int_id(
            resource_type=resource_type,
            resource_id=resource_id,
        )
