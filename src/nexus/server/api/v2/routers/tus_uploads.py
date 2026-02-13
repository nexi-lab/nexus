"""tus.io v1.0.0 compliant resumable upload router (Issue #788).

Endpoints:
- OPTIONS /api/v2/uploads         — Server capabilities
- POST    /api/v2/uploads         — Create upload session
- PATCH   /api/v2/uploads/{id}    — Upload chunk
- HEAD    /api/v2/uploads/{id}    — Get upload offset
- DELETE  /api/v2/uploads/{id}    — Terminate upload

Every endpoint (except OPTIONS) validates the Tus-Resumable header.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

if TYPE_CHECKING:
    from nexus.services.chunked_upload_service import ChunkedUploadService

logger = logging.getLogger(__name__)

TUS_RESUMABLE = "1.0.0"


def _validate_tus_resumable(tus_resumable: str | None = Header(None, alias="Tus-Resumable")) -> str:
    """Validate the Tus-Resumable header is present and correct."""
    if tus_resumable is None:
        raise HTTPException(
            status_code=412,
            detail="Missing Tus-Resumable header",
            headers={"Tus-Version": TUS_RESUMABLE},
        )
    if tus_resumable != TUS_RESUMABLE:
        raise HTTPException(
            status_code=412,
            detail=f"Unsupported Tus-Resumable version: {tus_resumable}. Expected {TUS_RESUMABLE}",
            headers={"Tus-Version": TUS_RESUMABLE},
        )
    return tus_resumable


def _parse_upload_metadata(raw: str | None) -> dict[str, str]:
    """Parse tus Upload-Metadata header.

    Format: "key1 base64value1,key2 base64value2"
    Keys without values are allowed (value defaults to "").
    """
    if not raw:
        return {}

    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(" ", 1)
        key = parts[0].strip()
        if len(parts) == 2:
            try:
                value = base64.b64decode(parts[1].strip()).decode("utf-8")
            except Exception:
                value = parts[1].strip()
        else:
            value = ""
        result[key] = value
    return result


def create_tus_uploads_router(
    get_upload_service: object,
) -> APIRouter:
    """Factory function to create the tus uploads router.

    Args:
        get_upload_service: Callable that returns the ChunkedUploadService.
            Follows the same factory pattern as create_async_files_router().

    Returns:
        Configured APIRouter for tus endpoints.
    """
    router = APIRouter(tags=["uploads"])

    def _get_service() -> ChunkedUploadService:
        svc: ChunkedUploadService | None = get_upload_service()  # type: ignore[operator]
        if svc is None:
            raise HTTPException(status_code=503, detail="Upload service not available")
        return svc

    # --- OPTIONS: Server capabilities ---

    @router.options("")
    async def tus_options() -> Response:
        """Return tus server capabilities."""
        service = _get_service()
        caps = service.get_server_capabilities()
        return Response(
            status_code=204,
            headers={
                **caps,
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "OPTIONS, POST, PATCH, HEAD, DELETE",
                "Access-Control-Allow-Headers": (
                    "Tus-Resumable, Upload-Length, Upload-Offset, "
                    "Upload-Metadata, Upload-Checksum, Content-Type"
                ),
                "Access-Control-Expose-Headers": (
                    "Tus-Resumable, Tus-Version, Tus-Extension, "
                    "Tus-Max-Size, Tus-Checksum-Algorithm, "
                    "Upload-Offset, Upload-Length, Upload-Expires, Location"
                ),
            },
        )

    # --- POST: Create upload ---

    @router.post("")
    async def tus_create(
        request: Request,
        _tus_resumable: str = Depends(_validate_tus_resumable),
    ) -> Response:
        """Create a new upload session (tus creation extension)."""
        from nexus.core.exceptions import ValidationError

        service = _get_service()

        # Parse headers
        upload_length_str = request.headers.get("Upload-Length")
        if upload_length_str is None:
            raise HTTPException(status_code=400, detail="Missing Upload-Length header")

        try:
            upload_length = int(upload_length_str)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid Upload-Length: {upload_length_str}"
            ) from exc

        metadata_raw = request.headers.get("Upload-Metadata")
        metadata = _parse_upload_metadata(metadata_raw)

        # Extract identity from request context if available
        zone_id = request.headers.get("X-Zone-Id", "default")
        user_id = request.headers.get("X-User-Id", "anonymous")

        try:
            session = await service.create_upload(
                target_path=metadata.get("filename", metadata.get("path", "/uploads/unknown")),
                upload_length=upload_length,
                metadata=metadata,
                zone_id=zone_id,
                user_id=user_id,
            )
        except RuntimeError as e:
            if "Too many concurrent" in str(e):
                raise HTTPException(status_code=429, detail=str(e)) from e
            raise
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Build Location URL
        location = str(request.url).rstrip("/") + f"/{session.upload_id}"

        return Response(
            status_code=201,
            headers={
                "Tus-Resumable": TUS_RESUMABLE,
                "Location": location,
                "Upload-Expires": session.expires_at.isoformat() if session.expires_at else "",
            },
        )

    # --- PATCH: Upload chunk ---

    @router.patch("/{upload_id}")
    async def tus_upload_chunk(
        upload_id: str,
        request: Request,
        _tus_resumable: str = Depends(_validate_tus_resumable),
    ) -> Response:
        """Upload a chunk of data (tus core protocol)."""
        from nexus.core.exceptions import (
            UploadChecksumMismatchError,
            UploadExpiredError,
            UploadNotFoundError,
            UploadOffsetMismatchError,
            ValidationError,
        )

        service = _get_service()

        # Validate Content-Type
        content_type = request.headers.get("Content-Type", "")
        if content_type != "application/offset+octet-stream":
            raise HTTPException(
                status_code=415,
                detail=f"Content-Type must be application/offset+octet-stream, got {content_type}",
            )

        # Parse offset
        offset_str = request.headers.get("Upload-Offset")
        if offset_str is None:
            raise HTTPException(status_code=400, detail="Missing Upload-Offset header")

        try:
            offset = int(offset_str)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid Upload-Offset: {offset_str}"
            ) from exc

        # Read chunk body
        chunk_data = await request.body()

        # Optional checksum
        checksum_header = request.headers.get("Upload-Checksum")

        try:
            session = await service.receive_chunk(
                upload_id=upload_id,
                offset=offset,
                chunk_data=chunk_data,
                checksum_header=checksum_header,
            )
        except UploadNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"Upload not found: {upload_id}") from e
        except UploadExpiredError as e:
            raise HTTPException(status_code=410, detail=f"Upload expired: {upload_id}") from e
        except UploadOffsetMismatchError as e:
            raise HTTPException(
                status_code=409,
                detail=f"Offset mismatch: expected {e.expected_offset}, got {e.received_offset}",
            ) from e
        except UploadChecksumMismatchError as e:
            raise HTTPException(
                status_code=460,
                detail=f"Checksum mismatch ({e.algorithm})",
            ) from e
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        headers = {
            "Tus-Resumable": TUS_RESUMABLE,
            "Upload-Offset": str(session.upload_offset),
        }
        if session.expires_at:
            headers["Upload-Expires"] = session.expires_at.isoformat()

        return Response(status_code=204, headers=headers)

    # --- HEAD: Get upload offset ---

    @router.head("/{upload_id}")
    async def tus_get_offset(
        upload_id: str,
        _tus_resumable: str = Depends(_validate_tus_resumable),
    ) -> Response:
        """Get the current offset of an upload (for resumption)."""
        from nexus.core.exceptions import UploadExpiredError, UploadNotFoundError

        service = _get_service()

        try:
            session = await service.get_upload_status(upload_id)
        except UploadNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"Upload not found: {upload_id}") from e
        except UploadExpiredError as e:
            raise HTTPException(status_code=410, detail=f"Upload expired: {upload_id}") from e

        headers = {
            "Tus-Resumable": TUS_RESUMABLE,
            "Upload-Offset": str(session.upload_offset),
            "Upload-Length": str(session.upload_length),
            "Cache-Control": "no-store",
        }
        if session.expires_at:
            headers["Upload-Expires"] = session.expires_at.isoformat()

        return Response(status_code=200, headers=headers)

    # --- DELETE: Terminate upload ---

    @router.delete("/{upload_id}")
    async def tus_terminate(
        upload_id: str,
        _tus_resumable: str = Depends(_validate_tus_resumable),
    ) -> Response:
        """Terminate an upload and release resources (tus termination extension)."""
        from nexus.core.exceptions import UploadNotFoundError

        service = _get_service()

        try:
            await service.terminate_upload(upload_id)
        except UploadNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"Upload not found: {upload_id}") from e

        return Response(
            status_code=204,
            headers={"Tus-Resumable": TUS_RESUMABLE},
        )

    return router
