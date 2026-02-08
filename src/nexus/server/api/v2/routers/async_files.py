"""Async File Operations REST API endpoints (Phase 4).

Provides async-native file operations using AsyncNexusFS:
- POST   /write           - Write file content
- GET    /read            - Read file content
- DELETE /delete          - Delete file
- GET    /exists          - Check file existence
- GET    /list            - List directory contents
- POST   /mkdir           - Create directory
- GET    /metadata        - Get file metadata
- POST   /batch-read      - Batch read multiple files
- GET    /stream          - Stream file content

All operations use true async I/O for better concurrency.
All operations pass user context for permission enforcement.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)

if TYPE_CHECKING:
    from nexus.core.async_nexus_fs import AsyncNexusFS

logger = logging.getLogger(__name__)


# =============================================================================
# Request/Response Models
# =============================================================================


class WriteRequest(BaseModel):
    """Request model for write operation."""

    path: str = Field(..., description="Virtual path to write")
    content: str = Field(..., description="File content (string or base64 encoded)")
    encoding: str | None = Field(None, description="Content encoding: 'utf8' (default) or 'base64'")
    if_match: str | None = Field(None, description="ETag for optimistic concurrency")
    if_none_match: bool = Field(False, description="Only write if file doesn't exist")


class WriteResponse(BaseModel):
    """Response model for write operation."""

    etag: str
    version: int
    size: int
    modified_at: str


class ReadResponse(BaseModel):
    """Response model for read operation."""

    content: str
    etag: str | None = None
    version: int | None = None
    modified_at: str | None = None
    size: int | None = None


class DeleteResponse(BaseModel):
    """Response model for delete operation."""

    deleted: bool
    path: str


class ExistsResponse(BaseModel):
    """Response model for exists check."""

    exists: bool


class ListResponse(BaseModel):
    """Response model for list directory."""

    items: list[str]


class MkdirRequest(BaseModel):
    """Request model for mkdir operation."""

    path: str = Field(..., description="Directory path to create")
    parents: bool = Field(True, description="Create parent directories if needed")


class MetadataResponse(BaseModel):
    """Response model for file metadata."""

    path: str
    size: int
    etag: str | None = None
    version: int
    is_directory: bool
    created_at: str | None = None
    modified_at: str | None = None


class BatchReadRequest(BaseModel):
    """Request model for batch read."""

    paths: list[str] = Field(..., description="List of paths to read")


# =============================================================================
# Router Factory
# =============================================================================


def create_async_files_router(
    async_fs: AsyncNexusFS | None = None,
    get_fs: Any | None = None,
) -> APIRouter:
    """
    Create an async files router.

    Supports two modes:
    1. Direct: Pass async_fs instance (for testing)
    2. Lazy: Pass get_fs callable that returns the instance at request time
       (for server lifespan where fs is initialized after app creation)

    Args:
        async_fs: Initialized AsyncNexusFS instance (direct mode)
        get_fs: Callable returning AsyncNexusFS (lazy mode)

    Returns:
        FastAPI router with async file endpoints
    """
    router = APIRouter(tags=["files"])

    # Import auth dependencies from main server
    from nexus.server.fastapi_server import get_auth_result, get_operation_context

    async def _get_fs() -> AsyncNexusFS:
        """Get AsyncNexusFS, supporting both direct and lazy modes."""
        if async_fs is not None:
            return async_fs
        if get_fs is not None:
            fs = get_fs()
            if fs is not None:
                return fs
        raise HTTPException(
            status_code=503,
            detail="AsyncNexusFS not initialized. Server may still be starting up.",
        )

    async def get_context(
        auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> Any:
        """Get operation context from auth result."""
        if auth_result is None or not auth_result.get("authenticated"):
            from nexus.core.permissions import OperationContext

            return OperationContext(
                user="anonymous",
                groups=[],
                zone_id="default",
            )
        return get_operation_context(auth_result)

    # =============================================================================
    # Write Endpoint
    # =============================================================================

    @router.post("/write", response_model=WriteResponse)
    async def write_file(
        request: WriteRequest,
        context: Any = Depends(get_context),
    ) -> WriteResponse:
        """
        Write content to a file.

        Creates parent directories if needed. Supports optimistic concurrency
        control with if_match (ETag check) and if_none_match (create-only).

        Content can be provided as:
        - Plain string (UTF-8 encoded automatically)
        - Base64 encoded binary (set encoding="base64")
        """
        try:
            fs = await _get_fs()
            # Decode content based on encoding
            if request.encoding == "base64":
                content = base64.b64decode(request.content)
            else:
                content = request.content.encode("utf-8")

            result = await fs.write(
                path=request.path,
                content=content,
                if_match=request.if_match,
                if_none_match=request.if_none_match,
                context=context,
            )

            return WriteResponse(
                etag=result["etag"],
                version=result["version"],
                size=result["size"],
                modified_at=result["modified_at"],
            )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Write error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Read Endpoint
    # =============================================================================

    @router.get("/read", response_model=ReadResponse)
    async def read_file(
        request: Request,
        path: str = Query(..., description="Path to read"),
        include_metadata: bool = Query(False, description="Include metadata in response"),
        context: Any = Depends(get_context),
    ) -> Response:
        """
        Read file content.

        Supports ETag-based caching via If-None-Match header.
        Returns 304 Not Modified if content hasn't changed.
        """
        try:
            fs = await _get_fs()
            # Check If-None-Match header for caching
            if_none_match = request.headers.get("If-None-Match")

            # Get metadata first for ETag check
            if if_none_match:
                meta = await fs.get_metadata(path)
                if meta and meta.etag:
                    client_etag = if_none_match.strip('"')
                    if client_etag == meta.etag:
                        return Response(
                            status_code=304,
                            headers={"ETag": f'"{meta.etag}"'},
                        )

            # Read content
            result = await fs.read(path, return_metadata=include_metadata, context=context)

            if include_metadata and isinstance(result, dict):
                content = result["content"]
                # Decode bytes to string for JSON response
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")

                response_data = ReadResponse(
                    content=content,
                    etag=result.get("etag"),
                    version=result.get("version"),
                    modified_at=result.get("modified_at"),
                    size=result.get("size"),
                )
                return Response(
                    content=response_data.model_dump_json(),
                    media_type="application/json",
                    headers={"ETag": f'"{result.get("etag")}"'} if result.get("etag") else {},
                )
            else:
                # Simple content response
                content = result
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")

                return Response(
                    content=ReadResponse(content=content).model_dump_json(),
                    media_type="application/json",
                )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Read error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Delete Endpoint
    # =============================================================================

    @router.delete("/delete", response_model=DeleteResponse)
    async def delete_file(
        path: str = Query(..., description="Path to delete"),
        context: Any = Depends(get_context),
    ) -> DeleteResponse:
        """Delete a file."""
        try:
            fs = await _get_fs()
            result = await fs.delete(path, context=context)
            return DeleteResponse(deleted=result["deleted"], path=result["path"])

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Delete error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Exists Endpoint
    # =============================================================================

    @router.get("/exists", response_model=ExistsResponse)
    async def file_exists(
        path: str = Query(..., description="Path to check"),
        context: Any = Depends(get_context),
    ) -> ExistsResponse:
        """Check if a file or directory exists."""
        try:
            fs = await _get_fs()
            exists = await fs.exists(path, context=context)
            return ExistsResponse(exists=exists)

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Exists error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # List Directory Endpoint
    # =============================================================================

    @router.get("/list", response_model=ListResponse)
    async def list_directory(
        path: str = Query(..., description="Directory path to list"),
        context: Any = Depends(get_context),
    ) -> ListResponse:
        """List directory contents."""
        try:
            fs = await _get_fs()
            items = await fs.list_dir(path, context=context)
            return ListResponse(items=items)

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"List error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Mkdir Endpoint
    # =============================================================================

    @router.post("/mkdir", status_code=status.HTTP_200_OK)
    async def create_directory(
        request: MkdirRequest,
        context: Any = Depends(get_context),
    ) -> dict[str, Any]:
        """Create a directory."""
        try:
            fs = await _get_fs()
            await fs.mkdir(request.path, parents=request.parents, context=context)
            return {"created": True, "path": request.path}

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Mkdir error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Metadata Endpoint
    # =============================================================================

    @router.get("/metadata", response_model=MetadataResponse)
    async def get_file_metadata(
        path: str = Query(..., description="Path to get metadata for"),
        context: Any = Depends(get_context),
    ) -> MetadataResponse:
        """Get file or directory metadata."""
        try:
            fs = await _get_fs()
            meta = await fs.get_metadata(path, context=context)
            if meta is None:
                raise NexusFileNotFoundError(path=path)

            return MetadataResponse(
                path=meta.path,
                size=meta.size,
                etag=meta.etag,
                version=meta.version,
                is_directory=meta.is_directory,
                created_at=meta.created_at.isoformat() if meta.created_at else None,
                modified_at=meta.modified_at.isoformat() if meta.modified_at else None,
            )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Metadata error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Batch Read Endpoint
    # =============================================================================

    @router.post("/batch-read")
    async def batch_read_files(
        request: BatchReadRequest,
        context: Any = Depends(get_context),
    ) -> dict[str, Any]:
        """
        Read multiple files in a single request.

        Returns a dict mapping path to content (or None if not found).
        More efficient than multiple individual reads.
        """
        try:
            fs = await _get_fs()
            results = await fs.batch_read(request.paths, context=context)

            # Convert bytes to string for JSON response
            response: dict[str, Any] = {}
            for path, content in results.items():
                if content is None:
                    response[path] = None
                elif isinstance(content, bytes):
                    response[path] = {
                        "content": content.decode("utf-8", errors="replace"),
                    }
                else:
                    response[path] = {"content": content}

            return response

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Batch read error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Stream Endpoint
    # =============================================================================

    @router.get("/stream")
    async def stream_file(
        path: str = Query(..., description="Path to stream"),
        chunk_size: int = Query(65536, description="Chunk size in bytes"),
        context: Any = Depends(get_context),
    ) -> StreamingResponse:
        """
        Stream file content in chunks.

        Useful for large files to avoid loading entire content into memory.
        Returns application/octet-stream content type.
        """
        try:
            fs = await _get_fs()
            # Verify file exists first
            meta = await fs.get_metadata(path, context=context)
            if meta is None:
                raise NexusFileNotFoundError(path=path)

            async def generate():
                async for chunk in fs.stream_read(path, chunk_size=chunk_size, context=context):
                    yield chunk

            return StreamingResponse(
                generate(),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{path.split("/")[-1]}"',
                    "ETag": f'"{meta.etag}"' if meta.etag else "",
                },
            )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Stream error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
