"""E2E tests for Point-in-Time Query API (Issue #1185).

Tests the full integration of as_of_system and as_of_event parameters
through the REST API using a real nexus server process.

Uses the shared test_app fixture from conftest.py which starts an actual
nexus server process for true e2e testing.
"""

import time
from datetime import UTC, datetime

import httpx
import pytest

UTC = UTC

# Run these tests in the same xdist worker to avoid server conflicts
pytestmark = [pytest.mark.xdist_group("memory_pit"), pytest.mark.quarantine]


class TestPointInTimeQueryE2E:
    """E2E tests for POST /api/v2/memories/query endpoint (#1185)."""

    def _store_memory(
        self,
        client: httpx.Client,
        content: str,
        scope: str = "user",
        memory_type: str | None = None,
        namespace: str | None = None,
        path_key: str | None = None,
        valid_at: str | None = None,
    ) -> str:
        """Helper to store a memory and return its ID."""
        payload = {"content": content, "scope": scope}
        if memory_type:
            payload["memory_type"] = memory_type
        if namespace:
            payload["namespace"] = namespace
        if path_key:
            payload["path_key"] = path_key
        if valid_at:
            payload["valid_at"] = valid_at

        resp = client.post("/api/v2/memories", json=payload)
        assert resp.status_code == 201, f"Store failed: {resp.text}"
        return resp.json()["memory_id"]

    def test_query_endpoint_with_as_of_system(self, test_app: httpx.Client):
        """Test as_of_system parameter filters by system time."""
        # Store first memory
        memory_id_1 = self._store_memory(test_app, "First memory - early", memory_type="pit_test")

        # Record timestamp after first memory
        time.sleep(0.05)
        point_in_time = datetime.now(UTC).isoformat()
        time.sleep(0.05)

        # Store second memory after the point
        memory_id_2 = self._store_memory(test_app, "Second memory - late", memory_type="pit_test")

        # Query with as_of_system should only return first memory
        query_response = test_app.post(
            "/api/v2/memories/query",
            json={
                "memory_type": "pit_test",
                "as_of_system": point_in_time,
            },
        )
        assert query_response.status_code == 200, f"Query failed: {query_response.text}"
        results = query_response.json()["results"]

        memory_ids = [r["memory_id"] for r in results]
        assert memory_id_1 in memory_ids, "First memory should be included"
        assert memory_id_2 not in memory_ids, "Second memory should be excluded"

    def test_query_endpoint_with_as_of_event(self, test_app: httpx.Client):
        """Test as_of_event parameter filters by validity window."""
        # Store memory with explicit valid_at
        memory_id = self._store_memory(
            test_app,
            "Fact valid from Jan 15",
            memory_type="pit_event_test",
            valid_at="2024-01-15T00:00:00Z",
        )

        # Query before valid_at - should not find memory
        query_before = test_app.post(
            "/api/v2/memories/query",
            json={
                "memory_type": "pit_event_test",
                "as_of_event": "2024-01-14T00:00:00Z",
            },
        )
        assert query_before.status_code == 200
        results_before = query_before.json()["results"]
        assert memory_id not in [r["memory_id"] for r in results_before], (
            "Memory should not be visible before valid_at"
        )

        # Query after valid_at - should find memory
        query_after = test_app.post(
            "/api/v2/memories/query",
            json={
                "memory_type": "pit_event_test",
                "as_of_event": "2024-01-16T00:00:00Z",
            },
        )
        assert query_after.status_code == 200
        results_after = query_after.json()["results"]
        assert memory_id in [r["memory_id"] for r in results_after], (
            "Memory should be visible after valid_at"
        )

    def test_query_endpoint_returns_filter_metadata(self, test_app: httpx.Client):
        """Test that query response includes filter metadata."""
        query_response = test_app.post(
            "/api/v2/memories/query",
            json={
                "scope": "user",
                "as_of_system": "2024-02-15T14:30:00Z",
                "as_of_event": "2024-02-15T00:00:00Z",
            },
        )
        assert query_response.status_code == 200
        response_data = query_response.json()

        # Verify filter metadata is returned
        assert "filters" in response_data
        assert response_data["filters"]["as_of_system"] == "2024-02-15T14:30:00Z"
        assert response_data["filters"]["as_of_event"] == "2024-02-15T00:00:00Z"
        assert response_data["filters"]["scope"] == "user"

    def test_query_historical_version_content(self, test_app: httpx.Client):
        """Test as_of_system returns historical version content."""
        # Store initial memory with upsert mode
        _ = self._store_memory(
            test_app,
            "Version 1: Original",
            namespace="pit/history",
            path_key="content",
        )

        # Record timestamp after v1
        time.sleep(0.05)
        point_in_time = datetime.now(UTC).isoformat()
        time.sleep(0.05)

        # Update memory (creates v2)
        self._store_memory(
            test_app,
            "Version 2: Updated",
            namespace="pit/history",
            path_key="content",
        )

        # Query at historical point should return v1 content
        query_response = test_app.post(
            "/api/v2/memories/query",
            json={
                "namespace": "pit/history",
                "as_of_system": point_in_time,
            },
        )
        assert query_response.status_code == 200
        results = query_response.json()["results"]

        assert len(results) == 1
        assert "Version 1" in results[0]["content"], "as_of_system should return historical content"

        # Query current should return v2 content
        current_response = test_app.post(
            "/api/v2/memories/query",
            json={"namespace": "pit/history"},
        )
        assert current_response.status_code == 200
        current_results = current_response.json()["results"]

        assert len(current_results) == 1
        assert "Version 2" in current_results[0]["content"], (
            "Current query should return latest content"
        )


class TestInvalidateAndQueryE2E:
    """E2E tests for invalidate + point-in-time query workflow (#1185)."""

    def test_as_of_event_excludes_memories_before_valid_at(self, test_app: httpx.Client):
        """Test that as_of_event respects valid_at boundaries."""
        # Create memory with valid_at in the past
        payload = {
            "content": "Bob works at Acme",
            "scope": "user",
            "memory_type": "pit_valid_test",
            "valid_at": "2024-06-01T00:00:00Z",  # Valid from June 1
        }
        store_response = test_app.post("/api/v2/memories", json=payload)
        assert store_response.status_code == 201
        memory_id = store_response.json()["memory_id"]

        # Query before valid_at (May) - should NOT find
        query_may = test_app.post(
            "/api/v2/memories/query",
            json={
                "memory_type": "pit_valid_test",
                "as_of_event": "2024-05-15T00:00:00Z",
            },
        )
        assert query_may.status_code == 200
        may_ids = [r["memory_id"] for r in query_may.json()["results"]]
        assert memory_id not in may_ids, "Memory should not be visible before valid_at"

        # Query after valid_at (July) - should find
        query_july = test_app.post(
            "/api/v2/memories/query",
            json={
                "memory_type": "pit_valid_test",
                "as_of_event": "2024-07-15T00:00:00Z",
            },
        )
        assert query_july.status_code == 200
        july_ids = [r["memory_id"] for r in query_july.json()["results"]]
        assert memory_id in july_ids, "Memory should be visible after valid_at"
