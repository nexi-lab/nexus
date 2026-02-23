"""Utility functions for extracting and resolving context information.

Tier-neutral utility (``nexus.lib``) — zero kernel dependency.

Provides centralized helpers for:
- Extracting zone_id from context with defaults
- Extracting user identity (type, id) from context
- Resolving database URLs with environment variable priority
- Parsing OperationContext from dicts (Issue #2033)
- Building created_by strings for version tracking (Issue #2033)
- Extracting subject tuples from context (Issue #2033)
"""

import logging
import os
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


def get_zone_id(context: Any) -> str:
    """
    Extract zone_id from context with default fallback.

    Args:
        context: Operation context object (may have zone_id attribute)

    Returns:
        Zone ID string, defaults to "root" if not found

    Examples:
        >>> ctx = OperationContext(zone_id="acme")
        >>> get_zone_id(ctx)
        'acme'
        >>> get_zone_id(None)
        'root'
    """
    if context and hasattr(context, "zone_id") and context.zone_id:
        return str(context.zone_id)
    return ROOT_ZONE_ID


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
        or getattr(context, "user_id", None)
    )
    return (subject_type, subject_id)


def get_database_url(obj: Any, context: Any = None) -> str:  # noqa: ARG001
    """
    Get database URL with standard priority resolution.

    Priority order:
    1. TOKEN_MANAGER_DB environment variable
    2. obj._config.db_path (if available)
    3. obj.db_path (direct attribute, if available)

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
    logger.debug(f"TOKEN_MANAGER_DB env var: {database_url}")

    if not database_url:
        if (
            hasattr(obj, "_config")
            and obj._config
            and hasattr(obj._config, "db_path")
            and obj._config.db_path
        ):
            database_url = obj._config.db_path
            logger.debug(f"Using obj._config.db_path: {database_url}")
        elif hasattr(obj, "db_path") and obj.db_path:
            database_url = str(obj.db_path)
            logger.debug(f"Using obj.db_path: {database_url}")

    if not database_url:
        raise RuntimeError(
            "No database path configured. Set TOKEN_MANAGER_DB environment "
            "variable or ensure metadata.database_url is configured."
        )

    return database_url


def resolve_skill_base_path(context: Any) -> str:
    """
    Determine skill base path based on context (user vs zone vs system).

    Priority order:
    1. User-specific path: /skills/users/{user_id}/
    2. Zone-specific path: /skills/zones/{zone_id}/
    3. System default path: /skills/system/

    Args:
        context: Operation context with optional user_id and zone_id

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

        zone_id = getattr(context, "zone_id", None)
        if zone_id:
            return f"/skills/zones/{zone_id}/"

    return "/skills/system/"


# -------------------------------------------------------------------------
# Extracted from NexusFS (Issue #2033)
# -------------------------------------------------------------------------


def parse_context(context: OperationContext | dict | None = None) -> OperationContext:
    """Parse context dict or OperationContext into OperationContext.

    Args:
        context: Optional context dict or OperationContext.

    Returns:
        OperationContext instance.
    """
    if isinstance(context, OperationContext):
        return context

    if context is None:
        context = {}

    return OperationContext(
        user_id=context.get("user_id", "system"),
        groups=context.get("groups", []),
        zone_id=context.get("zone_id"),
        agent_id=context.get("agent_id"),
        is_admin=context.get("is_admin", False),
        is_system=context.get("is_system", False),
    )


def get_created_by(
    context: OperationContext | dict | None,
    default_context: OperationContext,
) -> str | None:
    """Get the created_by value for version history tracking.

    Args:
        context: Operation context with per-request values.
        default_context: Fallback context when *context* is None.

    Returns:
        Combined string, e.g. ``'user:alice,agent:bot'``, or None.
    """
    user: str | None = None
    agent: str | None = None

    if context is None:
        user = getattr(default_context, "user_id", None)
        agent = default_context.agent_id
    elif hasattr(context, "agent_id"):
        user = getattr(context, "user_id", None)
        agent = context.agent_id
    elif isinstance(context, dict):
        user = context.get("user_id")
        agent = context.get("agent_id")
    else:
        user = getattr(default_context, "user_id", None)
        agent = default_context.agent_id

    parts: list[str] = []
    if user:
        parts.append(f"user:{user}")
    if agent:
        parts.append(f"agent:{agent}")

    return ",".join(parts) if parts else None


def get_subject_from_context(context: Any) -> tuple[str, str] | None:
    """Extract subject from operation context.

    Args:
        context: Operation context (OperationContext or dict).

    Returns:
        Subject tuple ``(type, id)`` or None.
    """
    if not context:
        return None

    # Handle dict format (used by RPC server and tests)
    if isinstance(context, dict):
        subject = context.get("subject")
        if subject and isinstance(subject, tuple) and len(subject) == 2:
            return (str(subject[0]), str(subject[1]))

        subject_type = context.get("subject_type", "user")
        subject_id = context.get("subject_id") or context.get("user_id")
        if subject_id:
            return (subject_type, subject_id)

        return None

    # Handle OperationContext format
    if hasattr(context, "get_subject") and callable(context.get_subject):
        result = context.get_subject()
        if result is not None:
            return (str(result[0]), str(result[1]))
        return None

    # Fallback: construct from attributes
    if hasattr(context, "subject_type") and hasattr(context, "subject_id"):
        subject_type = getattr(context, "subject_type", "user")
        subject_id = getattr(context, "subject_id", None) or getattr(context, "user_id", None)
        if subject_id:
            return (subject_type, subject_id)

    # Last resort: use user field
    if hasattr(context, "user_id") and context.user_id:
        return ("user", context.user_id)

    return None
