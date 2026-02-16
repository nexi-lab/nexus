"""User management helper functions.

Provides utility functions for:
- User lookup by various identifiers (email, username, OAuth, external ID)
- ReBAC group-based zone membership management
- Zone group naming conventions
- User creation with uniqueness checks
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# ==============================================================================
# ReBAC Group Naming Helpers — canonical source: services/permissions/utils/zone.py
# Re-exported here for backward compatibility with server-layer callers.
# ==============================================================================
from nexus.services.permissions.utils.zone import (  # noqa: F401 — re-exported
    can_invite_to_zone,
    is_zone_admin,
    is_zone_group,
    is_zone_owner,
    parse_zone_from_group,
    zone_group_id,
)
from nexus.storage.models import (
    UserModel,
    UserOAuthAccountModel,
)


def add_user_to_zone(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
    role: str = "member",
    caller_user_id: str | None = None,
) -> str:
    """Add user to zone via ReBAC group.

    SECURITY: Only zone admins/owners can invite users.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID to add
        zone_id: Zone ID
        role: Role in zone ("owner", "admin", or "member")
        caller_user_id: Optional user ID of caller (for permission check)

    Returns:
        ReBAC tuple ID

    Raises:
        PermissionError: If caller is not zone admin/owner
        ValueError: If role is invalid

    Example:
        # Add user as member (requires admin/owner)
        add_user_to_zone(rebac_mgr, "bob", "acme", "member", caller_user_id="alice")

        # Add user as admin (requires admin/owner)
        add_user_to_zone(rebac_mgr, "bob", "acme", "admin", caller_user_id="alice")

        # Add user as owner (requires owner)
        add_user_to_zone(rebac_mgr, "bob", "acme", "owner", caller_user_id="alice")
    """
    # SECURITY: Check if caller can invite users
    if caller_user_id:
        # Only owners can add other owners
        if role == "owner":
            if not is_zone_owner(rebac_manager, caller_user_id, zone_id):
                raise PermissionError(
                    f"Only zone owners can add other owners. "
                    f"User '{caller_user_id}' is not owner of zone '{zone_id}'"
                )
        else:
            # Admins and owners can add admins/members
            if not can_invite_to_zone(rebac_manager, caller_user_id, zone_id):
                raise PermissionError(
                    f"Only zone admins/owners can invite users. "
                    f"User '{caller_user_id}' is not admin/owner of zone '{zone_id}'"
                )

    # Validate role
    if role not in ("owner", "admin", "member"):
        raise ValueError(f"Invalid role '{role}'. Must be 'owner', 'admin', or 'member'")

    # Determine group ID based on role
    group_id = zone_group_id(zone_id)
    if role == "owner":
        group_id = f"{group_id}-owners"
    elif role == "admin":
        group_id = f"{group_id}-admins"
    # else: role == "member" -> use base group_id

    tuple_id: str = rebac_manager.rebac_write(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        zone_id=zone_id,
    )
    return tuple_id


def remove_user_from_zone(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
    role: str | None = None,
) -> None:
    """Remove user from zone via ReBAC group.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        zone_id: Zone ID
        role: Optional role to remove ("owner", "admin", or "member"). If None, removes all.

    Note:
        Removing owners should be done carefully - ensure at least one owner remains.
    """
    if role is None:
        # Remove from all groups (owner, admin, member)
        import contextlib

        for r in ["owner", "admin", "member"]:
            # Ignore errors if tuple doesn't exist
            with contextlib.suppress(Exception):
                remove_user_from_zone(rebac_manager, user_id, zone_id, r)
        return

    group_id = zone_group_id(zone_id)
    if role == "owner":
        group_id = f"{group_id}-owners"
    elif role == "admin":
        group_id = f"{group_id}-admins"
    # else: role == "member" -> use base group_id

    rebac_manager.rebac_delete(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        zone_id=zone_id,
    )


def get_user_zones(rebac_manager: Any, user_id: str) -> list[str]:
    """Get list of zone IDs that user belongs to.

    Uses direct DB query on rebac_tuples to find all zones where the user
    has any relation (owner, admin, member). Matches on the zone_id context
    column, which is set by add_user_to_zone() for all roles.

    Args:
        rebac_manager: ReBAC manager instance (must have _connection())
        user_id: User ID

    Returns:
        List of zone IDs

    Example:
        zones = get_user_zones(rebac_mgr, "user-123")
        # Returns: ["acme", "techcorp"]
    """
    zone_ids: list[str] = []
    try:
        with rebac_manager._connection() as conn:
            cursor = conn.cursor() if hasattr(conn, "cursor") else conn
            cursor.execute(
                "SELECT DISTINCT zone_id FROM rebac_tuples "
                "WHERE subject_type = 'user' AND subject_id = ? "
                "AND zone_id IS NOT NULL",
                (user_id,),
            )
            for row in cursor.fetchall():
                zid = row[0] if isinstance(row, (tuple, list)) else row["zone_id"]
                if zid and zid not in zone_ids:
                    zone_ids.append(zid)
    except Exception:
        pass
    return zone_ids


def user_belongs_to_zone(rebac_manager: Any, user_id: str, zone_id: str) -> bool:
    """Check if user belongs to zone.

    Checks rebac_tuples for any tuple where the user is the subject and the
    zone_id context column matches. This covers all roles (owner, admin,
    member) since add_user_to_zone() always sets zone_id on the tuple.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        zone_id: Zone ID

    Returns:
        True if user belongs to zone
    """
    try:
        with rebac_manager._connection() as conn:
            cursor = conn.cursor() if hasattr(conn, "cursor") else conn
            cursor.execute(
                "SELECT 1 FROM rebac_tuples "
                "WHERE subject_type = 'user' AND subject_id = ? "
                "AND zone_id = ? "
                "LIMIT 1",
                (user_id, zone_id),
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


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


def get_user_default_zone(rebac_manager: Any, user_id: str, _session: Session) -> str | None:
    """Get user's default zone.

    Priority:
    1. User's session preference (stored in session/cookie) - TODO: implement
    2. First zone in membership list
    3. None if user has no zones

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        session: Database session (for future session preference lookup)

    Returns:
        Zone ID or None if user has no zone memberships
    """
    # Get user's zone memberships
    zone_ids = get_user_zones(rebac_manager, user_id)

    if not zone_ids:
        return None

    # TODO: Add session preference lookup here
    # For now, return first zone
    return zone_ids[0]


def require_zone_context(
    rebac_manager: Any,
    user_id: str,
    zone_id: str | None,
    session: Session,
    auto_create: bool = False,
) -> str:
    """Require zone context for operation.

    If zone_id not provided, use default zone.
    If no default zone, raise error or create default.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        zone_id: Optional zone ID from request
        session: Database session
        auto_create: If True, create default zone if user has none

    Returns:
        Zone ID (guaranteed to be set)

    Raises:
        ValueError: If user has no zone memberships and auto_create=False
    """
    if zone_id:
        # Verify user belongs to this zone
        if not user_belongs_to_zone(rebac_manager, user_id, zone_id):
            raise ValueError(f"User {user_id} does not belong to zone {zone_id}")
        return zone_id

    # Get default zone
    default_zone = get_user_default_zone(rebac_manager, user_id, session)
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
