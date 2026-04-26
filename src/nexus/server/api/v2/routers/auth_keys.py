"""Auth key management REST API.

Provides REST endpoints for API key lifecycle operations:
- POST   /api/v2/auth/keys          — create a new API key
- GET    /api/v2/auth/keys          — list API keys
- GET    /api/v2/auth/keys/{key_id} — get key details
- DELETE /api/v2/auth/keys/{key_id} — revoke a key

Wraps the existing RPC admin handlers with proper REST semantics.
Admin auth required for all operations.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v2/auth/keys",
    tags=["auth"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class GrantRequest(BaseModel):
    """A single per-path permission grant."""

    path: str = Field(..., description="File or directory path (use /* suffix for directory)")
    role: Literal["viewer", "editor", "owner"] = Field(
        ..., description="Role: 'viewer', 'editor', or 'owner'"
    )

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v or not v.startswith("/"):
            raise ValueError("path must be absolute (start with '/')")
        if ".." in v.split("/"):
            raise ValueError("path must not contain '..' segments")
        return v


# Maps human-readable role names to ReBAC relation names.
ROLE_TO_RELATION: dict[str, str] = {
    "viewer": "direct_viewer",
    "editor": "direct_editor",
    "owner": "direct_owner",
}

MAX_GRANTS_PER_REQUEST = 100


class CreateKeyRequest(BaseModel):
    """Request body for creating an API key."""

    label: str | None = Field(default=None, description="Human-readable key name")
    name: str | None = Field(default=None, description="Alias for label")
    user_id: str | None = Field(default=None, description="User ID (auto-generated if omitted)")
    zone_id: str = Field(default=ROOT_ZONE_ID, description="Zone to bind the key to")
    subject_type: str = Field(default="user", description="Subject type: user, agent, service")
    subject_id: str | None = Field(default=None, description="Subject ID (defaults to user_id)")
    is_admin: bool = Field(default=False, description="Whether the key has admin privileges")
    expires_days: int | None = Field(default=None, description="Expiration in days from now")
    grants: list[GrantRequest] | None = Field(
        default=None,
        description="Optional per-path permission grants to create as ReBAC tuples",
    )

    @field_validator("grants")
    @classmethod
    def _validate_grants_limit(cls, v: list[GrantRequest] | None) -> list[GrantRequest] | None:
        if v is not None and len(v) > MAX_GRANTS_PER_REQUEST:
            raise ValueError(f"Too many grants: max {MAX_GRANTS_PER_REQUEST}")
        return v


class ListKeysParams(BaseModel):
    """Query parameters for listing API keys."""

    user_id: str | None = None
    zone_id: str | None = None
    is_admin: bool | None = None
    include_revoked: bool = False
    include_expired: bool = False
    limit: int = 100
    offset: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_rebac_manager(request: Request) -> Any:
    """Resolve the ReBACManager from app.state.

    Raises:
        HTTPException: 503 if rebac_manager is not available.
    """
    rebac_manager = getattr(request.app.state, "rebac_manager", None)
    if rebac_manager is None:
        raise HTTPException(
            status_code=503,
            detail="ReBAC manager not available; cannot create grants",
        )
    return rebac_manager


def _create_key_grants(
    rebac_manager: Any,
    subject_type: str,
    subject_id: str,
    grants: list[GrantRequest],
    zone_id: str,
    expires_at: datetime | None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Create ReBAC permission tuples for API key grants.

    Returns (created_grants for response, list of tuple_ids for storage).
    """
    tuples: list[dict[str, Any]] = []
    created_grants: list[dict[str, str]] = []

    for grant in grants:
        relation = ROLE_TO_RELATION[grant.role]
        tuples.append(
            {
                "subject": (subject_type, subject_id),
                "relation": relation,
                "object": ("file", grant.path),
                "zone_id": zone_id,
                "expires_at": expires_at,
            }
        )
        created_grants.append({"path": grant.path, "role": grant.role})

    tuple_ids: list[str] = []
    if tuples:
        # Snapshot pre-existing tuple_ids so we only track genuinely new ones
        pre_existing: set[str] = set()
        for t in tuples:
            for f in rebac_manager.rebac_list_tuples(
                subject=(t["subject"][0], t["subject"][1]),
                relation=t["relation"],
                object=(t["object"][0], t["object"][1]),
            ):
                tid = f.get("tuple_id", "")
                if tid:
                    pre_existing.add(tid)

        rebac_manager.rebac_write_batch(tuples)

        # Collect only newly created tuple_ids
        for t in tuples:
            for f in rebac_manager.rebac_list_tuples(
                subject=(t["subject"][0], t["subject"][1]),
                relation=t["relation"],
                object=(t["object"][0], t["object"][1]),
            ):
                tid = f.get("tuple_id", "")
                if tid and tid not in pre_existing and tid not in tuple_ids:
                    tuple_ids.append(tid)

    return created_grants, tuple_ids


