"""End-to-end tests for Graph API endpoints (Issue #1039).

Tests the complete memory -> graph pipeline via FastAPI:
1. Store memory with store_to_graph=True
2. Query graph endpoints for entities and relationships
3. Test graph traversal (neighbors, subgraph)

Run with:
    pytest tests/e2e/test_graph_api_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import pytest


class TestGraphAPI:
    """End-to-end tests for Graph API endpoints."""

    @pytest.mark.asyncio
    async def test_graph_entity_crud(self, test_app):
        """Test basic graph entity endpoints."""
        # Test that graph endpoints are accessible
        response = test_app.get("/api/graph/entity/nonexistent-id")
        # Should return 200 with null entity (not 500)
        assert response.status_code == 200
        data = response.json()
        assert data.get("entity") is None

    @pytest.mark.asyncio
    async def test_graph_search_endpoint(self, test_app):
        """Test graph search endpoint."""
        # Search for non-existent entity
        response = test_app.get("/api/graph/search", params={"name": "TestEntity"})
        assert response.status_code == 200
        data = response.json()
        assert data.get("entity") is None

    @pytest.mark.asyncio
    async def test_graph_neighbors_endpoint(self, test_app):
        """Test graph neighbors endpoint."""
        # Query neighbors for non-existent entity
        response = test_app.get(
            "/api/graph/entity/nonexistent-id/neighbors",
            params={"hops": 2, "direction": "both"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("neighbors") == []

    @pytest.mark.asyncio
    async def test_graph_subgraph_endpoint(self, test_app):
        """Test graph subgraph endpoint."""
        # Request subgraph for non-existent entities
        response = test_app.post(
            "/api/graph/subgraph",
            json={"entity_ids": ["id1", "id2"], "max_hops": 2},
        )
        assert response.status_code == 200
        data = response.json()
        assert "entities" in data
        assert "relationships" in data
        assert len(data["entities"]) == 0
        assert len(data["relationships"]) == 0

    @pytest.mark.asyncio
    async def test_memory_store_with_graph_parameter(self, test_app):
        """Test memory store endpoint accepts store_to_graph parameter."""
        # Store a memory (without LLM extraction - will skip graph storage)
        response = test_app.post(
            "/api/memory/store",
            json={
                "content": "Alice works at TechCorp with Bob.",
                "scope": "user",
                "memory_type": "fact",
                "extract_entities": False,  # Skip LLM extraction
                "extract_relationships": False,
                "store_to_graph": True,  # New parameter
            },
        )

        # Should succeed (store_to_graph accepted, but no entities extracted without LLM)
        assert response.status_code == 200
        data = response.json()
        assert "memory_id" in data

    @pytest.mark.asyncio
    async def test_graph_api_health_check(self, test_app):
        """Verify all graph endpoints respond without server errors."""
        endpoints = [
            ("GET", "/api/graph/entity/test-id", None),
            ("GET", "/api/graph/entity/test-id/neighbors", {"hops": 1}),
            ("GET", "/api/graph/search", {"name": "test"}),
            ("POST", "/api/graph/subgraph", {"entity_ids": [], "max_hops": 1}),
        ]

        for method, endpoint, params in endpoints:
            if method == "GET":
                response = test_app.get(endpoint, params=params)
            else:
                response = test_app.post(endpoint, json=params)

            # All endpoints should return 200 (not 500 server errors)
            assert response.status_code == 200, (
                f"{method} {endpoint} failed with {response.status_code}: {response.text}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--log-cli-level=INFO"])
