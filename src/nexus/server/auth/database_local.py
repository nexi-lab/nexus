"""Backward-compatibility shim — use nexus.auth.providers.database_local instead."""

from nexus.auth.providers.database_local import DatabaseLocalAuth
from nexus.auth.user_queries import (
    check_email_available,
    check_username_available,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    validate_user_uniqueness,
)

__all__ = [
    "DatabaseLocalAuth",
    "get_user_by_email",
    "get_user_by_username",
    "get_user_by_id",
    "check_email_available",
    "check_username_available",
    "validate_user_uniqueness",
]
