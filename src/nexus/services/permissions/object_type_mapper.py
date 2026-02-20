"""Backward-compat shim — canonical: nexus.rebac.object_type_mapper.

Deprecated: import from nexus.rebac.object_type_mapper instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.object_type_mapper is deprecated. "
    "Import from nexus.rebac.object_type_mapper instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.object_type_mapper import ObjectTypeMapper  # noqa: F401, E402

__all__ = ["ObjectTypeMapper"]
