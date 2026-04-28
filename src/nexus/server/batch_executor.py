"""Batch operation executor for io_uring-style VFS submission (Issue #1242).

Executes multiple VFS operations in a single request, reducing HTTP round-trips.
Operations execute sequentially; each produces its own status code.

Design decisions (from architecture review):
    - Direct VFS dispatch via NexusFS (no ASGI loopback)
    - VFS operations only: read, write, delete, stat, exists, list, mkdir
    - Per-operation timeout via asyncio.wait_for (30s default)
    - Strict validation: 1-50 ops, max 10MB total payload

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §7.2 (io_uring → Batch API)
    - Linux io_uring: submission queue / completion queue pattern
"""

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, Tag, model_validator

from nexus.contracts.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Maximum total payload size for write operations (10 MB).
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024

# Default per-operation timeout in seconds.
# Issue #2071: Sourced from ProfileTuning.network.default_http_timeout at runtime.
# Kept as module-level fallback for callers without DI tuning.
DEFAULT_OPERATION_TIMEOUT = 30.0

# =============================================================================
# Operation Models (Pydantic discriminated union)
# =============================================================================


class ReadOp(BaseModel):
    """Read file content."""

    op: Literal["read"] = "read"
    path: str = Field(..., description="Virtual path to read")


class WriteOp(BaseModel):
    """Write content to a file."""

    op: Literal["write"] = "write"
    path: str = Field(..., description="Virtual path to write")
    content: str = Field(..., description="File content (string or base64 encoded)")
    encoding: str | None = Field(None, description="Content encoding: 'utf8' (default) or 'base64'")


class DeleteOp(BaseModel):
    """Delete a file."""

    op: Literal["delete"] = "delete"
    path: str = Field(..., description="Virtual path to delete")


class StatOp(BaseModel):
    """Get file metadata (size, version, timestamps)."""

    op: Literal["stat"] = "stat"
    path: str = Field(..., description="Virtual path to stat")


class ExistsOp(BaseModel):
    """Check if a path exists."""

    op: Literal["exists"] = "exists"
    path: str = Field(..., description="Virtual path to check")


class ListOp(BaseModel):
    """List directory contents."""

    op: Literal["list"] = "list"
    path: str = Field(..., description="Directory path to list")


class MkdirOp(BaseModel):
    """Create a directory."""

    op: Literal["mkdir"] = "mkdir"
    path: str = Field(..., description="Directory path to create")
    parents: bool = Field(True, description="Create parent directories if needed")


def _get_op_discriminator(v: Any) -> str:
    """Extract discriminator value from raw data or model instance."""
    if isinstance(v, dict):
        return str(v.get("op", ""))
    return str(getattr(v, "op", ""))


BatchOperation = Annotated[
    Annotated[ReadOp, Tag("read")]
    | Annotated[WriteOp, Tag("write")]
    | Annotated[DeleteOp, Tag("delete")]
    | Annotated[StatOp, Tag("stat")]
    | Annotated[ExistsOp, Tag("exists")]
    | Annotated[ListOp, Tag("list")]
    | Annotated[MkdirOp, Tag("mkdir")],
    Discriminator(_get_op_discriminator),
]

# =============================================================================
# Request / Response Models
# =============================================================================


class BatchRequest(BaseModel):
    """Batch VFS operation request (io_uring submission queue)."""

    operations: list[BatchOperation] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="VFS operations to execute sequentially (1-50)",
    )
    stop_on_error: bool = Field(
        False,
        description="Stop executing on first failure; remaining ops get status 424",
    )

    @model_validator(mode="after")
    def _check_total_payload_size(self) -> "BatchRequest":
        """Reject if total write content exceeds 10 MB."""
        total = 0
        for op in self.operations:
            if isinstance(op, WriteOp):
                total += len(op.content.encode("utf-8"))
        if total > MAX_PAYLOAD_BYTES:
            msg = (
                f"Total write payload ({total:,} bytes) exceeds "
                f"maximum ({MAX_PAYLOAD_BYTES:,} bytes)"
            )
            raise ValueError(msg)
        return self


class OperationResult(BaseModel):
    """Result of a single batch operation (io_uring completion entry)."""

    index: int = Field(..., description="Position in the original operations array")
    status: int = Field(..., description="HTTP-style status code for this operation")
    data: dict[str, Any] | None = Field(None, description="Operation result data (None on error)")
    error: str | None = Field(None, description="Error message (None on success)")


class BatchResponse(BaseModel):
    """Batch operation response (io_uring completion queue)."""

    results: list[OperationResult]


# =============================================================================
# Exception → HTTP status mapping
# =============================================================================

_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    NexusPermissionError: 403,
    NexusFileNotFoundError: 404,
    InvalidPathError: 400,
    ConflictError: 409,
    FileExistsError: 409,
    FileNotFoundError: 404,
}


def _map_exception(exc: Exception) -> tuple[int, str]:
    """Map a VFS exception to (status_code, error_message)."""
    for exc_type, status in _EXCEPTION_STATUS_MAP.items():
        if isinstance(exc, exc_type):
            return status, str(exc)
    return 500, str(exc)


# =============================================================================
# Batch Executor
# =============================================================================


