"""End-to-end tests for delegation API (Issue #1271, #1618).

Tests the HTTP API surface using FastAPI TestClient with the real
router and dependency overrides for auth. Validates request/response
schemas, auth enforcement, error mapping, pagination, and new #1618 fields.

Issue 6A: Uses real router from delegation.py (no test router copy).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.delegation import router
from nexus.services.delegation.models import (
    DelegationMode,
    DelegationRecord,
    DelegationResult,
    DelegationStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_delegation_service():
    """Create a mock DelegationService."""
    service = MagicMock()
    service.delegate.return_value = DelegationResult(
        delegation_id="del_test_001",
        worker_agent_id="worker_e2e",
        api_key="sk-test-e2e-key-12345",
        mount_table=["/workspace/proj"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        delegation_mode=DelegationMode.COPY,
    )
    # #1618: list_delegations returns (records, total)
    service.list_delegations.return_value = ([], 0)
    service.revoke_delegation.return_value = True
    service.get_delegation_by_id.return_value = DelegationRecord(
        delegation_id="del_123",
        agent_id="worker_rev",
        parent_agent_id="coordinator_e2e",
        delegation_mode=DelegationMode.COPY,
        status=DelegationStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
    service.get_delegation_chain.return_value = []
    return service


@pytest.fixture()
def agent_auth() -> dict[str, Any]:
    """Mock auth result for an agent caller."""
    return {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "coordinator_e2e",
        "user_id": "alice",
        "zone_id": "default",
        "is_admin": False,
        "metadata": {"user_id": "alice"},
    }


@pytest.fixture()
def user_auth() -> dict[str, Any]:
    """Mock auth result for a user caller (should be rejected)."""
    return {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "user_id": "alice",
        "zone_id": "default",
        "is_admin": False,
    }


def _create_test_app(
    mock_service: Any, auth_result: dict[str, Any]
) -> FastAPI:
    """Create FastAPI app with real router and dependency overrides.

    Issue 6A: Uses real router from delegation.py with FastAPI
    dependency_overrides for auth (no test router copy).
    """
    from nexus.server.api.v2.routers.delegation import _get_require_auth

    app = FastAPI()
    app.state.delegation_service = mock_service

    # Get the actual auth dependency resolved at module load time
    actual_auth_dep = _get_require_auth()

    async def _mock_auth():
        return auth_result

    app.include_router(router)
    app.dependency_overrides[actual_auth_dep] = _mock_auth

    return app


@pytest.fixture()
def client(mock_delegation_service, agent_auth):
    """TestClient with agent auth using real router."""
    app = _create_test_app(mock_delegation_service, agent_auth)
    return TestClient(app)


@pytest.fixture()
def user_client(mock_delegation_service, user_auth):
    """TestClient with user auth (for rejection tests)."""
    app = _create_test_app(mock_delegation_service, user_auth)
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /api/v2/agents/delegate
# ---------------------------------------------------------------------------


class TestCreateDelegation:
    def test_happy_path_copy_mode(self, client):
        """POST with valid copy mode request returns delegation details."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_e2e",
                "worker_name": "Worker E2E",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["delegation_id"] == "del_test_001"
        assert data["worker_agent_id"] == "worker_e2e"
        assert data["api_key"] == "sk-test-e2e-key-12345"
        assert data["delegation_mode"] == "copy"
        assert data["mount_table"] == ["/workspace/proj"]
        assert data["expires_at"] is not None

    def test_with_intent_and_scope(self, client, mock_delegation_service):
        """POST with #1618 fields: intent, can_sub_delegate, scope."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_scoped",
                "worker_name": "Worker Scoped",
                "namespace_mode": "copy",
                "intent": "Run unit tests",
                "can_sub_delegate": True,
                "scope": {
                    "allowed_operations": ["read", "execute"],
                    "resource_patterns": ["*.py"],
                    "max_depth": 2,
                },
            },
        )

        assert response.status_code == 200
        # Verify service was called with new fields
        call_kwargs = mock_delegation_service.delegate.call_args.kwargs
        assert call_kwargs["intent"] == "Run unit tests"
        assert call_kwargs["can_sub_delegate"] is True
        assert call_kwargs["scope"] is not None

    def test_clean_mode(self, client):
        """POST with clean mode and add_grants."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_clean",
                "worker_name": "Worker Clean",
                "namespace_mode": "clean",
                "add_grants": ["/workspace/a.txt"],
            },
        )
        assert response.status_code == 200

    def test_shared_mode(self, client):
        """POST with shared mode."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_shared",
                "worker_name": "Worker Shared",
                "namespace_mode": "shared",
            },
        )
        assert response.status_code == 200

    def test_invalid_namespace_mode(self, client):
        """Invalid namespace_mode returns 400."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_bad",
                "worker_name": "Worker Bad",
                "namespace_mode": "invalid_mode",
            },
        )
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()

    def test_ttl_exceeds_max(self, client):
        """TTL > 86400 is rejected by Pydantic validation."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_ttl",
                "worker_name": "Worker TTL",
                "namespace_mode": "copy",
                "ttl_seconds": 100000,
            },
        )
        assert response.status_code == 422  # Pydantic validation error

    def test_ttl_zero(self, client):
        """TTL = 0 is rejected by Pydantic validation."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_ttl0",
                "worker_name": "Worker TTL0",
                "namespace_mode": "copy",
                "ttl_seconds": 0,
            },
        )
        assert response.status_code == 422

    def test_missing_required_fields(self, client):
        """Missing required fields returns 422."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={"worker_id": "worker_only"},
        )
        assert response.status_code == 422

    def test_escalation_error_returns_403(self, client, mock_delegation_service):
        """EscalationError maps to HTTP 403."""
        from nexus.services.delegation.errors import EscalationError

        mock_delegation_service.delegate.side_effect = EscalationError("not allowed")
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_esc",
                "worker_name": "Worker Esc",
                "namespace_mode": "clean",
                "add_grants": ["/secret.txt"],
            },
        )
        assert response.status_code == 403

    def test_chain_error_returns_403(self, client, mock_delegation_service):
        """DelegationChainError maps to HTTP 403."""
        from nexus.services.delegation.errors import DelegationChainError

        mock_delegation_service.delegate.side_effect = DelegationChainError("no chains")
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_chain",
                "worker_name": "Worker Chain",
                "namespace_mode": "copy",
            },
        )
        assert response.status_code == 403

    def test_depth_exceeded_returns_403(self, client, mock_delegation_service):
        """DepthExceededError maps to HTTP 403 (#1618)."""
        from nexus.services.delegation.errors import DepthExceededError

        mock_delegation_service.delegate.side_effect = DepthExceededError("too deep")
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_deep",
                "worker_name": "Worker Deep",
                "namespace_mode": "copy",
            },
        )
        assert response.status_code == 403

    def test_invalid_prefix_returns_400(self, client, mock_delegation_service):
        """InvalidPrefixError maps to HTTP 400 (#1618)."""
        from nexus.services.delegation.errors import InvalidPrefixError

        mock_delegation_service.delegate.side_effect = InvalidPrefixError("bad prefix")
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_prefix",
                "worker_name": "Worker Prefix",
                "namespace_mode": "copy",
                "scope_prefix": "relative/path",
            },
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/v2/agents/delegate/{delegation_id}
# ---------------------------------------------------------------------------


