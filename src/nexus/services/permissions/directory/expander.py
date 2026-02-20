"""Backward-compat shim — canonical: nexus.rebac.directory.expander.

Deprecated: import from nexus.rebac.directory.expander instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.directory.expander is deprecated. "
    "Import from nexus.rebac.directory.expander instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.directory.expander import DirectoryExpander  # noqa: F401, E402

__all__ = ["DirectoryExpander"]
