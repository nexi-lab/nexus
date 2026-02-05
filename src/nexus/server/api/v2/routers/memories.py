"""Memory REST API endpoints.

Provides 12 endpoints for memory CRUD, search, and version operations:
- POST   /api/v2/memories                      - Store memory
- GET    /api/v2/memories/{id}                 - Get memory by ID
- PUT    /api/v2/memories/{id}                 - Update memory
- DELETE /api/v2/memories/{id}                 - Delete memory
- POST   /api/v2/memories/{id}/invalidate      - Invalidate memory (#1183)
- POST   /api/v2/memories/{id}/revalidate      - Revalidate memory (#1183)
- POST   /api/v2/memories/search               - Semantic search
- POST   /api/v2/memories/batch                - Batch store
- GET    /api/v2/memories/{id}/history         - Version history (#1184)
- GET    /api/v2/memories/{id}/versions/{ver}  - Get specific version (#1184)
- POST   /api/v2/memories/{id}/rollback        - Rollback to version (#1184)
- GET    /api/v2/memories/{id}/diff            - Diff between versions (#1184)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from nexus.server.api.v2.models import (
    MemoryBatchStoreRequest,
    MemoryBatchStoreResponse,
    MemorySearchRequest,
    MemoryStoreRequest,
    MemoryStoreResponse,
    MemoryUpdateRequest,
    MemoryVersionHistoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/memories", tags=["memories"])


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_app_state() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Get operation context from auth result."""
    from nexus.server.fastapi_server import get_operation_context

    return get_operation_context(auth_result)


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=MemoryStoreResponse, status_code=status.HTTP_201_CREATED)
async def store_memory(
    request: MemoryStoreRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> MemoryStoreResponse:
    """Store a new memory with full options.

    Creates a new memory entry with content, scope, type, importance,
    and optional entity/temporal extraction.

    Args:
        request: Memory storage request with content and metadata

    Returns:
        Memory ID and status

    Raises:
        503: NexusFS not initialized
        500: Storage error
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)

        # Convert content to string if dict
        content = request.content
        if isinstance(content, dict):
            import json

            content = json.dumps(content)

        memory_id = app_state.nexus_fs.memory.store(
            content=content,
            scope=request.scope,
            memory_type=request.memory_type,
            importance=request.importance,
            namespace=request.namespace,
            path_key=request.path_key,
            state=request.state,
            extract_entities=request.extract_entities,
            extract_temporal=request.extract_temporal,
            extract_relationships=request.extract_relationships,
            store_to_graph=request.store_to_graph,
            valid_at=request.valid_at,  # #1183: Bi-temporal validity
            _metadata=request.metadata,
            context=context,
        )

        return MemoryStoreResponse(memory_id=memory_id, status="created")

    except Exception as e:
        logger.error(f"Memory store error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory store error: {e}") from e


@router.get("/{memory_id}", response_model=dict[str, Any])
async def get_memory(
    memory_id: str,
    include_history: bool = Query(False, description="Include version history"),
    track_access: bool = Query(True, description="Track access for importance decay"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get a memory by ID with optional version history.

    Retrieves memory content, metadata, and optionally its version history.
    Access tracking updates importance decay calculations.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)
        result = app_state.nexus_fs.memory.get(
            memory_id, track_access=track_access, context=context
        )

        if result is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        response = {"memory": result}

        # Add version history if requested
        if include_history:
            try:
                from nexus.storage.models import VersionHistoryModel

                session = app_state.nexus_fs.memory.session
                versions = (
                    session.query(VersionHistoryModel)
                    .filter(
                        VersionHistoryModel.resource_id == memory_id,
                        VersionHistoryModel.resource_type == "memory",
                    )
                    .order_by(VersionHistoryModel.version_number.desc())
                    .all()
                )
                response["versions"] = [
                    {
                        "version": v.version_number,
                        "content_hash": v.content_hash,
                        "created_at": v.created_at.isoformat() if v.created_at else None,
                        "metadata": getattr(v, "metadata", None),
                    }
                    for v in versions
                ]
            except Exception as e:
                logger.warning(f"Failed to fetch version history: {e}")
                response["versions"] = []

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory get error: {e}") from e


@router.put("/{memory_id}", response_model=MemoryStoreResponse)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> MemoryStoreResponse:
    """Update an existing memory.

    Updates memory content and/or metadata. Creates a new version
    in the version history.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)

        # First verify the memory exists
        existing = app_state.nexus_fs.memory.get(memory_id, track_access=False, context=context)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        # Build update kwargs
        content = request.content
        if content is not None and isinstance(content, dict):
            import json

            content = json.dumps(content)

        # Use store with path_key for upsert behavior
        # This creates a new version of the memory
        new_memory_id = app_state.nexus_fs.memory.store(
            content=content if content is not None else existing.get("content"),
            scope=existing.get("scope", "user"),
            memory_type=existing.get("memory_type"),
            importance=request.importance
            if request.importance is not None
            else existing.get("importance"),
            namespace=request.namespace
            if request.namespace is not None
            else existing.get("namespace"),
            path_key=memory_id,  # Use memory_id as path_key for upsert
            state=request.state if request.state is not None else existing.get("state", "active"),
            _metadata=request.metadata,
            context=context,
        )

        return MemoryStoreResponse(memory_id=new_memory_id, status="updated")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory update error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory update error: {e}") from e


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    soft: bool = Query(True, description="Soft delete (set state=inactive)"),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Delete a memory.

    By default performs soft delete (state='inactive').
    Hard delete requires explicit soft=false.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        if soft:
            # Soft delete - deactivate
            deleted = app_state.nexus_fs.memory.deactivate(memory_id)
        else:
            # Hard delete
            deleted = app_state.nexus_fs.memory.delete(memory_id)

        if not deleted:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        return {"deleted": True, "memory_id": memory_id, "soft": soft}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory delete error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory delete error: {e}") from e


