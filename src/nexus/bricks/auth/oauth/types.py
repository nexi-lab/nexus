"""Compat shim — canonical location is :mod:`nexus.lib.oauth.types`."""

from nexus.lib.oauth.types import (
    OAuthCredential,
    OAuthError,
    PendingOAuthRegistration,
    _mask_token,
)

__all__ = [
    "OAuthCredential",
    "OAuthError",
    "PendingOAuthRegistration",
    "_mask_token",
]
