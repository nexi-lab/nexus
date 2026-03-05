"""E2E tests for agent warmup endpoint (Issue #2172).

Tests the full pipeline: HTTP → REST endpoint → AgentWarmupService → AgentRegistry.
Verifies the POST /api/v2/agents/{agent_id}/warmup endpoint works end-to-end
with permissions enabled, exercises warmup step execution, and validates
timing/performance.

Uses httpx ASGITransport for in-process testing (no subprocess).
"""

import asyncio
import os
from pathlib import Path

import httpx
import pytest


def _create_test_app(tmp_path: Path, enforce_permissions: bool = False):
    """Create a FastAPI app with real NexusFS + AgentWarmupService."""
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.core.config import PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app
    from tests.helpers.dict_metastore import DictMetastore

    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-warmup-e2e")

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(exist_ok=True)
    backend = CASLocalBackend(root_path=str(storage_dir))
    metadata_store = DictMetastore()

    db_url = f"sqlite:///{tmp_path / 'records.db'}"

    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=None,
        permissions=PermissionConfig(
            enforce=enforce_permissions,
            allow_admin_bypass=True,
            enforce_zone_isolation=False,
            enable_tiger_cache=False,
            enable_deferred=False,
        ),
        is_admin=True,
    )

    api_key = "test-api-key-warmup-e2e"
    app = create_app(nexus_fs=nx, api_key=api_key, database_url=db_url)
    return app, api_key


def _run_async(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def test_app_with_warmup(tmp_path):
    """Create test FastAPI app with warmup service and permissions."""
    from nexus.lib.sync_bridge import shutdown_sync_bridge

    app, api_key = _create_test_app(tmp_path, enforce_permissions=True)

    # Wire AgentWarmupService onto app.state for the warmup endpoint
    from nexus.system_services.agents.agent_registry import AgentRegistry
    from nexus.system_services.agents.agent_warmup import AgentWarmupService
    from nexus.system_services.agents.warmup_steps import register_standard_steps

    # Create a fresh AgentRegistry for this test
    from tests.helpers.in_memory_record_store import InMemoryRecordStore

    record_store = InMemoryRecordStore()
    registry = AgentRegistry(record_store=record_store, flush_interval=60)
    app.state.agent_registry = registry

    warmup_service = AgentWarmupService(
        agent_registry=registry,
        enabled_bricks=frozenset({"search", "pay", "auth"}),
    )
    register_standard_steps(warmup_service)
    app.state.agent_warmup_service = warmup_service

    yield app, api_key, registry, record_store

    record_store.close()
    shutdown_sync_bridge()


class TestWarmupEndpointE2E:
    """E2E: POST /api/v2/agents/{agent_id}/warmup through FastAPI."""

    def test_warmup_service_not_available_returns_503(self, tmp_path):
        """When warmup service is not wired, endpoint returns 503."""
        from nexus.lib.sync_bridge import shutdown_sync_bridge

        app, api_key = _create_test_app(tmp_path, enforce_permissions=False)
        # Explicitly set warmup service to None
        app.state.agent_warmup_service = None

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v2/agents/agent-999/warmup",
                    json={"steps": []},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert resp.status_code == 503
                assert "warmup" in resp.json()["detail"].lower()

        _run_async(_test())
        shutdown_sync_bridge()

    def test_warmup_nonexistent_agent(self, test_app_with_warmup):
        """Warmup on nonexistent agent → success=False with error."""
        app, api_key, _registry, _rs = test_app_with_warmup

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v2/agents/nonexistent-agent/warmup",
                    json={"steps": [{"name": "load_credentials"}]},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert "not found" in data["error"]
                assert data["agent_id"] == "nonexistent-agent"

        _run_async(_test())

    def test_warmup_registered_agent_empty_steps(self, test_app_with_warmup):
        """Warmup with empty steps → immediate CONNECTED."""
        app, api_key, registry, _rs = test_app_with_warmup

        # Register an agent
        registry.register("e2e-agent-1", "alice")

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v2/agents/e2e-agent-1/warmup",
                    json={"steps": []},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["agent_id"] == "e2e-agent-1"
                assert data["duration_ms"] >= 0

        _run_async(_test())

        # Verify agent transitioned to CONNECTED
        from nexus.contracts.agent_types import AgentState

        record = registry.get("e2e-agent-1")
        assert record.state is AgentState.CONNECTED

    def test_warmup_with_custom_steps(self, test_app_with_warmup):
        """Warmup with custom steps including optional + required."""
        app, api_key, registry, _rs = test_app_with_warmup

        registry.register("e2e-agent-2", "bob")

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v2/agents/e2e-agent-2/warmup",
                    json={
                        "steps": [
                            {"name": "load_credentials", "timeout_seconds": 5, "required": True},
                            {"name": "verify_bricks", "timeout_seconds": 5, "required": True},
                            {"name": "warm_caches", "timeout_seconds": 5, "required": False},
                            {"name": "connect_mcp", "timeout_seconds": 5, "required": False},
                        ]
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["agent_id"] == "e2e-agent-2"
                assert "load_credentials" in data["steps_completed"]
                assert "verify_bricks" in data["steps_completed"]
                assert data["duration_ms"] > 0

        _run_async(_test())

    def test_warmup_idempotent_on_connected_agent(self, test_app_with_warmup):
        """Second warmup on already-CONNECTED agent → idempotent success."""
        app, api_key, registry, _rs = test_app_with_warmup

        registry.register("e2e-agent-3", "carol")

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                # First warmup
                resp1 = await client.post(
                    "/api/v2/agents/e2e-agent-3/warmup",
                    json={"steps": []},
                    headers=headers,
                )
                assert resp1.status_code == 200
                assert resp1.json()["success"] is True

                # Second warmup (already CONNECTED)
                resp2 = await client.post(
                    "/api/v2/agents/e2e-agent-3/warmup",
                    json={"steps": [{"name": "load_credentials"}]},
                    headers=headers,
                )
                assert resp2.status_code == 200
                assert resp2.json()["success"] is True

        _run_async(_test())

    def test_warmup_performance_under_100ms(self, test_app_with_warmup):
        """Warmup with standard steps completes in under 100ms (no I/O)."""
        app, api_key, registry, _rs = test_app_with_warmup

        registry.register("e2e-agent-perf", "perf-user")

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v2/agents/e2e-agent-perf/warmup",
                    json={
                        "steps": [
                            {"name": "load_credentials", "timeout_seconds": 5},
                            {"name": "verify_bricks", "timeout_seconds": 5},
                            {"name": "load_context", "timeout_seconds": 5},
                        ]
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                # Performance: warmup should complete in < 100ms for in-memory steps
                assert data["duration_ms"] < 100, (
                    f"Warmup took {data['duration_ms']:.1f}ms, expected < 100ms"
                )

        _run_async(_test())

    def test_warmup_no_body_uses_standard_steps(self, test_app_with_warmup):
        """POST with no body uses STANDARD_WARMUP steps."""
        app, api_key, registry, _rs = test_app_with_warmup

        registry.register("e2e-agent-default", "default-user")

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v2/agents/e2e-agent-default/warmup",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["agent_id"] == "e2e-agent-default"

        _run_async(_test())
