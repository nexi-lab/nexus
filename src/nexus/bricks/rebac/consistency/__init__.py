"""Consistency — Zanzibar-style zone isolation and revision tracking (Issue #2179).

Canonical implementation (moved from services/permissions/consistency/).
"""

from nexus.bricks.rebac.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)
from nexus.bricks.rebac.consistency.zone_manager import (
    ZoneIsolationError,
    ZoneManager,
)

__all__ = [
    "ZoneIsolationError",
    "ZoneManager",
    "get_zone_revision_for_grant",
    "increment_version_token",
]
