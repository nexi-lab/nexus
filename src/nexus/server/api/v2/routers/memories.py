"""Memory REST API endpoints.

Provides 14 endpoints for memory CRUD, search, and version operations:
- GET    /api/v2/memories/stats                 - Paging statistics (#1258)
- POST   /api/v2/memories                      - Store memory
- GET    /api/v2/memories/{id}                 - Get memory by ID
- PUT    /api/v2/memories/{id}                 - Update memory
- DELETE /api/v2/memories/{id}                 - Delete memory
- POST   /api/v2/memories/{id}/invalidate      - Invalidate memory (#1183)
- POST   /api/v2/memories/{id}/revalidate      - Revalidate memory (#1183)
- POST   /api/v2/memories/search               - Semantic search
- POST   /api/v2/memories/query                - Point-in-time query (#1185)
- POST   /api/v2/memories/batch                - Batch store
- GET    /api/v2/memories/{id}/history         - Version history (#1184)
- GET    /api/v2/memories/{id}/versions/{ver}  - Get specific version (#1184)
- POST   /api/v2/memories/{id}/rollback        - Rollback to version (#1184)
- GET    /api/v2/memories/{id}/diff            - Diff between versions (#1184)
- GET    /api/v2/memories/{id}/lineage         - Append-only lineage chain (#1188)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from nexus.server.api.v2.dependencies import (
    _get_operation_context,
    _get_require_auth,
    get_memory_api,
)
from nexus.server.api.v2.models import (
    MemoryBatchStoreRequest,
    MemoryBatchStoreResponse,
    MemoryGetResponse,
    MemoryQueryRequest,
    MemorySearchRequest,
    MemoryStoreRequest,
    MemoryStoreResponse,
    MemoryUpdateRequest,
    MemoryVersionHistoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/memories", tags=["memories"])


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/stats")
async def get_memory_paging_stats(
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Get memory paging statistics (Issue #1258).

    Returns distribution of memories across tiers if paging is enabled.
    """
    try:
        if hasattr(memory_api, "get_paging_stats"):
            stats: dict[str, Any] = memory_api.get_paging_stats()
            return stats
        return {
            "paging_enabled": False,
            "message": "Memory paging not enabled on this server",
        }
    except Exception as e:
        logger.error(f"Memory paging stats error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve paging statistics") from e


