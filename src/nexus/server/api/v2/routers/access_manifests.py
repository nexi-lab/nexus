"""Access Manifests API v2 router (Issue #1754).

Provides manifest lifecycle endpoints:
- POST /api/v2/access-manifests           — Create manifest (generates ReBAC tuples)
- GET  /api/v2/access-manifests/{id}      — Get manifest
- GET  /api/v2/access-manifests           — List manifests (paginated)
- POST /api/v2/access-manifests/{id}/evaluate — Evaluate tool permission
- POST /api/v2/access-manifests/{id}/revoke   — Revoke (deletes ReBAC tuples)

Pattern: Follows identity.py router (Depends on app.state, require_auth).
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)


def _authorize_agent_access(
    auth_result: dict[str, Any],
    agent_id: str,
    action: str = "access",
) -> None:
    """Verify caller is authorized to act on an agent's manifests.

    Admins may operate on any agent.  Non-admins must match agent_id
    or share the same zone scope.
    """
    ctx = get_operation_context(auth_result)
    if ctx.is_admin:
        return
    if ctx.subject_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"Not authorized to {action} manifests for agent '{agent_id}'",
        )


router = APIRouter(prefix="/api/v2/access-manifests", tags=["access_manifests"])

# =============================================================================
# Request/Response Models
# =============================================================================


class ManifestEntryRequest(BaseModel):
    """Single rule in a manifest."""

    tool_pattern: str = Field(..., description="Glob pattern for tool names")
    permission: str = Field(..., description="'allow' or 'deny'")
    max_calls_per_minute: int | None = Field(None, description="Optional rate limit")


class CreateManifestRequest(BaseModel):
    """Request to create an access manifest."""

    agent_id: str = Field(..., description="Agent this manifest applies to")
    name: str = Field(..., description="Human-readable name")
    entries: list[ManifestEntryRequest] = Field(
        ..., description="Ordered access rules (first-match-wins)"
    )
    zone_id: str = Field("root", description="Zone scope")
    created_by: str = Field("", description="Creator identifier")
    valid_hours: int = Field(720, description="Validity period in hours", ge=1, le=8760)
    credential_id: str | None = Field(None, description="Optional backing VC")


class EvaluateRequest(BaseModel):
    """Request to evaluate tool permission."""

    tool_name: str = Field(..., description="Tool name to evaluate")


# =============================================================================
# Dependencies
# =============================================================================


def _get_manifest_service(request: Request) -> Any:
    """Get AccessManifestService from ServiceRegistry or app.state."""
    nx = getattr(request.app.state, "nexus_fs", None)
    if nx is not None:
        _svc_fn = getattr(nx, "service", None)
        if _svc_fn is not None:
            svc = _svc_fn("manifest_resolver")
            if svc is not None:
                return svc

    # Fallback: check direct app.state
    svc = getattr(request.app.state, "access_manifest_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Access manifest service not available")
    return svc


# =============================================================================
# Endpoints
# =============================================================================


@router.post("")
async def create_manifest(
    body: CreateManifestRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    manifest_service: Any = Depends(_get_manifest_service),
) -> dict:
    """Create a new access manifest with ReBAC tuple generation."""
    _authorize_agent_access(auth_result, body.agent_id, "create")
    from nexus.contracts.access_manifest_types import ManifestEntry, ToolPermission

    entries = tuple(
        ManifestEntry(
            tool_pattern=e.tool_pattern,
            permission=ToolPermission(e.permission),
            max_calls_per_minute=e.max_calls_per_minute,
        )
        for e in body.entries
    )

    # H5: Enforce zone isolation — use authenticated zone, not request body
    caller_zone = auth_result.get("zone_id")
    if (
        body.zone_id
        and caller_zone
        and body.zone_id != caller_zone
        and not auth_result.get("is_admin", False)
    ):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot create manifest in zone '{body.zone_id}' — authenticated for zone '{caller_zone}'",
        )
    effective_zone = body.zone_id or caller_zone

    manifest = await asyncio.to_thread(
        manifest_service.create_manifest,
        agent_id=body.agent_id,
        name=body.name,
        entries=entries,
        zone_id=effective_zone,
        created_by=body.created_by,
        valid_hours=body.valid_hours,
        credential_id=body.credential_id,
    )

    return {
        "manifest_id": manifest.id,
        "agent_id": manifest.agent_id,
        "zone_id": manifest.zone_id,
        "name": manifest.name,
        "entries": [
            {
                "tool_pattern": e.tool_pattern,
                "permission": e.permission,
                "max_calls_per_minute": e.max_calls_per_minute,
            }
            for e in manifest.entries
        ],
        "status": manifest.status,
        "valid_from": manifest.valid_from,
        "valid_until": manifest.valid_until,
    }


@router.get("/{manifest_id}")
async def get_manifest(
    manifest_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    manifest_service: Any = Depends(_get_manifest_service),
) -> dict:
    """Get a single manifest by ID."""
    manifest = await asyncio.to_thread(manifest_service.get_manifest, manifest_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifest not found")
    _authorize_agent_access(auth_result, manifest.agent_id, "read")

    return {
        "manifest_id": manifest.id,
        "agent_id": manifest.agent_id,
        "zone_id": manifest.zone_id,
        "name": manifest.name,
        "entries": [
            {
                "tool_pattern": e.tool_pattern,
                "permission": e.permission,
                "max_calls_per_minute": e.max_calls_per_minute,
            }
            for e in manifest.entries
        ],
        "status": manifest.status,
        "valid_from": manifest.valid_from,
        "valid_until": manifest.valid_until,
    }


@router.get("")
async def list_manifests(
    agent_id: str | None = None,
    zone_id: str | None = None,
    active_only: bool = False,
    offset: int = 0,
    limit: int = 50,
    auth_result: dict[str, Any] = Depends(require_auth),
    manifest_service: Any = Depends(_get_manifest_service),
) -> dict:
    """List manifests with optional filters (paginated)."""
    # Scope non-admin callers to their own agent_id
    ctx = get_operation_context(auth_result)
    if not ctx.is_admin and agent_id and ctx.subject_id != agent_id:
        raise HTTPException(
            status_code=403, detail="Not authorized to list manifests for this agent"
        )
    if not ctx.is_admin and not agent_id:
        agent_id = ctx.subject_id
    manifests = await asyncio.to_thread(
        manifest_service.list_manifests,
        agent_id=agent_id,
        zone_id=zone_id,
        active_only=active_only,
        offset=offset,
        limit=limit,
    )

    return {
        "manifests": [
            {
                "manifest_id": m.id,
                "agent_id": m.agent_id,
                "zone_id": m.zone_id,
                "name": m.name,
                "status": m.status,
                "valid_from": m.valid_from,
                "valid_until": m.valid_until,
            }
            for m in manifests
        ],
        "offset": offset,
        "limit": limit,
        "count": len(manifests),
    }


@router.post("/{manifest_id}/evaluate")
async def evaluate_tool(
    manifest_id: str,
    body: EvaluateRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    manifest_service: Any = Depends(_get_manifest_service),
) -> dict:
    """Evaluate whether a tool is allowed for a manifest's agent.

    Returns a full evaluation trace (proof tree) showing which manifest
    entries were checked, which one matched, and the final decision.
    """
    manifest = await asyncio.to_thread(manifest_service.get_manifest, manifest_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifest not found")
    _authorize_agent_access(auth_result, manifest.agent_id, "evaluate")

    trace = await asyncio.to_thread(
        manifest_service.evaluate_with_trace,
        manifest.agent_id,
        body.tool_name,
        manifest.zone_id,
    )

    return {
        "tool_name": trace.tool_name,
        "permission": trace.decision,
        "agent_id": manifest.agent_id,
        "manifest_id": manifest_id,
        "trace": {
            "matched_index": trace.matched_index,
            "default_applied": trace.default_applied,
            "entries": [
                {
                    "index": e.index,
                    "tool_pattern": e.tool_pattern,
                    "permission": e.permission,
                    "matched": e.matched,
                    "max_calls_per_minute": e.max_calls_per_minute,
                }
                for e in trace.entries
            ],
        },
    }


@router.post("/{manifest_id}/revoke")
async def revoke_manifest(
    manifest_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    manifest_service: Any = Depends(_get_manifest_service),
) -> dict:
    """Revoke a manifest and delete its ReBAC tuples."""
    # Verify ownership before revoking
    manifest = await asyncio.to_thread(manifest_service.get_manifest, manifest_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifest not found")
    _authorize_agent_access(auth_result, manifest.agent_id, "revoke")
    revoked = await asyncio.to_thread(manifest_service.revoke_manifest, manifest_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Manifest not found")

    return {"manifest_id": manifest_id, "status": "revoked"}
