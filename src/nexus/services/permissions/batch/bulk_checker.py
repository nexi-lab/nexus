"""Backward-compat shim — canonical: nexus.rebac.batch.bulk_checker.

Deprecated: import from nexus.rebac.batch.bulk_checker instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.batch.bulk_checker is deprecated. "
    "Import from nexus.rebac.batch.bulk_checker instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.batch.bulk_checker import BulkPermissionChecker  # noqa: F401, E402

__all__ = ["BulkPermissionChecker"]
