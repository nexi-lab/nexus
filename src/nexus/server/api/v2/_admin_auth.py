"""Shared admin-auth helper for #3790 follow-up routers.

The MCP-mount router (``mcp.py``) and the ReBAC tuple router (``rebac.py``)
both need an admin gate that admits two paths:

1. The standard Nexus admin path (``Depends(require_admin)``) — works
   when ``NEXUS_API_KEY`` or a configured ``auth_provider`` recognises
   the bearer token as ``is_admin=True``. This is what production
   deployments use.

2. ``NEXUS_APPROVALS_ADMIN_TOKEN`` fallback. The Issue #3790 E2E
   ``running_nexus`` fixture sets this token but does not seed
   ``NEXUS_API_KEY``, so the routers accept a Bearer token that
   matches ``NEXUS_APPROVALS_ADMIN_TOKEN`` (constant-time compare) as
   admin-equivalent. This is intentionally narrow: the env var is
   already trusted by ``ApprovalsServicer`` for gRPC bypass, so reusing
   it for HTTP admin parity is consistent. We do NOT introduce a third
   auth mode and do NOT bypass any safety check downstream — calls go
   through normal service validation (MCPService.mcp_mount,
   ReBACManager.rebac_write, etc.).
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Header, HTTPException, Request

from nexus.server.dependencies import resolve_auth


async def require_followup_admin(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
    x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
    x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
) -> dict[str, Any]:
    """Admit admin callers via the standard pipeline OR the approvals
    admin token (Issue #3790 follow-up routers).

    Returns the auth_result dict on success.

    Raises:
        HTTPException 401: no/invalid auth.
        HTTPException 403: authenticated but not admin and bearer does
            not match ``NEXUS_APPROVALS_ADMIN_TOKEN``.
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
