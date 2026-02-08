"""E2E tests for Non-destructive Memory Updates (Append-Only Pattern) (#1188).

Tests the FastAPI endpoints for append-only behavior:
- PUT  /api/v2/memories/{id}          - Update creates new row (append-only)
- DELETE /api/v2/memories/{id}        - Soft-delete preserves row
- POST /api/v2/memories/query         - Default filters exclude superseded
- POST /api/v2/memories/query         - include_superseded=true shows old versions
- GET  /api/v2/memories/{id}/lineage  - Lineage chain traversal
- GET  /api/v2/memories/{id}/history  - Version history works with any chain ID

Uses the shared test_app fixture from conftest.py which starts an actual
nexus server process for true e2e testing.
"""

import time

import httpx
import pytest

pytestmark = pytest.mark.xdist_group("memory_append_only")


class TestAppendOnlyUpdateE2E:
    """E2E tests for append-only update behavior (#1188)."""

    def _store_memory(
        self,
        client: httpx.Client,
        content: str,
        namespace: str | None = None,
        path_key: str | None = None,
    ) -> str:
        """Helper to store a memory and return its ID."""
        payload = {"content": content, "scope": "user"}
        if namespace:
            payload["namespace"] = namespace
        if path_key:
            payload["path_key"] = path_key

        resp = client.post("/api/v2/memories", json=payload)
        assert resp.status_code == 201, f"Store failed: {resp.text}"
        return resp.json()["memory_id"]

    def test_update_creates_new_memory_id(self, test_app: httpx.Client):
        """PUT /memories/{id} should return a NEW memory_id (append-only)."""
        # Store original
        original_id = self._store_memory(test_app, "Original content", path_key="ao-test-1")

        # Update via PUT
        resp = test_app.put(
            f"/api/v2/memories/{original_id}",
            json={"content": "Updated content"},
        )
        assert resp.status_code == 200, f"Update failed: {resp.text}"
        data = resp.json()

        # Key assertion: new memory_id is different from original
        new_id = data["memory_id"]
        assert new_id != original_id, "Append-only: update should create new memory_id"
        assert data["status"] == "updated"

    def test_update_preserves_original_row(self, test_app: httpx.Client):
        """After update, original memory row should still exist (not deleted)."""
        original_id = self._store_memory(test_app, "Preserved content", path_key="ao-test-2")

        # Update
        resp = test_app.put(
            f"/api/v2/memories/{original_id}",
            json={"content": "New content"},
        )
        new_id = resp.json()["memory_id"]

        # GET on original ID should resolve to the current (new) version
        resp = test_app.get(f"/api/v2/memories/{original_id}")
        assert resp.status_code == 200, f"Get original failed: {resp.text}"
        memory = resp.json()["memory"]
        # The resolved memory should have the NEW content
        assert memory["content"] == "New content"

        # GET on new ID should also work
        resp = test_app.get(f"/api/v2/memories/{new_id}")
        assert resp.status_code == 200
        assert resp.json()["memory"]["content"] == "New content"

    def test_multiple_updates_chain(self, test_app: httpx.Client):
        """Multiple updates should create a chain of versions."""
        v1_id = self._store_memory(test_app, "Version 1", path_key="ao-test-3")

        # Update to v2
        resp = test_app.put(
            f"/api/v2/memories/{v1_id}",
            json={"content": "Version 2"},
        )
        v2_id = resp.json()["memory_id"]

        # Update to v3
        resp = test_app.put(
            f"/api/v2/memories/{v2_id}",
            json={"content": "Version 3"},
        )
        v3_id = resp.json()["memory_id"]

        # All three IDs should be different
        assert len({v1_id, v2_id, v3_id}) == 3

        # GET on any ID should resolve to latest content
        for mid in [v1_id, v2_id, v3_id]:
            resp = test_app.get(f"/api/v2/memories/{mid}")
            assert resp.status_code == 200
            assert resp.json()["memory"]["content"] == "Version 3"


