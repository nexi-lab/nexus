"""Graph RPC Service — knowledge graph queries.

Issue #2056.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class GraphRPCService:
    """RPC surface for knowledge graph operations."""

    def __init__(self, record_store: Any) -> None:
        self._record_store = record_store

    def _get_graph_store(self) -> Any:
        """Lazily get or create GraphStore."""
        from nexus.bricks.search.graph_store import GraphStore

        return GraphStore(self._record_store)

    @rpc_expose(description="Get a graph entity by ID")
    async def graph_entity(self, entity_id: str) -> dict[str, Any]:
        store = self._get_graph_store()
        entity = await store.get_entity(entity_id)
        if entity is None:
            return {"error": f"Entity {entity_id} not found"}
        return {
            "entity_id": entity.entity_id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "properties": entity.properties,
        }

    @rpc_expose(description="Get neighbors of a graph entity")
    async def graph_neighbors(
        self,
        entity_id: str,
        hops: int = 1,
        direction: str = "both",
    ) -> dict[str, Any]:
        store = self._get_graph_store()
        neighbors = await store.get_neighbors(entity_id, hops=hops, direction=direction)
        return {
            "entity_id": entity_id,
            "neighbors": [
                {
                    "entity_id": n.entity_id,
                    "name": n.name,
                    "entity_type": n.entity_type,
                    "relation": getattr(n, "relation", None),
                }
                for n in neighbors
            ],
            "count": len(neighbors),
        }

    @rpc_expose(description="Get subgraph for multiple entities")
    async def graph_subgraph(
        self,
        entity_ids: list[str],
        max_hops: int = 1,
    ) -> dict[str, Any]:
        store = self._get_graph_store()
        subgraph = await store.get_subgraph(entity_ids, max_hops=max_hops)
        return {
            "nodes": [
                {"entity_id": n.entity_id, "name": n.name, "entity_type": n.entity_type}
                for n in subgraph.nodes
            ],
            "edges": [
                {"source": e.source, "target": e.target, "relation": e.relation}
                for e in subgraph.edges
            ],
        }

    @rpc_expose(description="Search graph entities by name")
    async def graph_search(
        self,
        name: str,
        entity_type: str | None = None,
        fuzzy: bool = False,
    ) -> dict[str, Any]:
        store = self._get_graph_store()
        results = await store.search_entities(name=name, entity_type=entity_type, fuzzy=fuzzy)
        return {
            "results": [
                {"entity_id": r.entity_id, "name": r.name, "entity_type": r.entity_type}
                for r in results
            ],
            "count": len(results),
        }
