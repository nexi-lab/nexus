"""Tests for the api_error_handler decorator.

Issue #995: API versioning — code quality improvements.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.error_handling import api_error_handler

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app() -> FastAPI:
    """Build a test app with endpoints exercising the decorator."""
    router = APIRouter(prefix="/api/v2/test", tags=["test"])

    @router.get("/ok")
    @api_error_handler(context="get item")
    async def get_ok() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/value-error")
    @api_error_handler(context="get item")
    async def get_value_error() -> dict[str, str]:
        raise ValueError("Item not found")

    @router.get("/key-error")
    @api_error_handler(context="get item")
    async def get_key_error() -> dict[str, str]:
        raise KeyError("widget-123")

    @router.get("/permission-error")
    @api_error_handler(context="get item")
    async def get_perm_error() -> dict[str, str]:
        raise PermissionError("not allowed")

    @router.get("/generic-error")
    @api_error_handler(context="process item")
    async def get_generic_error() -> dict[str, str]:
        raise RuntimeError("unexpected failure")

    @router.get("/http-exception")
    @api_error_handler(context="get item")
    async def get_http_exception() -> dict[str, str]:
        raise HTTPException(status_code=409, detail="conflict")

    @router.get("/custom-map")
    @api_error_handler(
        context="custom op",
        error_map={TypeError: (422, "Bad type: {error}")},
    )
    async def get_custom_map() -> dict[str, str]:
        raise TypeError("expected int, got str")

    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# =============================================================================
# Tests
# =============================================================================


class TestApiErrorHandler:
    def test_success_passthrough(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/ok")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_value_error_maps_to_404(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/value-error")
        assert resp.status_code == 404
        assert "Item not found" in resp.json()["detail"]

    def test_key_error_maps_to_404(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/key-error")
        assert resp.status_code == 404
        assert "widget-123" in resp.json()["detail"]

    def test_permission_error_maps_to_403(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/permission-error")
        assert resp.status_code == 403
        assert "not allowed" in resp.json()["detail"]

    def test_generic_exception_maps_to_500(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/generic-error")
        assert resp.status_code == 500
        assert "Failed to process item" in resp.json()["detail"]

    def test_http_exception_passthrough(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/http-exception")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "conflict"

    def test_custom_error_map(self, client: TestClient) -> None:
        resp = client.get("/api/v2/test/custom-map")
        assert resp.status_code == 422
        assert "Bad type" in resp.json()["detail"]

    def test_context_in_error_message(self, client: TestClient) -> None:
        """Generic errors include the context string in the detail."""
        resp = client.get("/api/v2/test/generic-error")
        assert "process item" in resp.json()["detail"]
