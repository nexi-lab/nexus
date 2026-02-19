"""Backward-compatibility shim — canonical module: nexus.services.permissions.consistency.

Issue #2074: Deduplicated consistency modules. All implementations now live in
``nexus.services.permissions.consistency``. This shim re-exports for backward
compatibility so existing ``from nexus.rebac.consistency import ...`` continues
to work.
"""

from nexus.services.permissions.consistency import (
    ZoneIsolationError,
    ZoneManager,
    get_zone_revision_for_grant,
    increment_version_token,
)

__all__ = [
    "ZoneIsolationError",
    "ZoneManager",
    "get_zone_revision_for_grant",
    "increment_version_token",
]
