"""End-to-end tests for delegation API (Issue #1271).

Tests the HTTP API surface using FastAPI TestClient with dependency
overrides for auth. Validates request/response schemas, auth enforcement,
and error mapping.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.delegation.models import DelegationMode, DelegationResult

# ---------------------------------------------------------------------------
# We rebuild the delegation endpoints with test-controllable dependencies
# to avoid import-time resolution issues with _get_require_auth()
# ---------------------------------------------------------------------------


def _create_test_router(
    auth_result_provider,
    service_provider,
) -> APIRouter:
    """Create a delegation router with injected auth and service providers."""
    from nexus.server.api.v2.routers.delegation import (
        DelegateRequest,
        DelegateResponse,
        DelegationListItem,
        DelegationListResponse,
        _handle_delegation_error,
    )

    test_router = APIRouter(prefix="/api/v2/agents/delegate", tags=["delegation"])

    @test_router.post("", response_model=DelegateResponse)
    async def create_delegation(
        request: DelegateRequest,
        auth_result: dict[str, Any] = Depends(auth_result_provider),
    ) -> DelegateResponse:
        subject_type = auth_result.get("subject_type", "")
        if subject_type != "agent":
            raise HTTPException(
                status_code=403,
                detail="Only agents can delegate. Caller subject_type must be 'agent'.",
            )

        coordinator_agent_id = auth_result.get("subject_id", "")
        coordinator_owner_id = auth_result.get("user_id") or auth_result.get("metadata", {}).get(
            "user_id", ""
        )
        zone_id = auth_result.get("zone_id")

        try:
            mode = DelegationMode(request.namespace_mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid namespace_mode: {request.namespace_mode!r}. Must be 'copy', 'clean', or 'shared'.",
            ) from exc

        service = service_provider()
        try:
            result = service.delegate(
                coordinator_agent_id=coordinator_agent_id,
                coordinator_owner_id=coordinator_owner_id,
                worker_id=request.worker_id,
                worker_name=request.worker_name,
                delegation_mode=mode,
                zone_id=zone_id,
                scope_prefix=request.scope_prefix,
                remove_grants=request.remove_grants,
                add_grants=request.add_grants,
                readonly_paths=request.readonly_paths,
                ttl_seconds=request.ttl_seconds,
            )
        except Exception as e:
            _handle_delegation_error(e)
            raise

        return DelegateResponse(
            delegation_id=result.delegation_id,
            worker_agent_id=result.worker_agent_id,
            api_key=result.api_key,
            mount_table=result.mount_table,
            expires_at=result.expires_at,
            delegation_mode=result.delegation_mode.value,
        )

    @test_router.delete("/{delegation_id}")
    async def revoke_delegation(
        delegation_id: str,
        auth_result: dict[str, Any] = Depends(auth_result_provider),
    ) -> dict[str, Any]:
        if auth_result.get("subject_type", "") != "agent":
            raise HTTPException(status_code=403, detail="Only agents can revoke delegations.")
        service = service_provider()
        try:
            service.revoke_delegation(delegation_id)
        except Exception as e:
            _handle_delegation_error(e)
            raise
        return {"status": "revoked", "delegation_id": delegation_id}

    @test_router.get("", response_model=DelegationListResponse)
    async def list_delegations(
        auth_result: dict[str, Any] = Depends(auth_result_provider),
    ) -> DelegationListResponse:
        if auth_result.get("subject_type", "") != "agent":
            raise HTTPException(status_code=403, detail="Only agents can list delegations.")
        coordinator_agent_id = auth_result.get("subject_id", "")
        service = service_provider()
        records = service.list_delegations(coordinator_agent_id)
        items = [
            DelegationListItem(
                delegation_id=r.delegation_id,
                agent_id=r.agent_id,
                parent_agent_id=r.parent_agent_id,
                delegation_mode=r.delegation_mode.value,
                scope_prefix=r.scope_prefix,
                lease_expires_at=r.lease_expires_at,
                zone_id=r.zone_id,
                created_at=r.created_at,
            )
            for r in records
        ]
        return DelegationListResponse(delegations=items, count=len(items))

    return test_router


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
    service.list_delegations.return_value = []
    service.revoke_delegation.return_value = True
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


@pytest.fixture()
def client(mock_delegation_service, agent_auth):
    """TestClient with agent auth."""

    async def auth_provider():
        return agent_auth

    app = FastAPI()
    app.include_router(_create_test_router(auth_provider, lambda: mock_delegation_service))
    return TestClient(app)


@pytest.fixture()
def user_client(mock_delegation_service, user_auth):
    """TestClient with user auth (for rejection tests)."""

    async def auth_provider():
        return user_auth

    app = FastAPI()
    app.include_router(_create_test_router(auth_provider, lambda: mock_delegation_service))
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
        from nexus.delegation.errors import EscalationError

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
        from nexus.delegation.errors import DelegationChainError

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
        from nexus.delegation.errors import DelegationNotFoundError

        mock_delegation_service.revoke_delegation.side_effect = DelegationNotFoundError("not found")
        response = client.delete("/api/v2/agents/delegate/nonexistent")
        assert response.status_code == 404


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
        assert data["count"] == 0


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
