"""Server-level E2E test: delegation API → AgentRegistry path (Issue #1588).

Tests the HTTP delegation endpoint with real DelegationService + AgentRegistry
+ EntityRegistry + ReBAC to validate the full server stack uses AgentRegistry
as the single registration path. No mocks for core services.
"""

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.bricks.delegation.models import DelegationMode
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.server.api.v2.routers.delegation import (
    DelegateRequest,
    DelegateResponse,
    _handle_delegation_error,
)
from nexus.system_services.agents.agent_registry import AgentRegistry
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# ---------------------------------------------------------------------------
# Fixtures: real services
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_store():
    """Shared in-memory RecordStore for all components."""
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def engine(record_store):
    return record_store.engine


@pytest.fixture()
def session_factory(record_store):
    return record_store.session_factory


@pytest.fixture()
def entity_registry(record_store):
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry(record_store, entity_registry):
    return AgentRegistry(
        record_store=record_store,
        entity_registry=entity_registry,
    )


@pytest.fixture()
def rebac_manager(engine):
    manager = EnhancedReBACManager(engine=engine, cache_ttl_seconds=0, max_depth=10)
    yield manager
    manager.close()


@pytest.fixture()
def delegation_service(record_store, rebac_manager, entity_registry, agent_registry):
    return DelegationService(
        record_store=record_store,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )


def _setup_coordinator(entity_registry, rebac_manager, agent_registry):
    """Register user + coordinator agent with file grants."""
    entity_registry.register_entity("user", "alice")
    agent_registry.register("coord-srv", "alice", zone_id="root", name="Coordinator")

    rebac_manager.rebac_write_batch(
        [
            {
                "subject": ("agent", "coord-srv"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/main.py"),
                "zone_id": "root",
            },
        ]
    )


def _create_test_app(delegation_service, agent_auth):
    """Create a minimal FastAPI app with the delegation endpoint wired to real services."""

    async def auth_provider():
        return agent_auth

    router = APIRouter(prefix="/api/v2/agents/delegate", tags=["delegation"])

    @router.post("", response_model=DelegateResponse)
    async def create_delegation(
        request: DelegateRequest,
        auth_result: dict[str, Any] = Depends(auth_provider),
    ) -> DelegateResponse:
        subject_type = auth_result.get("subject_type", "")
        if subject_type != "agent":
            raise HTTPException(status_code=403, detail="Only agents can delegate.")

        coordinator_agent_id = auth_result.get("subject_id", "")
        coordinator_owner_id = auth_result.get("user_id", "")
        zone_id = auth_result.get("zone_id")

        try:
            mode = DelegationMode(request.namespace_mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid mode: {request.namespace_mode}"
            ) from exc

        try:
            result = delegation_service.delegate(
                coordinator_agent_id=coordinator_agent_id,
                coordinator_owner_id=coordinator_owner_id,
                worker_id=request.worker_id,
                worker_name=request.worker_name,
                delegation_mode=mode,
                zone_id=zone_id,
                add_grants=request.add_grants,
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

    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestServerDelegationPath:
    """Full HTTP → DelegationService → AgentRegistry path."""

    @pytest.fixture()
    def client(self, delegation_service, entity_registry, rebac_manager, agent_registry):
        _setup_coordinator(entity_registry, rebac_manager, agent_registry)

        auth = {
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": "coord-srv",
            "user_id": "alice",
            "zone_id": "root",
        }
        app = _create_test_app(delegation_service, auth)
        return TestClient(app)

    def test_delegate_via_http_creates_in_agent_registry(
        self, client, agent_registry, entity_registry
    ):
        """POST /delegate → worker exists in AgentRegistry + EntityRegistry."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker-http-1",
                "worker_name": "HTTP Worker",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["worker_agent_id"] == "worker-http-1"
        assert data["api_key"].startswith("sk-")

        # Verify AgentRegistry
        worker = agent_registry.get("worker-http-1")
        assert worker is not None
        assert worker.owner_id == "alice"

        # Verify EntityRegistry (via bridge)
        entity = entity_registry.get_entity("agent", "worker-http-1")
        assert entity is not None
        assert entity.parent_id == "alice"

    @patch("nexus.core.agent_registry.logger")
    def test_single_registry_write_in_logs(self, mock_logger, client, agent_registry):
        """Delegation produces exactly one '[AGENT-REG] Registered' log entry."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker-log-1",
                "worker_name": "Log Worker",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200

        # Count registration log calls
        reg_calls = [
            call for call in mock_logger.debug.call_args_list if "Registered agent" in str(call)
        ]
        assert len(reg_calls) == 1, (
            f"Expected exactly 1 registration log, got {len(reg_calls)}: {reg_calls}"
        )

    def test_clean_mode_via_http(self, client, agent_registry):
        """Clean mode delegation works through full HTTP path."""
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "worker-clean-http",
                "worker_name": "Clean HTTP Worker",
                "namespace_mode": "clean",
                "add_grants": ["/workspace/main.py"],
            },
        )

        assert response.status_code == 200
        assert agent_registry.get("worker-clean-http") is not None
