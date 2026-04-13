"""File Operations REST API endpoints (Phase 4).

Provides file operations using NexusFS via asyncio.to_thread():
- POST   /write           - Write file content
- GET    /read            - Read file content
- DELETE /delete          - Delete file
- GET    /exists          - Check file existence
- GET    /list            - List directory contents
- POST   /mkdir           - Create directory
- GET    /metadata        - Get file metadata
- POST   /batch-read      - Batch read multiple files (legacy, lenient)
- POST   /batch/write     - Atomic batch write (Issue #3700)
- POST   /batch/read      - Atomic batch read with optional partial mode (Issue #3700)
- GET    /stream          - Stream file content
- POST   /rename          - Rename/move file
- POST   /copy            - Copy file
- POST   /rename-batch     - Bulk rename/move files
- POST   /copy-bulk       - Bulk copy files
- GET    /glob            - Glob pattern search across files
- GET    /grep            - Regex pattern search within files

Async NexusFS methods (read, write, sys_stat, sys_readdir, sys_unlink,
access, mkdir) are awaited directly. Sync methods (read_bulk,
stream, write_stream) use asyncio.to_thread() for thread offloading.
All operations pass user context for permission enforcement.
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from nexus.bricks.search.primitives.glob_fast import glob_filter
from nexus.bricks.search.primitives.grep_fast import grep_files_mmap
from nexus.bricks.snapshot.errors import TransactionConflictError as _TransactionConflictError
from nexus.contracts.exceptions import (
    AccessDeniedError,
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.core.path_utils import validate_path as _normalize_path

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================


async def _read_connector_by_physical_path(
    fs: Any,
    display_path: str,
    physical_path: str,
    context: Any,
) -> bytes | None:
    """Read connector file by resolving display path → physical backend path.

    Routes through the mount point's connector backend using the raw
    physical_path, not the human-readable display_path. Returns None
    if routing fails so the caller can fall back to standard fs.read().
    """
    try:
        # Extract mount point from display path (e.g. /mnt/gmail from /mnt/gmail/INBOX/...)
        parts = display_path.split("/")
        if len(parts) < 3:
            return None
        mount_point = "/".join(parts[:3])  # /mnt/gmail or /mnt/calendar

        route = fs.router.route(mount_point)
        if route is None:
            return None

        from nexus.contracts.types import OperationContext

        read_context = OperationContext(
            user_id=getattr(context, "user_id", "anonymous"),
            groups=getattr(context, "groups", []),
            backend_path=physical_path,
            virtual_path=display_path,
        )

        content = route.backend.read_content("", context=read_context)
        if isinstance(content, bytes):
            return content
        return bytes(content) if content else None
    except Exception:
        return None


def _to_file_item(entry: dict[str, Any], prefix: str) -> "FileItemResponse":
    """Convert a sys_readdir details dict to a FileItemResponse.

    sys_readdir(details=True) returns dicts with path, size, etag, entry_type.
    entry_type: 0=file, 1=directory, 2=mount, 3=pipe, 4=stream.
    """
    raw_path: str = entry.get("path", "")
    # Use entry_type if available: 1=dir, 2=mount (both act as directories).
    # Also treat entries with trailing "/" as directories (legacy fallback).
    # entry_type 0 with no etag and size 0 is likely an implicit directory
    # (created by writing files underneath it in CAS-based storage).
    et = entry.get("entry_type", 0)
    has_extension = "." in raw_path.rstrip("/").rsplit("/", 1)[-1]
    # Size-zero entries without extensions are directories (mount points, folders).
    # This overrides explicit is_directory=False from the metastore which
    # incorrectly marks mount points as files.
    looks_like_dir = entry.get("size", 0) == 0 and not entry.get("etag") and not has_extension
    if "is_directory" in entry and not looks_like_dir:
        is_dir = bool(entry["is_directory"])
    else:
        is_dir = et in (1, 2) or raw_path.endswith("/") or looks_like_dir
    clean_path = raw_path.rstrip("/")
    name = (
        clean_path[len(prefix) :]
        if clean_path.startswith(prefix)
        else clean_path.rsplit("/", 1)[-1]
    )

    return FileItemResponse(
        name=name,
        path=clean_path,
        is_directory=is_dir,
        size=entry.get("size", 0) if not is_dir else 0,
        modified_at=entry.get("modified_at"),
        etag=entry.get("etag"),
        mime_type=None,
        zone_id=entry.get("zone_id"),
        version=entry.get("version"),
        owner=entry.get("owner_id"),
    )


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


class FileItemResponse(BaseModel):
    """Single file/directory entry with metadata.

    Uses serialization_alias so JSON output matches the TUI's camelCase convention.
    """

    name: str
    path: str
    is_directory: bool = Field(default=False, serialization_alias="isDirectory")
    size: int = 0
    modified_at: str | None = Field(None, serialization_alias="modifiedAt")
    etag: str | None = None
    mime_type: str | None = Field(None, serialization_alias="mimeType")
    version: int | None = None
    owner: str | None = None
    permissions: str | None = None
    zone_id: str | None = Field(None, serialization_alias="zoneId")


class ListResponse(BaseModel):
    """Response model for list directory — supports cursor pagination.

    @see Issue #3102, Decision 2A
    """

    items: list[FileItemResponse]
    has_more: bool = False
    next_cursor: str | None = None


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


class GlobResponse(BaseModel):
    """Response model for glob search."""

    matches: list[str]
    total: int
    truncated: bool
    pattern: str
    base_path: str


class GrepMatch(BaseModel):
    """A single grep match."""

    file: str
    line: int
    content: str
    match: str


class GrepResponse(BaseModel):
    """Response model for grep search."""

    matches: list[GrepMatch]
    total: int
    truncated: bool
    pattern: str
    base_path: str


class BatchReadRequest(BaseModel):
    """Request model for batch read (legacy /batch-read endpoint)."""

    paths: list[str] = Field(..., description="List of paths to read")


# ---------------------------------------------------------------------------
# Batch write/read models (Issue #3700)
# ---------------------------------------------------------------------------

_MAX_BATCH_FILES = 500
_MAX_BATCH_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MB


class BatchWriteFileItem(BaseModel):
    """A single file entry in a batch write request."""

    path: str = Field(..., description="Virtual path to write")
    content_base64: str = Field(..., description="File content encoded as base64")

    @field_validator("content_base64")
    @classmethod
    def validate_base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception as exc:
            raise ValueError(f"Invalid base64 encoding: {exc}") from exc
        return v


class BatchWriteRequest(BaseModel):
    """Request model for POST /batch/write."""

    files: list[BatchWriteFileItem] = Field(
        ...,
        description=f"Files to write (max {_MAX_BATCH_FILES})",
        max_length=_MAX_BATCH_FILES,
    )

    @field_validator("files")
    @classmethod
    def validate_total_bytes(cls, v: list[BatchWriteFileItem]) -> list[BatchWriteFileItem]:
        total = sum(len(base64.b64decode(f.content_base64)) for f in v)
        if total > _MAX_BATCH_TOTAL_BYTES:
            raise ValueError(
                f"Total decoded size {total} bytes exceeds limit of {_MAX_BATCH_TOTAL_BYTES} bytes"
            )
        return v


class BatchWriteResult(BaseModel):
    """Result for a single file in a batch write response."""

    path: str
    etag: str | None
    version: int
    modified_at: Any | None
    size: int


class BatchWriteResponse(BaseModel):
    """Response model for POST /batch/write."""

    results: list[BatchWriteResult]


class BatchReadAtomicRequest(BaseModel):
    """Request model for POST /batch/read."""

    paths: list[str] = Field(
        ...,
        description=f"Paths to read (max {_MAX_BATCH_FILES})",
        max_length=_MAX_BATCH_FILES,
    )
    partial: bool = Field(
        False,
        description=(
            "If false (default): any missing/inaccessible path raises an error. "
            "If true: returns per-item success or error for every path."
        ),
    )


class BatchReadSuccess(BaseModel):
    """A successfully read file item in a batch read response."""

    type: str = Field("success", frozen=True)
    path: str
    content_base64: str
    etag: str | None
    version: int
    modified_at: Any | None
    size: int


class BatchReadError(BaseModel):
    """A failed file item in a partial batch read response."""

    type: str = Field("error", frozen=True)
    path: str
    error: str


class BatchReadResponse(BaseModel):
    """Response model for POST /batch/read."""

    results: list[BatchReadSuccess | BatchReadError]


class RenameRequest(BaseModel):
    """Request model for rename/move operation."""

    source: str = Field(..., description="Source path to rename from")
    destination: str = Field(..., description="Destination path to rename to")


class RenameResponse(BaseModel):
    """Response model for rename operation."""

    success: bool
    source: str
    destination: str


class CopyRequest(BaseModel):
    """Request model for copy operation."""

    source: str = Field(..., description="Source path to copy from")
    destination: str = Field(..., description="Destination path to copy to")


class CopyResponse(BaseModel):
    """Response model for copy operation."""

    success: bool
    source: str
    destination: str
    bytes_copied: int


class RenameOperation(BaseModel):
    """A single rename operation within a bulk request."""

    source: str = Field(..., description="Source path to rename from")
    destination: str = Field(..., description="Destination path to rename to")


class RenameBatchRequest(BaseModel):
    """Request model for bulk rename operations."""

    operations: list[RenameOperation] = Field(
        ..., description="List of rename operations (max 50)", max_length=50
    )


class BulkRenameResult(BaseModel):
    """Result of a single rename within a bulk operation."""

    source: str
    destination: str
    success: bool
    error: str | None = None


class RenameBatchResponse(BaseModel):
    """Response model for bulk rename operations."""

    results: list[BulkRenameResult]


class CopyOperation(BaseModel):
    """A single copy operation within a bulk request."""

    source: str = Field(..., description="Source path to copy from")
    destination: str = Field(..., description="Destination path to copy to")


class CopyBulkRequest(BaseModel):
    """Request model for bulk copy operations."""

    operations: list[CopyOperation] = Field(
        ..., description="List of copy operations (max 50)", max_length=50
    )


class BulkCopyResult(BaseModel):
    """Result of a single copy within a bulk operation."""

    source: str
    destination: str
    success: bool
    bytes_copied: int | None = None
    error: str | None = None


class CopyBulkResponse(BaseModel):
    """Response model for bulk copy operations."""

    results: list[BulkCopyResult]


# =============================================================================
# Router Factory
# =============================================================================


def create_async_files_router(
    nexus_fs: Any | None = None,
    get_fs: Any | None = None,
) -> APIRouter:
    """
    Create a files router.

    Supports two modes:
    1. Direct: Pass nexus_fs instance (for testing)
    2. Lazy: Pass get_fs callable that returns the instance at request time
       (for server lifespan where fs is initialized after app creation)

    Args:
        nexus_fs: Initialized NexusFS instance (direct mode)
        get_fs: Callable returning NexusFS (lazy mode)

    Returns:
        FastAPI router with file endpoints
    """
    router = APIRouter(tags=["files"])

    # Import auth dependencies from main server
    from nexus.server.dependencies import get_auth_result, get_operation_context

    async def _get_fs() -> Any:
        """Get NexusFS, supporting both direct and lazy modes."""
        if nexus_fs is not None:
            return nexus_fs
        if get_fs is not None:
            fs = get_fs()
            if fs is not None:
                return cast(Any, fs)
        raise HTTPException(
            status_code=503,
            detail="NexusFS not initialized. Server may still be starting up.",
        )

    async def get_context(
        auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> Any:
        """Get operation context from auth result."""
        if auth_result is None or not auth_result.get("authenticated"):
            raise HTTPException(status_code=401, detail="Authentication required")
        return get_operation_context(auth_result)

    # =============================================================================
    # Write Endpoint
    # =============================================================================

    @router.post("/write", response_model=WriteResponse)
    async def write_file(
        request: WriteRequest,
        write_mode: str | None = Query(
            None,
            description="Write consistency mode: 'sync' (default, strong) or 'async' (eventual)",
        ),
        transaction_id: str | None = Query(
            None,
            description="Active transaction ID to track this write in (from snapshots API)",
        ),
        context: Any = Depends(get_context),
    ) -> Response:
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

            # Snapshot tracking setup: validate conflict + capture original state BEFORE write
            _ss = None
            _norm_path: str | None = None
            _original_hash: str | None = None
            _original_metadata: dict[str, Any] | None = None
            if transaction_id:
                _ss = fs.service("snapshot_service") if hasattr(fs, "service") else None
                if _ss is not None:
                    _norm_path = _normalize_path(request.path)
                    _ss.validate_path_available(transaction_id, _norm_path)
                    # Capture original state for rollback
                    try:
                        _orig_meta = fs.metadata.get(_norm_path)
                        if _orig_meta:
                            _original_hash = _orig_meta.etag
                            _original_metadata = {
                                "size": _orig_meta.size,
                                "version": _orig_meta.version,
                                "modified_at": _orig_meta.modified_at.isoformat()
                                if _orig_meta.modified_at
                                else None,
                                "backend_name": getattr(_orig_meta, "backend_name", None),
                            }
                    except Exception:
                        _original_hash = None
                else:
                    logger.warning(
                        "txn track: snapshot_service unavailable, skipping txn=%s", transaction_id
                    )

            # Decode content based on encoding
            if request.encoding == "base64":
                content = base64.b64decode(request.content)
            else:
                content = request.content.encode("utf-8")

            # Build write kwargs with optional write_mode (Issue #2929)
            write_kwargs: dict[str, Any] = {
                "path": request.path,
                "buf": content,
                "context": context,
            }
            if write_mode is not None:
                from nexus.contracts.types import WriteMode

                try:
                    mode = WriteMode(write_mode)
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid write_mode: {write_mode!r}. "
                        f"Valid values: {[m.value for m in WriteMode]}",
                    ) from None
                write_kwargs["consistency"] = mode.to_metastore_consistency()

            # fs.write is async — call directly
            result = fs.write(**write_kwargs)

            # Track write in transaction AFTER successful write.
            # Skip if _write_internal already tracked it (path already in registry).
            if (
                _ss is not None
                and _norm_path is not None
                and _ss._registry.get_transaction_for_path(_norm_path) != transaction_id
            ):
                try:
                    _ss.track_write(
                        transaction_id=transaction_id,
                        path=_norm_path,
                        original_hash=_original_hash,
                        original_metadata=_original_metadata,
                        new_hash=result.get("etag"),
                    )
                    logger.info("txn tracked write: txn=%s path=%s", transaction_id, _norm_path)
                except Exception as _track_err:
                    logger.warning("txn track write failed: %s", _track_err)

            modified = result["modified_at"]
            if hasattr(modified, "isoformat"):
                modified = modified.isoformat()
            response_data = WriteResponse(
                etag=result["etag"],
                version=result["version"],
                size=result["size"],
                modified_at=str(modified),
            )
            return Response(
                content=response_data.model_dump_json(),
                media_type="application/json",
            )

        except HTTPException:
            raise
        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except _TransactionConflictError as e:
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
        version: str | None = Query(
            None,
            description="Content hash (etag) to read a specific historical version from CAS",
        ),
        transaction_id: str | None = Query(
            None,
            description="Transaction ID — required when version is set to validate the hash belongs to this path",
        ),
        section: str | None = Query(
            None,
            description="(Markdown only) Read a specific section by heading text (case-insensitive)",
        ),
        block_type: str | None = Query(
            None,
            description="(Markdown only) Filter by block type within section: 'code' or 'table'",
        ),
        context: Any = Depends(get_context),
    ) -> Response:
        """
        Read file content.

        Supports ETag-based caching via If-None-Match header.
        Returns 304 Not Modified if content hasn't changed.

        When `version` is provided it is treated as a content-addressed hash
        (etag) and the content is fetched directly from CAS via the backend
        that owns `path`.  `transaction_id` is required alongside `version`
        so the server can validate that the requested hash actually belongs to
        `path` in that transaction, preventing cross-path blob reads.
        """
        try:
            fs = await _get_fs()

            # Fast path: content-hash (etag) lookup — used by the diff viewer
            # to retrieve a historical snapshot stored in CAS.
            if version:
                if not transaction_id:
                    raise HTTPException(
                        status_code=400,
                        detail="transaction_id is required when version is specified",
                    )
                # --- Zone-ownership check (mirrors snapshots router) ---
                _snap_svc = getattr(request.app.state, "transactional_snapshot_service", None)
                if _snap_svc is None:
                    _snap_svc = getattr(fs, "_snapshot_service", None)
                if _snap_svc is None:
                    raise HTTPException(status_code=503, detail="Snapshot service not available")
                from nexus.contracts.constants import ROOT_ZONE_ID as _ROOT_ZONE_ID

                _caller_zone = getattr(context, "zone_id", None) or _ROOT_ZONE_ID
                _txn_info = await _snap_svc.get_transaction(transaction_id)
                if _txn_info is None or _txn_info.zone_id != _caller_zone:
                    raise HTTPException(
                        status_code=404, detail=f"Transaction not found: {transaction_id}"
                    )
                # --- Validate hash belongs to path in this transaction ---
                _entries = await _snap_svc.list_entries(transaction_id)
                _valid = any(
                    e.path == path and (e.original_hash == version or e.new_hash == version)
                    for e in _entries
                )
                if not _valid:
                    raise HTTPException(
                        status_code=403,
                        detail=f"version is not recorded for this path in transaction {transaction_id!r}",
                    )
                try:
                    route = fs.router.route(path)
                except Exception as exc:
                    raise NexusFileNotFoundError(f"{path} (version {version})") from exc
                if route is None:
                    raise NexusFileNotFoundError(f"{path} (version {version})")
                # --- Enforce standard read authorization via the VFS path ---
                try:
                    _accessible = fs.access(path, context=context)
                except NexusPermissionError as e:
                    raise HTTPException(status_code=403, detail=str(e)) from e
                if not _accessible:
                    raise NexusFileNotFoundError(path)
                # --- Gate on CAS-capable backend ---
                from nexus.contracts.backend_features import BackendFeature as _BF

                if not route.backend.has_feature(_BF.CAS):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Historical version reads are only supported on CAS-addressed backends; "
                            f"backend for {path!r} does not support content-addressed history"
                        ),
                    )
                import dataclasses as _dc

                _read_ctx = _dc.replace(
                    context,
                    backend_path=getattr(route, "backend_path", path),
                    virtual_path=path,
                )
                raw: bytes = await asyncio.to_thread(route.backend.read_content, version, _read_ctx)
                text_v = raw.decode("utf-8", errors="replace")
                resp_v = ReadResponse(content=text_v, etag=version)
                return Response(
                    content=resp_v.model_dump_json(),
                    media_type="application/json",
                    headers={"ETag": f'"{version}"'},
                )

            # Check If-None-Match header for caching
            if_none_match = request.headers.get("If-None-Match")

            # Get metadata first for ETag check
            if if_none_match:
                meta = fs.sys_stat(path)
                if meta and meta.etag:
                    client_etag = if_none_match.strip('"')
                    if client_etag == meta.etag:
                        return Response(
                            status_code=304,
                            headers={"ETag": f'"{meta.etag}"'},
                        )

            # Issue #3266: For connector display paths (/mnt/*), resolve
            # virtual_path → physical_path via search service and read
            # directly from the connector backend. This avoids passing
            # a backend-relative path into fs.read() which expects VFS paths.
            connector_content: bytes | None = None
            if path.startswith("/mnt/"):
                search = fs.service("search")
                if search is not None:
                    resolve_fn = getattr(search, "resolve_physical_path", None)
                    physical: str | None = resolve_fn(path) if resolve_fn else None
                    if physical:
                        connector_content = await _read_connector_by_physical_path(
                            fs,
                            path,
                            physical,
                            context,
                        )

            if connector_content is not None:
                # Connector fast path — return content directly
                text = connector_content.decode("utf-8", errors="replace")
                if include_metadata:
                    resp = ReadResponse(
                        content=text,
                        etag=None,
                        version=None,
                        modified_at=None,
                        size=len(connector_content),
                    )
                else:
                    resp = ReadResponse(content=text)
                return Response(
                    content=resp.model_dump_json(),
                    media_type="application/json",
                )

            # Standard VFS read
            result = fs.read(path, return_metadata=include_metadata, context=context)

            # --- Markdown partial read (Issue #3718) ---
            if section and path.endswith(".md"):
                raw_content: bytes
                if include_metadata and isinstance(result, dict):
                    _rc = result["content"]
                    raw_content = _rc if isinstance(_rc, bytes) else _rc.encode("utf-8")
                elif isinstance(result, bytes):
                    raw_content = result
                else:
                    raw_content = str(result).encode("utf-8")

                partial = _md_partial_read(fs, path, raw_content, section, block_type)
                if partial is not None:
                    return Response(
                        content=ReadResponse(content=partial).model_dump_json(),
                        media_type="application/json",
                    )

            if include_metadata and isinstance(result, dict):
                file_content: str = result["content"]
                if isinstance(file_content, bytes):
                    file_content = file_content.decode("utf-8", errors="replace")

                response_data = ReadResponse(
                    content=file_content,
                    etag=result.get("etag"),
                    version=result.get("version"),
                    modified_at=result.get("modified_at"),
                    size=result.get("size"),
                )
                etag_val = result.get("etag")
                return Response(
                    content=response_data.model_dump_json(),
                    media_type="application/json",
                    headers={"ETag": f'"{etag_val}"'} if etag_val else {},
                )
            else:
                # Simple content response
                plain: str = (
                    result
                    if isinstance(result, str)
                    else (
                        result.decode("utf-8", errors="replace")
                        if isinstance(result, bytes)
                        else str(result)
                    )
                )
                return Response(
                    content=ReadResponse(content=plain).model_dump_json(),
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
    # Markdown Structure Endpoint (Issue #3718)
    # =============================================================================

    @router.get("/md-structure")
    async def md_structure(
        path: str = Query(..., description="Path to a markdown file"),
        context: Any = Depends(get_context),
    ) -> Response:
        """Return the structural index of a markdown file (headings, blocks, token estimates).

        No file content is returned — only metadata for targeted reads via
        ``GET /read?section=...&block_type=...``.
        """
        try:
            fs = await _get_fs()
            # Permission gate + content fetch for lazy index rebuild.
            accessible = await fs.access(path, context=context)
            if not accessible:
                raise NexusFileNotFoundError(path)
            raw = await fs.read(path, context=context)
            content = (
                raw
                if isinstance(raw, bytes)
                else (raw["content"] if isinstance(raw, dict) else str(raw).encode("utf-8"))
            )
            if isinstance(content, str):
                content = content.encode("utf-8")
            content_hash = _md_get_etag(fs, path)
            listing = _md_get_listing(fs, path, content=content, content_hash=content_hash)
            if listing is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No markdown structure available for {path}",
                )
            return Response(
                content=json.dumps(listing),
                media_type="application/json",
            )
        except HTTPException:
            raise
        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"md-structure error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Markdown helpers (Issue #3718) ---

    def _md_get_etag(fs: Any, path: str) -> str:
        """Get the authoritative file etag from the metastore primary row."""
        meta = getattr(fs, "metadata", None)
        if meta is None:
            return ""
        try:
            file_meta = meta.get(path)
            return file_meta.etag if file_meta and file_meta.etag else ""
        except Exception:
            return ""

    def _md_partial_read(
        fs: Any,
        path: str,
        content: bytes,
        section_name: str,
        block_type: str | None,
    ) -> str | None:
        """Attempt a partial markdown read. Returns content string or None."""
        try:
            hook = fs.service("md_structure") if hasattr(fs, "service") else None
            if hook is None or not hasattr(hook, "read_section"):
                return None
            content_hash = _md_get_etag(fs, path)
            return hook.read_section(path, content, content_hash, section_name, block_type)
        except Exception:
            logger.debug("md partial read failed for %s", path, exc_info=True)
            return None

    def _md_get_listing(
        fs: Any,
        path: str,
        content: bytes | None = None,
        content_hash: str = "",
    ) -> list[dict[str, Any]] | None:
        """Get the structure listing for a markdown file."""
        try:
            hook = fs.service("md_structure") if hasattr(fs, "service") else None
            if hook is None or not hasattr(hook, "get_structure_listing"):
                return None
            return hook.get_structure_listing(path, content=content, content_hash=content_hash)
        except Exception:
            logger.debug("md structure listing failed for %s", path, exc_info=True)
            return None

    # =============================================================================
    # Delete Endpoint
    # =============================================================================

    @router.delete("/delete", response_model=DeleteResponse)
    async def delete_file(
        path: str = Query(..., description="Path to delete"),
        transaction_id: str | None = Query(
            None,
            description="Active transaction ID to track this delete in",
        ),
        context: Any = Depends(get_context),
    ) -> DeleteResponse:
        """Delete a file."""
        try:
            fs = await _get_fs()

            _ss = None
            _norm_path: str | None = None
            _original_hash: str | None = None
            _original_metadata: dict[str, Any] | None = None
            if transaction_id:
                _ss = fs.service("snapshot_service") if hasattr(fs, "service") else None
                if _ss is not None:
                    _norm_path = _normalize_path(path)
                    _ss.validate_path_available(transaction_id, _norm_path)
                    try:
                        _orig_meta = fs.metadata.get(_norm_path)
                        if _orig_meta:
                            _original_hash = _orig_meta.etag
                            _original_metadata = {
                                "size": _orig_meta.size,
                                "version": _orig_meta.version,
                                "modified_at": _orig_meta.modified_at.isoformat()
                                if _orig_meta.modified_at
                                else None,
                                "backend_name": getattr(_orig_meta, "backend_name", None),
                            }
                    except Exception:
                        _original_hash = None

            fs.sys_unlink(path, context=context)

            if (
                _ss is not None
                and _norm_path is not None
                and _ss._registry.get_transaction_for_path(_norm_path) != transaction_id
            ):
                try:
                    _ss.track_delete(
                        transaction_id=transaction_id,
                        path=_norm_path,
                        original_hash=_original_hash,
                        original_metadata=_original_metadata,
                    )
                except Exception as _track_err:
                    logger.warning("txn track delete failed: %s", _track_err)

            return DeleteResponse(deleted=True, path=path)

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except _TransactionConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
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
            exists = fs.access(path, context=context)
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

    @router.get("/list", response_model=ListResponse, response_model_by_alias=True)
    async def list_directory(
        path: str = Query(..., description="Directory path to list"),
        limit: int | None = Query(
            None, ge=1, le=1000, description="Max items per page (default: all)"
        ),
        cursor: str | None = Query(
            None, description="Opaque cursor from previous response's next_cursor"
        ),
        context: Any = Depends(get_context),
    ) -> ListResponse:
        """List directory contents with optional cursor pagination.

        When ``limit`` is provided, returns at most ``limit`` items plus
        ``has_more`` / ``next_cursor`` for fetching the next page.  Without
        ``limit``, all entries are returned in a single response (backward
        compatible).

        @see Issue #3102, Decision 2A
        """
        try:
            fs = await _get_fs()

            # Decode opaque cursor (base64-encoded path)
            cursor_path: str | None = None
            if cursor is not None:
                try:
                    cursor_path = base64.b64decode(cursor).decode("utf-8")
                except Exception:
                    raise HTTPException(status_code=400, detail="Invalid cursor") from None

            # Issue #3266: Prefer search service for listing (metastore-first).
            # Same pattern as the RPC list handler in filesystem.py.
            # Note: search.list() returns plain paths for dynamic connectors,
            # while sys_readdir(details=True) returns detail dicts. We use
            # sys_readdir as the primary path and only fall back to search
            # for connector mounts where metastore-first listing is needed.
            result = fs.sys_readdir(
                path,
                recursive=False,
                details=True,
                context=context,
                limit=limit,
                cursor=cursor_path,
            )

            # Issue #3266: If sys_readdir returned nothing for a connector
            # mount, try the search service (metastore-first listing).
            result_items = result.items if hasattr(result, "items") else result
            if not result_items and path.startswith("/mnt/"):
                search = fs.service("search")
                if search is not None:
                    search_result = search.list(
                        path=path,
                        recursive=False,
                        context=context,
                    )
                    if search_result:
                        # Convert plain paths to detail dicts
                        prefix = path.rstrip("/") + "/"
                        detail_list = []
                        for entry in search_result:
                            entry_path = entry if isinstance(entry, str) else str(entry)
                            entry_path = entry_path.rstrip("/")
                            name = entry_path.split("/")[-1] if "/" in entry_path else entry_path
                            is_dir = entry.endswith("/") if isinstance(entry, str) else False
                            detail_list.append(
                                {
                                    "path": entry_path,
                                    "name": name,
                                    "is_directory": is_dir,
                                    "size": 0,
                                }
                            )
                        result = detail_list

            # Inject mount point parent directories into the listing.
            # Mount points exist in the VFS router but not in the metastore,
            # so sys_readdir doesn't return them. We synthesize directory
            # entries for immediate children of the listed path that are
            # mount point ancestors.
            try:
                mount_svc = fs.service("mount")
                if mount_svc:
                    mount_list_result = mount_svc.list_mounts()
                    if hasattr(mount_list_result, "__await__"):
                        mounts_raw = await mount_list_result
                    else:
                        mounts_raw = mount_list_result
                    listed_prefix = path.rstrip("/") + "/"
                    existing_paths = {
                        (e.get("path", "") if isinstance(e, dict) else str(e)).rstrip("/")
                        for e in (
                            result if isinstance(result, list) else getattr(result, "items", [])
                        )
                    }
                    for m in mounts_raw:
                        mp = (m.get("mount_point", "") if isinstance(m, dict) else str(m)).rstrip(
                            "/"
                        )
                        if not mp.startswith(listed_prefix) and not (
                            path == "/" and mp.startswith("/")
                        ):
                            continue
                        # Find the immediate child segment
                        relative = mp.lstrip("/") if path == "/" else mp[len(listed_prefix) :]
                        child = relative.split("/")[0] if "/" in relative else relative
                        if not child:
                            continue
                        child_path = f"{path.rstrip('/')}/{child}"
                        if child_path.rstrip("/") not in existing_paths:
                            synthetic = {
                                "path": child_path,
                                "name": child,
                                "entry_type": 1,
                                "size": 0,
                                "is_directory": True,
                            }
                            if isinstance(result, list):
                                result.append(synthetic)
                            elif hasattr(result, "items"):
                                result.items.append(synthetic)
                            existing_paths.add(child_path.rstrip("/"))
            except Exception:
                pass  # Best effort — listing still works without mount injection

            prefix = path.rstrip("/") + "/"
            # The listed path itself (e.g. "/" → empty name) must not appear
            # in its own listing — that causes infinite recursion in tree UIs.
            clean_path = path.rstrip("/") or "/"

            # Internal paths stored in the metastore (system config, ReBAC
            # namespaces) must not leak into user-facing directory listings.
            _INTERNAL_PREFIXES = ("cfg:", "ns:")

            def _is_visible(entry: dict) -> bool:
                p = entry.get("path", "")
                name = p.lstrip("/").split("/")[0] if "/" in p else p.lstrip("/")
                return p.rstrip("/") != clean_path.rstrip("/") and not name.startswith(
                    _INTERNAL_PREFIXES
                )

            # Paginated path: keep advancing until the page has visible items
            # or there are no more results. This prevents empty pages when
            # internal entries (cfg:, ns:) consume an entire page.
            if limit is not None:
                file_items: list[FileItemResponse] = []
                # sys_readdir may return a paginated object (with .items,
                # .has_more, .next_cursor) or a plain list for connector
                # backends. Normalize to avoid AttributeError.
                if isinstance(result, list):
                    _page_items = result
                    has_more = False
                    next_cursor_raw = None
                else:
                    _page_items = result.items
                    has_more = result.has_more
                    next_cursor_raw = result.next_cursor

                # Collect visible items from current page
                for entry in _page_items:
                    if _is_visible(entry):
                        file_items.append(_to_file_item(entry, prefix))

                # If filtering emptied the page but more data exists, keep fetching
                while not file_items and has_more and next_cursor_raw:
                    result = fs.sys_readdir(
                        path,
                        recursive=False,
                        details=True,
                        context=context,
                        limit=limit,
                        cursor=next_cursor_raw,
                    )
                    if isinstance(result, list):
                        _page_items = result
                        has_more = False
                        next_cursor_raw = None
                    else:
                        _page_items = result.items
                        has_more = result.has_more
                        next_cursor_raw = result.next_cursor
                    for entry in _page_items:
                        if _is_visible(entry):
                            file_items.append(_to_file_item(entry, prefix))

                next_cursor = (
                    base64.b64encode(next_cursor_raw.encode("utf-8")).decode("ascii")
                    if next_cursor_raw
                    else None
                )
                return ListResponse(
                    items=file_items,
                    has_more=has_more,
                    next_cursor=next_cursor,
                )

            # Non-paginated path (backward compat): result is list[dict]
            file_items = [_to_file_item(entry, prefix) for entry in result if _is_visible(entry)]
            return ListResponse(items=file_items, has_more=False)

        except HTTPException:
            raise
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
            # fs.mkdir is async — call directly
            fs.mkdir(request.path, parents=request.parents, context=context)
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
            meta = fs.sys_stat(path, context=context)
            if meta is None:
                raise NexusFileNotFoundError(path=path)

            return MetadataResponse(
                path=meta.path,
                size=meta.size,
                etag=meta.etag,
                version=meta.version,
                is_directory=meta.is_dir,
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
            results = await asyncio.to_thread(fs.read_bulk, request.paths, context=context)

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
    # Batch Write Endpoint (Issue #3700)
    # =============================================================================

    @router.post("/batch/write", response_model=BatchWriteResponse)
    async def batch_write_files(
        request: BatchWriteRequest,
        context: Any = Depends(get_context),
    ) -> BatchWriteResponse:
        """
        Write multiple files in a single round-trip for improved performance.

        **Best-effort, not atomic**: each file is written independently. A
        mid-batch failure leaves already-written files on disk; no rollback or
        compensation is performed. Callers that need true all-or-nothing
        semantics must implement their own retry/reconcile logic using the
        returned etags.

        13× faster than N sequential writes for small files.

        Content must be base64-encoded. Max {_MAX_BATCH_FILES} files and
        {_MAX_BATCH_TOTAL_BYTES // (1024*1024)} MB total decoded size per request.
        """
        try:
            fs = await _get_fs()
            files = [(item.path, base64.b64decode(item.content_base64)) for item in request.files]
            raw_results = fs.write_batch(files, context=context)
            return BatchWriteResponse(
                results=[
                    BatchWriteResult(
                        path=r["path"] if "path" in r else files[i][0],
                        etag=r.get("etag"),
                        version=r.get("version", 0),
                        modified_at=r.get("modified_at"),
                        size=r.get("size", 0),
                    )
                    for i, r in enumerate(raw_results)
                ]
            )
        except (NexusPermissionError, AccessDeniedError) as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Batch write error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Batch Read Endpoint v2 (Issue #3700)
    # =============================================================================

    @router.post("/batch/read", response_model=BatchReadResponse)
    async def batch_read_files_v2(
        request: BatchReadAtomicRequest,
        context: Any = Depends(get_context),
    ) -> BatchReadResponse:
        """
        Read multiple files in a single atomic round-trip.

        Uses the Rust kernel's parallel read path — faster and more consistent
        than N sequential reads.

        In strict mode (partial=false, default): any missing or inaccessible
        path raises a 404/403. In partial mode (partial=true): returns a
        per-item success or error for every path.
        """
        try:
            fs = await _get_fs()
            raw_results = fs.read_batch(request.paths, partial=request.partial, context=context)
            # Belt-and-suspenders aggregate size guard (Finding #3).
            # NexusFS.read_batch() already pre-checks via metadata sizes; this
            # post-read guard catches any content that slipped through (e.g. from
            # external-mount fallback paths whose metadata sizes were unknown).
            _total_read = sum(
                len(r.get("content", b"")) for r in raw_results if isinstance(r, dict)
            )
            if _total_read > _MAX_BATCH_TOTAL_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Batch read response size {_total_read} bytes exceeds "
                        f"{_MAX_BATCH_TOTAL_BYTES // (1024 * 1024)} MB limit"
                    ),
                )
            items: list[BatchReadSuccess | BatchReadError] = []
            for r in raw_results:
                if "error" in r:
                    items.append(BatchReadError(type="error", path=r["path"], error=r["error"]))
                else:
                    items.append(
                        BatchReadSuccess(
                            type="success",
                            path=r["path"],
                            content_base64=base64.b64encode(r["content"]).decode(),
                            etag=r.get("etag"),
                            version=r.get("version", 0),
                            modified_at=r.get("modified_at"),
                            size=r.get("size", 0),
                        )
                    )
            return BatchReadResponse(results=items)
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (NexusPermissionError, AccessDeniedError) as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            # Pass through 413 (post-read size guard) and any other HTTP exceptions
            # without re-wrapping them as 500.  Must come before the blanket handler.
            raise
        except ValueError as e:
            # Kernel-side size-limit check (read_batch) raises ValueError with a
            # descriptive message.  Surface it as 413 Request Entity Too Large.
            raise HTTPException(status_code=413, detail=str(e)) from e
        except Exception as e:
            logger.exception("Batch read v2 error: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Stream Endpoint
    # =============================================================================

    @router.get("/stream", response_model=None)
    async def stream_file(
        request: Request,
        path: str = Query(..., description="Path to stream"),
        chunk_size: int = Query(65536, description="Chunk size in bytes"),
        context: Any = Depends(get_context),
    ) -> Response | StreamingResponse:
        """
        Stream file content in chunks with HTTP Range support (RFC 9110).

        Supports partial content (206), full content (200), and range
        not satisfiable (416) responses. Useful for large files, download
        resumption, and media seeking.
        """
        from nexus.server.range_utils import build_range_response

        try:
            fs = await _get_fs()
            meta = fs.sys_stat(path, context=context)
            if meta is None:
                raise NexusFileNotFoundError(path=path)

            def _range_generator(start: int, end: int, cs: int) -> Iterator[bytes]:
                """Sync generator wrapping NexusFS.read_range()."""
                data = fs.read_range(path, start, end, context=context)
                if isinstance(data, bytes):
                    for i in range(0, len(data), cs):
                        yield data[i : i + cs]

            async def _full_generator() -> AsyncIterator[bytes]:
                """Sync generator wrapping NexusFS.read()."""
                data = fs.sys_read(path, context=context)
                if isinstance(data, bytes):
                    for i in range(0, len(data), chunk_size):
                        yield data[i : i + chunk_size]
                else:
                    raw = str(data).encode("utf-8")
                    for i in range(0, len(raw), chunk_size):
                        yield raw[i : i + chunk_size]

            return build_range_response(
                request_headers=request.headers,
                content_generator=_range_generator,
                full_generator=_full_generator,
                total_size=meta.size,
                etag=meta.etag,
                content_type=meta.mime_type or "application/octet-stream",
                filename=path.split("/")[-1],
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

    # =============================================================================
    # Rename Endpoint
    # =============================================================================

    STREAMING_COPY_THRESHOLD = 10 * 1024 * 1024  # 10 MB

    @router.post("/rename", response_model=RenameResponse)
    async def rename_file(
        request: RenameRequest,
        context: Any = Depends(get_context),
    ) -> RenameResponse:
        """
        Rename or move a file.

        This is a metadata-only O(1) operation — file content is not copied.
        Works instantly regardless of file size.
        """
        try:
            fs = await _get_fs()
            fs.sys_rename(request.source, request.destination, context=context)
            return RenameResponse(
                success=True,
                source=request.source,
                destination=request.destination,
            )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Rename error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Copy Endpoint
    # =============================================================================

    @router.post("/copy", response_model=CopyResponse)
    async def copy_file(
        request: CopyRequest,
        context: Any = Depends(get_context),
    ) -> CopyResponse:
        """
        Copy a file from source to destination.

        Uses streaming for files >= 10 MB to avoid loading entire content
        into memory. Smaller files are read/written in a single operation.
        """
        try:
            fs = await _get_fs()

            # Check source exists and get size
            meta = fs.sys_stat(request.source, context=context)
            if meta is None:
                raise NexusFileNotFoundError(path=request.source)

            file_size = meta.get("size", 0) or 0

            if file_size < STREAMING_COPY_THRESHOLD:
                # Small file: read all then write all
                content = fs.sys_read(request.source, context=context)
                fs.write(request.destination, buf=content, context=context)
                bytes_copied = len(content)
            else:
                # Large file: streaming copy
                chunks = await asyncio.to_thread(fs.stream, request.source, context=context)
                result = await asyncio.to_thread(
                    fs.write_stream, request.destination, chunks, context=context
                )
                bytes_copied = result.get("size", file_size)

            return CopyResponse(
                success=True,
                source=request.source,
                destination=request.destination,
                bytes_copied=bytes_copied,
            )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except NexusFileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Copy error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Bulk Rename Endpoint
    # =============================================================================

    @router.post("/rename-batch", response_model=RenameBatchResponse)
    async def rename_batch(
        request: RenameBatchRequest,
        context: Any = Depends(get_context),
    ) -> RenameBatchResponse:
        """
        Rename/move multiple files in a single request.

        Each operation is processed independently — failures on one do not
        affect others. Maximum 50 operations per request.
        """
        try:
            fs = await _get_fs()
            renames = [(op.source, op.destination) for op in request.operations]
            raw_results = fs.rename_batch(renames, context=context)

            results: list[BulkRenameResult] = []
            for op in request.operations:
                entry = raw_results.get(op.source, {})
                results.append(
                    BulkRenameResult(
                        source=op.source,
                        destination=op.destination,
                        success=entry.get("success", False),
                        error=entry.get("error"),
                    )
                )

            return RenameBatchResponse(results=results)

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Bulk rename error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Bulk Copy Endpoint
    # =============================================================================

    @router.post("/copy-bulk", response_model=CopyBulkResponse)
    async def copy_bulk(
        request: CopyBulkRequest,
        context: Any = Depends(get_context),
    ) -> CopyBulkResponse:
        """
        Copy multiple files in a single request.

        Each operation is processed independently — failures on one do not
        affect others. Maximum 50 operations per request. Uses streaming
        for files >= 10 MB.
        """
        try:
            fs = await _get_fs()
            results: list[BulkCopyResult] = []

            for op in request.operations:
                try:
                    meta = fs.sys_stat(op.source, context=context)
                    if meta is None:
                        results.append(
                            BulkCopyResult(
                                source=op.source,
                                destination=op.destination,
                                success=False,
                                error=f"Source not found: {op.source}",
                            )
                        )
                        continue

                    file_size = meta.get("size", 0) or 0

                    if file_size < STREAMING_COPY_THRESHOLD:
                        content = fs.sys_read(op.source, context=context)
                        fs.write(op.destination, buf=content, context=context)
                        bytes_copied = len(content)
                    else:
                        chunks = await asyncio.to_thread(fs.stream, op.source, context=context)
                        write_result = await asyncio.to_thread(
                            fs.write_stream, op.destination, chunks, context=context
                        )
                        bytes_copied = write_result.get("size", file_size)

                    results.append(
                        BulkCopyResult(
                            source=op.source,
                            destination=op.destination,
                            success=True,
                            bytes_copied=bytes_copied,
                        )
                    )
                except Exception as e:
                    results.append(
                        BulkCopyResult(
                            source=op.source,
                            destination=op.destination,
                            success=False,
                            error=str(e),
                        )
                    )

            return CopyBulkResponse(results=results)

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Bulk copy error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Glob Search Endpoint
    # =============================================================================

    @router.get("/glob", response_model=GlobResponse)
    async def glob_search(
        pattern: str = Query(..., description="Glob pattern to match (e.g. '**/*.py')"),
        path: str = Query("/", description="Base path to search under"),
        limit: int = Query(100, description="Maximum number of results", ge=1, le=1000),
        context: Any = Depends(get_context),
    ) -> GlobResponse:
        """
        Search for files matching a glob pattern.

        Uses Rust-accelerated glob matching when available, with automatic
        fallback to Python fnmatch.
        """
        try:
            fs = await _get_fs()

            # List all files under the base path
            # sys_readdir is async — call directly, not via to_thread
            all_paths = fs.sys_readdir(path, recursive=True, context=context)

            # Apply glob pattern filter
            matched = await asyncio.to_thread(glob_filter, all_paths, include_patterns=[pattern])

            total = len(matched)
            truncated = total > limit
            result_paths = matched[:limit]

            return GlobResponse(
                matches=result_paths,
                total=total,
                truncated=truncated,
                pattern=pattern,
                base_path=path,
            )

        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Glob error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # =============================================================================
    # Grep Search Endpoint
    # =============================================================================

    @router.get("/grep", response_model=GrepResponse)
    async def grep_search(
        pattern: str = Query(..., description="Regex pattern to search for"),
        path: str = Query("/", description="Base path to search under"),
        ignore_case: bool = Query(False, description="Case-insensitive matching"),
        limit: int = Query(100, description="Maximum number of results", ge=1, le=1000),
        context: Any = Depends(get_context),
    ) -> GrepResponse:
        """
        Search for content matching a regex pattern within files.

        Uses Rust-accelerated mmap-based grep when available, with automatic
        fallback to Python re module.
        """
        import re

        try:
            fs = await _get_fs()

            # List all files under the base path
            # sys_readdir is async — call directly, not via to_thread
            all_paths = fs.sys_readdir(path, recursive=True, context=context)

            # Try Rust mmap grep first
            results = await asyncio.to_thread(
                grep_files_mmap, pattern, all_paths, ignore_case, limit
            )

            # Fallback to Python re if Rust is unavailable
            if results is None:
                flags = re.IGNORECASE if ignore_case else 0
                try:
                    compiled = re.compile(pattern, flags)
                except re.error as e:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid regex pattern: {e}"
                    ) from e

                results = []
                for file_path in all_paths:
                    if len(results) >= limit:
                        break
                    try:
                        content = fs.read(file_path, context=context)
                        if isinstance(content, bytes):
                            content = content.decode("utf-8", errors="replace")
                        elif isinstance(content, dict):
                            content = content.get("content", "")
                            if isinstance(content, bytes):
                                content = content.decode("utf-8", errors="replace")
                        for line_num, line in enumerate(str(content).splitlines(), 1):
                            m = compiled.search(line)
                            if m:
                                results.append(
                                    {
                                        "file": file_path,
                                        "line": line_num,
                                        "content": line,
                                        "match": m.group(0),
                                    }
                                )
                                if len(results) >= limit:
                                    break
                    except Exception:
                        # Skip files that can't be read (binary, permissions, etc.)
                        continue

            total = len(results)
            truncated = total >= limit
            matches = [
                GrepMatch(
                    file=r["file"],
                    line=r["line"],
                    content=r["content"],
                    match=r["match"],
                )
                for r in results[:limit]
            ]

            return GrepResponse(
                matches=matches,
                total=total,
                truncated=truncated,
                pattern=pattern,
                base_path=path,
            )

        except HTTPException:
            raise
        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except InvalidPathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Grep error: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
