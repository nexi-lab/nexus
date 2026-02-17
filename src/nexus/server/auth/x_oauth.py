"""Backward-compat shim — canonical module is nexus.auth.x_oauth."""

from nexus.auth.x_oauth import XOAuthProvider  # noqa: F401

__all__ = ["XOAuthProvider"]