class TestRevokeDelegation:
    def test_revoke_success(self, client):
        """DELETE returns success for existing delegation."""
        response = client.delete("/api/v2/agents/delegate/del_123")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "revoked"
        assert data["delegation_id"] == "del_123"

    def test_revoke_not_found(self, client, mock_delegation_service):
        """DELETE for non-existent delegation returns 404."""
        mock_delegation_service.get_delegation_by_id.return_value = None
        response = client.delete("/api/v2/agents/delegate/nonexistent")
        assert response.status_code == 404

    def test_revoke_not_owner(self, client, mock_delegation_service):
        """DELETE by non-owner agent returns 403."""
        mock_delegation_service.get_delegation_by_id.return_value = DelegationRecord(
            delegation_id="del_other",
            agent_id="other_worker",
            parent_agent_id="other_coordinator",  # not coordinator_e2e
            delegation_mode=DelegationMode.COPY,
            status=DelegationStatus.ACTIVE,
            created_at=datetime.now(UTC),
        )
        response = client.delete("/api/v2/agents/delegate/del_other")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v2/agents/delegate
# ---------------------------------------------------------------------------


class TestListDelegations:
    def test_list_empty(self, client):
        """GET returns empty list when no delegations."""
        response = client.get("/api/v2/agents/delegate")
        assert response.status_code == 200
        data = response.json()
        assert data["delegations"] == []
        assert data["total"] == 0
        assert data["limit"] == 50
        assert data["offset"] == 0

    def test_list_with_pagination(self, client, mock_delegation_service):
        """GET with pagination params."""
        now = datetime.now(UTC)
        records = [
            DelegationRecord(
                delegation_id=f"del_{i}",
                agent_id=f"worker_{i}",
                parent_agent_id="coordinator_e2e",
                delegation_mode=DelegationMode.COPY,
                status=DelegationStatus.ACTIVE,
                created_at=now,
            )
            for i in range(3)
        ]
        mock_delegation_service.list_delegations.return_value = (records, 10)

        response = client.get("/api/v2/agents/delegate?limit=3&offset=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data["delegations"]) == 3
        assert data["total"] == 10
        assert data["limit"] == 3
        assert data["offset"] == 5

    def test_list_with_status_filter(self, client, mock_delegation_service):
        """GET with status filter."""
        mock_delegation_service.list_delegations.return_value = ([], 0)

        response = client.get("/api/v2/agents/delegate?status=revoked")
        assert response.status_code == 200

        # Verify status_filter was passed to service
        call_kwargs = mock_delegation_service.list_delegations.call_args.kwargs
        assert call_kwargs["status_filter"] == DelegationStatus.REVOKED

    def test_list_with_invalid_status(self, client):
        """GET with invalid status returns 400."""
        response = client.get("/api/v2/agents/delegate?status=invalid")
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()

    def test_list_response_includes_new_fields(self, client, mock_delegation_service):
        """#1618: List response includes status, intent, depth, can_sub_delegate."""
        now = datetime.now(UTC)
        records = [
            DelegationRecord(
                delegation_id="del_enriched",
                agent_id="worker_enriched",
                parent_agent_id="coordinator_e2e",
                delegation_mode=DelegationMode.COPY,
                status=DelegationStatus.ACTIVE,
                intent="Analyze code quality",
                depth=1,
                can_sub_delegate=True,
                created_at=now,
            )
        ]
        mock_delegation_service.list_delegations.return_value = (records, 1)

        response = client.get("/api/v2/agents/delegate")
        assert response.status_code == 200
        item = response.json()["delegations"][0]
        assert item["status"] == "active"
        assert item["intent"] == "Analyze code quality"
        assert item["depth"] == 1
        assert item["can_sub_delegate"] is True


# ---------------------------------------------------------------------------
# Auth enforcement: user callers rejected
# ---------------------------------------------------------------------------


class TestAuthEnforcement:
    def test_user_cannot_create_delegation(self, user_client):
        """POST by a user (not agent) returns 403."""
        response = user_client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker_unauth",
                "worker_name": "Worker Unauth",
                "namespace_mode": "copy",
            },
        )
        assert response.status_code == 403
        assert "agent" in response.json()["detail"].lower()

    def test_user_cannot_list_delegations(self, user_client):
        """GET by a user (not agent) returns 403."""
        response = user_client.get("/api/v2/agents/delegate")
        assert response.status_code == 403

    def test_user_cannot_revoke_delegation(self, user_client):
        """DELETE by a user (not agent) returns 403."""
        response = user_client.delete("/api/v2/agents/delegate/del_123")
        assert response.status_code == 403
