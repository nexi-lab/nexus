"""Backward-compat shim — canonical: nexus.rebac.utils.zone.

Deprecated: import from nexus.rebac.utils.zone instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.utils.zone is deprecated. "
    "Import from nexus.rebac.utils.zone instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.utils.zone import (  # noqa: F401, E402
    can_invite_to_zone,
    is_zone_admin,
    is_zone_group,
    is_zone_owner,
    normalize_zone_id,
    parse_zone_from_group,
    zone_group_id,
)

__all__ = [
    "can_invite_to_zone",
    "is_zone_admin",
    "is_zone_group",
    "is_zone_owner",
    "normalize_zone_id",
    "parse_zone_from_group",
    "zone_group_id",
]
