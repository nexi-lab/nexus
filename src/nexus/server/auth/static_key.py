"""Backward-compatibility shim — use nexus.auth.providers.static_key instead."""

from nexus.auth.constants import API_KEY_PREFIX
from nexus.auth.providers.static_key import StaticAPIKeyAuth

__all__ = ["StaticAPIKeyAuth", "API_KEY_PREFIX"]
