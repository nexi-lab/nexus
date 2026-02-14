"""Graph API router (Issue #1288).

Provides knowledge graph query endpoints:
- GET  /api/graph/entity/{entity_id}            -- get entity by ID
- GET  /api/graph/entity/{entity_id}/neighbors   -- get N-hop neighbors
- POST /api/graph/subgraph                       -- extract subgraph for GraphRAG
- GET  /api/graph/search                         -- search entities by name

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.server.api.v1.dependencies import get_async_session_factory, get_nexus_fs
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["graph"])


# ---------------------------------------------------------------------------
# Shared helpers (DRY: replaces 4x copy-pasted boilerplate)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _graph_session(async_session_factory: Any, zone_id: str) -> AsyncIterator[Any]:
    """Create a GraphStore from the RecordStoreABC async session factory.

    Uses the shared async_session_factory from RecordStoreABC rather than
    constructing independent engines from raw URLs.

    Yields:
        A ``GraphStore`` instance ready for queries.
    """
    from nexus.search.graph_store import GraphStore

    async with async_session_factory() as session:
        yield GraphStore(session, zone_id=zone_id)


def _zone_id_from(nexus_fs: Any) -> str:
    """Extract zone_id from a NexusFS instance, defaulting to ``"default"``."""
    return getattr(nexus_fs, "zone_id", None) or "default"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/graph/entity/{entity_id}")
async def get_graph_entity(
    entity_id: str,
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
    async_sf: Any = Depends(get_async_session_factory),
) -> dict[str, Any]:
    """Get an entity by ID from the knowledge graph.

    Args:
        entity_id: The entity UUID

    Returns:
        Entity details or null if not found
    """
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(async_sf, zone_id) as graph_store:
            entity = await graph_store.get_entity(entity_id)
            return {"entity": entity.to_dict() if entity else None}
    except Exception as e:
        logger.error(f"Graph entity error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Graph entity error: {e}") from e


@router.get("/api/graph/entity/{entity_id}/neighbors")
async def get_graph_neighbors(
    entity_id: str,
    hops: int = Query(1, ge=1, le=5, description="Number of hops (1-5)"),
    direction: str = Query("both", description="Direction: outgoing, incoming, both"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
    async_sf: Any = Depends(get_async_session_factory),
) -> dict[str, Any]:
    """Get N-hop neighbors of an entity.

    Args:
        entity_id: Starting entity UUID
        hops: Number of hops (1-5)
        direction: Relationship direction to follow

    Returns:
        List of neighbor entities with depth and path info
    """
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(async_sf, zone_id) as graph_store:
            neighbors = await graph_store.get_neighbors(entity_id, hops=hops, direction=direction)
            return {
                "neighbors": [
                    {
                        "entity": n.entity.to_dict(),
                        "depth": n.depth,
                        "path": n.path,
                    }
                    for n in neighbors
                ]
            }
    except Exception as e:
        logger.error(f"Graph neighbors error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Graph neighbors error: {e}") from e


@router.post("/api/graph/subgraph")
async def get_graph_subgraph(
    request: Request,
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
    async_sf: Any = Depends(get_async_session_factory),
) -> dict[str, Any]:
    """Extract a subgraph for GraphRAG context building.

    Request body:
    {
        "entity_ids": ["entity-id-1", "entity-id-2"],
        "max_hops": 2
    }

    Returns:
        Subgraph with entities and relationships
    """
    try:
        body = await request.json()
        entity_ids = body.get("entity_ids", [])
        max_hops = body.get("max_hops", 2)

        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(async_sf, zone_id) as graph_store:
            subgraph = await graph_store.get_subgraph(entity_ids, max_hops=max_hops)
            result: dict[str, Any] = subgraph.to_dict()
            return result
    except Exception as e:
        logger.error(f"Graph subgraph error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Graph subgraph error: {e}") from e


@router.get("/api/graph/search")
async def search_graph_entities(
    name: str = Query(..., description="Entity name to search for"),
    entity_type: str | None = Query(None, description="Filter by entity type"),
    fuzzy: bool = Query(False, description="Search in aliases as well"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
    async_sf: Any = Depends(get_async_session_factory),
) -> dict[str, Any]:
    """Search for entities by name.

    Args:
        name: Entity name to search for
        entity_type: Optional entity type filter (PERSON, ORG, CONCEPT, etc.)
        fuzzy: If true, search aliases as well

    Returns:
        Matching entity or null
    """
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(async_sf, zone_id) as graph_store:
            entity = await graph_store.find_entity(name=name, entity_type=entity_type, fuzzy=fuzzy)
            return {"entity": entity.to_dict() if entity else None}
    except Exception as e:
        logger.error(f"Graph search error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Graph search error: {e}") from e
