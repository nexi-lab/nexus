"""Backward-compat shim — canonical: nexus.rebac.cache.tiger.facade.

Deprecated: import from nexus.rebac.cache.tiger.facade instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.cache.tiger.facade is deprecated. "
    "Import from nexus.rebac.cache.tiger.facade instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.tiger.facade import TigerFacade  # noqa: F401, E402

__all__ = ["TigerFacade"]
