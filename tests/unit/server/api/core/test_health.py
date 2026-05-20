"""Tests for core health endpoints."""

import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.core.health import router


def _app_with_health(nexus_fs: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.nexus_fs = nexus_fs
    app.state.api_key = None
    app.state.auth_provider = None
    return app


def test_health_does_not_probe_federation_when_peers_unset(monkeypatch):
    monkeypatch.delenv("NEXUS_PEERS", raising=False)
    calls = []

    fake_runtime = SimpleNamespace(
        federation_is_initialized=lambda kernel: calls.append(kernel) or False
    )
    monkeypatch.setitem(sys.modules, "nexus_runtime", fake_runtime)

    fs = SimpleNamespace(
        _kernel=object(),
        _perm_config=SimpleNamespace(enforce=True),
        _enforce_zone_isolation=True,
    )
    response = TestClient(_app_with_health(fs)).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert calls == []


def test_health_reports_workspace_index_status_when_present():
    fs = SimpleNamespace(
        _kernel=object(),
        _perm_config=SimpleNamespace(enforce=False),
        _enforce_zone_isolation=True,
        _health_state={"status": "indexing"},
    )

    response = TestClient(_app_with_health(fs)).get("/health")

    assert response.status_code == 200
    assert response.json()["workspace_index_status"] == "indexing"
