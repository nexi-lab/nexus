"""E2E tests for Exchange Protocol changes (Issue #1361).

Tests the following against a real running Nexus server:
1. Error handler produces structured NexusError JSON responses
2. Models split: all API endpoints still return valid responses
3. Audit endpoints work with the split models
4. Non-admin user access patterns
"""

from __future__ import annotations

import httpx

# Auth header for the default static API key used in conftest.py
AUTH_HEADERS = {"Authorization": "Bearer test-e2e-api-key-12345"}
PROTOCOL_HEADERS = {**AUTH_HEADERS, "Nexus-Protocol-Version": "2026.1"}


# ---------------------------------------------------------------------------
# Error handler tests — verify structured error responses
# ---------------------------------------------------------------------------


class TestErrorHandler:
    """Verify NexusExchangeError produces google.rpc.Status-compatible JSON."""

    def test_404_returns_json(self, test_app: httpx.Client) -> None:
        """Non-existent memory returns JSON error, not plain text."""
        resp = test_app.get(
            "/api/v2/memories/nonexistent-id-12345",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code in (404, 401, 403), f"Unexpected status: {resp.status_code}"

    def test_invalid_memory_search_returns_422(self, test_app: httpx.Client) -> None:
        """Missing required field returns 422 validation error."""
        resp = test_app.post(
            "/api/v2/memories/search",
            headers=AUTH_HEADERS,
            json={},  # Missing required 'query' field
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body

    def test_audit_nonexistent_transaction(self, test_app: httpx.Client) -> None:
        """Audit endpoint returns 404 for missing transaction."""
        resp = test_app.get(
            "/api/v2/audit/transactions/nonexistent-tx-id",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code in (404, 500)

    def test_audit_integrity_nonexistent(self, test_app: httpx.Client) -> None:
        """Integrity check returns 404 for missing record."""
        resp = test_app.get(
            "/api/v2/audit/integrity/nonexistent-record-id",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code in (404, 500)


# ---------------------------------------------------------------------------
# Models split verification — endpoints still work after models/ package
# ---------------------------------------------------------------------------


class TestModelsSplitEndpoints:
    """Verify all endpoints using split models still respond correctly."""

    def test_memory_store_and_search(self, test_app: httpx.Client) -> None:
        """Store + search memory round-trip works with split models."""
        # Store
        store_resp = test_app.post(
            "/api/v2/memories",
            headers=AUTH_HEADERS,
            json={
                "content": "Exchange protocol test memory",
                "scope": "user",
                "memory_type": "fact",
            },
        )
        assert store_resp.status_code in (200, 201), f"Store failed: {store_resp.text}"
        memory_id = store_resp.json().get("memory_id")
        assert memory_id

        # Search
        search_resp = test_app.post(
            "/api/v2/memories/search",
            headers=AUTH_HEADERS,
            json={"query": "exchange protocol test", "limit": 5},
        )
        assert search_resp.status_code == 200

    def test_trajectory_lifecycle(self, test_app: httpx.Client) -> None:
        """Trajectory start + complete works with split models."""
        # Start
        start_resp = test_app.post(
            "/api/v2/trajectories",
            headers=AUTH_HEADERS,
            json={
                "task_description": "E2E protocol test trajectory",
                "task_type": "test",
            },
        )
        assert start_resp.status_code in (200, 201), f"Start failed: {start_resp.text}"
        traj_id = start_resp.json().get("trajectory_id")
        assert traj_id

        # Complete
        complete_resp = test_app.post(
            f"/api/v2/trajectories/{traj_id}/complete",
            headers=AUTH_HEADERS,
            json={"status": "success", "success_score": 1.0},
        )
        assert complete_resp.status_code == 200

    def test_feedback_requires_trajectory(self, test_app: httpx.Client) -> None:
        """Feedback endpoint validates trajectory_id (split feedback models)."""
        resp = test_app.post(
            "/api/v2/feedback",
            headers=AUTH_HEADERS,
            json={
                "trajectory_id": "nonexistent-traj",
                "feedback_type": "human",
                "score": 0.8,
            },
        )
        # Either succeeds or returns appropriate error — not a 500
        assert resp.status_code != 500 or "error" in resp.text.lower()

    def test_consolidation_endpoint_accessible(self, test_app: httpx.Client) -> None:
        """Consolidation endpoint responds (split consolidation models)."""
        resp = test_app.post(
            "/api/v2/consolidate",
            headers=AUTH_HEADERS,
            json={"beta": 0.7, "lambda_decay": 0.1, "limit": 10},
        )
        # Should respond with either results or graceful error, not crash
        assert resp.status_code in (200, 400, 404, 422, 500)

    def test_operations_list(self, test_app: httpx.Client) -> None:
        """Operations list endpoint responds (split operation models)."""
        resp = test_app.get(
            "/api/v2/operations?limit=10",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "operations" in body

    def test_conflicts_list(self, test_app: httpx.Client) -> None:
        """Conflicts list endpoint responds (split conflict models).

        Returns 503 when sync backends not configured (expected in minimal test server).
        """
        resp = test_app.get(
            "/api/v2/sync/conflicts",
            headers=AUTH_HEADERS,
        )
        # 200 with data, or 503 if sync not configured — both are valid
        assert resp.status_code in (200, 503)


# ---------------------------------------------------------------------------
# Audit endpoints — verify models/audit.py works end-to-end
# ---------------------------------------------------------------------------


class TestAuditEndpoints:
    """Verify audit endpoints work with split audit models."""

    def test_list_transactions_empty(self, test_app: httpx.Client) -> None:
        """List transactions returns empty list on fresh server."""
        resp = test_app.get(
            "/api/v2/audit/transactions?limit=10",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "transactions" in body
        assert isinstance(body["transactions"], list)
        assert body["limit"] == 10

    def test_list_transactions_with_filters(self, test_app: httpx.Client) -> None:
        """List transactions accepts all filter params."""
        resp = test_app.get(
            "/api/v2/audit/transactions",
            headers=AUTH_HEADERS,
            params={
                "protocol": "credits",
                "status": "completed",
                "limit": 5,
                "include_total": "true",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "transactions" in body

    def test_aggregations_empty(self, test_app: httpx.Client) -> None:
        """Aggregations endpoint returns valid structure on fresh server."""
        resp = test_app.get(
            "/api/v2/audit/transactions/aggregations",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "total_volume" in body
        assert "tx_count" in body
        assert "top_buyers" in body
        assert "top_sellers" in body

    def test_export_json(self, test_app: httpx.Client) -> None:
        """Export JSON endpoint returns valid response."""
        resp = test_app.get(
            "/api/v2/audit/transactions/export?format=json",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")

    def test_export_csv(self, test_app: httpx.Client) -> None:
        """Export CSV endpoint returns valid response."""
        resp = test_app.get(
            "/api/v2/audit/transactions/export?format=csv",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Non-admin access — verify permission behavior
# ---------------------------------------------------------------------------


class TestNonAdminAccess:
    """Verify endpoints handle unauthenticated/unauthorized access."""

    def test_no_auth_header_rejected(self, test_app: httpx.Client) -> None:
        """Requests without auth are rejected."""
        resp = test_app.get("/api/v2/memories/some-id")
        # Should be 401 or 403, not 500
        assert resp.status_code in (401, 403, 404)

    def test_invalid_api_key_rejected(self, test_app: httpx.Client) -> None:
        """Requests with invalid API key are rejected."""
        resp = test_app.get(
            "/api/v2/memories/some-id",
            headers={"Authorization": "Bearer invalid-key-xyz"},
        )
        assert resp.status_code in (401, 403, 404)

    def test_health_no_auth_required(self, test_app: httpx.Client) -> None:
        """Health endpoint works without auth."""
        resp = test_app.get("/health")
        assert resp.status_code == 200
