"""Integration tests for batch router (Issue #1242).

Tests the full FastAPI router with a mock NexusFS via TestClient.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.server.api.v2.routers.batch import create_batch_router


def _make_mock_context() -> MagicMock:
    """Create a minimal mock OperationContext."""
    ctx = MagicMock()
    ctx.user_id = "test-user"
    ctx.zone_id = ROOT_ZONE_ID
    ctx.groups = []
    ctx.is_admin = False
    ctx.is_system = False
    return ctx


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Create a mock NexusFS with default behaviors."""
    fs = MagicMock()
    fs.read.return_value = b"hello world"
    fs.write.return_value = {
        "content_id": "abc123",
        "version": 1,
        "size": 11,
        "modified_at": "2026-02-17T00:00:00Z",
        "path": "/test.txt",
    }
    fs.delete.return_value = {"deleted": True, "path": "/test.txt"}
    fs.exists.return_value = True
    fs.list.return_value = ["/a.txt", "/b.txt"]
    fs.mkdir.return_value = None

    meta = MagicMock()
    meta.path = "/test.txt"
    meta.size = 11
    meta.content_id = "abc123"
    meta.version = 1
    meta.is_dir = False
    meta.created_at = None
    meta.modified_at = None
    fs.get_metadata.return_value = meta
    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    """Create a FastAPI test client with batch router (no auth import chain)."""
    app = FastAPI()
    router = create_batch_router(
        nexus_fs=mock_fs,
        get_context_override=lambda: _make_mock_context(),
    )
    app.include_router(router, prefix="/api/v2")
    return TestClient(app)


class TestBatchEndpointBasic:
    """Basic batch endpoint tests."""

    def test_single_read_operation(self, client: TestClient) -> None:
        response = client.post(
            "/api/v2/batch",
            json={"operations": [{"op": "read", "path": "/test.txt"}]},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["index"] == 0
        assert data["results"][0]["status"] == 200

    def test_multiple_operations(self, client: TestClient) -> None:
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/test.txt"},
                    {"op": "write", "path": "/new.txt", "content": "data"},
                    {"op": "exists", "path": "/test.txt"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 3
        for i, result in enumerate(data["results"]):
            assert result["index"] == i

    def test_all_operation_types(self, client: TestClient) -> None:
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/test.txt"},
                    {"op": "write", "path": "/new.txt", "content": "data"},
                    {"op": "delete", "path": "/old.txt"},
                    {"op": "stat", "path": "/test.txt"},
                    {"op": "exists", "path": "/test.txt"},
                    {"op": "list", "path": "/"},
                    {"op": "mkdir", "path": "/newdir"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 7
        for result in data["results"]:
            assert result["status"] < 400, f"Op {result['index']} failed: {result.get('error')}"

    def test_write_with_base64_encoding(self, client: TestClient) -> None:
        import base64

        content = base64.b64encode(b"binary data").decode("ascii")
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "write", "path": "/bin.dat", "content": content, "encoding": "base64"},
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["status"] == 201


class TestBatchEndpointErrors:
    """Error handling tests."""

    def test_empty_operations_returns_422(self, client: TestClient) -> None:
        response = client.post("/api/v2/batch", json={"operations": []})
        assert response.status_code == 422

    def test_too_many_operations_returns_422(self, client: TestClient) -> None:
        ops = [{"op": "read", "path": f"/file{i}.txt"} for i in range(51)]
        response = client.post("/api/v2/batch", json={"operations": ops})
        assert response.status_code == 422

    def test_invalid_op_type_returns_422(self, client: TestClient) -> None:
        response = client.post(
            "/api/v2/batch",
            json={"operations": [{"op": "invalid", "path": "/test.txt"}]},
        )
        assert response.status_code == 422

    def test_partial_failure_returns_200(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.write.side_effect = NexusPermissionError("denied")
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/test.txt"},
                    {"op": "write", "path": "/protected.txt", "content": "data"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 200
        assert data["results"][1]["status"] == 403


class TestBatchStopOnError:
    """stop_on_error flag tests."""

    def test_stop_on_error_skips_remaining(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.read.side_effect = NexusFileNotFoundError(path="/missing.txt")
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/missing.txt"},
                    {"op": "write", "path": "/other.txt", "content": "data"},
                ],
                "stop_on_error": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 404
        assert data["results"][1]["status"] == 424

    def test_stop_on_error_false_continues(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.read.side_effect = NexusFileNotFoundError(path="/missing.txt")
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/missing.txt"},
                    {"op": "exists", "path": "/test.txt"},
                ],
                "stop_on_error": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 404
        assert data["results"][1]["status"] == 200


class TestBatchPayloadValidation:
    """Payload size validation tests."""

    def test_oversized_payload_returns_422(self, client: TestClient) -> None:
        large = "x" * (10 * 1024 * 1024 + 1)
        response = client.post(
            "/api/v2/batch",
            json={"operations": [{"op": "write", "path": "/big.txt", "content": large}]},
        )
        assert response.status_code == 422
