"""Shared utilities for connector backends.

Extracted from Backend ABC to decouple OAuth/database concerns from storage.
"""

from __future__ import annotations

import os


def resolve_database_url(db_param: str) -> str:
    """Resolve database URL with TOKEN_MANAGER_DB environment variable priority.

    Used by connector backends (GDrive, Gmail, X, Slack, GCalendar) to
    resolve the database URL for TokenManager, giving priority to the
    TOKEN_MANAGER_DB environment variable over the provided parameter.

    Args:
        db_param: Database URL or path provided to the connector

    Returns:
        Resolved database URL (from env var if set, otherwise db_param)

    Examples:
        >>> import os
        >>> os.environ['TOKEN_MANAGER_DB'] = 'postgresql://localhost/nexus'
        >>> resolve_database_url('sqlite:///local.db')
        'postgresql://localhost/nexus'
    """
    return os.getenv("TOKEN_MANAGER_DB") or db_param