class TestSoftDeleteE2E:
    """E2E tests for soft-delete behavior (#1188)."""

    def _store_memory(self, client: httpx.Client, content: str, path_key: str | None = None) -> str:
        payload = {"content": content, "scope": "user"}
        if path_key:
            payload["path_key"] = path_key
        resp = client.post("/api/v2/memories", json=payload)
        assert resp.status_code == 201
        return resp.json()["memory_id"]

    def test_delete_is_soft_by_default(self, test_app: httpx.Client):
        """DELETE should soft-delete (memory preserved but not returned by GET)."""
        memory_id = self._store_memory(test_app, "To be soft-deleted")

        # Delete
        resp = test_app.delete(f"/api/v2/memories/{memory_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True

        # GET should return 404 (soft-deleted memories filtered out)
        resp = test_app.get(f"/api/v2/memories/{memory_id}")
        assert resp.status_code == 404

    def test_hard_delete_with_soft_false(self, test_app: httpx.Client):
        """DELETE with soft=false should also work (calls memory.delete)."""
        memory_id = self._store_memory(test_app, "To be hard-deleted")

        # Hard delete
        resp = test_app.delete(f"/api/v2/memories/{memory_id}?soft=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True

        # GET should return 404
        resp = test_app.get(f"/api/v2/memories/{memory_id}")
        assert resp.status_code == 404


class TestQueryFilteringE2E:
    """E2E tests for query filtering with append-only (#1188)."""

    def _store_memory(
        self,
        client: httpx.Client,
        content: str,
        namespace: str | None = None,
        path_key: str | None = None,
    ) -> str:
        payload = {"content": content, "scope": "user"}
        if namespace:
            payload["namespace"] = namespace
        if path_key:
            payload["path_key"] = path_key
        resp = client.post("/api/v2/memories", json=payload)
        assert resp.status_code == 201
        return resp.json()["memory_id"]

    def test_default_query_excludes_superseded(self, test_app: httpx.Client):
        """Default query should only return current (non-superseded) memories."""
        ns = f"filter-test-{int(time.time() * 1000)}"
        original_id = self._store_memory(test_app, "Original", namespace=ns, path_key="filter-1")

        # Update creates new version, superseding original
        resp = test_app.put(
            f"/api/v2/memories/{original_id}",
            json={"content": "Updated", "namespace": ns},
        )
        assert resp.status_code == 200

        # Query without include_superseded should return only 1 result
        resp = test_app.post(
            "/api/v2/memories/query",
            json={"namespace": ns},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1, f"Expected 1 current memory, got {data['total']}"
        assert data["results"][0]["content"] == "Updated"

    def test_include_superseded_returns_all(self, test_app: httpx.Client):
        """Query with include_superseded=true should return old versions too."""
        ns = f"superseded-test-{int(time.time() * 1000)}"
        original_id = self._store_memory(test_app, "V1 content", namespace=ns, path_key="sup-1")

        # Update
        resp = test_app.put(
            f"/api/v2/memories/{original_id}",
            json={"content": "V2 content", "namespace": ns},
        )
        assert resp.status_code == 200

        # Query WITH include_superseded
        resp = test_app.post(
            "/api/v2/memories/query",
            json={"namespace": ns, "include_superseded": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2, f"Expected 2 memories (current + superseded), got {data['total']}"
        assert data["filters"]["include_superseded"] is True

        # Verify both versions are present
        contents = {r["content"] for r in data["results"]}
        assert "V1 content" in contents
        assert "V2 content" in contents


class TestLineageE2E:
    """E2E tests for lineage traversal (#1188)."""

    def _store_memory(
        self,
        client: httpx.Client,
        content: str,
        path_key: str | None = None,
    ) -> str:
        payload = {"content": content, "scope": "user"}
        if path_key:
            payload["path_key"] = path_key
        resp = client.post("/api/v2/memories", json=payload)
        assert resp.status_code == 201
        return resp.json()["memory_id"]

    def test_lineage_endpoint_single_memory(self, test_app: httpx.Client):
        """Lineage for a single memory should return chain of length 1."""
        memory_id = self._store_memory(test_app, "Solo memory")

        resp = test_app.get(f"/api/v2/memories/{memory_id}/lineage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["chain_length"] == 1
        assert data["current_memory_id"] == memory_id
        assert len(data["lineage"]) == 1
        assert data["lineage"][0]["content"] == "Solo memory"

    def test_lineage_after_updates(self, test_app: httpx.Client):
        """Lineage should show all versions in chronological order."""
        v1_id = self._store_memory(test_app, "Lineage V1", path_key="lineage-test")

        resp = test_app.put(
            f"/api/v2/memories/{v1_id}",
            json={"content": "Lineage V2"},
        )
        v2_id = resp.json()["memory_id"]

        resp = test_app.put(
            f"/api/v2/memories/{v2_id}",
            json={"content": "Lineage V3"},
        )
        v3_id = resp.json()["memory_id"]

        # Get lineage from ANY id in the chain
        resp = test_app.get(f"/api/v2/memories/{v1_id}/lineage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["chain_length"] == 3
        assert data["current_memory_id"] == v3_id

        # Chronological order: oldest first
        contents = [entry["content"] for entry in data["lineage"]]
        assert contents == ["Lineage V1", "Lineage V2", "Lineage V3"]

    def test_lineage_from_middle_of_chain(self, test_app: httpx.Client):
        """Lineage should work when called with a middle-of-chain ID."""
        v1_id = self._store_memory(test_app, "Chain V1", path_key="mid-chain")

        resp = test_app.put(
            f"/api/v2/memories/{v1_id}",
            json={"content": "Chain V2"},
        )
        v2_id = resp.json()["memory_id"]

        resp = test_app.put(
            f"/api/v2/memories/{v2_id}",
            json={"content": "Chain V3"},
        )

        # Request lineage using the MIDDLE id (v2)
        resp = test_app.get(f"/api/v2/memories/{v2_id}/lineage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["chain_length"] == 3

    def test_lineage_nonexistent_memory(self, test_app: httpx.Client):
        """Lineage for nonexistent memory should return 404."""
        resp = test_app.get("/api/v2/memories/nonexistent-id/lineage")
        assert resp.status_code == 404


class TestHistoryWithChainE2E:
    """E2E tests for version history working across append-only chains (#1188)."""

    def _store_memory(
        self,
        client: httpx.Client,
        content: str,
        path_key: str | None = None,
    ) -> str:
        payload = {"content": content, "scope": "user"}
        if path_key:
            payload["path_key"] = path_key
        resp = client.post("/api/v2/memories", json=payload)
        assert resp.status_code == 201
        return resp.json()["memory_id"]

    def test_history_with_original_id(self, test_app: httpx.Client):
        """Version history should work when given original (superseded) memory_id."""
        original_id = self._store_memory(test_app, "History V1", path_key="hist-chain")

        # Update creates new memory in chain
        resp = test_app.put(
            f"/api/v2/memories/{original_id}",
            json={"content": "History V2"},
        )
        assert resp.status_code == 200

        # Get history using ORIGINAL id
        resp = test_app.get(f"/api/v2/memories/{original_id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_version"] == 2
        assert len(data["versions"]) >= 2

    def test_rollback_with_chain(self, test_app: httpx.Client):
        """Rollback should work correctly with append-only chain."""
        v1_id = self._store_memory(test_app, "Rollback V1", path_key="rb-chain")

        # Update to V2
        resp = test_app.put(
            f"/api/v2/memories/{v1_id}",
            json={"content": "Rollback V2"},
        )
        resp.json()["memory_id"]  # v2 created

        # Rollback to version 1 using original ID
        resp = test_app.post(
            f"/api/v2/memories/{v1_id}/rollback?version=1",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rolled_back"] is True
        assert data["content"] == "Rollback V1"

    def test_diff_across_chain(self, test_app: httpx.Client):
        """Diff should work across append-only chain versions."""
        v1_id = self._store_memory(test_app, "Diff A", path_key="diff-chain")

        resp = test_app.put(
            f"/api/v2/memories/{v1_id}",
            json={"content": "Diff B"},
        )
        assert resp.status_code == 200

        # Diff v1 vs v2 using original ID
        resp = test_app.get(
            f"/api/v2/memories/{v1_id}/diff?v1=1&v2=2&mode=content",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "content"
        assert "Diff A" in data["diff"] or "Diff B" in data["diff"]
