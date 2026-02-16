"""E2E tests for Memory Versioning API (#1184).

Tests the FastAPI endpoints for memory version tracking:
- GET  /api/v2/memories/{id}/history         - Version history
- GET  /api/v2/memories/{id}/versions/{ver}  - Get specific version
- POST /api/v2/memories/{id}/rollback        - Rollback to version
- GET  /api/v2/memories/{id}/diff            - Diff between versions

Uses the shared test_app fixture from conftest.py which starts an actual
nexus server process for true e2e testing.
"""

import httpx


class TestMemoryVersioningE2E:
    """E2E tests for memory versioning endpoints (#1184)."""

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

    def test_version_history_on_create(self, test_app: httpx.Client):
        """Test that storing a memory creates version 1."""
        # Store memory
        memory_id = self._store_memory(test_app, "Initial content")

        # Get history
        resp = test_app.get(f"/api/v2/memories/{memory_id}/history")
        assert resp.status_code == 200, f"History failed: {resp.text}"

        data = resp.json()
        assert data["memory_id"] == memory_id
        assert data["current_version"] == 1
        assert len(data["versions"]) == 1
        assert data["versions"][0]["version"] == 1
        assert data["versions"][0]["source_type"] == "original"

    def test_version_history_on_update(self, test_app: httpx.Client):
        """Test that updating a memory creates a new version."""
        # Store initial memory with upsert mode
        memory_id = self._store_memory(
            test_app, "Version 1", namespace="test/versioning", path_key="doc"
        )

        # Update the memory (upsert mode)
        self._store_memory(
            test_app, "Version 2 - updated", namespace="test/versioning", path_key="doc"
        )

        # Get history
        resp = test_app.get(f"/api/v2/memories/{memory_id}/history")
        assert resp.status_code == 200

        data = resp.json()
        assert data["current_version"] == 2
        assert len(data["versions"]) == 2
        # Versions are in reverse chronological order
        assert data["versions"][0]["version"] == 2
        assert data["versions"][0]["source_type"] == "update"
        assert data["versions"][1]["version"] == 1
        assert data["versions"][1]["source_type"] == "original"

    def test_get_specific_version(self, test_app: httpx.Client):
        """Test retrieving a specific version of a memory."""
        # Store initial memory with upsert mode
        memory_id = self._store_memory(
            test_app, "First version content", namespace="test/version_get", path_key="doc"
        )

        # Update to create version 2
        self._store_memory(
            test_app, "Second version content", namespace="test/version_get", path_key="doc"
        )

        # Get version 1
        resp1 = test_app.get(f"/api/v2/memories/{memory_id}/versions/1")
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["version"] == 1
        assert data1["content"] == "First version content"

        # Get version 2
        resp2 = test_app.get(f"/api/v2/memories/{memory_id}/versions/2")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["version"] == 2
        assert data2["content"] == "Second version content"

    def test_get_nonexistent_version(self, test_app: httpx.Client):
        """Test that requesting non-existent version returns 404."""
        memory_id = self._store_memory(test_app, "Only one version")

        resp = test_app.get(f"/api/v2/memories/{memory_id}/versions/999")
        assert resp.status_code == 404

    def test_rollback_to_previous_version(self, test_app: httpx.Client):
        """Test rolling back a memory to a previous version."""
        # Store initial memory
        memory_id = self._store_memory(
            test_app, "Good content v1", namespace="test/rollback", path_key="doc"
        )

        # Update to create version 2 (bad content)
        self._store_memory(
            test_app, "Bad content v2 - this is wrong", namespace="test/rollback", path_key="doc"
        )

        # Verify current content is bad
        resp = test_app.get(f"/api/v2/memories/{memory_id}")
        assert "wrong" in resp.json()["memory"]["content"]

        # Rollback to version 1
        rollback_resp = test_app.post(f"/api/v2/memories/{memory_id}/rollback?version=1")
        assert rollback_resp.status_code == 200

        data = rollback_resp.json()
        assert data["rolled_back"] is True
        assert data["rolled_back_to_version"] == 1
        assert data["current_version"] == 3  # Rollback creates a new version
        assert data["content"] == "Good content v1"

        # Verify content is restored
        resp2 = test_app.get(f"/api/v2/memories/{memory_id}")
        assert resp2.json()["memory"]["content"] == "Good content v1"

        # Check version history shows rollback
        history_resp = test_app.get(f"/api/v2/memories/{memory_id}/history")
        versions = history_resp.json()["versions"]
        assert len(versions) == 3
        assert versions[0]["version"] == 3
        assert versions[0]["source_type"] == "rollback"

    def test_rollback_invalid_version(self, test_app: httpx.Client):
        """Test that rolling back to invalid version returns 404."""
        memory_id = self._store_memory(test_app, "Only one version")

        resp = test_app.post(f"/api/v2/memories/{memory_id}/rollback?version=999")
        assert resp.status_code == 404

    def test_diff_versions_metadata_mode(self, test_app: httpx.Client):
        """Test comparing versions in metadata mode."""
        # Store initial memory
        memory_id = self._store_memory(test_app, "Short", namespace="test/diff", path_key="doc")

        # Update with longer content
        self._store_memory(
            test_app,
            "This is much longer content than before",
            namespace="test/diff",
            path_key="doc",
        )

        # Get metadata diff
        resp = test_app.get(
            f"/api/v2/memories/{memory_id}/diff",
            params={"v1": 1, "v2": 2, "mode": "metadata"},
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["v1"] == 1
        assert data["v2"] == 2
        assert data["content_changed"] is True
        assert data["size_delta"] > 0  # v2 is larger

    def test_diff_versions_content_mode(self, test_app: httpx.Client):
        """Test comparing versions in content mode."""
        # Store initial memory
        memory_id = self._store_memory(
            test_app, "Line 1\nLine 2\nLine 3", namespace="test/diff_content", path_key="doc"
        )

        # Update with modified content
        self._store_memory(
            test_app,
            "Line 1\nModified Line 2\nLine 3\nLine 4",
            namespace="test/diff_content",
            path_key="doc",
        )

        # Get content diff
        resp = test_app.get(
            f"/api/v2/memories/{memory_id}/diff",
            params={"v1": 1, "v2": 2, "mode": "content"},
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["mode"] == "content"
        assert "diff" in data
        # Unified diff should contain markers
        diff_text = data["diff"]
        assert "---" in diff_text or "-Line 2" in diff_text or "+Modified" in diff_text

    def test_diff_invalid_version(self, test_app: httpx.Client):
        """Test that diffing with invalid version returns 404."""
        memory_id = self._store_memory(test_app, "Only one version")

        resp = test_app.get(
            f"/api/v2/memories/{memory_id}/diff",
            params={"v1": 1, "v2": 999},
        )
        assert resp.status_code == 404

    def test_multiple_updates_version_sequence(self, test_app: httpx.Client):
        """Test that multiple updates create sequential versions."""
        # Store initial memory
        memory_id = self._store_memory(
            test_app, "Version 1", namespace="test/sequence", path_key="doc"
        )

        # Create multiple updates
        for i in range(2, 6):
            self._store_memory(test_app, f"Version {i}", namespace="test/sequence", path_key="doc")

        # Check version history
        resp = test_app.get(f"/api/v2/memories/{memory_id}/history")
        data = resp.json()

        assert data["current_version"] == 5
        assert len(data["versions"]) == 5

        # Versions should be in reverse order
        version_nums = [v["version"] for v in data["versions"]]
        assert version_nums == [5, 4, 3, 2, 1]
