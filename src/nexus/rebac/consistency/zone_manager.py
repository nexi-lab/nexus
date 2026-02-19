"""Backward-compatibility shim — canonical module: nexus.services.permissions.consistency.zone_manager.

Issue #2074: Deduplicated consistency modules.
"""

from nexus.services.permissions.consistency.zone_manager import (
    ZoneIsolationError,
    ZoneManager,
)

__all__ = ["ZoneIsolationError", "ZoneManager"]