def _store_grant_tuple_ids(db_provider: Any, key_id: str, tuple_ids: list[str]) -> None:
    """Persist grant tuple_ids on the API key record for targeted revocation cleanup."""
    from sqlalchemy import select

    from nexus.storage.models import APIKeyModel

    with db_provider.session_factory() as session:
        api_key = session.scalar(select(APIKeyModel).where(APIKeyModel.key_id == key_id))
        if api_key is not None:
            api_key.grant_tuple_ids = json.dumps(tuple_ids)
            session.commit()


def _resolve_db_auth(request: Request) -> Any:
    """Resolve the DatabaseAPIKeyAuth provider from app.state.

    Unwraps DiscriminatingAuthProvider if needed.

    Raises:
        HTTPException: 503 if no database auth provider is configured.
    """
    auth_provider = getattr(request.app.state, "auth_provider", None)
    if auth_provider is None:
        raise HTTPException(status_code=503, detail="Auth provider not configured")

    # If it's a DiscriminatingAuthProvider, get the underlying api_key_provider
    if hasattr(auth_provider, "api_key_provider") and auth_provider.api_key_provider is not None:
        db_provider = auth_provider.api_key_provider
    else:
        db_provider = auth_provider

    if not hasattr(db_provider, "session_factory"):
        raise HTTPException(
            status_code=503,
            detail="Database auth provider with session_factory required for key management",
        )

    return db_provider


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_key(
    request: Request,
    body: CreateKeyRequest,
) -> dict[str, Any]:
    """Create a new API key."""
    from types import SimpleNamespace

    from nexus.server.rpc.handlers.admin import handle_admin_create_key

    db_provider = _resolve_db_auth(request)

    # Fail-fast: verify rebac_manager is available before creating the key
    if body.grants:
        _resolve_rebac_manager(request)

    key_name = body.label or body.name or "unnamed"

    params = SimpleNamespace(
        user_id=body.user_id or "",
        name=key_name,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        zone_id=body.zone_id,
        is_admin=body.is_admin,
        expires_days=body.expires_days,
    )
    # Build a minimal context with is_admin=True (already verified by require_admin)
    context = SimpleNamespace(is_admin=True)

    result = handle_admin_create_key(db_provider, params, context)

    from nexus.storage.api_key_ops import get_primary_zone

    with db_provider.session_factory() as session:
        primary_zone = get_primary_zone(session, result["key_id"])

    # Normalize response keys for test fixture compatibility.
    # zone_id: prefer junction primary (get_primary_zone); fall back to result dict for
    # keys created via the legacy DatabaseAPIKeyAuth path which writes zone_id to the
    # column but not to the junction (#3871).
    response: dict[str, Any] = {
        "key_id": result["key_id"],
        "id": result["key_id"],
        "key": result["api_key"],
        "raw_key": result["api_key"],
        "user_id": result["user_id"],
        "name": result.get("name", key_name),
        "zone_id": primary_zone
        if primary_zone is not None
        else result.get("zone_id", body.zone_id),
        "is_admin": result.get("is_admin", body.is_admin),
        "expires_at": result.get("expires_at"),
    }

    # Create ReBAC grants if requested — rollback key on failure
    if body.grants:
        rebac_manager = _resolve_rebac_manager(request)
        subject_id = result.get("subject_id") or result["user_id"]
        subject_type = body.subject_type or "user"

        expires_at = None
        if body.expires_days:
            expires_at = datetime.now(UTC) + timedelta(days=body.expires_days)

        try:
            created_grants, tuple_ids = _create_key_grants(
                rebac_manager=rebac_manager,
                subject_type=subject_type,
                subject_id=subject_id,
                grants=body.grants,
                zone_id=body.zone_id,
                expires_at=expires_at,
            )
        except Exception:
            # Grant creation failed — revoke the key so it doesn't exist without grants
            logger.exception("Grant creation failed; rolling back API key %s", result["key_id"])
            from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

            with db_provider.session_factory() as session:
                DatabaseAPIKeyAuth.revoke_key(session, result["key_id"])
                session.commit()
            raise HTTPException(
                status_code=500,
                detail="Failed to create grants; API key has been rolled back",
            ) from None

        # Persist tuple_ids on the key for targeted cleanup on revocation
        if tuple_ids:
            _store_grant_tuple_ids(db_provider, result["key_id"], tuple_ids)

        response["grants"] = created_grants

    return response


