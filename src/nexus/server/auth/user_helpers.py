"""User management helper functions.

Provides utility functions for:
- User lookup by various identifiers (email, username, OAuth, external ID)
- ReBAC group-based zone membership management (re-exported from core)
- Zone group naming conventions (re-exported from core)
- User creation with uniqueness checks
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# Issue #1519, 3A: Zone helper functions moved to core/zone_helpers.py
# (no server dependencies). Re-exported here for backward compatibility.
from nexus.core.zone_helpers import (  # noqa: F401
    add_user_to_zone,
    can_invite_to_zone,
    get_user_zones,
    is_zone_admin,
    is_zone_group,
    is_zone_owner,
    parse_zone_from_group,
    remove_user_from_zone,
    user_belongs_to_zone,
    zone_group_id,
)
from nexus.storage.models import (
    UserModel,
    UserOAuthAccountModel,
)
from nexus.storage.models.permissions import ReBACTupleModel

logger = logging.getLogger(__name__)

# ==============================================================================
# User Lookup Functions
# ==============================================================================


def get_user_by_email(session: Session, email: str) -> UserModel | None:
    """Get active user by email.

    Args:
        session: Database session
        email: Email address

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.email == email,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_username(session: Session, username: str) -> UserModel | None:
    """Get active user by username.

    Args:
        session: Database session
        username: Username

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.username == username,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_id(session: Session, user_id: str) -> UserModel | None:
    """Get active user by user ID.

    Args:
        session: Database session
        user_id: User ID

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.user_id == user_id,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_external_id(
    session: Session,
    external_user_id: str,
    external_user_service: str,
) -> UserModel | None:
    """Get active user by external service ID.

    Args:
        session: Database session
        external_user_id: User ID in external service
        external_user_service: External service identifier (e.g., 'auth0', 'okta')

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.external_user_id == external_user_id,
            UserModel.external_user_service == external_user_service,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_oauth_provider(
    session: Session,
    provider: str,
    provider_user_id: str,
) -> UserModel | None:
    """Get user via OAuth account.

    Args:
        session: Database session
        provider: OAuth provider (e.g., 'google', 'github')
        provider_user_id: User ID from OAuth provider

    Returns:
        UserModel or None if not found or inactive
    """
    oauth_account = session.scalar(
        select(UserOAuthAccountModel).where(
            UserOAuthAccountModel.provider == provider,
            UserOAuthAccountModel.provider_user_id == provider_user_id,
        )
    )
    if not oauth_account:
        return None

    return session.scalar(
        select(UserModel).where(
            UserModel.user_id == oauth_account.user_id,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


# ==============================================================================
# User Creation with Uniqueness Checks
# ==============================================================================


def check_email_available(session: Session, email: str) -> bool:
    """Check if email is available for registration.

    Only checks active users (soft-deleted users' emails can be reused).

    Args:
        session: Database session
        email: Email to check

    Returns:
        True if email is available (not used by any active user)
    """
    existing = session.scalar(
        select(UserModel).where(
            UserModel.email == email,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )
    return existing is None


def check_username_available(session: Session, username: str) -> bool:
    """Check if username is available for registration.

    Only checks active users (soft-deleted users' usernames can be reused).

    Args:
        session: Database session
        username: Username to check

    Returns:
        True if username is available (not used by any active user)
    """
    existing = session.scalar(
        select(UserModel).where(
            UserModel.username == username,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )
    return existing is None


def validate_user_uniqueness(
    session: Session,
    email: str | None = None,
    username: str | None = None,
) -> None:
    """Validate that email and username are unique among active users.

    This is used for SQLite < 3.8.0 where partial indexes are not supported.

    Args:
        session: Database session
        email: Email to check (optional)
        username: Username to check (optional)

    Raises:
        ValueError: If email or username already exists
    """
    if email and not check_email_available(session, email):
        raise ValueError(f"Email {email} already exists")

    if username and not check_username_available(session, username):
        raise ValueError(f"Username {username} already exists")


# ==============================================================================
# Default Zone Selection
# ==============================================================================


def get_user_default_zone(session: Session, user_id: str) -> str | None:
    """Get user's default zone.

    Priority:
    1. User's session preference (stored in session/cookie) - TODO: implement
    2. First zone in membership list
    3. None if user has no zones

    Args:
        session: SQLAlchemy ORM session
        user_id: User ID

    Returns:
        Zone ID or None if user has no zone memberships
    """
    # Get user's zone memberships
    zone_ids = get_user_zones(session, user_id)

    if not zone_ids:
        return None

    # TODO: Add session preference lookup here
    # For now, return first zone
    return zone_ids[0]


def require_zone_context(
    session: Session,
    user_id: str,
    zone_id: str | None,
    auto_create: bool = False,
) -> str:
    """Require zone context for operation.

    If zone_id not provided, use default zone.
    If no default zone, raise error or create default.

    Args:
        session: SQLAlchemy ORM session
        user_id: User ID
        zone_id: Optional zone ID from request
        auto_create: If True, create default zone if user has none

    Returns:
        Zone ID (guaranteed to be set)

    Raises:
        ValueError: If user has no zone memberships and auto_create=False
    """
    if zone_id:
        # Verify user belongs to this zone
        if not user_belongs_to_zone(session, user_id, zone_id):
            raise ValueError(f"User {user_id} does not belong to zone {zone_id}")
        return zone_id

    # Get default zone
    default_zone = get_user_default_zone(session, user_id)
    if default_zone:
        return default_zone

    # No zone - create default or error
    if auto_create:
        # TODO: Implement default zone creation
        # For now, raise error
        raise ValueError(
            f"User {user_id} has no zone memberships. Auto-create not yet implemented."
        )
    else:
        raise ValueError(f"User {user_id} has no zone memberships")


# ==============================================================================
# User Soft Delete
# ==============================================================================


def soft_delete_user(session: Session, user_id: str) -> UserModel | None:
    """Soft delete a user.

    Sets is_active=0 and deleted_at=now().
    This preserves audit trail and allows email/username reuse.

    Args:
        session: Database session
        user_id: User ID to soft delete

    Returns:
        Updated UserModel or None if not found
    """
    user = session.get(UserModel, user_id)
    if not user:
        return None

    user.is_active = 0
    user.deleted_at = datetime.now(UTC)
    session.add(user)
    session.flush()
    return user


def restore_user(session: Session, user_id: str) -> UserModel | None:
    """Restore a soft-deleted user.

    Sets is_active=1 and deleted_at=None.

    Args:
        session: Database session
        user_id: User ID to restore

    Returns:
        Updated UserModel or None if not found
    """
    user = session.get(UserModel, user_id)
    if not user:
        return None

    user.is_active = 1
    user.deleted_at = None
    session.add(user)
    session.flush()
    return user
