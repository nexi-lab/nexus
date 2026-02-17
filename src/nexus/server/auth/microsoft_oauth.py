"""Backward-compat shim — canonical module is nexus.auth.microsoft_oauth."""

from nexus.auth.microsoft_oauth import MicrosoftOAuthProvider  # noqa: F401

__all__ = ["MicrosoftOAuthProvider"]