@router.post("/{memory_id}/invalidate")
async def invalidate_memory(
    memory_id: str,
    invalid_at: str | None = Query(
        None, description="When fact became invalid (ISO-8601, default: now)"
    ),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Invalidate a memory (mark as no longer valid) (#1183).

    This is a temporal soft-delete that marks when a fact became false.
    The memory remains queryable for historical analysis but is excluded
    from "current facts" queries (include_invalid=False).

    Unlike DELETE, invalidate() preserves the memory for audit trails
    and point-in-time queries.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        result = app_state.nexus_fs.memory.invalidate(memory_id, invalid_at=invalid_at)
        if not result:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        return {"invalidated": True, "memory_id": memory_id, "invalid_at": invalid_at or "now"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory invalidate error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory invalidate error: {e}") from e


@router.post("/{memory_id}/revalidate")
async def revalidate_memory(
    memory_id: str,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Revalidate a memory (clear invalid_at timestamp) (#1183).

    Use when a previously invalidated fact becomes true again.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        result = app_state.nexus_fs.memory.revalidate(memory_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        return {"revalidated": True, "memory_id": memory_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory revalidate error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory revalidate error: {e}") from e


@router.post("/search", response_model=dict[str, Any])
async def search_memories(
    request: MemorySearchRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Search memories with semantic/keyword/hybrid search.

    Performs semantic search using embeddings, keyword search,
    or hybrid search combining both approaches.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        results = app_state.nexus_fs.memory.search(
            query=request.query,
            scope=request.scope,
            memory_type=request.memory_type,
            limit=request.limit,
            search_mode=request.search_mode,
            after=request.after,
            before=request.before,
            during=request.during,
        )

        return {
            "results": results,
            "total": len(results),
            "query": request.query,
            "search_mode": request.search_mode,
        }

    except Exception as e:
        logger.error(f"Memory search error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory search error: {e}") from e


@router.post("/batch", response_model=MemoryBatchStoreResponse, status_code=status.HTTP_201_CREATED)
async def batch_store_memories(
    request: MemoryBatchStoreRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> MemoryBatchStoreResponse:
    """Batch store multiple memories.

    Stores multiple memories in a single request. Returns counts
    of successful and failed stores.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)
        memory_ids: list[str] = []
        errors: list[dict[str, Any]] = []

        for i, mem_request in enumerate(request.memories):
            try:
                content = mem_request.content
                if isinstance(content, dict):
                    import json

                    content = json.dumps(content)

                memory_id = app_state.nexus_fs.memory.store(
                    content=content,
                    scope=mem_request.scope,
                    memory_type=mem_request.memory_type,
                    importance=mem_request.importance,
                    namespace=mem_request.namespace,
                    path_key=mem_request.path_key,
                    state=mem_request.state,
                    extract_entities=mem_request.extract_entities,
                    extract_temporal=mem_request.extract_temporal,
                    extract_relationships=mem_request.extract_relationships,
                    store_to_graph=mem_request.store_to_graph,
                    _metadata=mem_request.metadata,
                    context=context,
                )
                memory_ids.append(memory_id)
            except Exception as e:
                errors.append({"index": i, "error": str(e)})

        return MemoryBatchStoreResponse(
            stored=len(memory_ids),
            failed=len(errors),
            memory_ids=memory_ids,
            errors=errors if errors else None,
        )

    except Exception as e:
        logger.error(f"Batch store error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Batch store error: {e}") from e


@router.get("/{memory_id}/history", response_model=MemoryVersionHistoryResponse)
async def get_memory_history(
    memory_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> MemoryVersionHistoryResponse:
    """Get version history for a memory (#1184).

    Returns all versions of a memory with their content hashes,
    timestamps, and change tracking metadata.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)

        # First verify memory exists
        memory = app_state.nexus_fs.memory.get(memory_id, track_access=False, context=context)
        if memory is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        # Use the new list_versions API (#1184)
        versions = app_state.nexus_fs.memory.list_versions(memory_id)

        # Get current version from memory model
        memory_model = app_state.nexus_fs.memory.memory_router.get_memory_by_id(memory_id)
        current_version = memory_model.current_version if memory_model else 1

        return MemoryVersionHistoryResponse(
            memory_id=memory_id,
            current_version=current_version,
            versions=versions,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory history error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory history error: {e}") from e


@router.get("/{memory_id}/versions/{version}", response_model=dict[str, Any])
async def get_memory_version(
    memory_id: str,
    version: int,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get a specific version of a memory (#1184).

    Retrieves the content and metadata for a specific historical version
    of a memory using CAS storage.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)
        result = app_state.nexus_fs.memory.get_version(memory_id, version, context=context)

        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version} not found for memory {memory_id}",
            )

        return dict(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory get version error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory get version error: {e}") from e


@router.post("/{memory_id}/rollback", response_model=dict[str, Any])
async def rollback_memory(
    memory_id: str,
    version: int = Query(..., description="Version number to rollback to"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Rollback a memory to a previous version (#1184).

    Restores the memory content to a specific historical version.
    Creates a new version entry with source_type='rollback' to maintain
    audit trail.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)
        app_state.nexus_fs.memory.rollback(memory_id, version, context=context)

        # Get the updated memory to return current state
        memory = app_state.nexus_fs.memory.get(memory_id, track_access=False, context=context)
        memory_model = app_state.nexus_fs.memory.memory_router.get_memory_by_id(memory_id)

        return {
            "rolled_back": True,
            "memory_id": memory_id,
            "rolled_back_to_version": version,
            "current_version": memory_model.current_version if memory_model else None,
            "content": memory.get("content") if memory else None,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory rollback error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory rollback error: {e}") from e


@router.get("/{memory_id}/diff", response_model=dict[str, Any])
async def diff_memory_versions(
    memory_id: str,
    v1: int = Query(..., description="First version number"),
    v2: int = Query(..., description="Second version number"),
    mode: str = Query("metadata", description="Diff mode: 'metadata' or 'content'"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Compare two versions of a memory (#1184).

    Args:
        v1: First version number
        v2: Second version number
        mode: Diff mode - "metadata" returns size/hash comparison,
              "content" returns unified diff format

    Returns:
        For mode="metadata": Dict with version comparison info
        For mode="content": Dict with unified diff string
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        context = _get_operation_context(auth_result)
        diff_mode = "metadata" if mode == "metadata" else "content"
        result = app_state.nexus_fs.memory.diff_versions(
            memory_id, v1, v2, mode=diff_mode, context=context
        )

        if isinstance(result, str):
            # Content diff mode returns string
            return {"diff": result, "mode": "content", "v1": v1, "v2": v2}
        else:
            # Metadata diff mode returns dict
            return dict(result)

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory diff error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory diff error: {e}") from e
