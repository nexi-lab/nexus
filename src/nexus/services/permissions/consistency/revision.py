"""Backward-compat shim — canonical: nexus.rebac.consistency.revision.

Deprecated: import from nexus.rebac.consistency.revision instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.consistency.revision is deprecated. "
    "Import from nexus.rebac.consistency.revision instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.consistency.revision import (  # noqa: F401, E402
    get_zone_revision_for_grant,
    increment_version_token,
)

__all__ = ["get_zone_revision_for_grant", "increment_version_token"]
