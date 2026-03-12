"""Admin API RPC handler functions (v0.5.1).

Extracted from fastapi_server.py (#1602). Admin handlers accept both
``nexus_fs`` and ``auth_provider`` as explicit parameters.
"""

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def require_admin(context: Any) -> None:
    """Require admin privileges for admin operations."""
    from nexus.contracts.exceptions import NexusPermissionError

    if not context or not getattr(context, "is_admin", False):
        raise NexusPermissionError("Admin privileges required for this operation")


def require_database_auth(auth_provider: Any) -> None:
    """Validate that a database auth provider with session_factory is configured.

    Raises:
        ConfigurationError: If auth_provider is missing or lacks session_factory.
    """
    from nexus.contracts.exceptions import ConfigurationError

    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise ConfigurationError(
            "Admin operations require DatabaseAPIKeyAuth provider with session_factory"
        )


def format_api_key_response(api_key: Any, *, include_sensitive: bool = False) -> dict[str, Any]:
    """Serialize an APIKeyModel to a response dict.

    Args:
        api_key: SQLAlchemy APIKeyModel instance.
        include_sensitive: If True, include revocation and usage fields.

    Returns:
        Dict with key metadata, safe for JSON serialization.
    """
    result: dict[str, Any] = {
        "key_id": api_key.key_id,
        "user_id": api_key.user_id,
        "subject_type": api_key.subject_type,
        "subject_id": api_key.subject_id,
        "name": api_key.name,
        "zone_id": api_key.zone_id,
        "is_admin": bool(api_key.is_admin),
        "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
        "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
    }
    if include_sensitive:
        result["revoked"] = bool(api_key.revoked)
        result["revoked_at"] = api_key.revoked_at.isoformat() if api_key.revoked_at else None
        result["last_used_at"] = api_key.last_used_at.isoformat() if api_key.last_used_at else None
    return result


def handle_admin_write_permission(nexus_fs: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_write_permission — write ReBAC relationship tuples.

    Unlike other admin handlers, this receives ``nexus_fs`` (not auth_provider)
    because the ``_rebac_manager`` lives on the NexusFS instance.
    """
    from nexus.contracts.exceptions import ConfigurationError

    require_admin(context)

    rebac = getattr(nexus_fs, "_rebac_manager", None) or getattr(nexus_fs, "rebac_manager", None)
    if rebac is None:
        raise ConfigurationError("ReBAC manager not available on this server")

    tuples = getattr(params, "tuples", [])
    created = 0
    for t in tuples:
        subject = tuple(t["subject"])
        relation = t["relation"]
        obj = tuple(t["object"])
        zone_id = t.get("zone_id", "root")
        rebac.rebac_write(
            subject=subject,
            relation=relation,
            object=obj,
            zone_id=zone_id,
        )
        created += 1

    return {"created": created}


def handle_admin_create_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_create_key method."""
    import uuid
    from datetime import timedelta

    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.bricks.rebac.entity_registry import EntityRegistry

    require_admin(context)
    require_database_auth(auth_provider)

    user_id = params.user_id
    if not user_id:
        user_id = f"user_{uuid.uuid4().hex[:12]}"

    if params.subject_type == "user" or not params.subject_type:
        # Resolve _record_store: DiscriminatingAuthProvider delegates to api_key_provider
        _record_store = getattr(auth_provider, "_record_store", None)
        if _record_store is None and hasattr(auth_provider, "api_key_provider"):
            _record_store = getattr(auth_provider.api_key_provider, "_record_store", None)
        if _record_store is not None:
            entity_registry = EntityRegistry(_record_store)
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
    require_database_auth(auth_provider)

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

        keys = [format_api_key_response(key, include_sensitive=True) for key in api_keys]
        return {"keys": keys, "total": total}


def handle_admin_get_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_get_key method."""
    from sqlalchemy import select

    from nexus.contracts.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    require_admin(context)
    require_database_auth(auth_provider)

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        if params.zone_id:
            stmt = stmt.where(APIKeyModel.zone_id == params.zone_id)
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        return format_api_key_response(api_key, include_sensitive=True)


def handle_admin_revoke_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_revoke_key method."""
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.contracts.exceptions import NexusFileNotFoundError

    require_admin(context)
    require_database_auth(auth_provider)

    with auth_provider.session_factory() as session:
        success = DatabaseAPIKeyAuth.revoke_key(session, params.key_id, zone_id=params.zone_id)
        if not success:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        session.commit()
        return {"success": True, "key_id": params.key_id}


def handle_admin_update_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_update_key method."""
    from datetime import timedelta

    from sqlalchemy import select

    from nexus.contracts.exceptions import NexusFileNotFoundError, ValidationError
    from nexus.storage.models import APIKeyModel

    require_admin(context)
    require_database_auth(auth_provider)

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        if params.zone_id:
            stmt = stmt.where(APIKeyModel.zone_id == params.zone_id)
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        # Self-demotion guard: prevent removing admin from the last admin key
        if params.is_admin is False and api_key.is_admin:
            from sqlalchemy import func

            count_stmt = select(func.count()).where(
                APIKeyModel.is_admin == 1,
                APIKeyModel.revoked == 0,
            )
            if api_key.zone_id:
                count_stmt = count_stmt.where(APIKeyModel.zone_id == api_key.zone_id)
            admin_count = session.scalar(count_stmt)
            if admin_count is not None and admin_count <= 1:
                raise ValidationError("Cannot remove admin privileges from the last admin key")

        if params.name is not None:
            api_key.name = params.name
        if params.is_admin is not None:
            api_key.is_admin = int(params.is_admin)
        if params.expires_days is not None:
            api_key.expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

        session.commit()

        return {
            "success": True,
            **format_api_key_response(api_key),
        }
