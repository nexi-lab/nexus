"""Backward-compat shim: nexus.services.permissions.hotspot_detector.

Canonical location: ``nexus.rebac.hotspot_detector``
"""

from nexus.rebac.hotspot_detector import (
    HotspotConfig,
    HotspotDetector,
    HotspotEntry,
    HotspotPrefetcher,
    hotspot_prefetch_task,
)

__all__ = [
    "HotspotConfig",
    "HotspotDetector",
    "HotspotEntry",
    "HotspotPrefetcher",
    "hotspot_prefetch_task",
]
