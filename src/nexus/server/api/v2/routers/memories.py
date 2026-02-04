"""Memory REST API endpoints.

Provides 7 endpoints for memory CRUD and search operations:
- POST   /api/v2/memories              - Store memory
- GET    /api/v2/memories/{id}         - Get memory by ID
- PUT    /api/v2/memories/{id}         - Update memory
- DELETE /api/v2/memories/{id}         - Delete memory
- POST   /api/v2/memories/search       - Semantic search
- POST   /api/v2/memories/batch        - Batch store
- GET    /api/v2/memories/{id}/history - Version history
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
    """Get version history for a memory.

    Returns all versions of a memory with their content hashes
    and timestamps.
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

        # Query version history
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

        version_list = [
            {
                "version": v.version_number,
                "content_hash": v.content_hash,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "metadata": getattr(v, "metadata", None),
            }
            for v in versions
        ]

        # Get current version from memory or default to 1
        current_version = len(version_list) if version_list else 1

        return MemoryVersionHistoryResponse(
            memory_id=memory_id,
            current_version=current_version,
            versions=version_list,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory history error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory history error: {e}") from e