@router.get("")
async def list_keys(
    request: Request,
    user_id: str | None = None,
    zone_id: str | None = None,
    is_admin: bool | None = None,
    include_revoked: bool = False,
    include_expired: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List API keys with optional filters."""
    from types import SimpleNamespace

    from nexus.server.rpc.handlers.admin import handle_admin_list_keys

    db_provider = _resolve_db_auth(request)
    params = SimpleNamespace(
        user_id=user_id,
        zone_id=zone_id,
        is_admin=is_admin,
        include_revoked=include_revoked,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    context = SimpleNamespace(is_admin=True)
    return handle_admin_list_keys(db_provider, params, context)


@router.get("/{key_id}")
async def get_key(
    request: Request,
    key_id: str,
    zone_id: str | None = None,
) -> dict[str, Any]:
    """Get details for a specific API key."""
    from types import SimpleNamespace

    from nexus.server.rpc.handlers.admin import handle_admin_get_key

    db_provider = _resolve_db_auth(request)
    params = SimpleNamespace(key_id=key_id, zone_id=zone_id)
    context = SimpleNamespace(is_admin=True)
    return handle_admin_get_key(db_provider, params, context)


@router.delete("/{key_id}")
async def revoke_key(
    request: Request,
    key_id: str,
    zone_id: str | None = None,
) -> dict[str, Any]:
    """Revoke an API key and clean up only the ReBAC grants created for it."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from nexus.server.dependencies import _reset_auth_cache
    from nexus.server.rpc.handlers.admin import handle_admin_revoke_key
    from nexus.storage.models import APIKeyModel

    db_provider = _resolve_db_auth(request)

    # Read grant_tuple_ids before revocation so we can do targeted cleanup
    grant_tuple_ids: list[str] = []
    with db_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        if zone_id:
            from nexus.storage.models import APIKeyZoneModel

            stmt = stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id).where(
                APIKeyZoneModel.zone_id == zone_id
            )
        api_key = session.scalar(stmt)
        if api_key is not None and api_key.grant_tuple_ids:
            try:
                grant_tuple_ids = json.loads(api_key.grant_tuple_ids)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid grant_tuple_ids JSON on key %s", key_id)

    params = SimpleNamespace(key_id=key_id, zone_id=zone_id)
    context = SimpleNamespace(is_admin=True)
    result = handle_admin_revoke_key(db_provider, params, context)

    # Delete only the ReBAC tuples that were created for this specific key
    if grant_tuple_ids:
        rebac_manager = getattr(request.app.state, "rebac_manager", None)
        if rebac_manager is not None:
            deleted = 0
            for tid in grant_tuple_ids:
                if rebac_manager.rebac_delete(tid):
                    deleted += 1
            if deleted:
                logger.info(
                    "Cleaned up %d/%d ReBAC grant tuple(s) for key %s",
                    deleted,
                    len(grant_tuple_ids),
                    key_id,
                )

    # Flush auth cache so revoked key is immediately rejected (Issue #2195)
    auth_cache = getattr(request.app.state, "auth_cache_store", None)
    await _reset_auth_cache(auth_cache)

    return result
