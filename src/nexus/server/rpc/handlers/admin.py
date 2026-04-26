"""Admin API RPC handler functions (v0.5.1).

Extracted from fastapi_server.py (#1602). Admin handlers accept both
``nexus_fs`` and ``auth_provider`` as explicit parameters.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


ROLE_TO_RELATION: dict[str, str] = {
    "viewer": "direct_viewer",
    "editor": "direct_editor",
    "owner": "direct_owner",
}


def _resolve_record_store(auth_provider: Any) -> Any | None:
    """Resolve _record_store from auth_provider, unwrapping if needed."""
    store = getattr(auth_provider, "_record_store", None)
    if store is None and hasattr(auth_provider, "api_key_provider"):
        store = getattr(auth_provider.api_key_provider, "_record_store", None)
    return store


def _create_grants_for_key(
    auth_provider: Any,
    subject_type: str,
    subject_id: str,
    grants: list[dict[str, str]],
    zone_id: str,
    expires_at: "datetime | None",
    key_id: str,
) -> list[dict[str, str]]:
    """Create ReBAC permission tuples for API key grants (Issue #3128).

    Creates tuples and stores their IDs on the API key record for
    targeted cleanup on revocation.

    Args:
        auth_provider: Auth provider (to resolve record_store/engine).
        subject_type: e.g. "user" or "agent".
        subject_id: Subject identifier.
        grants: List of {"path": ..., "role": ...} dicts.
        zone_id: Zone to scope tuples to.
        expires_at: Optional expiry for the tuples.
        key_id: API key ID to store tuple_ids on.

    Returns:
        List of created grant dicts for the response.
    """
    from sqlalchemy import select

    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.storage.models import APIKeyModel

    record_store = _resolve_record_store(auth_provider)
    if record_store is None:
        raise RuntimeError("Cannot create grants: record_store not available")

    manager = ReBACManager(engine=record_store.engine, cache_ttl_seconds=1, max_depth=5)
    try:
        tuples: list[dict[str, Any]] = []
        created: list[dict[str, str]] = []
        for grant in grants:
            path = grant.get("path", "")
            role = grant.get("role", "")
            relation = ROLE_TO_RELATION.get(role)
            if not relation:
                raise ValueError(
                    f"Invalid role: {role!r}. Must be one of: {list(ROLE_TO_RELATION)}"
                )
            if not path or not path.startswith("/"):
                raise ValueError(f"Invalid path: {path!r}. Must be absolute.")
            tuples.append(
                {
                    "subject": (subject_type, subject_id),
                    "relation": relation,
                    "object": ("file", path),
                    "zone_id": zone_id or ROOT_ZONE_ID,
                    "expires_at": expires_at,
                }
            )
            created.append({"path": path, "role": role})

        if tuples:
            # Snapshot pre-existing tuple_ids so we only track genuinely new ones
            pre_existing: set[str] = set()
            for t in tuples:
                for f in manager.rebac_list_tuples(
                    subject=(t["subject"][0], t["subject"][1]),
                    relation=t["relation"],
                    object=(t["object"][0], t["object"][1]),
                ):
                    tid = f.get("tuple_id", "")
                    if tid:
                        pre_existing.add(tid)

            manager.rebac_write_batch(tuples)

            # Collect only newly created tuple_ids
            tuple_ids: list[str] = []
            for t in tuples:
                for f in manager.rebac_list_tuples(
                    subject=(t["subject"][0], t["subject"][1]),
                    relation=t["relation"],
                    object=(t["object"][0], t["object"][1]),
                ):
                    tid = f.get("tuple_id", "")
                    if tid and tid not in pre_existing and tid not in tuple_ids:
                        tuple_ids.append(tid)

            # Store tuple_ids on the key for targeted revocation cleanup
            if tuple_ids:
                with auth_provider.session_factory() as session:
                    api_key = session.scalar(
                        select(APIKeyModel).where(APIKeyModel.key_id == key_id)
                    )
                    if api_key is not None:
                        api_key.grant_tuple_ids = json.dumps(tuple_ids)
                        session.commit()

        return created
    finally:
        manager.close()


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


def format_api_key_response(
    api_key: Any,
    *,
    include_sensitive: bool = False,
    primary_zone: str | None = None,
) -> dict[str, Any]:
    """Serialize an APIKeyModel to a response dict.

    Args:
        api_key: SQLAlchemy APIKeyModel instance.
        include_sensitive: If True, include revocation and usage fields.
        primary_zone: Precomputed primary zone from junction (get_primary_zone).
            When provided, emitted as zone_id instead of the deprecated
            APIKeyModel.zone_id column.  Callers that don't pass this arg
            fall back to the legacy column read for backward compat.

    Returns:
        Dict with key metadata, safe for JSON serialization.
    """
    result: dict[str, Any] = {
        "key_id": api_key.key_id,
        "user_id": api_key.user_id,
        "subject_type": api_key.subject_type,
        "subject_id": api_key.subject_id,
        "name": api_key.name,
        "zone_id": primary_zone if primary_zone is not None else api_key.zone_id,
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
        zone_id = t.get("zone_id", ROOT_ZONE_ID)
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

    _record_store = _resolve_record_store(auth_provider)
    if _record_store is not None:
        entity_registry = EntityRegistry(_record_store)
        subject_type = params.subject_type or "user"
        entity_id = params.subject_id or user_id if subject_type == "agent" else user_id
        entity_registry.register_entity(
            entity_type=subject_type,
            entity_id=entity_id,
            parent_type="zone",
            parent_id=params.zone_id,
            entity_metadata={"name": params.name} if params.name else None,
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

        result: dict[str, Any] = {
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

        # Create ReBAC grants if requested (Issue #3128)
        grants = getattr(params, "grants", None)
        if grants:
            created = _create_grants_for_key(
                auth_provider=auth_provider,
                subject_type=params.subject_type or "user",
                subject_id=params.subject_id or user_id,
                grants=grants,
                zone_id=params.zone_id,
                expires_at=expires_at,
                key_id=key_id,
            )
            result["grants"] = created

        return result


def handle_admin_list_keys(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_list_keys method.

    Performance optimized: All filtering happens in SQL instead of Python.

    Zone access filter — matches every key that grants this zone, not only
    keys whose primary is this zone (#3871).
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
            from nexus.storage.models import APIKeyZoneModel

            stmt = (
                stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
                .where(APIKeyZoneModel.zone_id == params.zone_id)
                .distinct()
            )
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

        from nexus.storage.api_key_ops import get_primary_zones_for_keys

        primary_map = get_primary_zones_for_keys(session, [k.key_id for k in api_keys])
        keys = [
            format_api_key_response(
                key, include_sensitive=True, primary_zone=primary_map.get(key.key_id)
            )
            for key in api_keys
        ]
        return {"keys": keys, "total": total}


def handle_admin_get_key(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_get_key method.

    Zone access filter — matches every key that grants this zone, not only
    keys whose primary is this zone (#3871).
    """
    from sqlalchemy import select

    from nexus.contracts.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    require_admin(context)
    require_database_auth(auth_provider)

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        if params.zone_id:
            from nexus.storage.models import APIKeyZoneModel

            stmt = stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id).where(
                APIKeyZoneModel.zone_id == params.zone_id
            )
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        from nexus.storage.api_key_ops import get_primary_zone

        primary = get_primary_zone(session, api_key.key_id)
        return format_api_key_response(api_key, include_sensitive=True, primary_zone=primary)


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
    """Handle admin_update_key method.

    Zone access filter — matches every key that grants this zone, not only
    keys whose primary is this zone (#3871).

    The self-demotion guard counts admins whose junction zones overlap (OR/IN)
    with the caller's zone set, and uses ``count(DISTINCT key_id)`` to handle
    multi-zone admins correctly.  A multi-zone admin is the "last" only if no
    other admin covers any of their zones.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from nexus.contracts.exceptions import NexusFileNotFoundError, ValidationError
    from nexus.storage.models import APIKeyModel

    require_admin(context)
    require_database_auth(auth_provider)

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        if params.zone_id:
            from nexus.storage.models import APIKeyZoneModel

            stmt = stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id).where(
                APIKeyZoneModel.zone_id == params.zone_id
            )
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        # Self-demotion guard: prevent removing admin from the last admin key
        if params.is_admin is False and api_key.is_admin:
            import sqlalchemy as sa
            from sqlalchemy import func

            from nexus.storage.api_key_ops import get_zones_for_key

            caller_zones = get_zones_for_key(session, api_key.key_id)

            # Base predicates: live admin keys.
            base_filters = [APIKeyModel.is_admin == 1, APIKeyModel.revoked == 0]

            if caller_zones:
                from nexus.storage.models import APIKeyZoneModel

                count_stmt = (
                    select(func.count(sa.distinct(APIKeyModel.key_id)))
                    .select_from(APIKeyModel)
                    .where(*base_filters)
                    .join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
                    .where(APIKeyZoneModel.zone_id.in_(caller_zones))
                )
            else:
                count_stmt = (
                    select(func.count(sa.distinct(APIKeyModel.key_id)))
                    .select_from(APIKeyModel)
                    .where(*base_filters)
                )
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

        from nexus.storage.api_key_ops import get_primary_zone

        primary = get_primary_zone(session, api_key.key_id)
        return {
            "success": True,
            **format_api_key_response(api_key, primary_zone=primary),
        }
