"""Backward-compatibility shim — use nexus.auth.providers.database_key instead."""

from nexus.auth.constants import API_KEY_MIN_LENGTH, API_KEY_PREFIX, HMAC_SALT
from nexus.auth.providers.database_key import DatabaseAPIKeyAuth

__all__ = ["DatabaseAPIKeyAuth", "API_KEY_PREFIX", "API_KEY_MIN_LENGTH", "HMAC_SALT"]
