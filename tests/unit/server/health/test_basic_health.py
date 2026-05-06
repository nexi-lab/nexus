"""Tests for the public /health endpoint."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.core.health import router


def _make_client(nexus_fs: object | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.nexus_fs = nexus_fs
    app.state.api_key = None
    app.state.auth_provider = None
    return TestClient(app)


def test_health_skips_federation_probe_when_peers_unset(monkeypatch) -> None:
    calls = []

    def federation_is_initialized(kernel: object) -> bool:
        calls.append(kernel)
        return False

    monkeypatch.delenv("NEXUS_PEERS", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "nexus_runtime",
        SimpleNamespace(federation_is_initialized=federation_is_initialized),
    )

    mock_fs = MagicMock()
    mock_fs._kernel = object()

    resp = _make_client(mock_fs).get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    assert calls == []


def test_health_waits_for_federation_when_peers_set(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_PEERS", "node-a,node-b")
    monkeypatch.setitem(
        sys.modules,
        "nexus_runtime",
        SimpleNamespace(federation_is_initialized=lambda _kernel: False),
    )

    mock_fs = MagicMock()
    mock_fs._kernel = object()

    resp = _make_client(mock_fs).get("/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "starting"
