"""Knowledge graph HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class GraphClient(BaseServiceClient):
    """Client for knowledge graph query endpoints."""

    def entity(self, entity_id: str) -> dict[str, Any]:
        """Get entity by ID."""
        return self._request("GET", f"/api/v2/graph/entity/{entity_id}")

    def neighbors(self, entity_id: str, *, hops: int = 1) -> dict[str, Any]:
        """Get neighboring entities."""
        return self._request(
            "GET",
            f"/api/v2/graph/entity/{entity_id}/neighbors",
            params={"hops": hops},
        )

    def subgraph(self, entity_id: str, *, depth: int = 2) -> dict[str, Any]:
        """Extract a subgraph rooted at entity."""
        return self._request(
            "POST",
            "/api/v2/graph/subgraph",
            json_body={"entity_id": entity_id, "depth": depth},
        )

    def search(self, query: str) -> dict[str, Any]:
        """Search the knowledge graph."""
        return self._request("GET", "/api/v2/graph/search", params={"query": query})
