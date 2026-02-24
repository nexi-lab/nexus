"""Auth key management REST API.

Provides REST endpoints for API key lifecycle operations:
- POST   /api/v2/auth/keys          — create a new API key
- GET    /api/v2/auth/keys          — list API keys
- GET    /api/v2/auth/keys/{key_id} — get key details
- DELETE /api/v2/auth/keys/{key_id} — revoke a key

Wraps the existing RPC admin handlers with proper REST semantics.
Admin auth required for all operations.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

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

    # Normalize response keys for test fixture compatibility
    return {
        "key_id": result["key_id"],
        "id": result["key_id"],
        "key": result["api_key"],
        "raw_key": result["api_key"],
        "user_id": result["user_id"],
        "name": result.get("name", key_name),
        "zone_id": result.get("zone_id", body.zone_id),
        "is_admin": result.get("is_admin", body.is_admin),
        "expires_at": result.get("expires_at"),
    }


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
    """Revoke an API key."""
    from types import SimpleNamespace

    from nexus.server.dependencies import _reset_auth_cache
    from nexus.server.rpc.handlers.admin import handle_admin_revoke_key

    db_provider = _resolve_db_auth(request)
    params = SimpleNamespace(key_id=key_id, zone_id=zone_id)
    context = SimpleNamespace(is_admin=True)
    result = handle_admin_revoke_key(db_provider, params, context)

    # Flush auth cache so revoked key is immediately rejected (Issue #2195)
    auth_cache = getattr(request.app.state, "auth_cache_store", None)
    await _reset_auth_cache(auth_cache)

    return result
