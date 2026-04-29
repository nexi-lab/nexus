"""MCP mount management REST API (Issue #3790).

Thin HTTP wrapper around ``MCPService.mcp_mount`` / ``mcp_unmount`` /
``mcp_list_mounts`` so operators can drive MCP server lifecycle through a
REST surface — and so the Issue #3790 follow-up E2E can exercise the
full SSRF → PolicyGate path from outside the daemon process.

Endpoints
---------
- ``POST   /api/v2/mcp/mounts``            — mount an MCP server
- ``GET    /api/v2/mcp/mounts``            — list mounts
- ``DELETE /api/v2/mcp/mounts/{name}``     — unmount

Auth
----
All endpoints are admin-only. Two admission paths:

1. The standard Nexus admin path (``Depends(require_admin)``) — works
   when ``NEXUS_API_KEY`` or a configured ``auth_provider`` recognises
   the bearer token as ``is_admin=True``. This is what production
   deployments use.

2. ``NEXUS_APPROVALS_ADMIN_TOKEN`` fallback. The Issue #3790 E2E
   ``running_nexus`` fixture sets this token but does not seed
   ``NEXUS_API_KEY``, so the router accepts a Bearer token that
   matches ``NEXUS_APPROVALS_ADMIN_TOKEN`` (constant-time compare) as
   admin-equivalent. This is intentionally narrow: the env var is
   already trusted by ``ApprovalsServicer`` for gRPC bypass, so reusing
   it for MCP mount admin parity is consistent. We do NOT introduce a
   third auth mode and do NOT bypass any safety check downstream — the
   URL still goes through ``MCPService.mcp_mount`` which enforces
   SSRF + PolicyGate as usual.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.server.dependencies import resolve_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/mcp", tags=["mcp"])


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


async def _require_mcp_admin(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
    x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
    x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
) -> dict[str, Any]:
    """Admit admin callers via the standard pipeline OR the approvals admin token.

    See module docstring for the auth-design rationale (Issue #3790
    follow-up E2E needs a fixture-recognised admin token).
    """
    client_host = request.client.host if request.client else None

    auth_result = await resolve_auth(
        app_state=request.app.state,
        authorization=authorization,
        x_agent_id=x_agent_id,
        x_nexus_subject=x_nexus_subject,
        x_nexus_zone_id=x_nexus_zone_id,
        client_host=client_host,
    )
    # Fall through to the approvals-admin-token fallback before 403, because
    # the same caller may be presenting a bearer that the standard provider
    # doesn't recognise as admin but matches the approvals env-var token.
    if (
        auth_result is not None
        and auth_result.get("authenticated")
        and auth_result.get("is_admin", False)
    ):
        return auth_result

    # Approvals-admin-token fallback (#3790).
    approvals_admin = os.environ.get("NEXUS_APPROVALS_ADMIN_TOKEN") or None
    if approvals_admin and authorization:
        token = authorization[7:] if authorization.startswith("Bearer ") else authorization
        if token and hmac.compare_digest(token, approvals_admin):
            return {
                "authenticated": True,
                "is_admin": True,
                "subject_type": "user",
                "subject_id": "approvals-admin",
                "zone_id": x_nexus_zone_id,
                "via": "approvals_admin_token",
            }

    if auth_result is None or not auth_result.get("authenticated"):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    raise HTTPException(status_code=403, detail="Admin privileges required")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class MountRequest(BaseModel):
    """Body for ``POST /api/v2/mcp/mounts``.

    Fields mirror ``MCPService.mcp_mount(...)``. Either ``command`` or
    ``url`` must be supplied — validation is delegated to MCPService so
    error semantics stay consistent across HTTP / gRPC / direct calls.
    """

    name: str = Field(..., description="Unique mount name")
    transport: Literal["sse", "http", "stdio"] | None = Field(
        default=None,
        description=("Transport — auto-detected when omitted (stdio for command, sse for url)"),
    )
    command: str | None = Field(default=None, description="Command for stdio transport")
    url: str | None = Field(default=None, description="URL for sse/http transport")
    args: list[str] | None = Field(default=None, description="Command arguments (stdio)")
    env: dict[str, str] | None = Field(default=None, description="Process env (stdio)")
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers (sse/http)")
    description: str | None = Field(default=None, description="Human-readable description")
    tier: str = Field(default="system", description="Tier: user/zone/system")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_mcp_service(request: Request) -> Any:
    """Resolve MCPService from app.state.nexus_fs.

    The MCP brick attaches its service via ``nx.service("mcp")`` (see
    ``factory/_wired.py``). We dereference defensively so degraded
    startups (sandbox profile, MCP brick disabled) return a clean 503
    rather than crashing with AttributeError.
    """
    nexus_fs = getattr(request.app.state, "nexus_fs", None)
    if nexus_fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not available")
    service_getter = getattr(nexus_fs, "service", None)
    if service_getter is None:
        raise HTTPException(status_code=503, detail="MCP service registry not available")
    try:
        mcp_service = service_getter("mcp")
    except Exception as e:
        logger.warning("MCPService lookup failed: %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail="MCP service unavailable") from None
    if mcp_service is None:
        raise HTTPException(status_code=503, detail="MCP service not registered")
    return mcp_service


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/mounts", status_code=201)
async def create_mount(
    request: Request,
    body: MountRequest,
    _auth: dict[str, Any] = Depends(_require_mcp_admin),
) -> dict[str, Any]:
    """Mount an MCP server.

    Body validation deliberately stays thin: MCPService.mcp_mount raises
    ``ValidationError`` for invalid combinations (neither command nor
    url, both set, etc). Those map to 400 via the global
    ``nexus_error_handler``.
    """
    mcp_service = _resolve_mcp_service(request)

    result: dict[str, Any] = await mcp_service.mcp_mount(
        name=body.name,
        transport=body.transport,
        command=body.command,
        url=body.url,
        args=body.args,
        env=body.env,
        headers=body.headers,
        description=body.description,
        tier=body.tier,
        context=None,
    )
    return result


@router.get("/mounts")
async def list_mounts(
    request: Request,
    tier: str | None = None,
    include_unmounted: bool = True,
    _auth: dict[str, Any] = Depends(_require_mcp_admin),
) -> dict[str, Any]:
    """List MCP server mounts (admin-only)."""
    mcp_service = _resolve_mcp_service(request)
    mounts: list[dict[str, Any]] = await mcp_service.mcp_list_mounts(
        tier=tier,
        include_unmounted=include_unmounted,
        context=None,
    )
    return {"mounts": mounts, "count": len(mounts)}


@router.delete("/mounts/{name}")
async def delete_mount(
    request: Request,
    name: str,
    _auth: dict[str, Any] = Depends(_require_mcp_admin),
) -> dict[str, Any]:
    """Unmount an MCP server (admin-only)."""
    mcp_service = _resolve_mcp_service(request)
    result: dict[str, Any] = await mcp_service.mcp_unmount(name=name, context=None)
    return result