class BatchExecutor:
    """Executes a batch of VFS operations sequentially against NexusFS.

    Follows the io_uring pattern: operations submitted as a batch,
    results returned with per-operation status codes.

    Args:
        fs: The async filesystem to execute operations against.
        operation_timeout: Per-operation timeout in seconds (default 30).
    """

    def __init__(
        self,
        fs: Any,
        operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> None:
        self._fs = fs
        self._timeout = operation_timeout

    async def execute(
        self,
        request: "BatchRequest",
        context: "OperationContext",
    ) -> BatchResponse:
        """Execute all operations in the batch sequentially.

        Returns a BatchResponse with one OperationResult per operation.
        On stop_on_error, remaining operations after a failure get status 424.
        """
        results: list[OperationResult] = []

        for i, op in enumerate(request.operations):
            try:
                result = await asyncio.wait_for(
                    self._dispatch(op, context),
                    timeout=self._timeout,
                )
                result.index = i
                results.append(result)
            except TimeoutError:
                results.append(
                    OperationResult(
                        index=i,
                        status=504,
                        data=None,
                        error="Operation timed out",
                    )
                )
                if request.stop_on_error:
                    results.extend(self._skip_remaining(i + 1, len(request.operations)))
                    break
            except Exception as exc:
                status, error = _map_exception(exc)
                results.append(OperationResult(index=i, status=status, data=None, error=error))
                if request.stop_on_error:
                    results.extend(self._skip_remaining(i + 1, len(request.operations)))
                    break

        return BatchResponse(results=results)

    async def _dispatch(
        self,
        op: ReadOp | WriteOp | DeleteOp | StatOp | ExistsOp | ListOp | MkdirOp,
        context: "OperationContext",
    ) -> OperationResult:
        """Dispatch a single operation to the appropriate NexusFS method."""
        if isinstance(op, ReadOp):
            return await self._exec_read(op, context)
        if isinstance(op, WriteOp):
            return await self._exec_write(op, context)
        if isinstance(op, DeleteOp):
            return await self._exec_delete(op, context)
        if isinstance(op, StatOp):
            return await self._exec_stat(op, context)
        if isinstance(op, ExistsOp):
            return await self._exec_exists(op, context)
        if isinstance(op, ListOp):
            return await self._exec_list(op, context)
        if isinstance(op, MkdirOp):
            return await self._exec_mkdir(op, context)
        msg = f"Unknown operation type: {type(op).__name__}"
        raise ValueError(msg)

    async def _exec_read(self, op: ReadOp, context: "OperationContext") -> OperationResult:
        raw_content: Any = await asyncio.to_thread(self._fs.read, op.path, context=context)
        text: str
        if isinstance(raw_content, bytes):
            text = raw_content.decode("utf-8", errors="replace")
        elif isinstance(raw_content, dict):
            # read with return_metadata=True returns a dict
            inner = raw_content.get("content", b"")
            text = (
                inner.decode("utf-8", errors="replace") if isinstance(inner, bytes) else str(inner)
            )
        else:
            text = str(raw_content)
        return OperationResult(index=0, status=200, data={"content": text}, error=None)

    async def _exec_write(self, op: WriteOp, context: "OperationContext") -> OperationResult:
        if op.encoding == "base64":
            content: bytes | str = base64.b64decode(op.content)
        else:
            content = op.content.encode("utf-8")
        result = await asyncio.to_thread(
            self._fs.write,
            path=op.path,
            content=content,
            context=context,
        )
        return OperationResult(index=0, status=201, data=result, error=None)

    async def _exec_delete(self, op: DeleteOp, context: "OperationContext") -> OperationResult:
        result = await asyncio.to_thread(self._fs.delete, op.path, context=context)
        return OperationResult(index=0, status=200, data=result, error=None)

    async def _exec_stat(self, op: StatOp, context: "OperationContext") -> OperationResult:
        meta = await asyncio.to_thread(self._fs.get_metadata, op.path, context=context)
        if meta is None:
            raise NexusFileNotFoundError(path=op.path)
        return OperationResult(
            index=0,
            status=200,
            data={
                "path": meta.path,
                "size": meta.size,
                "content_id": meta.content_id,
                "version": meta.version,
                "is_directory": meta.is_dir,
                "created_at": (meta.created_at.isoformat() if meta.created_at else None),
                "modified_at": (meta.modified_at.isoformat() if meta.modified_at else None),
            },
            error=None,
        )

    async def _exec_exists(self, op: ExistsOp, context: "OperationContext") -> OperationResult:
        exists = await asyncio.to_thread(self._fs.exists, op.path, context=context)
        return OperationResult(index=0, status=200, data={"exists": exists}, error=None)

    async def _exec_list(self, op: ListOp, context: "OperationContext") -> OperationResult:
        full_paths = await asyncio.to_thread(
            self._fs.list, op.path, recursive=False, context=context
        )
        # NexusFS.list() returns full paths; strip prefix to get names
        prefix = op.path.rstrip("/") + "/"
        items = [
            fp[len(prefix) :] if fp.startswith(prefix) else fp.rsplit("/", 1)[-1]
            for fp in full_paths
        ]
        return OperationResult(index=0, status=200, data={"items": items}, error=None)

    async def _exec_mkdir(self, op: MkdirOp, context: "OperationContext") -> OperationResult:
        await asyncio.to_thread(self._fs.mkdir, op.path, parents=op.parents, context=context)
        return OperationResult(
            index=0, status=201, data={"created": True, "path": op.path}, error=None
        )

    @staticmethod
    def _skip_remaining(start: int, end: int) -> list[OperationResult]:
        """Generate 424 (Failed Dependency) results for skipped operations."""
        return [
            OperationResult(
                index=j,
                status=424,
                data=None,
                error="Skipped due to prior failure (stop_on_error=true)",
            )
            for j in range(start, end)
        ]
