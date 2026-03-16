"""Brick drain finalizer — delegates zone cleanup to BrickLifecycleManager (Issue #2070).

Bridges the zone lifecycle (ZoneLifecycleService) and brick lifecycle
(BrickLifecycleManager) systems.  When registered as a ZoneFinalizerProtocol
finalizer, it calls ``BrickLifecycleManager.deprovision_zone()`` which runs
``drain()`` then ``finalize()`` on all zone-aware bricks in DAG order.

Should run as a **concurrent** finalizer since brick drain/finalize is
independent of the SQL-based cleanup finalizers.
"""

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager


class BrickDrainFinalizer:
    """Finalizer that drains and finalizes bricks for a deprovisioning zone."""

    def __init__(self, brick_lifecycle_manager: "BrickLifecycleManager") -> None:
        self._blm = brick_lifecycle_manager

    @property
    def finalizer_key(self) -> str:
        return "nexus.core/brick-drain"

    async def finalize_zone(self, zone_id: str) -> None:
        """Delegate to BrickLifecycleManager.deprovision_zone()."""
        report = await self._blm.deprovision_zone(zone_id)
        logger.info(
            "[BrickDrainFinalizer] zone=%s drained=%d finalized=%d errors=%d",
            zone_id,
            report.bricks_drained,
            report.bricks_finalized,
            report.drain_errors + report.finalize_errors,
        )
        total_errors = report.drain_errors + report.finalize_errors
        if total_errors > 0:
            raise RuntimeError(f"BrickDrain had {total_errors} error(s) for zone {zone_id}")
