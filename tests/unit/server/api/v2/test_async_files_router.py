"""Tests for Async Files REST API router.

Tests for issue #940: Async File Operations REST API.
Covers all 9 endpoints: write, read, delete, exists, list,
mkdir, metadata, batch-read, stream.

Uses mocked AsyncNexusFS to test router logic in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)


def _make_mock_get_auth_result(auth_result):
    """Create a mock get_auth_result dependency function."""

    async def mock_get_auth_result():
        return auth_result

    return mock_get_auth_result


def _make_mock_get_operation_context():
    """Create a mock get_operation_context function."""
    mock_ctx = MagicMock()
    mock_ctx.user = "test-agent"
    mock_ctx.zone_id = "default"

    def mock_get_operation_context(auth_result):
        return mock_ctx

    return mock_get_operation_context


# =============================================================================
# Helpers
# =============================================================================


@dataclass
class FakeMetadata:
    """Minimal metadata object mimicking AsyncNexusFS metadata result."""

    path: str = "/test.txt"
    size: int = 100
    etag: str = "abc123"
    version: int = 1
    is_dir: bool = False
    created_at: datetime | None = None
    modified_at: datetime | None = None
    mime_type: str | None = "application/octet-stream"

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        if self.modified_at is None:
            self.modified_at = datetime(2026, 1, 1, tzinfo=UTC)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_async_fs():
    """Mock AsyncNexusFS with all async methods."""
    fs = AsyncMock()

    fs.write.return_value = {
        "etag": "abc123",
        "version": 1,
        "size": 12,
        "modified_at": "2026-01-01T00:00:00",
    }
    fs.read.return_value = b"file content"
    fs.delete.return_value = {"deleted": True, "path": "/test.txt"}
    fs.exists.return_value = True
    fs.list_dir.return_value = ["file1.txt", "file2.txt"]
    fs.mkdir.return_value = None
    fs.get_metadata.return_value = FakeMetadata()
    fs.batch_read.return_value = {
        "/a.txt": b"content a",
        "/b.txt": b"content b",
    }

    async def _stream_chunks(*args, **kwargs):
        yield b"chunk1"
        yield b"chunk2"

    fs.stream_read = _stream_chunks
    fs.stream_read_range = _stream_chunks

    return fs


@pytest.fixture
def mock_auth_result():
    """Mock authenticated auth result."""
    return {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "test-agent",
        "zone_id": "default",
    }


@pytest.fixture
def app(mock_async_fs, mock_auth_result):
    """Create test FastAPI app with async files router."""
    mock_auth_fn = _make_mock_get_auth_result(mock_auth_result)
    mock_ctx_fn = _make_mock_get_operation_context()

    with (
        patch(
            "nexus.server.fastapi_server.get_auth_result",
            mock_auth_fn,
        ),
        patch(
            "nexus.server.fastapi_server.get_operation_context",
            mock_ctx_fn,
        ),
    ):
        from nexus.server.api.v2.routers.async_files import create_async_files_router

        router = create_async_files_router(async_fs=mock_async_fs)
        test_app = FastAPI()
        test_app.include_router(router, prefix="/api/v2/files")
        yield test_app


@pytest.fixture
def client(app):
    """Test client."""
    return TestClient(app)


# =============================================================================
# Test: POST /api/v2/files/write
# =============================================================================


class TestWriteFile:
    def test_write_success(self, client, mock_async_fs):
        response = client.post(
            "/api/v2/files/write",
            json={"path": "/test.txt", "content": "hello world"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["etag"] == "abc123"
        assert data["version"] == 1
        assert data["size"] == 12

    def test_write_base64_encoding(self, client, mock_async_fs):
        response = client.post(
            "/api/v2/files/write",
            json={
                "path": "/binary.bin",
                "content": "aGVsbG8=",
                "encoding": "base64",
            },
        )
        assert response.status_code == 200

    def test_write_with_if_match(self, client, mock_async_fs):
        response = client.post(
            "/api/v2/files/write",
            json={
                "path": "/test.txt",
                "content": "updated",
                "if_match": "old-etag",
            },
        )
        assert response.status_code == 200
        call_kwargs = mock_async_fs.write.call_args.kwargs
        assert call_kwargs["if_match"] == "old-etag"

    def test_write_with_if_none_match(self, client, mock_async_fs):
        response = client.post(
            "/api/v2/files/write",
            json={
                "path": "/new.txt",
                "content": "new file",
                "if_none_match": True,
            },
        )
        assert response.status_code == 200
        call_kwargs = mock_async_fs.write.call_args.kwargs
        assert call_kwargs["if_none_match"] is True

    def test_write_permission_denied(self, client, mock_async_fs):
        mock_async_fs.write.side_effect = NexusPermissionError("Access denied")
        response = client.post(
            "/api/v2/files/write",
            json={"path": "/restricted.txt", "content": "test"},
        )
        assert response.status_code == 403

    def test_write_invalid_path(self, client, mock_async_fs):
        mock_async_fs.write.side_effect = InvalidPathError("Invalid path")
        response = client.post(
            "/api/v2/files/write",
            json={"path": "../escape", "content": "test"},
        )
        assert response.status_code == 400

    def test_write_conflict(self, client, mock_async_fs):
        mock_async_fs.write.side_effect = ConflictError("/test.txt", "old-etag", "new-etag")
        response = client.post(
            "/api/v2/files/write",
            json={"path": "/test.txt", "content": "test", "if_match": "wrong"},
        )
        assert response.status_code == 409

    def test_write_file_exists(self, client, mock_async_fs):
        mock_async_fs.write.side_effect = FileExistsError("File exists")
        response = client.post(
            "/api/v2/files/write",
            json={"path": "/test.txt", "content": "test", "if_none_match": True},
        )
        assert response.status_code == 409

    def test_write_server_error(self, client, mock_async_fs):
        mock_async_fs.write.side_effect = RuntimeError("Unexpected")
        response = client.post(
            "/api/v2/files/write",
            json={"path": "/test.txt", "content": "test"},
        )
        assert response.status_code == 500


# =============================================================================
# Test: GET /api/v2/files/read
# =============================================================================


class TestReadFile:
    def test_read_success(self, client, mock_async_fs):
        response = client.get("/api/v2/files/read", params={"path": "/test.txt"})
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "file content"

    def test_read_with_metadata(self, client, mock_async_fs):
        mock_async_fs.read.return_value = {
            "content": b"file content",
            "etag": "abc123",
            "version": 1,
            "modified_at": "2026-01-01",
            "size": 12,
        }
        response = client.get(
            "/api/v2/files/read",
            params={"path": "/test.txt", "include_metadata": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "file content"
        assert data["etag"] == "abc123"

    def test_read_not_found(self, client, mock_async_fs):
        mock_async_fs.read.side_effect = NexusFileNotFoundError(path="/missing.txt")
        response = client.get("/api/v2/files/read", params={"path": "/missing.txt"})
        assert response.status_code == 404

    def test_read_permission_denied(self, client, mock_async_fs):
        mock_async_fs.read.side_effect = NexusPermissionError("Access denied")
        response = client.get("/api/v2/files/read", params={"path": "/secret.txt"})
        assert response.status_code == 403

    def test_read_invalid_path(self, client, mock_async_fs):
        mock_async_fs.read.side_effect = InvalidPathError("Invalid path")
        response = client.get("/api/v2/files/read", params={"path": "../escape"})
        assert response.status_code == 400


# =============================================================================
# Test: DELETE /api/v2/files/delete
# =============================================================================


class TestDeleteFile:
    def test_delete_success(self, client, mock_async_fs):
        response = client.delete("/api/v2/files/delete", params={"path": "/test.txt"})
        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] is True
        assert data["path"] == "/test.txt"

    def test_delete_not_found(self, client, mock_async_fs):
        mock_async_fs.delete.side_effect = NexusFileNotFoundError(path="/missing.txt")
        response = client.delete("/api/v2/files/delete", params={"path": "/missing.txt"})
        assert response.status_code == 404

    def test_delete_permission_denied(self, client, mock_async_fs):
        mock_async_fs.delete.side_effect = NexusPermissionError("Access denied")
        response = client.delete("/api/v2/files/delete", params={"path": "/protected.txt"})
        assert response.status_code == 403


# =============================================================================
# Test: GET /api/v2/files/exists
# =============================================================================


class TestFileExists:
    def test_exists_true(self, client, mock_async_fs):
        response = client.get("/api/v2/files/exists", params={"path": "/test.txt"})
        assert response.status_code == 200
        assert response.json()["exists"] is True

    def test_exists_false(self, client, mock_async_fs):
        mock_async_fs.exists.return_value = False
        response = client.get("/api/v2/files/exists", params={"path": "/nope.txt"})
        assert response.status_code == 200
        assert response.json()["exists"] is False

    def test_exists_permission_denied(self, client, mock_async_fs):
        mock_async_fs.exists.side_effect = NexusPermissionError("Access denied")
        response = client.get("/api/v2/files/exists", params={"path": "/secret.txt"})
        assert response.status_code == 403


# =============================================================================
# Test: GET /api/v2/files/list
# =============================================================================


class TestListDirectory:
    def test_list_success(self, client, mock_async_fs):
        response = client.get("/api/v2/files/list", params={"path": "/docs"})
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == ["file1.txt", "file2.txt"]

    def test_list_not_found(self, client, mock_async_fs):
        mock_async_fs.list_dir.side_effect = NexusFileNotFoundError(path="/missing")
        response = client.get("/api/v2/files/list", params={"path": "/missing"})
        assert response.status_code == 404

    def test_list_permission_denied(self, client, mock_async_fs):
        mock_async_fs.list_dir.side_effect = NexusPermissionError("Access denied")
        response = client.get("/api/v2/files/list", params={"path": "/private"})
        assert response.status_code == 403


# =============================================================================
# Test: POST /api/v2/files/mkdir
# =============================================================================


class TestMkdir:
    def test_mkdir_success(self, client, mock_async_fs):
        response = client.post(
            "/api/v2/files/mkdir",
            json={"path": "/new-dir", "parents": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["created"] is True
        assert data["path"] == "/new-dir"

    def test_mkdir_already_exists(self, client, mock_async_fs):
        mock_async_fs.mkdir.side_effect = FileExistsError("Already exists")
        response = client.post(
            "/api/v2/files/mkdir",
            json={"path": "/existing-dir"},
        )
        assert response.status_code == 409

    def test_mkdir_parent_not_found(self, client, mock_async_fs):
        mock_async_fs.mkdir.side_effect = FileNotFoundError("Parent not found")
        response = client.post(
            "/api/v2/files/mkdir",
            json={"path": "/no-parent/sub", "parents": False},
        )
        assert response.status_code == 404

    def test_mkdir_permission_denied(self, client, mock_async_fs):
        mock_async_fs.mkdir.side_effect = NexusPermissionError("Access denied")
        response = client.post(
            "/api/v2/files/mkdir",
            json={"path": "/protected-dir"},
        )
        assert response.status_code == 403


# =============================================================================
# Test: GET /api/v2/files/metadata
# =============================================================================


class TestMetadata:
    def test_metadata_success(self, client, mock_async_fs):
        response = client.get("/api/v2/files/metadata", params={"path": "/test.txt"})
        assert response.status_code == 200
        data = response.json()
        assert data["path"] == "/test.txt"
        assert data["size"] == 100
        assert data["etag"] == "abc123"
        assert data["version"] == 1
        assert data["is_directory"] is False

    def test_metadata_not_found(self, client, mock_async_fs):
        mock_async_fs.get_metadata.return_value = None
        response = client.get("/api/v2/files/metadata", params={"path": "/missing.txt"})
        assert response.status_code == 404

    def test_metadata_permission_denied(self, client, mock_async_fs):
        mock_async_fs.get_metadata.side_effect = NexusPermissionError("Access denied")
        response = client.get("/api/v2/files/metadata", params={"path": "/secret.txt"})
        assert response.status_code == 403


# =============================================================================
# Test: POST /api/v2/files/batch-read
# =============================================================================


class TestBatchRead:
    def test_batch_read_success(self, client, mock_async_fs):
        response = client.post(
            "/api/v2/files/batch-read",
            json={"paths": ["/a.txt", "/b.txt"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "/a.txt" in data
        assert data["/a.txt"]["content"] == "content a"
        assert data["/b.txt"]["content"] == "content b"

    def test_batch_read_with_missing(self, client, mock_async_fs):
        mock_async_fs.batch_read.return_value = {
            "/a.txt": b"content a",
            "/missing.txt": None,
        }
        response = client.post(
            "/api/v2/files/batch-read",
            json={"paths": ["/a.txt", "/missing.txt"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["/a.txt"]["content"] == "content a"
        assert data["/missing.txt"] is None

    def test_batch_read_permission_denied(self, client, mock_async_fs):
        mock_async_fs.batch_read.side_effect = NexusPermissionError("Access denied")
        response = client.post(
            "/api/v2/files/batch-read",
            json={"paths": ["/secret.txt"]},
        )
        assert response.status_code == 403


# =============================================================================
# Test: GET /api/v2/files/stream
# =============================================================================


class TestStreamFile:
    def test_stream_success(self, client, mock_async_fs):
        response = client.get("/api/v2/files/stream", params={"path": "/test.txt"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert b"chunk1" in response.content
        assert b"chunk2" in response.content

    def test_stream_not_found(self, client, mock_async_fs):
        mock_async_fs.get_metadata.return_value = None
        response = client.get("/api/v2/files/stream", params={"path": "/missing.txt"})
        assert response.status_code == 404

    def test_stream_permission_denied(self, client, mock_async_fs):
        mock_async_fs.get_metadata.side_effect = NexusPermissionError("Access denied")
        response = client.get("/api/v2/files/stream", params={"path": "/secret.txt"})
        assert response.status_code == 403


# =============================================================================
# Test: FS not initialized (503)
# =============================================================================


class TestFsNotInitialized:
    def test_returns_error_when_fs_is_none(self):
        """When neither async_fs nor get_fs returns a value, endpoints return an error.

        Note: _get_fs raises HTTPException(503) but the endpoint's generic
        except Exception handler catches it and returns 500. This is a known
        limitation that will be fixed when we add the @api_error_handler
        decorator (Phase 3, Issue #5).
        """
        mock_auth_fn = _make_mock_get_auth_result({"authenticated": True})
        mock_ctx_fn = _make_mock_get_operation_context()

        with (
            patch(
                "nexus.server.fastapi_server.get_auth_result",
                mock_auth_fn,
            ),
            patch(
                "nexus.server.fastapi_server.get_operation_context",
                mock_ctx_fn,
            ),
        ):
            from nexus.server.api.v2.routers.async_files import create_async_files_router

            router = create_async_files_router(async_fs=None, get_fs=lambda: None)
            test_app = FastAPI()
            test_app.include_router(router, prefix="/api/v2/files")
            test_client = TestClient(test_app, raise_server_exceptions=False)

            response = test_client.get("/api/v2/files/read", params={"path": "/test.txt"})
            # _get_fs raises HTTPException(503) but the generic except catches it
            assert response.status_code in (500, 503)
