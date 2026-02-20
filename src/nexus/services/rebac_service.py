"""Backward-compat shim — canonical: nexus.rebac.service.

Deprecated: import from nexus.rebac.service instead.
"""

import warnings

warnings.warn(
    "nexus.services.rebac_service is deprecated. Import from nexus.rebac.service instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.service import ReBACService  # noqa: F401, E402

__all__ = ["ReBACService"]
