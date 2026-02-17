"""Backward-compat shim — canonical module is nexus.auth.oauth_crypto."""

from nexus.auth.oauth_crypto import OAuthCrypto  # noqa: F401

__all__ = ["OAuthCrypto"]
