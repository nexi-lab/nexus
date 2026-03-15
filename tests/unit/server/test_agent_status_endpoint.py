"""Tests for agent spec/status REST API endpoints (Issue #2169).

Tests cover:
1. GET /status returns 200 with correct fields
2. PUT /spec sets spec and returns updated spec
3. GET /spec returns stored spec
4. GET /status returns 404 for unknown agent
5. GET /spec returns 404 for agent without spec
6. Drift detection visible in status response
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.agent_types import (
    AgentPhase,
    AgentResources,
    AgentResourceUsage,
    AgentSpec,
    AgentStatus,
    QoSClass,
)
from nexus.server.api.v2.routers.agent_status import router

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------


def _create_test_app(mock_registry: Any) -> FastAPI:
    """Create a minimal FastAPI app with the agent_status router."""
    app = FastAPI()
    app.state.async_agent_registry = mock_registry

    # Override auth to be a no-op for testing
    from nexus.server.api.v2.routers.agent_status import _get_async_agent_registry

    app.dependency_overrides[_get_async_agent_registry] = lambda: mock_registry
    app.include_router(router)
    return app


def _make_spec(**overrides: object) -> AgentSpec:
    defaults = {
        "agent_type": "analyst",
        "capabilities": frozenset({"search", "analyze"}),
        "resource_requests": AgentResources(token_budget=5000),
        "resource_limits": AgentResources(token_budget=10000),
        "qos_class": QoSClass.STANDARD,
        "zone_affinity": "zone-acme",
        "spec_generation": 3,
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)


def _make_status(**overrides: object) -> AgentStatus:
    defaults = {
        "phase": AgentPhase.ACTIVE,
        "observed_generation": 3,
        "conditions": (),
        "resource_usage": AgentResourceUsage(),
        "last_heartbeat": datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
        "last_activity": datetime(2025, 6, 1, 12, 5, tzinfo=UTC),
        "inbox_depth": 0,
        "context_usage_pct": 0.0,
    }
    defaults.update(overrides)
    return AgentStatus(**defaults)


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


class TestGetAgentStatus:
    def test_returns_200_with_correct_fields(self) -> None:
        mock_registry = AsyncMock()
        mock_registry.get_status.return_value = _make_status()

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.get("/api/v2/agents/agent-1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phase"] == "active"
        assert data["observed_generation"] == 3
        assert data["conditions"] == []
        assert data["inbox_depth"] == 0
        assert data["last_heartbeat"] is not None
        assert data["last_activity"] is not None

    def test_returns_404_for_unknown_agent(self) -> None:
        mock_registry = AsyncMock()
        mock_registry.get_status.return_value = None

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.get("/api/v2/agents/missing/status")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /spec
# ---------------------------------------------------------------------------


class TestSetAgentSpec:
    def test_sets_spec_and_returns_updated(self) -> None:
        stored_spec = _make_spec(spec_generation=4)
        mock_registry = AsyncMock()
        mock_registry.set_spec.return_value = stored_spec

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.put(
            "/api/v2/agents/agent-1/spec",
            json={
                "agent_type": "analyst",
                "capabilities": ["search", "analyze"],
                "qos_class": "standard",
                "zone_affinity": "zone-acme",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_type"] == "analyst"
        assert data["spec_generation"] == 4
        assert data["qos_class"] == "standard"

    def test_returns_404_for_unknown_agent(self) -> None:
        mock_registry = AsyncMock()
        mock_registry.set_spec.side_effect = ValueError("Agent not found")

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.put(
            "/api/v2/agents/missing/spec",
            json={"agent_type": "analyst"},
        )
        assert resp.status_code == 404

    def test_invalid_qos_returns_422(self) -> None:
        mock_registry = AsyncMock()

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.put(
            "/api/v2/agents/agent-1/spec",
            json={"agent_type": "analyst", "qos_class": "invalid"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /spec
# ---------------------------------------------------------------------------


class TestGetAgentSpec:
    def test_returns_stored_spec(self) -> None:
        stored_spec = _make_spec()
        mock_registry = AsyncMock()
        mock_registry.get_spec.return_value = stored_spec

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.get("/api/v2/agents/agent-1/spec")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_type"] == "analyst"
        assert set(data["capabilities"]) == {"search", "analyze"}
        assert data["spec_generation"] == 3

    def test_returns_404_for_no_spec(self) -> None:
        mock_registry = AsyncMock()
        mock_registry.get_spec.return_value = None

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        resp = client.get("/api/v2/agents/agent-1/spec")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Drift detection via status endpoint
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_drift_visible_in_status(self) -> None:
        """Status observed_generation != spec_generation indicates drift."""
        mock_registry = AsyncMock()
        mock_registry.get_status.return_value = _make_status(observed_generation=2)
        mock_registry.get_spec.return_value = _make_spec(spec_generation=5)

        app = _create_test_app(mock_registry)
        from nexus.server.api.v2.dependencies import _get_require_auth

        app.dependency_overrides[_get_require_auth()] = lambda: {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "test",
            "zone_id": "root",
            "is_admin": True,
        }

        client = TestClient(app)
        status_resp = client.get("/api/v2/agents/agent-1/status")
        spec_resp = client.get("/api/v2/agents/agent-1/spec")

        assert status_resp.status_code == 200
        assert spec_resp.status_code == 200

        status_data = status_resp.json()
        spec_data = spec_resp.json()

        # Drift: observed_generation (2) != spec_generation (5)
        assert status_data["observed_generation"] != spec_data["spec_generation"]
