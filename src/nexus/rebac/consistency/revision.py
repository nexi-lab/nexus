"""Backward-compatibility shim — canonical module: nexus.services.permissions.consistency.revision.

Issue #2074: Deduplicated consistency modules.
"""

from nexus.services.permissions.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)

__all__ = ["get_zone_revision_for_grant", "increment_version_token"]
