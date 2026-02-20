"""Backward-compat shim — canonical: nexus.rebac.share_mixin.

Deprecated: import from nexus.rebac.share_mixin instead.
"""

import warnings

warnings.warn(
    "nexus.services.rebac_share_mixin is deprecated. Import from nexus.rebac.share_mixin instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.share_mixin import ReBACShareMixin  # noqa: F401, E402

__all__ = ["ReBACShareMixin"]
