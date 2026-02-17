"""User management helper functions — backward compatibility re-exports.

User lookups have moved to: nexus.auth.user_queries
Zone membership has moved to: nexus.server.services.zone_membership

This file re-exports all symbols for backward compatibility.
"""

from __future__ import annotations

# OAuth lookup stays here (not moving in Phase 1)
from sqlalchemy import select
from sqlalchemy.orm import Session

# User lookup functions (now in auth brick)
from nexus.auth.user_queries import (  # noqa: F401
    check_email_available,
    check_username_available,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    validate_user_uniqueness,
)

# Zone helper functions (canonical: core.zone_helpers, re-exported for backward compat)
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

# Extended zone membership functions (server composition layer)
from nexus.server.services.zone_membership import (  # noqa: F401
    get_user_default_zone,
    require_zone_context,
    restore_user,
    soft_delete_user,
)
from nexus.storage.models import (
    UserModel,
    UserOAuthAccountModel,
)


def get_user_by_external_id(
    session: Session,
    external_user_id: str,
    external_user_service: str,
) -> UserModel | None:
    """Get active user by external service ID."""
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
    """Get user via OAuth account."""
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
