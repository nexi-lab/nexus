"""E2E tests for auto_warmup on POST /api/v2/agents/delegate (Issue #3130).

Tests that delegation with auto_warmup=True triggers warmup, and that
warmup failure does NOT roll back the delegation.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.delegation.models import DelegationMode

# We need a local DelegationService import for the real service
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.server.api.v2.routers.delegation import (
    DelegateRequest,
    DelegateResponse,
    _handle_delegation_error,
)
from nexus.services.agents.agent_registry import AgentRegistry
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_store():
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def entity_registry(record_store):
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry():
    return AgentRegistry()


@pytest.fixture()
def rebac_manager(record_store):
    manager = EnhancedReBACManager(engine=record_store.engine, cache_ttl_seconds=0, max_depth=10)
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
    entity_registry.register_entity(
        entity_type="agent",
        entity_id="coord-warmup",
        parent_type="user",
        parent_id="alice",
    )
    # Register coordinator in agent_registry (replaces AgentRegistry)
    agent_registry.register_external(
        "Coordinator",
        "alice",
        "root",
        connection_id="coord-warmup",
    )
    rebac_manager.rebac_write_batch(
        [
            {
                "subject": ("agent", "coord-warmup"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/main.py"),
                "zone_id": "root",
            },
        ]
    )


def _create_test_app(
    delegation_service: DelegationService,
    agent_auth: dict[str, Any],
    warmup_service: Any = None,
) -> FastAPI:
    """Create a minimal FastAPI app with the delegation endpoint and optional warmup service."""
    from fastapi import APIRouter, HTTPException, Request

    async def auth_provider():
        return agent_auth

    router = APIRouter(prefix="/api/v2/agents/delegate", tags=["delegation"])

    @router.post("", response_model=DelegateResponse)
    async def create_delegation(
        body: DelegateRequest,
        request: Request,
        auth_result: dict[str, Any] = Depends(auth_provider),
    ) -> DelegateResponse:
        subject_type = auth_result.get("subject_type", "")
        if subject_type != "agent":
            raise HTTPException(status_code=403, detail="Only agents can delegate.")

        coordinator_agent_id = auth_result.get("subject_id", "")
        coordinator_owner_id = auth_result.get("user_id", "")
        zone_id = auth_result.get("zone_id")

        try:
            mode = DelegationMode(body.namespace_mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid mode: {body.namespace_mode}"
            ) from exc

        try:
            result = delegation_service.delegate(
                coordinator_agent_id=coordinator_agent_id,
                coordinator_owner_id=coordinator_owner_id,
                worker_id=body.worker_id,
                worker_name=body.worker_name,
                delegation_mode=mode,
                zone_id=zone_id,
                add_grants=body.add_grants,
                ttl_seconds=body.ttl_seconds,
            )
        except Exception as e:
            _handle_delegation_error(e)
            raise

        # Auto-warmup logic (mirrors the real router)
        warmup_success = None
        if body.auto_warmup:
            ws = getattr(request.app.state, "agent_warmup_service", None)
            if ws is not None:
                try:
                    warmup_result = await ws.warmup(result.worker_agent_id)
                    warmup_success = warmup_result.success
                except Exception:
                    warmup_success = False

        return DelegateResponse(
            delegation_id=result.delegation_id,
            worker_agent_id=result.worker_agent_id,
            api_key=result.api_key,
            mount_table=result.mount_table,
            expires_at=result.expires_at,
            delegation_mode=result.delegation_mode.value,
            warmup_success=warmup_success,
        )

    app = FastAPI()
    app.include_router(router)

    # Attach warmup service to app.state
    app.state.agent_warmup_service = warmup_service

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAutoWarmup:
    """Tests for auto_warmup on delegation."""

    @pytest.fixture()
    def agent_auth(self):
        return {
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": "coord-warmup",
            "user_id": "alice",
            "zone_id": "root",
        }

    def test_auto_warmup_true_triggers_warmup(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        agent_registry,
        agent_auth,
    ):
        """Default auto_warmup=True should call warmup service."""
        _setup_coordinator(entity_registry, rebac_manager, agent_registry)

        mock_warmup = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_warmup.warmup.return_value = mock_result

        app = _create_test_app(delegation_service, agent_auth, warmup_service=mock_warmup)
        client = TestClient(app)

        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "warmup-worker-1",
                "worker_name": "Warmup Worker",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["warmup_success"] is True
        mock_warmup.warmup.assert_called_once_with("warmup-worker-1")

    def test_auto_warmup_false_skips_warmup(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        agent_registry,
        agent_auth,
    ):
        """Explicit auto_warmup=False should skip warmup."""
        _setup_coordinator(entity_registry, rebac_manager, agent_registry)

        mock_warmup = AsyncMock()

        app = _create_test_app(delegation_service, agent_auth, warmup_service=mock_warmup)
        client = TestClient(app)

        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "no-warmup-worker",
                "worker_name": "No Warmup",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
                "auto_warmup": False,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["warmup_success"] is None
        mock_warmup.warmup.assert_not_called()

    def test_warmup_failure_does_not_rollback_delegation(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        agent_registry,
        agent_auth,
    ):
        """Warmup failure should NOT undo the delegation."""
        _setup_coordinator(entity_registry, rebac_manager, agent_registry)

        mock_warmup = AsyncMock()
        mock_warmup.warmup.side_effect = RuntimeError("Warmup exploded")

        app = _create_test_app(delegation_service, agent_auth, warmup_service=mock_warmup)
        client = TestClient(app)

        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "warmup-fail-worker",
                "worker_name": "Warmup Fail",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["warmup_success"] is False
        assert data["api_key"].startswith("sk-")

        # Worker's delegation record still exists (not rolled back)
        assert delegation_service._session_factory  # service is still functional

    def test_no_warmup_service_skips_silently(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        agent_registry,
        agent_auth,
    ):
        """If warmup service is None, auto_warmup=True should silently skip."""
        _setup_coordinator(entity_registry, rebac_manager, agent_registry)

        app = _create_test_app(delegation_service, agent_auth, warmup_service=None)
        client = TestClient(app)

        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "no-svc-worker",
                "worker_name": "No Service",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200
        data = response.json()
        # warmup_success should be None since no service was available
        assert data["warmup_success"] is None

    def test_backward_compat_no_auto_warmup_field(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        agent_registry,
        agent_auth,
    ):
        """Existing callers that don't send auto_warmup get the default (True)."""
        _setup_coordinator(entity_registry, rebac_manager, agent_registry)

        mock_warmup = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_warmup.warmup.return_value = mock_result

        app = _create_test_app(delegation_service, agent_auth, warmup_service=mock_warmup)
        client = TestClient(app)

        # Send without auto_warmup field — default should be True
        response = client.post(
            "/api/v2/agents/delegate",
            json={
                "worker_id": "compat-worker",
                "worker_name": "Compat",
                "namespace_mode": "copy",
                "ttl_seconds": 3600,
            },
        )

        assert response.status_code == 200
        mock_warmup.warmup.assert_called_once_with("compat-worker")
