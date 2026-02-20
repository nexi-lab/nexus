"""Backward-compat shim — canonical: nexus.rebac.cache.tiger.expander.

Deprecated: import from nexus.rebac.cache.tiger.expander instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.cache.tiger.expander is deprecated. "
    "Import from nexus.rebac.cache.tiger.expander instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.tiger.expander import DirectoryGrantExpander  # noqa: F401, E402

__all__ = ["DirectoryGrantExpander"]
