"""Backward-compat shim — canonical module is nexus.auth.token_manager."""

from nexus.auth.token_manager import TokenManager, _hash_token  # noqa: F401

__all__ = ["TokenManager", "_hash_token"]
