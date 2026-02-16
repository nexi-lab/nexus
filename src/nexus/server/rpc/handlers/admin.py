"""Admin API RPC handler functions (v0.5.1).

Extracted from fastapi_server.py (#1602). Admin handlers accept both
``nexus_fs`` and ``auth_provider`` as explicit parameters.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def require_admin(context: Any) -> None:
    """Require admin privileges for admin operations."""
    from nexus.core.exceptions import NexusPermissionError

    if not context or not getattr(context, "is_admin", False):
        raise NexusPermissionError("Admin privileges required for this operation")


def handle_admin_create_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_create_key method."""
    import uuid
    from datetime import timedelta

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.services.permissions.entity_registry import EntityRegistry

    require_admin(context)

    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    user_id = params.user_id
    if not user_id:
        user_id = f"user_{uuid.uuid4().hex[:12]}"

    if params.subject_type == "user" or not params.subject_type:
        entity_registry = EntityRegistry(auth_provider.session_factory)
        entity_registry.register_entity(
            entity_type="user",
            entity_id=user_id,
            parent_type="zone",
            parent_id=params.zone_id,
        )

    expires_at = None
    if params.expires_days:
        expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

    with auth_provider.session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id=user_id,
            name=params.name,
            subject_type=params.subject_type,
            subject_id=params.subject_id,
            zone_id=params.zone_id,
            is_admin=params.is_admin,
            expires_at=expires_at,
        )
        session.commit()

        return {
            "key_id": key_id,
            "api_key": raw_key,
            "user_id": user_id,
            "name": params.name,
            "subject_type": params.subject_type,
            "subject_id": params.subject_id or user_id,
            "zone_id": params.zone_id,
            "is_admin": params.is_admin,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }


def handle_admin_list_keys(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_list_keys method.

    Performance optimized: All filtering happens in SQL instead of Python.
    """
    from sqlalchemy import func, or_, select

    from nexus.storage.models import APIKeyModel

    require_admin(context)

    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel)

        if params.user_id:
            stmt = stmt.where(APIKeyModel.user_id == params.user_id)
        if params.zone_id:
            stmt = stmt.where(APIKeyModel.zone_id == params.zone_id)
        if params.is_admin is not None:
            stmt = stmt.where(APIKeyModel.is_admin == int(params.is_admin))
        if not params.include_revoked:
            stmt = stmt.where(APIKeyModel.revoked == 0)

        if not params.include_expired:
            now = datetime.now(UTC)
            stmt = stmt.where(
                or_(
                    APIKeyModel.expires_at.is_(None),
                    APIKeyModel.expires_at > now,
                )
            )

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = session.scalar(count_stmt) or 0

        stmt = stmt.order_by(APIKeyModel.created_at.desc())
        stmt = stmt.limit(params.limit).offset(params.offset)
        api_keys = list(session.scalars(stmt).all())

        keys = []
        for key in api_keys:
            keys.append(
                {
                    "key_id": key.key_id,
                    "user_id": key.user_id,
                    "subject_type": key.subject_type,
                    "subject_id": key.subject_id,
                    "name": key.name,
                    "zone_id": key.zone_id,
                    "is_admin": bool(key.is_admin),
                    "created_at": key.created_at.isoformat() if key.created_at else None,
                    "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                    "revoked": bool(key.revoked),
                    "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
                    "last_used_at": (key.last_used_at.isoformat() if key.last_used_at else None),
                }
            )

        return {"keys": keys, "total": total}


def handle_admin_get_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_get_key method."""
    from sqlalchemy import select

    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    require_admin(context)

    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        return {
            "key_id": api_key.key_id,
            "user_id": api_key.user_id,
            "subject_type": api_key.subject_type,
            "subject_id": api_key.subject_id,
            "name": api_key.name,
            "zone_id": api_key.zone_id,
            "is_admin": bool(api_key.is_admin),
            "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
            "revoked": bool(api_key.revoked),
            "revoked_at": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
            "last_used_at": (api_key.last_used_at.isoformat() if api_key.last_used_at else None),
        }


def handle_admin_revoke_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_revoke_key method."""
    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    require_admin(context)

    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        success = DatabaseAPIKeyAuth.revoke_key(session, params.key_id)
        if not success:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        session.commit()
        return {"success": True, "key_id": params.key_id}


def handle_admin_update_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_update_key method."""
    from datetime import timedelta

    from sqlalchemy import select

    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    require_admin(context)

    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        if params.name is not None:
            api_key.name = params.name
        if params.is_admin is not None:
            api_key.is_admin = int(params.is_admin)
        if params.expires_days is not None:
            api_key.expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

        session.commit()

        return {
            "success": True,
            "key_id": api_key.key_id,
            "name": api_key.name,
            "is_admin": bool(api_key.is_admin),
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        }
