"""
Zone-Aware ReBAC Manager — Backward Compatibility Shim

DEPRECATED: ZoneAwareReBACManager has been merged into EnhancedReBACManager
as part of Issue #1459 Phase 10 (flatten inheritance into composition).

All zone-aware logic now lives directly in EnhancedReBACManager, which
inherits from ReBACManager. This module re-exports for backward compatibility.

Migration:
    # Old:
    from nexus.services.permissions.rebac_manager_zone_aware import ZoneAwareReBACManager
    # New:
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager
"""

from __future__ import annotations

from nexus.services.permissions.consistency.zone_manager import (
    ZoneIsolationError,  # noqa: F401 — re-exported for backward compat
)
from nexus.services.permissions.rebac_manager_enhanced import (
    EnhancedReBACManager as ZoneAwareReBACManager,  # noqa: F401
)

__all__ = ["ZoneAwareReBACManager", "ZoneIsolationError"]
