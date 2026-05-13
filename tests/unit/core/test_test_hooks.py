from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from nexus.runtime.zone_runner import ZoneRegistry
from nexus.server.dependencies import require_admin


def test_zone_runner_test_hooks_route_work_through_registry() -> None:
    from nexus.core.test_hooks import build_test_hooks_router

    app = FastAPI()
    registry = ZoneRegistry()
    app.state.zone_registry = registry
    app.dependency_overrides[require_admin] = lambda: {"authenticated": True, "is_admin": True}
    app.include_router(build_test_hooks_router())

    try:
        with TestClient(app) as client:
            response = client.post("/api/test-hooks/zone-runners/unit-zone/sleep?delay_ms=1")

        assert response.status_code == 200
        payload = response.json()
        assert payload["zone_id"] == "unit-zone"
        assert payload["thread_name"] == "nexus-zone-unit-zone"
        assert any(runner.zone_id == "unit-zone" for runner in registry.all())
    finally:
        registry.stop_all()
