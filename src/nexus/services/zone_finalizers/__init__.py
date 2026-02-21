"""Zone finalizers — concrete cleanup services for zone deprovisioning (Issue #2061).

Each finalizer implements ``ZoneFinalizerProtocol`` and handles one
domain of zone-scoped resources (cache, search, mounts, ReBAC, bricks).
"""

from nexus.services.zone_finalizers.brick_drain_finalizer import BrickDrainFinalizer
from nexus.services.zone_finalizers.cache_finalizer import CacheZoneFinalizer
from nexus.services.zone_finalizers.mount_finalizer import MountZoneFinalizer
from nexus.services.zone_finalizers.rebac_finalizer import ReBACZoneFinalizer
from nexus.services.zone_finalizers.search_finalizer import SearchZoneFinalizer

__all__ = [
    "BrickDrainFinalizer",
    "CacheZoneFinalizer",
    "MountZoneFinalizer",
    "ReBACZoneFinalizer",
    "SearchZoneFinalizer",
]
