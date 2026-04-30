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
All endpoints are admin-only via the standard Nexus admin pipeline:
``Depends(require_followup_admin)`` requires ``NEXUS_API_KEY`` or an
``auth_provider`` that recognises the bearer as ``is_admin=True``. The
``NEXUS_APPROVALS_ADMIN_TOKEN`` env var is intentionally NOT honored
here — that token is scoped to the approvals gRPC server and must not
gate stdio mount creation (arbitrary subprocess execution).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.server.api.v2._admin_auth import require_followup_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/mcp", tags=["mcp"])


# Shared two-path admin gate (#3790). Aliased here so existing call sites
# (and any external imports of ``_require_mcp_admin``) keep working.
_require_mcp_admin = require_followup_admin


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
