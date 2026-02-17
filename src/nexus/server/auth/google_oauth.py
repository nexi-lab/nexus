"""Backward-compat shim — canonical module is nexus.auth.google_oauth."""

from nexus.auth.google_oauth import GoogleOAuthProvider  # noqa: F401

__all__ = ["GoogleOAuthProvider"]
