"""Memory API router (Issue #1288).

Provides memory management endpoints:
- GET    /api/memory/query       - query memories with temporal/entity filters
- GET    /api/memory/list        - list memories with namespace filtering
- POST   /api/memory/store       - store a new memory
- GET    /api/memory/{memory_id} - get a specific memory by ID

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.server.api.v1.dependencies import get_nexus_fs
from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory"])


@router.get("/api/memory/query")
async def memory_query(
    scope: str | None = Query(None, description="Filter by scope (agent/user/zone/global)"),
    memory_type: str | None = Query(None, description="Filter by memory type"),
    state: str = Query("active", description="Filter by state (inactive/active/all)"),
    after: str | None = Query(
        None, description="Filter memories created after this time (ISO-8601). #1023"
    ),
    before: str | None = Query(
        None, description="Filter memories created before this time (ISO-8601). #1023"
    ),
    during: str | None = Query(
        None, description="Filter memories during this period (e.g., '2025', '2025-01'). #1023"
    ),
    entity_type: str | None = Query(
        None, description="Filter by entity type (PERSON, ORG, LOCATION, DATE, etc.). #1025"
    ),
    person: str | None = Query(None, description="Filter by person name reference. #1025"),
    event_after: str | None = Query(
        None, description="Filter by event date >= value (ISO-8601). #1028"
    ),
    event_before: str | None = Query(
        None, description="Filter by event date <= value (ISO-8601). #1028"
    ),
    limit: int = Query(100, description="Maximum number of results", ge=1, le=1000),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """Query memories with optional temporal and entity filters.

    Supports temporal operators (Issue #1023):
    - after: Return memories created after this datetime
    - before: Return memories created before this datetime
    - during: Return memories created during this period (partial date like "2025" or "2025-01")

    Supports entity filters (Issue #1025 - SimpleMem symbolic layer):
    - entity_type: Filter by extracted entity type (PERSON, ORG, LOCATION, DATE, etc.)
    - person: Filter by person name reference

    Supports event date filters (Issue #1028 - Temporal anchoring):
    - event_after: Filter by earliest_date >= value (date mentioned in content)
    - event_before: Filter by latest_date <= value (date mentioned in content)

    Note: 'during' cannot be used together with 'after' or 'before'.

    Args:
        scope: Filter by scope
        memory_type: Filter by memory type
        state: Filter by state (default: active)
        after: ISO-8601 datetime or date string
        before: ISO-8601 datetime or date string
        during: Partial date string (year, year-month, or full date)
        entity_type: Entity type to filter by (e.g., PERSON, ORG)
        person: Person name to filter by
        event_after: ISO-8601 date to filter by earliest_date >= value. #1028
        event_before: ISO-8601 date to filter by latest_date <= value. #1028
        limit: Maximum number of results

    Returns:
        List of memories matching the filters
    """
    try:
        context = get_operation_context(_auth_result)

        results = nexus_fs.memory.query(
            scope=scope,
            memory_type=memory_type,
            state=state,
            after=after,
            before=before,
            during=during,
            entity_type=entity_type,
            person=person,
            event_after=event_after,
            event_before=event_before,
            limit=limit,
            context=context,
        )

        return {
            "memories": results,
            "total": len(results),
            "filters": {
                "scope": scope,
                "memory_type": memory_type,
                "state": state,
                "after": after,
                "before": before,
                "during": during,
                "entity_type": entity_type,
                "person": person,
                "event_after": event_after,
                "event_before": event_before,
            },
        }

    except ValueError as e:
        # Handle temporal validation errors
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Memory query error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory query error: {e}") from e


@router.get("/api/memory/list")
async def memory_list(
    scope: str | None = Query(None, description="Filter by scope"),
    memory_type: str | None = Query(None, description="Filter by memory type"),
    namespace: str | None = Query(None, description="Filter by exact namespace"),
    namespace_prefix: str | None = Query(None, description="Filter by namespace prefix"),
    state: str = Query("active", description="Filter by state (inactive/active/all)"),
    after: str | None = Query(
        None, description="Filter memories created after this time (ISO-8601). #1023"
    ),
    before: str | None = Query(
        None, description="Filter memories created before this time (ISO-8601). #1023"
    ),
    during: str | None = Query(
        None, description="Filter memories during this period (e.g., '2025', '2025-01'). #1023"
    ),
    limit: int = Query(100, description="Maximum number of results", ge=1, le=1000),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """List memories with optional temporal filters (Issue #1023).

    Similar to query but also supports namespace filtering.

    Args:
        scope: Filter by scope
        memory_type: Filter by memory type
        namespace: Filter by exact namespace
        namespace_prefix: Filter by namespace prefix
        state: Filter by state
        after: ISO-8601 datetime or date string
        before: ISO-8601 datetime or date string
        during: Partial date string
        limit: Maximum number of results

    Returns:
        List of memories matching the filters
    """
    try:
        context = get_operation_context(_auth_result)

        results = nexus_fs.memory.list(
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
            after=after,
            before=before,
            during=during,
            limit=limit,
            context=context,
        )

        return {
            "memories": results,
            "total": len(results),
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Memory list error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory list error: {e}") from e


@router.post("/api/memory/store")
async def memory_store(
    request: Request,
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """Store a new memory.

    Request body:
    {
        "content": "Memory content",
        "scope": "user",
        "memory_type": "fact",
        "importance": 0.8,
        "namespace": "optional/namespace",
        "path_key": "optional_key",
        "state": "active",
        "resolve_coreferences": false,
        "coreference_context": "Prior conversation context",
        "resolve_temporal": false,
        "temporal_reference_time": "2025-01-10T12:00:00Z",
        "extract_temporal": true,
        "extract_relationships": false,
        "relationship_types": ["MANAGES", "WORKS_WITH", "DEPENDS_ON"]
    }

    Returns:
        The created memory ID
    """
    try:
        body = await request.json()
        context = get_operation_context(_auth_result)

        memory_id = nexus_fs.memory.store(
            content=body.get("content", ""),
            scope=body.get("scope", "user"),
            memory_type=body.get("memory_type"),
            importance=body.get("importance"),
            namespace=body.get("namespace"),
            path_key=body.get("path_key"),
            state=body.get("state", "active"),
            resolve_coreferences=body.get("resolve_coreferences", False),
            coreference_context=body.get("coreference_context"),
            resolve_temporal=body.get("resolve_temporal", False),
            temporal_reference_time=body.get("temporal_reference_time"),
            extract_temporal=body.get("extract_temporal", True),
            extract_relationships=body.get("extract_relationships", False),
            relationship_types=body.get("relationship_types"),
            store_to_graph=body.get("store_to_graph", False),  # #1039
            context=context,
        )

        return {"memory_id": memory_id, "status": "created"}

    except Exception as e:
        logger.error(f"Memory store error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory store error: {e}") from e


@router.get("/api/memory/{memory_id}")
async def memory_get(
    memory_id: str,
    track_access: bool = Query(True, description="Track this access for decay calculation"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """Get a specific memory by ID.

    Returns memory with effective importance calculated based on time decay (Issue #1030).

    Args:
        memory_id: The memory UUID
        track_access: Whether to track this access for decay calculation (default: True)

    Returns:
        Memory details including:
        - importance: Current stored importance
        - importance_original: Original importance (before any decay)
        - importance_effective: Calculated importance with time decay applied
        - access_count: Number of times this memory has been accessed
        - last_accessed_at: Last access timestamp
    """
    try:
        context = get_operation_context(_auth_result)
        result = nexus_fs.memory.get(memory_id, track_access=track_access, context=context)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
        return {"memory": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Memory get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory get error: {e}") from e
