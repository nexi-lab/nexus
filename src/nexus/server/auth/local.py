"""Backward-compatibility shim — use nexus.auth.providers.local instead."""

from nexus.auth.providers.local import LocalAuth

__all__ = ["LocalAuth"]