@router.post("", response_model=MemoryStoreResponse, status_code=status.HTTP_201_CREATED)
async def store_memory(
    request: MemoryStoreRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> MemoryStoreResponse:
    """Store a new memory with full options."""
    try:
        context = _get_operation_context(auth_result)

        content = request.content
        if isinstance(content, dict):
            import json

            content = json.dumps(content)

        memory_id = memory_api.store(
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
            valid_at=request.valid_at,
            classify_stability=request.classify_stability,
            _metadata=request.metadata,
            context=context,
        )

        return MemoryStoreResponse(memory_id=memory_id, status="created")

    except Exception as e:
        logger.error(f"Memory store error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store memory") from e


@router.get("/{memory_id}", response_model=MemoryGetResponse)
async def get_memory(
    memory_id: str,
    include_history: bool = Query(False, description="Include version history"),
    track_access: bool = Query(True, description="Track access for importance decay"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Get a memory by ID with optional version history."""
    try:
        context = _get_operation_context(auth_result)
        result = memory_api.get(memory_id, track_access=track_access, context=context)

        if result is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        response = {"memory": result}

        if include_history:
            try:
                from nexus.storage.models import VersionHistoryModel

                session = memory_api.session
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
        raise HTTPException(status_code=500, detail="Failed to retrieve memory") from e


@router.put("/{memory_id}", response_model=MemoryStoreResponse)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> MemoryStoreResponse:
    """Update an existing memory."""
    try:
        context = _get_operation_context(auth_result)

        existing = memory_api.get(memory_id, track_access=False, context=context)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        content = request.content
        if content is not None and isinstance(content, dict):
            import json

            content = json.dumps(content)

        # #1188: Use ensure_upsert_key to handle path_key assignment
        upsert_path_key = memory_api.ensure_upsert_key(memory_id, existing)

        new_memory_id = memory_api.store(
            content=content if content is not None else existing.get("content"),
            scope=existing.get("scope", "user"),
            memory_type=existing.get("memory_type"),
            importance=request.importance
            if request.importance is not None
            else existing.get("importance"),
            namespace=request.namespace
            if request.namespace is not None
            else existing.get("namespace"),
            path_key=upsert_path_key,
            state=request.state if request.state is not None else existing.get("state", "active"),
            _metadata=request.metadata,
            context=context,
        )

        return MemoryStoreResponse(memory_id=new_memory_id, status="updated")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory update error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update memory") from e


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    soft: bool = Query(True, description="Soft delete (set state=deleted, preserves row)"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Delete a memory (#1188: non-destructive by default)."""
    try:
        context = _get_operation_context(auth_result)
        deleted = memory_api.delete(memory_id, context=context)

        if not deleted:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        return {"deleted": True, "memory_id": memory_id, "soft": soft}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory delete error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete memory") from e


@router.post("/{memory_id}/invalidate")
async def invalidate_memory(
    memory_id: str,
    invalid_at: str | None = Query(
        None, description="When fact became invalid (ISO-8601, default: now)"
    ),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Invalidate a memory (mark as no longer valid) (#1183)."""
    try:
        result = memory_api.invalidate(memory_id, invalid_at=invalid_at)
        if not result:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        return {"invalidated": True, "memory_id": memory_id, "invalid_at": invalid_at or "now"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory invalidate error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to invalidate memory") from e


@router.post("/{memory_id}/revalidate")
async def revalidate_memory(
    memory_id: str,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Revalidate a memory (clear invalid_at timestamp) (#1183)."""
    try:
        result = memory_api.revalidate(memory_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        return {"revalidated": True, "memory_id": memory_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory revalidate error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to revalidate memory") from e


@router.post("/search", response_model=dict[str, Any])
async def search_memories(
    request: MemorySearchRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Search memories with semantic/keyword/hybrid search."""
    try:
        results = memory_api.search(
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
        raise HTTPException(status_code=500, detail="Failed to search memories") from e


@router.post("/query", response_model=dict[str, Any])
async def query_memories(
    request: MemoryQueryRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Query memories with point-in-time temporal filters (#1185)."""
    try:
        context = _get_operation_context(auth_result)

        results = memory_api.query(
            scope=request.scope,
            memory_type=request.memory_type,
            namespace=request.namespace,
            namespace_prefix=request.namespace_prefix,
            state=request.state,
            after=request.after,
            before=request.before,
            during=request.during,
            entity_type=request.entity_type,
            person=request.person,
            event_after=request.event_after,
            event_before=request.event_before,
            include_invalid=request.include_invalid,
            include_superseded=request.include_superseded,
            temporal_stability=request.temporal_stability,
            as_of_event=request.as_of_event,
            as_of_system=request.as_of_system,
            limit=request.limit,
            offset=request.offset,
            context=context,
        )

        return {
            "results": results,
            "total": len(results),
            "filters": {
                "scope": request.scope,
                "memory_type": request.memory_type,
                "namespace": request.namespace,
                "namespace_prefix": request.namespace_prefix,
                "state": request.state,
                "as_of_event": request.as_of_event,
                "as_of_system": request.as_of_system,
                "include_invalid": request.include_invalid,
                "include_superseded": request.include_superseded,
            },
        }

    except Exception as e:
        logger.error(f"Memory query error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to query memories") from e


@router.post("/batch", response_model=MemoryBatchStoreResponse, status_code=status.HTTP_201_CREATED)
async def batch_store_memories(
    request: MemoryBatchStoreRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> MemoryBatchStoreResponse:
    """Batch store multiple memories.

    Note: memories are stored sequentially because the underlying
    SQLAlchemy session is not thread-safe for concurrent writes.
    """
    try:
        context = _get_operation_context(auth_result)
        memory_ids: list[str] = []
        errors: list[dict[str, Any]] = []

        # Sequential iteration — SQLAlchemy session is not thread-safe
        for i, mem_request in enumerate(request.memories):
            try:
                content = mem_request.content
                if isinstance(content, dict):
                    import json

                    content = json.dumps(content)

                memory_id = memory_api.store(
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
                    classify_stability=mem_request.classify_stability,
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
        raise HTTPException(status_code=500, detail="Failed to batch store memories") from e


@router.get("/{memory_id}/history", response_model=MemoryVersionHistoryResponse)
async def get_memory_history(
    memory_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> MemoryVersionHistoryResponse:
    """Get version history for a memory (#1184)."""
    try:
        context = _get_operation_context(auth_result)

        # #1188: Resolve to current version (follows superseded chain)
        current_model = memory_api.resolve_to_current(memory_id)
        if current_model is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        resolved_id = current_model.memory_id

        memory = memory_api.get(resolved_id, track_access=False, context=context)
        if memory is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        versions = memory_api.list_versions(memory_id)

        return MemoryVersionHistoryResponse(
            memory_id=memory_id,
            current_version=current_model.current_version,
            versions=versions,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory history error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve memory history") from e


@router.get("/{memory_id}/versions/{version}", response_model=dict[str, Any])
async def get_memory_version(
    memory_id: str,
    version: int,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Get a specific version of a memory (#1184)."""
    try:
        context = _get_operation_context(auth_result)
        result = memory_api.get_version(memory_id, version, context=context)

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
        raise HTTPException(status_code=500, detail="Failed to retrieve memory version") from e


@router.post("/{memory_id}/rollback", response_model=dict[str, Any])
async def rollback_memory(
    memory_id: str,
    version: int = Query(..., description="Version number to rollback to"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Rollback a memory to a previous version (#1184)."""
    try:
        context = _get_operation_context(auth_result)

        # #1188: Resolve to current version for rollback
        current_model = memory_api.resolve_to_current(memory_id)
        resolved_id = current_model.memory_id if current_model else memory_id

        memory_api.rollback(resolved_id, version, context=context)

        memory = memory_api.get(resolved_id, track_access=False, context=context)
        memory_model = memory_api.memory_router.get_memory_by_id(resolved_id)

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
        raise HTTPException(status_code=500, detail="Failed to rollback memory") from e


@router.get("/{memory_id}/diff", response_model=dict[str, Any])
async def diff_memory_versions(
    memory_id: str,
    v1: int = Query(..., description="First version number"),
    v2: int = Query(..., description="Second version number"),
    mode: str = Query("metadata", description="Diff mode: 'metadata' or 'content'"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Compare two versions of a memory (#1184)."""
    try:
        context = _get_operation_context(auth_result)
        diff_mode = "metadata" if mode == "metadata" else "content"
        result = memory_api.diff_versions(memory_id, v1, v2, mode=diff_mode, context=context)

        if isinstance(result, str):
            return {"diff": result, "mode": "content", "v1": v1, "v2": v2}
        return dict(result)

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory diff error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to diff memory versions") from e


@router.get("/{memory_id}/lineage", response_model=dict[str, Any])
async def get_memory_lineage(
    memory_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> dict[str, Any]:
    """Get the append-only lineage chain for a memory (#1188)."""
    try:
        context = _get_operation_context(auth_result)

        current_model = memory_api.resolve_to_current(memory_id)
        if current_model is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        memory = memory_api.get(current_model.memory_id, track_access=False, context=context)
        if memory is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")

        lineage = memory_api.get_history(memory_id)

        return {
            "memory_id": memory_id,
            "current_memory_id": current_model.memory_id,
            "chain_length": len(lineage),
            "lineage": lineage,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory lineage error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve memory lineage") from e
