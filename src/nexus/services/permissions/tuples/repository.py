"""Backward-compat shim — canonical: nexus.rebac.tuples.repository.

Deprecated: import from nexus.rebac.tuples.repository instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.tuples.repository is deprecated. "
    "Import from nexus.rebac.tuples.repository instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.tuples.repository import TupleRepository  # noqa: F401, E402

__all__ = ["TupleRepository"]
