"""Debug, whoami, and status endpoints.

Extracted from fastapi_server.py (#1602).
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from nexus.server.dependencies import get_auth_result, require_admin
from nexus.server.rate_limiting import RATE_LIMIT_ANONYMOUS, limiter

logger = logging.getLogger(__name__)

router = APIRouter()


class WhoamiResponse(BaseModel):
    """Authentication info response."""

    authenticated: bool
    subject_type: str | None = None
    subject_id: str | None = None
    zone_id: str | None = None
    is_admin: bool = False
    inherit_permissions: bool = True
    user: str | None = None


@router.get("/debug/asyncio", tags=["debug"], dependencies=[Depends(require_admin)])
async def debug_asyncio() -> dict[str, Any]:
    """Debug endpoint for asyncio task introspection."""
    result: dict[str, Any] = {
        "python_version": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
    }

    try:
        all_tasks = asyncio.all_tasks()
        current = asyncio.current_task()
        result["task_count"] = len(all_tasks)
        result["current_task"] = current.get_name() if current else None
        result["tasks"] = [
            {
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            for task in list(all_tasks)[:50]
        ]
    except Exception as e:
        result["tasks_error"] = str(e)

    # Python 3.14 call graph introspection
    from asyncio import format_call_graph

    try:
        result["call_graph_available"] = True
        result["call_graph"] = format_call_graph()
    except Exception as e:
        result["call_graph_error"] = str(e)

    return result


@router.get("/api/auth/whoami", response_model=WhoamiResponse)
@limiter.limit(RATE_LIMIT_ANONYMOUS)
async def whoami(
    request: Request,  # noqa: ARG001 — required by slowapi @limiter.limit
    response: Response,  # noqa: ARG001 — required by slowapi for header injection
    auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> WhoamiResponse:
    """Authentication info endpoint."""
    if auth_result is None or not auth_result.get("authenticated"):
        return WhoamiResponse(authenticated=False)

    return WhoamiResponse(
        authenticated=True,
        subject_type=auth_result.get("subject_type"),
        subject_id=auth_result.get("subject_id"),
        zone_id=auth_result.get("zone_id"),
        is_admin=auth_result.get("is_admin", False),
        inherit_permissions=auth_result.get("inherit_permissions", True),
        user=auth_result.get("subject_id"),
    )


@router.get("/api/nfs/status")
async def status(request: Request) -> dict[str, Any]:
    """Service status endpoint."""
    return {
        "status": "running",
        "service": "nexus-rpc",
        "version": "1.0",
        "async": True,
        "methods": list(request.app.state.exposed_methods.keys()),
    }
