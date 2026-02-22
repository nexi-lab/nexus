"""Pending OAuth manager (backward-compat shim).

Canonical location: ``nexus.auth.oauth.pending``
"""

import warnings

from nexus.auth.oauth.pending import PendingOAuthManager, get_pending_oauth_manager
from nexus.auth.oauth.types import PendingOAuthRegistration

warnings.warn(
    "Importing from nexus.server.auth.pending_oauth is deprecated. "
    "Use nexus.auth.oauth.pending instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["PendingOAuthManager", "PendingOAuthRegistration", "get_pending_oauth_manager"]
