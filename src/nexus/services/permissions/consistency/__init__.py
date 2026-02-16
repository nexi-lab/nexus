"""Consistency module — Zone isolation + version tokens.

Provides zone isolation enforcement (Zanzibar-style) and revision-based
consistency tokens for the ReBAC permission system.

Components:
- ``ZoneIsolationValidator``: Zone isolation enforcement and cross-zone share validation
- ``ZoneIsolationError``: Exception for cross-zone violations
- ``increment_version_token``: DB-backed monotonic version token generation
- ``get_zone_revision_for_grant``: Zone revision lookup for consistency guarantees

Related: Issue #1459 (decomposition), Issue #773 (zone isolation), Issue #1064 (wildcards)
"""

from nexus.services.permissions.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)
from nexus.services.permissions.consistency.zone_manager import (
    ZoneIsolationError,
    ZoneIsolationValidator,
)

__all__ = [
    "ZoneIsolationError",
    "ZoneIsolationValidator",
    "get_zone_revision_for_grant",
    "increment_version_token",
]
