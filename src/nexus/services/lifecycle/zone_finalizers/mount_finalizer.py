"""Mount zone finalizer — removes all mount points for a zone (Issue #2061).

Iterates mounts for the zone and delegates cleanup to
``MountService.remove_mount_sync()`` which handles metadata,
directory index, hierarchy tuples, and permission tuples.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MountZoneFinalizer:
    """Finalizer that removes all mount points belonging to a zone."""

    def __init__(self, mount_service: Any) -> None:
        self._mount_service = mount_service

    @property
    def finalizer_key(self) -> str:
        return "nexus.core/mount"

    async def finalize_zone(self, zone_id: str) -> None:
        """Remove all mounts for *zone_id*.

        .. todo:: Issue #2070: push zone_id filter to MountService.list_mounts_sync()
           to avoid O(total) scan.  Current approach is acceptable because mount
           counts are typically small (tens, not thousands).
        """
        mounts = self._mount_service.list_mounts()
        zone_mounts = [m for m in mounts if m.get("mount_point", "").startswith(f"/{zone_id}/")]

        removed = 0
        for mount in zone_mounts:
            mount_point = mount.get("mount_point", "")
            try:
                self._mount_service.remove_mount(mount_point)
                removed += 1
            except Exception as exc:
                logger.warning(
                    "[MountFinalizer] Failed to remove mount %s: %s",
                    mount_point,
                    exc,
                )
                raise

        logger.info(
            "[MountFinalizer] Removed %d mounts for zone %s",
            removed,
            zone_id,
        )
