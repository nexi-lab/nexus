"""Unit tests for the agent list REST API endpoint (Issue #2169).

Tests the GET /api/v2/agents endpoint for listing agents in a zone
with pagination support.
"""

# AgentInfo is a frozen dataclass — create a lightweight stand-in for tests
# since the protocol module uses namespace packages that aren't directly importable.
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.routers.agent_status import (
    _get_agent_registry,
    _get_nexus_fs,
    router,
)
from nexus.server.dependencies import require_auth


@dataclass(frozen=True, slots=True)
class ExternalInfo:
    """Test stand-in for ExternalProcessInfo."""

    connection_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """Test stand-in for AgentRegistry records returned by list_processes()."""

    pid: str
    owner_id: str
    zone_id: str | None
    name: str | None
    state: str
    generation: int
    external_info: ExternalInfo | None = None


# ---------------------------------------------------------------------------
# App setup -- isolated test app with dependency overrides
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(router)


def _make_process_record(
    pid: str = "agent-1",
    owner_id: str = "user-1",
    zone_id: str | None = "root",
    name: str | None = "TestAgent",
    state: str = "CONNECTED",
    generation: int = 1,
) -> ProcessRecord:
    """Create a ProcessRecord with sensible defaults."""
    return ProcessRecord(
        pid=pid,
        owner_id=owner_id,
        zone_id=zone_id,
        name=name,
        state=state,
        generation=generation,
    )


def _make_mock_agent_registry() -> MagicMock:
    """Create a mock AgentRegistry with sensible defaults."""
    pt = MagicMock()
    pt.list_processes = MagicMock(return_value=[])
    return pt


_mock_agent_registry = _make_mock_agent_registry()


def _override_agent_registry() -> MagicMock:
    return _mock_agent_registry


# Default auth
_auth_result = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "user-1",
    "zone_id": "root",
}

_test_app.dependency_overrides[require_auth] = lambda: _auth_result
_test_app.dependency_overrides[_get_agent_registry] = _override_agent_registry
_test_app.dependency_overrides[_get_nexus_fs] = lambda: None

client = TestClient(_test_app)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset mock between tests."""
    _mock_agent_registry.reset_mock()
    _mock_agent_registry.list_processes = MagicMock(return_value=[])


# ---------------------------------------------------------------------------
# GET /api/v2/agents
# ---------------------------------------------------------------------------


class TestListAgents:
    """Tests for GET /api/v2/agents."""

    def test_200_with_agents(self) -> None:
        agents = [
            _make_process_record(pid="agent-1", name="Alpha"),
            _make_process_record(pid="agent-2", name="Beta", state="DISCONNECTED"),
            _make_process_record(pid="agent-3", name="Gamma", generation=3),
        ]
        _mock_agent_registry.list_processes = MagicMock(return_value=agents)

        resp = client.get("/api/v2/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["agents"]) == 3

        # Verify agent fields
        first = data["agents"][0]
        assert first["agent_id"] == "agent-1"
        assert first["owner_id"] == "user-1"
        assert first["zone_id"] == "root"
        assert first["name"] == "Alpha"
        assert first["state"] == "CONNECTED"
        assert first["generation"] == 1

        second = data["agents"][1]
        assert second["agent_id"] == "agent-2"
        assert second["state"] == "DISCONNECTED"

        _mock_agent_registry.list_processes.assert_called_once_with(zone_id=ROOT_ZONE_ID)

    def test_200_empty_zone(self) -> None:
        _mock_agent_registry.list_processes = MagicMock(return_value=[])

        resp = client.get("/api/v2/agents?zone_id=empty-zone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["agents"] == []
        assert data["limit"] == 50
        assert data["offset"] == 0
        _mock_agent_registry.list_processes.assert_called_once_with(zone_id="empty-zone")

    def test_pagination(self) -> None:
        """With 5 agents, limit=2, offset=1 returns agents[1:3]."""
        agents = [_make_process_record(pid=f"agent-{i}", name=f"Agent{i}") for i in range(5)]
        _mock_agent_registry.list_processes = MagicMock(return_value=agents)

        resp = client.get("/api/v2/agents?limit=2&offset=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 1
        assert len(data["agents"]) == 2
        assert data["agents"][0]["agent_id"] == "agent-1"
        assert data["agents"][1]["agent_id"] == "agent-2"

    def test_default_zone_id_is_root(self) -> None:
        _mock_agent_registry.list_processes = MagicMock(return_value=[])

        resp = client.get("/api/v2/agents")
        assert resp.status_code == 200
        _mock_agent_registry.list_processes.assert_called_once_with(zone_id=ROOT_ZONE_ID)
