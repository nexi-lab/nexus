"""Backward-compat shim — canonical module is nexus.auth.oauth_factory."""

from nexus.auth.oauth_factory import OAuthProviderFactory  # noqa: F401

__all__ = ["OAuthProviderFactory"]
