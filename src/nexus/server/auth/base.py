"""Backward-compatibility shim — use nexus.auth.providers.base instead."""

from nexus.auth.providers.base import AuthProvider, AuthResult

__all__ = ["AuthProvider", "AuthResult"]
