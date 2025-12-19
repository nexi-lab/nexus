"""User management helper functions.

Provides utility functions for:
- User lookup by various identifiers (email, username, OAuth, external ID)
- ReBAC group-based tenant membership management
- Tenant group naming conventions
- User creation with uniqueness checks
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.storage.models import (
    UserModel,
    UserOAuthAccountModel,
)


# ==============================================================================
# ReBAC Group Naming Helpers
# ==============================================================================


def tenant_group_id(tenant_id: str) -> str:
    """Generate tenant group ID from tenant_id.

    Args:
        tenant_id: Tenant identifier

    Returns:
        Group ID in format: tenant-{tenant_id}

    Example:
        tenant_group_id("acme") → "tenant-acme"
    """
    return f"tenant-{tenant_id}"


def parse_tenant_from_group(group_id: str) -> str | None:
    """Extract tenant_id from group ID.

    Args:
        group_id: Group ID (e.g., "tenant-acme")

    Returns:
        Tenant ID or None if not a tenant group

    Example:
        parse_tenant_from_group("tenant-acme") → "acme"
        parse_tenant_from_group("engineering") → None
    """
    if group_id.startswith("tenant-"):
        return group_id[len("tenant-") :]
    return None


def is_tenant_group(group_id: str) -> bool:
    """Check if group ID is a tenant group.

    Args:
        group_id: Group ID to check

    Returns:
        True if group ID is a tenant group (starts with "tenant-")
    """
    return group_id.startswith("tenant-")


def add_user_to_tenant(
    rebac_manager: Any,
    user_id: str,
    tenant_id: str,
    role: str = "member",
) -> str:
    """Add user to tenant via ReBAC group.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        tenant_id: Tenant ID
        role: Role in tenant ("admin" or "member")

    Returns:
        ReBAC tuple ID

    Example:
        # Add user as member
        add_user_to_tenant(rebac_mgr, "user-123", "acme", "member")

        # Add user as admin
        add_user_to_tenant(rebac_mgr, "user-123", "acme", "admin")
    """
    group_id = tenant_group_id(tenant_id)
    if role == "admin":
        group_id = f"{group_id}-admins"

    return rebac_manager.rebac_write(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        tenant_id=tenant_id,
    )


def remove_user_from_tenant(
    rebac_manager: Any,
    user_id: str,
    tenant_id: str,
    role: str | None = None,
) -> None:
    """Remove user from tenant via ReBAC group.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        tenant_id: Tenant ID
        role: Optional role to remove ("admin" or "member"). If None, removes both.
    """
    if role is None:
        # Remove from both member and admin groups
        for r in ["member", "admin"]:
            remove_user_from_tenant(rebac_manager, user_id, tenant_id, r)
        return

    group_id = tenant_group_id(tenant_id)
    if role == "admin":
        group_id = f"{group_id}-admins"

    rebac_manager.rebac_delete(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        tenant_id=tenant_id,
    )


def get_user_tenants(rebac_manager: Any, user_id: str) -> list[str]:
    """Get list of tenant IDs that user belongs to.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID

    Returns:
        List of tenant IDs

    Example:
        tenants = get_user_tenants(rebac_mgr, "user-123")
        # Returns: ["acme", "techcorp"]
    """
    # Query ReBAC for user's group memberships
    tuples = rebac_manager.rebac_query(
        subject=("user", user_id),
        relation="member",
        object_type="group",
    )

    # Extract tenant IDs from tenant groups
    tenant_ids = []
    for t in tuples:
        tenant_id = parse_tenant_from_group(t.object.entity_id)
        if tenant_id and tenant_id not in tenant_ids:
            # Remove "-admins" suffix if present
            if tenant_id.endswith("-admins"):
                tenant_id = tenant_id[: -len("-admins")]
            tenant_ids.append(tenant_id)

    return tenant_ids


def user_belongs_to_tenant(
    rebac_manager: Any, user_id: str, tenant_id: str
) -> bool:
    """Check if user belongs to tenant.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        tenant_id: Tenant ID

    Returns:
        True if user belongs to tenant
    """
    return tenant_id in get_user_tenants(rebac_manager, user_id)


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
# Default Tenant Selection
# ==============================================================================


def get_user_default_tenant(
    rebac_manager: Any, user_id: str, session: Session
) -> str | None:
    """Get user's default tenant.

    Priority:
    1. User's session preference (stored in session/cookie) - TODO: implement
    2. First tenant in membership list
    3. None if user has no tenants

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        session: Database session (for future session preference lookup)

    Returns:
        Tenant ID or None if user has no tenant memberships
    """
    # Get user's tenant memberships
    tenant_ids = get_user_tenants(rebac_manager, user_id)

    if not tenant_ids:
        return None

    # TODO: Add session preference lookup here
    # For now, return first tenant
    return tenant_ids[0]


def require_tenant_context(
    rebac_manager: Any,
    user_id: str,
    tenant_id: str | None,
    session: Session,
    auto_create: bool = False,
) -> str:
    """Require tenant context for operation.

    If tenant_id not provided, use default tenant.
    If no default tenant, raise error or create default.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        tenant_id: Optional tenant ID from request
        session: Database session
        auto_create: If True, create default tenant if user has none

    Returns:
        Tenant ID (guaranteed to be set)

    Raises:
        ValueError: If user has no tenant memberships and auto_create=False
    """
    if tenant_id:
        # Verify user belongs to this tenant
        if not user_belongs_to_tenant(rebac_manager, user_id, tenant_id):
            raise ValueError(f"User {user_id} does not belong to tenant {tenant_id}")
        return tenant_id

    # Get default tenant
    default_tenant = get_user_default_tenant(rebac_manager, user_id, session)
    if default_tenant:
        return default_tenant

    # No tenant - create default or error
    if auto_create:
        # TODO: Implement default tenant creation
        # For now, raise error
        raise ValueError(
            f"User {user_id} has no tenant memberships. "
            "Auto-create not yet implemented."
        )
    else:
        raise ValueError(f"User {user_id} has no tenant memberships")


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
