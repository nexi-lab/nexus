"""
Utility functions for extracting and resolving context information.

This module provides centralized helpers for:
- Extracting tenant_id from context with defaults
- Extracting user identity (type, id) from context
- Resolving database URLs with environment variable priority

These utilities eliminate code duplication across nexus_fs_mounts, nexus_fs_oauth,
and workspace_registry modules.
"""

import os
from typing import Any


def get_tenant_id(context: Any) -> str:
    """
    Extract tenant_id from context with default fallback.

    Args:
        context: Operation context object (may have tenant_id attribute)

    Returns:
        Tenant ID string, defaults to "default" if not found

    Examples:
        >>> ctx = OperationContext(tenant_id="acme")
        >>> get_tenant_id(ctx)
        'acme'
        >>> get_tenant_id(None)
        'default'
    """
    if context and hasattr(context, "tenant_id") and context.tenant_id:
        return str(context.tenant_id)
    return "default"


def get_user_identity(context: Any) -> tuple[str, str | None]:
    """
    Extract user identity (type, id) from context.

    Checks multiple attributes for compatibility:
    - subject_type and subject_id (new convention)
    - user_id (alternative field)
    - user (legacy field)

    Args:
        context: Operation context object

    Returns:
        Tuple of (subject_type, subject_id) where:
        - subject_type: "user", "agent", etc. (defaults to "user")
        - subject_id: User/agent identifier (may be None)

    Examples:
        >>> ctx = OperationContext(subject_type="user", subject_id="alice")
        >>> get_user_identity(ctx)
        ('user', 'alice')
        >>> get_user_identity(None)
        ('user', None)
    """
    if not context:
        return ("user", None)

    subject_type = getattr(context, "subject_type", "user") or "user"
    subject_id = (
        getattr(context, "subject_id", None)
        or getattr(context, "user_id", None)
        or getattr(context, "user", None)
    )
    return (subject_type, subject_id)


def get_database_url(obj: Any, context: Any = None) -> str:  # noqa: ARG001
    """
    Get database URL with standard priority resolution.

    Priority order:
    1. TOKEN_MANAGER_DB environment variable
    2. obj._config.db_path (if available)
    3. obj.db_path (direct attribute, if available)
    4. obj.metadata.database_url (if available)

    Args:
        obj: Object to check for configuration (typically self)
        context: Optional operation context (currently unused, reserved for future)

    Returns:
        Database URL string

    Raises:
        RuntimeError: If no database path is configured

    Examples:
        >>> os.environ['TOKEN_MANAGER_DB'] = 'postgresql://localhost/nexus'
        >>> get_database_url(some_obj)
        'postgresql://localhost/nexus'
    """
    database_url = os.getenv("TOKEN_MANAGER_DB")

    if not database_url:
        if (
            hasattr(obj, "_config")
            and obj._config
            and hasattr(obj._config, "db_path")
            and obj._config.db_path
        ):
            database_url = obj._config.db_path
        elif hasattr(obj, "db_path") and obj.db_path:
            database_url = str(obj.db_path)
        elif hasattr(obj, "metadata") and hasattr(obj.metadata, "database_url"):
            database_url = obj.metadata.database_url

    if not database_url:
        raise RuntimeError(
            "No database path configured. Set TOKEN_MANAGER_DB environment "
            "variable or ensure metadata.database_url is configured."
        )

    return database_url


def resolve_skill_base_path(context: Any) -> str:
    """
    Determine skill base path based on context (user vs tenant vs system).

    Priority order:
    1. User-specific path: /skills/users/{user_id}/
    2. Tenant-specific path: /skills/tenants/{tenant_id}/
    3. System default path: /skills/system/

    Args:
        context: Operation context with optional user_id and tenant_id

    Returns:
        Base path string for skills

    Examples:
        >>> ctx = OperationContext(user_id="alice")
        >>> resolve_skill_base_path(ctx)
        '/skills/users/alice/'
    """
    if context:
        user_id = getattr(context, "user_id", None)
        if user_id:
            return f"/skills/users/{user_id}/"

        tenant_id = getattr(context, "tenant_id", None)
        if tenant_id:
            return f"/skills/tenants/{tenant_id}/"

    return "/skills/system/"
