"""Backward-compat shim — canonical: nexus.rebac.directory.

Deprecated: import from nexus.rebac.directory instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.directory is deprecated. "
    "Import from nexus.rebac.directory instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.directory import DirectoryExpander  # noqa: F401, E402

__all__ = ["DirectoryExpander"]
