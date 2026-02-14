"""Share link API router (Issue #227, #1288).

Provides public share link endpoints (anonymous access allowed):
- GET  /api/share/{link_id}           -- get share link info
- POST /api/share/{link_id}/access    -- access shared resource
- GET  /api/share/{link_id}/download  -- download via share link with Range support

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response, StreamingResponse

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.permissions import OperationContext
from nexus.server.api.v1.dependencies import get_nexus_fs
from nexus.server.dependencies import get_auth_result, get_operation_context
from nexus.server.fastapi_server import to_thread_with_timeout
from nexus.server.range_utils import build_range_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share"])


# =============================================================================
# Helpers
# =============================================================================


def _share_error_status_code(error_msg: str) -> int:
    """Map share-link error messages to HTTP status codes."""
    msg = error_msg.lower()
    if "not found" in msg:
        return 404
    if "expired" in msg or "revoked" in msg:
        return 410
    if "password" in msg:
        return 401
    if "limit" in msg:
        return 429
    return 400


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/api/share/{link_id}")
async def get_share_link_info(
    link_id: str,
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> JSONResponse:
    """Get share link information."""
    context = None
    if auth_result and auth_result.get("authenticated"):
        context = get_operation_context(auth_result)

    result = await to_thread_with_timeout(nexus_fs.get_share_link, link_id, context=context)
    if not result.success:
        error_msg = (result.error_message or "").lower()
        status_code = 404 if "not found" in error_msg else 400
        raise HTTPException(status_code=status_code, detail=result.error_message or "Error")

    return JSONResponse(content=result.data)


@router.post("/api/share/{link_id}/access")
async def access_share_link(
    link_id: str,
    request: Request,
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> JSONResponse:
    """Access a shared resource via share link."""
    password = None
    try:
        body = await request.json()
        password = body.get("password")
    except Exception:
        pass

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    context = None
    if auth_result and auth_result.get("authenticated"):
        context = get_operation_context(auth_result)

    result = await to_thread_with_timeout(
        nexus_fs.access_share_link,
        link_id,
        password=password,
        ip_address=ip_address,
        user_agent=user_agent,
        context=context,
    )
    if not result.success:
        error_msg = result.error_message or "Access denied"
        status_code = _share_error_status_code(error_msg)
        raise HTTPException(status_code=status_code, detail=error_msg)

    return JSONResponse(content=result.data)


@router.get("/api/share/{link_id}/download", response_model=None)
async def download_via_share_link(
    link_id: str,
    request: Request,
    password: str | None = Query(None, description="Password if link is protected"),
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> Response | StreamingResponse:
    """Download a file via share link with HTTP Range support."""
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    context = None
    if auth_result and auth_result.get("authenticated"):
        context = get_operation_context(auth_result)

    # First validate share link
    access_result = await to_thread_with_timeout(
        nexus_fs.access_share_link,
        link_id,
        password=password,
        ip_address=ip_address,
        user_agent=user_agent,
        context=context,
    )
    if not access_result.success:
        error_msg = access_result.error_message or "Access denied"
        status_code = _share_error_status_code(error_msg)
        raise HTTPException(status_code=status_code, detail=error_msg)

    data = access_result.data or {}
    file_path = data.get("path")
    zone_id = data.get("zone_id", "default")

    if not file_path:
        raise HTTPException(status_code=500, detail="Share link missing file path")

    try:
        stream_context = OperationContext(
            user="share_link",
            groups=[],
            zone_id=zone_id,
            subject_type="share_link",
            subject_id=link_id,
            is_admin=True,  # Bypass ReBAC - link already validated
        )

        meta = await to_thread_with_timeout(nexus_fs.stat, file_path, context=stream_context)
        content_hash = meta.get("etag") or meta.get("content_hash")
        if not content_hash:
            raise HTTPException(status_code=500, detail="File has no content hash")

        route = nexus_fs.router.route(file_path)
        backend = route.backend
        total_size = meta.get("size", 0)

        # Fall back to non-streaming if backend lacks stream_content
        if not hasattr(backend, "stream_content"):
            content = await to_thread_with_timeout(nexus_fs.read, file_path, context=stream_context)
            if isinstance(content, str):
                content_bytes = content.encode()
            elif isinstance(content, bytes):
                content_bytes = content
            else:
                content_bytes = b""
            return StreamingResponse(
                iter([content_bytes]),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{file_path.split("/")[-1]}"',
                },
            )

        return build_range_response(
            request_headers=request.headers,
            content_generator=lambda s, e, cs: backend.stream_range(
                content_hash, s, e, chunk_size=cs, context=stream_context
            ),
            full_generator=lambda: backend.stream_content(content_hash, context=stream_context),
            total_size=total_size,
            etag=content_hash,
            content_type="application/octet-stream",
            filename=file_path.split("/")[-1],
            extra_headers={"X-Content-Hash": content_hash},
        )
    except NexusFileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}") from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Share link download error for %s: %s", link_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Download error: {e}") from e
