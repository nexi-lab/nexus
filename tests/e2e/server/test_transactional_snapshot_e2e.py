"""Transactional Snapshot E2E tests (Issue #1752).

Tests the full lifecycle via real HTTP requests to a running nexus server.

Run with:
    uv run pytest tests/e2e/server/test_transactional_snapshot_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.e2e
class TestSnapshotFullLifecycle:
    """End-to-end: begin -> get -> commit."""

    def test_begin_get_commit(self, test_app: httpx.Client) -> None:
        """Full lifecycle: begin a snapshot, get its info, then commit."""
        # Begin
        begin_resp = test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "e2e-agent-1", "paths": ["/e2e-test.txt"]},
        )
        assert begin_resp.status_code == 200, f"begin failed: {begin_resp.text}"
        data = begin_resp.json()
        snapshot_id = data["snapshot_id"]
        assert len(snapshot_id) == 36  # UUID format

        # Get
        get_resp = test_app.get(f"/api/v2/snapshots/{snapshot_id}")
        assert get_resp.status_code == 200, f"get failed: {get_resp.text}"
        info = get_resp.json()
        assert info["snapshot_id"] == snapshot_id
        assert info["agent_id"] == "e2e-agent-1"
        assert info["status"] == "ACTIVE"
        assert info["paths"] == ["/e2e-test.txt"]

        # Commit
        commit_resp = test_app.post(f"/api/v2/snapshots/{snapshot_id}/commit")
        assert commit_resp.status_code == 204, f"commit failed: {commit_resp.text}"


@pytest.mark.e2e
class TestSnapshotRollbackLifecycle:
    """End-to-end: begin -> rollback."""

    def test_begin_rollback(self, test_app: httpx.Client) -> None:
        """Begin a snapshot then rollback."""
        begin_resp = test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "e2e-agent-2", "paths": ["/e2e-rollback.txt"]},
        )
        assert begin_resp.status_code == 200
        snapshot_id = begin_resp.json()["snapshot_id"]

        rollback_resp = test_app.post(f"/api/v2/snapshots/{snapshot_id}/rollback")
        assert rollback_resp.status_code == 200, f"rollback failed: {rollback_resp.text}"
        result = rollback_resp.json()
        assert result["snapshot_id"] == snapshot_id
        assert isinstance(result["reverted"], list)
        assert isinstance(result["conflicts"], list)
        assert isinstance(result["deleted"], list)
        assert "paths_total" in result["stats"]


@pytest.mark.e2e
class TestSnapshotListActive:
    """End-to-end: list active transactions."""

    def test_list_active(self, test_app: httpx.Client) -> None:
        """Create two snapshots and list active."""
        test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "e2e-list-agent", "paths": ["/list-a.txt"]},
        )
        test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "e2e-list-agent", "paths": ["/list-b.txt"]},
        )

        resp = test_app.get(
            "/api/v2/snapshots/active", params={"agent_id": "e2e-list-agent"}
        )
        assert resp.status_code == 200, f"list_active failed: {resp.text}"
        data = resp.json()
        assert data["count"] >= 2
        assert len(data["transactions"]) >= 2


@pytest.mark.e2e
class TestSnapshotErrorCases:
    """End-to-end: error responses."""

    def test_begin_empty_paths_400(self, test_app: httpx.Client) -> None:
        resp = test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "e2e-err", "paths": []},
        )
        assert resp.status_code == 400

    def test_commit_not_found_404(self, test_app: httpx.Client) -> None:
        resp = test_app.post("/api/v2/snapshots/nonexistent-id/commit")
        assert resp.status_code == 404

    def test_rollback_not_found_404(self, test_app: httpx.Client) -> None:
        resp = test_app.post("/api/v2/snapshots/nonexistent-id/rollback")
        assert resp.status_code == 404

    def test_get_not_found_404(self, test_app: httpx.Client) -> None:
        resp = test_app.get("/api/v2/snapshots/nonexistent-id")
        assert resp.status_code == 404


@pytest.mark.e2e
class TestSnapshotCleanup:
    """End-to-end: cleanup expired transactions."""

    def test_cleanup_returns_count(self, test_app: httpx.Client) -> None:
        resp = test_app.post("/api/v2/snapshots/cleanup")
        assert resp.status_code == 200, f"cleanup failed: {resp.text}"
        data = resp.json()
        assert "expired_count" in data
        assert isinstance(data["expired_count"], int)


@pytest.mark.e2e
class TestSnapshotAPIShapes:
    """End-to-end: verify JSON response shapes."""

    def test_begin_response_shape(self, test_app: httpx.Client) -> None:
        resp = test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "shape-agent", "paths": ["/shape.txt"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"snapshot_id"}

    def test_get_response_shape(self, test_app: httpx.Client) -> None:
        begin = test_app.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "shape-agent-2", "paths": ["/shape2.txt"]},
        )
        sid = begin.json()["snapshot_id"]
        resp = test_app.get(f"/api/v2/snapshots/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {
            "snapshot_id",
            "agent_id",
            "zone_id",
            "status",
            "paths",
            "created_at",
            "expires_at",
            "committed_at",
            "rolled_back_at",
        }
        assert set(data.keys()) == expected_keys

    def test_active_response_shape(self, test_app: httpx.Client) -> None:
        resp = test_app.get(
            "/api/v2/snapshots/active", params={"agent_id": "no-such-agent"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"transactions", "count"}
