"""Unit tests for the delegation REST API endpoints (detail, namespace).

Tests the 3 new endpoints:
- GET  /api/v2/agents/delegate/{id}            - Single delegation detail
- GET  /api/v2/agents/delegate/{id}/namespace  - Namespace detail
- PATCH /api/v2/agents/delegate/{id}/namespace - Update namespace config
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.delegation.models import (
    DelegationMode,
    DelegationRecord,
    DelegationStatus,
)
from nexus.server.api.v2.routers.delegation import (
    _get_delegation_service,
    router,
)
from nexus.server.dependencies import require_auth

# ---------------------------------------------------------------------------
# App setup -- isolated test app with dependency overrides
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(router)


def _make_delegation_record(**overrides: object) -> DelegationRecord:
    """Create a DelegationRecord with sensible defaults."""
    defaults = {
        "delegation_id": "del-001",
        "agent_id": "worker-agent-1",
        "parent_agent_id": "coordinator-1",
        "delegation_mode": DelegationMode.COPY,
        "status": DelegationStatus.ACTIVE,
        "scope_prefix": "/data",
        "lease_expires_at": None,
        "zone_id": "root",
        "intent": "test delegation",
        "depth": 0,
        "can_sub_delegate": False,
        "created_at": datetime(2026, 1, 1),
        "removed_grants": ("/secret",),
        "added_grants": ("/public",),
        "readonly_paths": ("/readonly",),
    }
    defaults.update(overrides)
    return DelegationRecord(**defaults)


def _make_mock_service() -> MagicMock:
    """Create a mock DelegationService with sensible defaults."""
    service = MagicMock()
    service.get_delegation_by_id.return_value = _make_delegation_record()
    service.update_namespace_config.return_value = _make_delegation_record()
    return service


_mock_service = _make_mock_service()


def _override_service() -> MagicMock:
    return _mock_service


# Default auth: agent caller
_agent_auth = {
    "authenticated": True,
    "subject_type": "agent",
    "subject_id": "coordinator-1",
    "user_id": "user-1",
    "zone_id": "root",
}

_test_app.dependency_overrides[require_auth] = lambda: _agent_auth
_test_app.dependency_overrides[_get_delegation_service] = _override_service

client = TestClient(_test_app)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset mock between tests and restore default auth."""
    _mock_service.reset_mock()
    _mock_service.get_delegation_by_id.return_value = _make_delegation_record()
    _mock_service.update_namespace_config.return_value = _make_delegation_record()
    # Restore default agent auth
    _test_app.dependency_overrides[require_auth] = lambda: _agent_auth


# ---------------------------------------------------------------------------
# GET /api/v2/agents/delegate/{delegation_id}  (single detail)
# ---------------------------------------------------------------------------


class TestGetDelegationDetail:
    """Tests for GET /api/v2/agents/delegate/{delegation_id}."""

    def test_200_valid_delegation(self) -> None:
        resp = client.get("/api/v2/agents/delegate/del-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["delegation_id"] == "del-001"
        assert data["agent_id"] == "worker-agent-1"
        assert data["parent_agent_id"] == "coordinator-1"
        assert data["delegation_mode"] == "copy"
        assert data["status"] == "active"
        assert data["scope_prefix"] == "/data"
        assert data["intent"] == "test delegation"
        assert data["depth"] == 0
        assert data["can_sub_delegate"] is False
        assert data["removed_grants"] == ["/secret"]
        assert data["added_grants"] == ["/public"]
        assert data["readonly_paths"] == ["/readonly"]
        _mock_service.get_delegation_by_id.assert_called_once_with("del-001")

    def test_404_nonexistent_delegation(self) -> None:
        _mock_service.get_delegation_by_id.return_value = None
        resp = client.get("/api/v2/agents/delegate/del-999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_403_non_agent_caller(self) -> None:
        _test_app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "user-1",
        }
        resp = client.get("/api/v2/agents/delegate/del-001")
        assert resp.status_code == 403
        assert "agent" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/v2/agents/delegate/{delegation_id}/namespace
# ---------------------------------------------------------------------------


class TestGetNamespaceDetail:
    """Tests for GET /api/v2/agents/delegate/{delegation_id}/namespace."""

    def test_200_valid_delegation(self) -> None:
        resp = client.get("/api/v2/agents/delegate/del-001/namespace")
        assert resp.status_code == 200
        data = resp.json()
        assert data["delegation_id"] == "del-001"
        assert data["agent_id"] == "worker-agent-1"
        assert data["delegation_mode"] == "copy"
        assert data["scope_prefix"] == "/data"
        assert data["removed_grants"] == ["/secret"]
        assert data["added_grants"] == ["/public"]
        assert data["readonly_paths"] == ["/readonly"]
        assert data["zone_id"] == "root"
        _mock_service.get_delegation_by_id.assert_called_once_with("del-001")

    def test_404_nonexistent_delegation(self) -> None:
        _mock_service.get_delegation_by_id.return_value = None
        resp = client.get("/api/v2/agents/delegate/del-999/namespace")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_403_non_agent_caller(self) -> None:
        _test_app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "user-1",
        }
        resp = client.get("/api/v2/agents/delegate/del-001/namespace")
        assert resp.status_code == 403
        assert "agent" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PATCH /api/v2/agents/delegate/{delegation_id}/namespace
# ---------------------------------------------------------------------------


class TestUpdateNamespaceConfig:
    """Tests for PATCH /api/v2/agents/delegate/{delegation_id}/namespace."""

    def test_200_valid_update(self) -> None:
        updated_record = _make_delegation_record(
            scope_prefix="/new-data",
            removed_grants=("/old-secret",),
            added_grants=("/new-public",),
            readonly_paths=("/new-readonly",),
        )
        _mock_service.update_namespace_config.return_value = updated_record

        resp = client.patch(
            "/api/v2/agents/delegate/del-001/namespace",
            json={
                "scope_prefix": "/new-data",
                "remove_grants": ["/old-secret"],
                "add_grants": ["/new-public"],
                "readonly_paths": ["/new-readonly"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope_prefix"] == "/new-data"
        assert data["removed_grants"] == ["/old-secret"]
        assert data["added_grants"] == ["/new-public"]
        assert data["readonly_paths"] == ["/new-readonly"]
        _mock_service.update_namespace_config.assert_called_once_with(
            delegation_id="del-001",
            scope_prefix="/new-data",
            remove_grants=["/old-secret"],
            add_grants=["/new-public"],
            readonly_paths=["/new-readonly"],
        )

    def test_404_nonexistent_delegation(self) -> None:
        _mock_service.get_delegation_by_id.return_value = None
        resp = client.patch(
            "/api/v2/agents/delegate/del-999/namespace",
            json={"scope_prefix": "/x"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_403_unauthorized_not_parent_agent(self) -> None:
        """Caller is an agent but not the parent of this delegation."""
        _test_app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": "other-agent-99",
            "user_id": "user-1",
            "zone_id": "root",
        }
        resp = client.patch(
            "/api/v2/agents/delegate/del-001/namespace",
            json={"scope_prefix": "/x"},
        )
        assert resp.status_code == 403
        assert "parent agent" in resp.json()["detail"].lower()

    def test_403_non_agent_caller(self) -> None:
        _test_app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "user-1",
        }
        resp = client.patch(
            "/api/v2/agents/delegate/del-001/namespace",
            json={"scope_prefix": "/x"},
        )
        assert resp.status_code == 403
        assert "agent" in resp.json()["detail"].lower()
