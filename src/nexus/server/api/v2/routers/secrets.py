"""User secrets management REST API.

Endpoints for managing user-scoped encrypted secrets:
- POST   /api/v2/secrets          — set (create/update) a secret
- GET    /api/v2/secrets          — list secret names
- DELETE /api/v2/secrets/{name}   — delete a secret
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.dependencies import _get_operation_context, get_auth_result

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v2/secrets",
    tags=["secrets"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SetSecretRequest(BaseModel):
    """Request body for setting a secret."""

    name: str = Field(..., min_length=1, max_length=255, description="Secret name")
    value: str = Field(..., min_length=1, description="Secret value (will be encrypted)")


class SecretMetadata(BaseModel):
    """Secret metadata (never includes value)."""

    secret_id: str
    name: str
    created_at: str | None = None
    updated_at: str | None = None


class SecretListResponse(BaseModel):
    """Response for listing secrets."""

    secrets: list[SecretMetadata]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_secrets_service(request: Request) -> Any:
    """Resolve UserSecretsService from app state."""
    service = getattr(request.app.state, "user_secrets_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="User secrets service not configured")
    return service


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=200)
async def set_secret(
    body: SetSecretRequest,
    request: Request,
    auth_result: dict[str, Any] = Depends(get_auth_result),
) -> dict[str, Any]:
    """Create or update a user secret."""
    context = _get_operation_context(auth_result)
    service = _get_secrets_service(request)

    secret_id = service.set_secret(
        user_id=context.user_id,
        name=body.name,
        value=body.value,
        zone_id=context.zone_id or ROOT_ZONE_ID,
    )

    return {"secret_id": secret_id, "name": body.name}


@router.get("")
async def list_secrets(
    request: Request,
    auth_result: dict[str, Any] = Depends(get_auth_result),
) -> SecretListResponse:
    """List all secret names for the current user (values never returned)."""
    context = _get_operation_context(auth_result)
    service = _get_secrets_service(request)

    secrets = service.list_secrets(
        user_id=context.user_id,
        zone_id=context.zone_id or ROOT_ZONE_ID,
    )

    return SecretListResponse(
        secrets=[SecretMetadata(**s) for s in secrets],
        count=len(secrets),
    )


@router.delete("/{name}")
async def delete_secret(
    name: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(get_auth_result),
) -> dict[str, Any]:
    """Delete a user secret by name."""
    context = _get_operation_context(auth_result)
    service = _get_secrets_service(request)

    deleted = service.delete_secret(
        user_id=context.user_id,
        name=name,
        zone_id=context.zone_id or ROOT_ZONE_ID,
    )

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Secret {name!r} not found")

    return {"deleted": True, "name": name}
