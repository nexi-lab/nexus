"""Batch VFS operations endpoint (Issue #1242).

Provides ``POST /api/v2/batch`` for io_uring-style batch submission
of VFS operations, reducing HTTP round-trips for chatty agent workloads.

Design decisions:
    - Presentation layer only (not a brick) per §5.5
    - Factory pattern for DI (same as async_files.py)
    - Direct VFS dispatch via NexusFS
    - Batch-specific rate limit (separate from per-endpoint limits)

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §7.2 (io_uring → Batch API)
    - Issue #1242: General /batch HTTP endpoint for io_uring-style submission
"""

import logging
from collections.abc import Callable
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException

from nexus.contracts.types import OperationContext
from nexus.server.batch_executor import BatchExecutor, BatchRequest, BatchResponse

logger = logging.getLogger(__name__)


def create_batch_router(
    nexus_fs: Any | None = None,
    get_fs: Any | None = None,
    get_context_override: Callable[..., Any] | None = None,
) -> APIRouter:
    """Create a batch operations router.

    Supports two modes (same pattern as async_files.py):
    1. Direct: Pass nexus_fs instance (for testing)
    2. Lazy: Pass get_fs callable that returns the instance at request time

    Args:
        nexus_fs: Initialized NexusFS instance (direct mode).
        get_fs: Callable returning NexusFS (lazy mode).
        get_context_override: Optional context provider for testing.
            When None, imports auth from fastapi_server.

    Returns:
        FastAPI router with the ``POST /batch`` endpoint.
    """
    router = APIRouter(tags=["batch"])

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

    # Build context dependency: use override if provided, else import from server.
    if get_context_override is not None:
        _context_dep = get_context_override
    else:
        # Lazy import to avoid pulling in full server dependency chain in tests.
        from nexus.server.dependencies import (
            get_auth_result as _real_get_auth_result,
        )
        from nexus.server.dependencies import (
            get_operation_context as _real_get_operation_context,
        )

        async def _context_dep(
            auth_result: dict[str, Any] | None = Depends(_real_get_auth_result),
        ) -> OperationContext:
            """Get operation context from auth result."""
            if auth_result is None or not auth_result.get("authenticated"):
                raise HTTPException(status_code=401, detail="Authentication required")
            return cast("OperationContext", _real_get_operation_context(auth_result))

    @router.post("/batch", response_model=BatchResponse)
    async def batch_operations(
        request: BatchRequest,
        context: Any = Depends(_context_dep),
    ) -> BatchResponse:
        """Execute multiple VFS operations in a single request.

        Follows the io_uring scatter/gather pattern: submit multiple operations,
        receive individual results. Operations execute **sequentially** so later
        operations can depend on earlier ones.

        Each operation produces its own status code. The batch response is
        always HTTP 200; check ``results[i].status`` for per-operation outcomes.

        Set ``stop_on_error: true`` to halt on the first failure (remaining
        operations receive status 424 — Failed Dependency).

        Limits:
        - 1–50 operations per batch
        - Max 10 MB total write payload
        - 30-second timeout per operation
        """
        fs = await _get_fs()
        executor = BatchExecutor(fs=fs)

        logger.info(
            "Batch request: %d operations, stop_on_error=%s",
            len(request.operations),
            request.stop_on_error,
        )

        response = await executor.execute(request, context=context)

        succeeded = sum(1 for r in response.results if r.status < 400)
        failed = len(response.results) - succeeded
        logger.info("Batch complete: %d succeeded, %d failed", succeeded, failed)

        return response

    return router
