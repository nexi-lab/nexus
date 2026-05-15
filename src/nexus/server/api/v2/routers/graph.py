"""Graph API v2 router (#2056).

Provides knowledge graph query endpoints:
- GET  /api/v2/graph/entity/{entity_id}            -- get entity by ID
- GET  /api/v2/graph/entity/{entity_id}/neighbors   -- get N-hop neighbors
- POST /api/v2/graph/subgraph                       -- extract subgraph for GraphRAG
- GET  /api/v2/graph/search                         -- search entities by name

Ported from v1 with improvements:
- Pydantic request model for subgraph endpoint
- Generic error messages (don't leak internal exceptions)
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/graph", tags=["graph"])

# =============================================================================
# Request Models
# =============================================================================


class SubgraphRequest(BaseModel):
    """Request model for extracting a subgraph."""

    entity_ids: list[str] = Field(default_factory=list, description="Entity IDs to include")
    max_hops: int = Field(default=2, ge=1, le=5, description="Maximum hop distance")


# =============================================================================
# Dependencies
# =============================================================================


def _get_nexus_fs(request: Request) -> Any:
    """Get NexusFS instance from app.state."""
    fs = getattr(request.app.state, "nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return fs


def _get_record_store(request: Request) -> Any:
    """Get RecordStore from app.state."""
    store = getattr(request.app.state, "record_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Record store not available")
    return store


def _get_async_read_session_factory(request: Request) -> Any:
    """Get async read session factory for graph queries."""
    factory = getattr(request.app.state, "async_read_session_factory", None)
    if factory is not None:
        return factory
    factory = getattr(request.app.state, "async_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="Async session factory not available",
        )
    return factory


# =============================================================================
# Helpers
# =============================================================================


@asynccontextmanager
async def _graph_session(
    record_store: Any, async_session_factory: Any, zone_id: str
) -> AsyncIterator[Any]:
    """Create a GraphStore from the RecordStoreABC async session factory."""
    # Removed: txtai handles this (Issue #2663)
    # graph_store module was deleted. Raise 503 until txtai graph is wired.
    try:
        from nexus.bricks.search.graph_store import GraphStore
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="Graph store not available (migrating to txtai, Issue #2663)",
        ) from exc

    async with async_session_factory() as session:
        yield GraphStore(record_store, session, zone_id=zone_id)


def _zone_id_from(nexus_fs: Any) -> str:
    """Extract zone_id from a NexusFS instance, defaulting to "root"."""
    return getattr(nexus_fs, "zone_id", None) or ROOT_ZONE_ID


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/entity/{entity_id}")
async def get_graph_entity(
    entity_id: str,
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(_get_nexus_fs),
    record_store: Any = Depends(_get_record_store),
    async_sf: Any = Depends(_get_async_read_session_factory),
) -> dict[str, Any]:
    """Get an entity by ID from the knowledge graph."""
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(record_store, async_sf, zone_id) as graph_store:
            entity = await graph_store.get_entity(entity_id)
            return {"entity": entity.to_dict() if entity else None}
    except Exception as e:
        logger.error("Graph entity error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve graph entity") from e


@router.get("/entity/{entity_id}/neighbors")
async def get_graph_neighbors(
    entity_id: str,
    hops: int = Query(1, ge=1, le=5, description="Number of hops (1-5)"),
    direction: str = Query("both", description="Direction: outgoing, incoming, both"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(_get_nexus_fs),
    record_store: Any = Depends(_get_record_store),
    async_sf: Any = Depends(_get_async_read_session_factory),
) -> dict[str, Any]:
    """Get N-hop neighbors of an entity."""
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(record_store, async_sf, zone_id) as graph_store:
            neighbors = await graph_store.get_neighbors(entity_id, hops=hops, direction=direction)
            return {
                "neighbors": [
                    {"entity": n.entity.to_dict(), "depth": n.depth, "path": n.path}
                    for n in neighbors
                ]
            }
    except Exception as e:
        logger.error("Graph neighbors error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve graph neighbors") from e


@router.post("/subgraph")
async def get_graph_subgraph(
    body: SubgraphRequest,
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(_get_nexus_fs),
    record_store: Any = Depends(_get_record_store),
    async_sf: Any = Depends(_get_async_read_session_factory),
) -> dict[str, Any]:
    """Extract a subgraph for GraphRAG context building."""
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(record_store, async_sf, zone_id) as graph_store:
            subgraph = await graph_store.get_subgraph(body.entity_ids, max_hops=body.max_hops)
            result: dict[str, Any] = subgraph.to_dict()
            return result
    except Exception as e:
        logger.error("Graph subgraph error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to extract subgraph") from e


@router.get("/search")
async def search_graph_entities(
    name: str = Query(..., description="Entity name to search for"),
    entity_type: str | None = Query(None, description="Filter by entity type"),
    fuzzy: bool = Query(False, description="Search in aliases as well"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(_get_nexus_fs),
    record_store: Any = Depends(_get_record_store),
    async_sf: Any = Depends(_get_async_read_session_factory),
) -> dict[str, Any]:
    """Search for entities by name."""
    try:
        zone_id = _zone_id_from(nexus_fs)
        async with _graph_session(record_store, async_sf, zone_id) as graph_store:
            entity = await graph_store.find_entity(name=name, entity_type=entity_type, fuzzy=fuzzy)
            return {"entity": entity.to_dict() if entity else None}
    except Exception as e:
        logger.error("Graph search error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to search graph entities") from e
