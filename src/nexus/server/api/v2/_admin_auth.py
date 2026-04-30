"""Shared admin-auth helper for #3790 follow-up routers.

The MCP-mount router (``mcp.py``) and the ReBAC tuple router (``rebac.py``)
both gate on the standard Nexus admin pipeline — i.e. ``auth_provider``
must mark the bearer token as ``is_admin=True``, or ``NEXUS_API_KEY`` must
match. Production deployments use this path; the Issue #3790 E2E fixture
seeds ``NEXUS_API_KEY`` so the same path covers tests.

We deliberately do NOT honor ``NEXUS_APPROVALS_ADMIN_TOKEN`` for HTTP
admin routes. That env var is wired as a gRPC fallback for the
ApprovalsV1 servicer only; reusing it here would let a leaked approvals
token write arbitrary ReBAC tuples and create stdio MCP mounts (which
carry ``command`` / ``args`` / ``env``) — i.e. arbitrary subprocess
execution. Keep the blast radius scoped: approvals env-var ⇒ approvals
gRPC, full admin ⇒ standard admin pipeline.
"""

from __future__ import annotations

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
    """Admit admin callers via the standard Nexus admin pipeline.

    Returns the auth_result dict on success.

    Raises:
        HTTPException 401: no/invalid auth.
        HTTPException 403: authenticated but not admin.
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
    if (
        auth_result is not None
        and auth_result.get("authenticated")
        and auth_result.get("is_admin", False)
    ):
        return auth_result

    if auth_result is None or not auth_result.get("authenticated"):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    raise HTTPException(status_code=403, detail="Admin privileges required")
