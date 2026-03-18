"""Tests for write_mode query parameter on /write endpoint (Issue #2929)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Create a mock NexusFS with async methods."""
    fs = MagicMock()
    # write is async — must return a coroutine
    fs.write = AsyncMock(
        return_value={
            "etag": "abc123",
            "version": 1,
            "size": 11,
            "modified_at": "2026-02-17T00:00:00Z",
            "path": "/test.txt",
        }
    )
    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    """Create a TestClient with mock FS and bypassed auth."""
    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)

    # Override auth to return an authenticated result (bypasses real auth)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }

    return TestClient(app)


class TestWriteModeQueryParam:
    """write_mode is a query parameter, not in the body."""

    def test_write_without_write_mode(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Default write (no write_mode) works."""
        resp = client.post("/write", json={"path": "/test.txt", "content": "hello"})
        assert resp.status_code == 200
        mock_fs.write.assert_called_once()
        call_kwargs = mock_fs.write.call_args[1]
        assert "consistency" not in call_kwargs

    def test_write_with_sync_mode(self, client: TestClient, mock_fs: MagicMock) -> None:
        """write_mode=sync passes consistency='sc' to fs.write."""
        resp = client.post("/write?write_mode=sync", json={"path": "/test.txt", "content": "hello"})
        assert resp.status_code == 200
        call_kwargs = mock_fs.write.call_args[1]
        assert call_kwargs["consistency"] == "sc"

    def test_write_with_async_mode(self, client: TestClient, mock_fs: MagicMock) -> None:
        """write_mode=async passes consistency='ec' to fs.write."""
        resp = client.post(
            "/write?write_mode=async", json={"path": "/test.txt", "content": "hello"}
        )
        assert resp.status_code == 200
        call_kwargs = mock_fs.write.call_args[1]
        assert call_kwargs["consistency"] == "ec"

    def test_write_with_invalid_mode(self, client: TestClient) -> None:
        """Invalid write_mode returns 400."""
        resp = client.post(
            "/write?write_mode=invalid", json={"path": "/test.txt", "content": "hello"}
        )
        assert resp.status_code == 400
        assert "Invalid write_mode" in resp.json()["detail"]

    def test_write_mode_not_in_body(self, client: TestClient, mock_fs: MagicMock) -> None:
        """write_mode in the body is ignored (it's a query param only)."""
        resp = client.post(
            "/write",
            json={"path": "/test.txt", "content": "hello", "write_mode": "sync"},
        )
        assert resp.status_code == 200
        call_kwargs = mock_fs.write.call_args[1]
        # write_mode in body should NOT set consistency
        assert "consistency" not in call_kwargs
