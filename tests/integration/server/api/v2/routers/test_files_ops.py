"""Tests for rename, copy, rename-batch, and copy-batch file operation endpoints."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.exceptions import (
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result


def _make_async(return_value):
    """Create an AsyncMock with a fixed return value."""
    m = AsyncMock(return_value=return_value)
    return m


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Create a mock NexusFS with async methods for rename/copy."""
    fs = MagicMock()

    # sys_rename is async
    fs.sys_rename = AsyncMock(return_value={})

    # rename_batch is async
    fs.rename_batch = AsyncMock(return_value={})

    # sys_stat is async, returns a dict
    fs.sys_stat = AsyncMock(
        return_value={
            "path": "/source.txt",
            "size": 1024,
            "content_id": "abc123",
            "mime_type": "text/plain",
            "is_directory": False,
        }
    )

    # sys_read is async, returns bytes
    fs.sys_read = AsyncMock(return_value=b"file content here")

    # write is async, returns metadata dict
    fs.write = AsyncMock(
        return_value={
            "content_id": "def456",
            "version": 1,
            "size": 17,
            "modified_at": "2026-03-16T00:00:00Z",
        }
    )

    # stream and write_stream are sync (used via asyncio.to_thread)
    fs.stream.return_value = iter([b"chunk1", b"chunk2"])
    fs.write_stream.return_value = {
        "content_id": "ghi789",
        "version": 1,
        "size": 12,
        "modified_at": "2026-03-16T00:00:00Z",
    }

    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    """Create a TestClient with mock FS and bypassed auth."""
    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)

    # Override auth to return an authenticated result
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }

    return TestClient(app)


# =============================================================================
# POST /rename — Happy Path
# =============================================================================


class TestRename:
    """Tests for POST /rename endpoint."""

    def test_rename_success(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Successful rename returns 200 with source and destination."""
        resp = client.post(
            "/rename",
            json={
                "source": "/old.txt",
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["source"] == "/old.txt"
        assert body["destination"] == "/new.txt"
        mock_fs.sys_rename.assert_awaited_once()
        call_args = mock_fs.sys_rename.call_args
        assert call_args[0][0] == "/old.txt"
        assert call_args[0][1] == "/new.txt"

    def test_rename_source_not_found(self, client: TestClient, mock_fs: MagicMock) -> None:
        """404 when source file does not exist."""
        mock_fs.sys_rename = AsyncMock(side_effect=NexusFileNotFoundError(path="/missing.txt"))
        resp = client.post(
            "/rename",
            json={
                "source": "/missing.txt",
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 404

    def test_rename_destination_exists(self, client: TestClient, mock_fs: MagicMock) -> None:
        """409 when destination already exists."""
        mock_fs.sys_rename = AsyncMock(
            side_effect=FileExistsError("Destination path already exists: /new.txt")
        )
        resp = client.post(
            "/rename",
            json={
                "source": "/old.txt",
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 409

    def test_rename_permission_denied(self, client: TestClient, mock_fs: MagicMock) -> None:
        """403 when user lacks permission."""
        mock_fs.sys_rename = AsyncMock(side_effect=NexusPermissionError("Access denied"))
        resp = client.post(
            "/rename",
            json={
                "source": "/old.txt",
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 403

    def test_rename_invalid_path(self, client: TestClient, mock_fs: MagicMock) -> None:
        """400 when path is invalid."""
        mock_fs.sys_rename = AsyncMock(side_effect=InvalidPathError("Invalid path"))
        resp = client.post(
            "/rename",
            json={
                "source": "../escape.txt",
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 400

    def test_rename_internal_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """500 for unexpected errors."""
        mock_fs.sys_rename = AsyncMock(side_effect=RuntimeError("disk failure"))
        resp = client.post(
            "/rename",
            json={
                "source": "/old.txt",
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 500

    def test_rename_missing_source_field(self, client: TestClient) -> None:
        """422 when source field is missing from request body."""
        resp = client.post(
            "/rename",
            json={
                "destination": "/new.txt",
            },
        )
        assert resp.status_code == 422

    def test_rename_missing_destination_field(self, client: TestClient) -> None:
        """422 when destination field is missing from request body."""
        resp = client.post(
            "/rename",
            json={
                "source": "/old.txt",
            },
        )
        assert resp.status_code == 422


# =============================================================================
# POST /copy — Happy Path + Size Threshold
# =============================================================================


class TestCopy:
    """Tests for POST /copy endpoint."""

    def test_copy_small_file(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Small file (< 10MB) uses read/write path."""
        mock_fs.sys_stat = AsyncMock(return_value={"size": 1024})
        mock_fs.sys_read = AsyncMock(return_value=b"small content")

        resp = client.post(
            "/copy",
            json={
                "source": "/source.txt",
                "destination": "/dest.txt",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["source"] == "/source.txt"
        assert body["destination"] == "/dest.txt"
        assert body["bytes_copied"] == len(b"small content")

        # Verify read/write were called (not stream)
        mock_fs.sys_read.assert_awaited_once()
        mock_fs.write.assert_awaited_once()
        mock_fs.stream.assert_not_called()
        mock_fs.write_stream.assert_not_called()

    def test_copy_large_file_uses_streaming(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Large file (>= 10MB) uses stream/write_stream path."""
        large_size = 15 * 1024 * 1024  # 15 MB
        mock_fs.sys_stat = AsyncMock(return_value={"size": large_size})
        mock_fs.stream.return_value = iter([b"chunk1", b"chunk2"])
        mock_fs.write_stream.return_value = {"size": large_size}

        resp = client.post(
            "/copy",
            json={
                "source": "/large.bin",
                "destination": "/large_copy.bin",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["bytes_copied"] == large_size

        # Verify streaming was used (not read/write)
        mock_fs.stream.assert_called_once()
        mock_fs.write_stream.assert_called_once()
        mock_fs.sys_read.assert_not_called()

    def test_copy_exactly_at_threshold_uses_streaming(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """File exactly at 10MB threshold uses streaming."""
        threshold = 10 * 1024 * 1024
        mock_fs.sys_stat = AsyncMock(return_value={"size": threshold})
        mock_fs.stream.return_value = iter([b"data"])
        mock_fs.write_stream.return_value = {"size": threshold}

        resp = client.post(
            "/copy",
            json={
                "source": "/exact.bin",
                "destination": "/exact_copy.bin",
            },
        )
        assert resp.status_code == 200
        mock_fs.stream.assert_called_once()
        mock_fs.write_stream.assert_called_once()

    def test_copy_just_below_threshold_uses_read_write(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """File just below 10MB threshold uses read/write."""
        just_below = 10 * 1024 * 1024 - 1
        mock_fs.sys_stat = AsyncMock(return_value={"size": just_below})
        content = b"x" * 100  # mock content (not actually just_below bytes)
        mock_fs.sys_read = AsyncMock(return_value=content)

        resp = client.post(
            "/copy",
            json={
                "source": "/almost.bin",
                "destination": "/almost_copy.bin",
            },
        )
        assert resp.status_code == 200
        mock_fs.sys_read.assert_awaited_once()
        mock_fs.write.assert_awaited_once()
        mock_fs.stream.assert_not_called()

    def test_copy_source_not_found(self, client: TestClient, mock_fs: MagicMock) -> None:
        """404 when source file does not exist."""
        mock_fs.sys_stat = AsyncMock(return_value=None)
        resp = client.post(
            "/copy",
            json={
                "source": "/missing.txt",
                "destination": "/dest.txt",
            },
        )
        assert resp.status_code == 404

    def test_copy_destination_exists(self, client: TestClient, mock_fs: MagicMock) -> None:
        """409 when destination already exists."""
        mock_fs.sys_stat = AsyncMock(return_value={"size": 100})
        mock_fs.sys_read = AsyncMock(return_value=b"data")
        mock_fs.write = AsyncMock(side_effect=FileExistsError("Destination already exists"))
        resp = client.post(
            "/copy",
            json={
                "source": "/source.txt",
                "destination": "/existing.txt",
            },
        )
        assert resp.status_code == 409

    def test_copy_permission_denied(self, client: TestClient, mock_fs: MagicMock) -> None:
        """403 when user lacks permission."""
        mock_fs.sys_stat = AsyncMock(side_effect=NexusPermissionError("Access denied"))
        resp = client.post(
            "/copy",
            json={
                "source": "/secret.txt",
                "destination": "/dest.txt",
            },
        )
        assert resp.status_code == 403

    def test_copy_invalid_path(self, client: TestClient, mock_fs: MagicMock) -> None:
        """400 for invalid paths."""
        mock_fs.sys_stat = AsyncMock(side_effect=InvalidPathError("Bad path"))
        resp = client.post(
            "/copy",
            json={
                "source": "../escape",
                "destination": "/dest.txt",
            },
        )
        assert resp.status_code == 400

    def test_copy_zero_size_file(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Zero-size file uses read/write (below threshold)."""
        mock_fs.sys_stat = AsyncMock(return_value={"size": 0})
        mock_fs.sys_read = AsyncMock(return_value=b"")

        resp = client.post(
            "/copy",
            json={
                "source": "/empty.txt",
                "destination": "/empty_copy.txt",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["bytes_copied"] == 0
        mock_fs.sys_read.assert_awaited_once()


# =============================================================================
# POST /rename-batch
# =============================================================================


class TestRenameBatch:
    """Tests for POST /rename-batch endpoint."""

    def test_rename_batch_all_success(self, client: TestClient, mock_fs: MagicMock) -> None:
        """All renames succeed."""
        mock_fs.rename_batch = AsyncMock(
            return_value={
                "/a.txt": {"success": True, "new_path": "/b.txt"},
                "/c.txt": {"success": True, "new_path": "/d.txt"},
            }
        )

        resp = client.post(
            "/rename-batch",
            json={
                "operations": [
                    {"source": "/a.txt", "destination": "/b.txt"},
                    {"source": "/c.txt", "destination": "/d.txt"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 2
        assert all(r["success"] for r in body["results"])
        assert body["results"][0]["source"] == "/a.txt"
        assert body["results"][0]["destination"] == "/b.txt"

    def test_rename_batch_partial_failure(self, client: TestClient, mock_fs: MagicMock) -> None:
        """One rename fails, others succeed."""
        mock_fs.rename_batch = AsyncMock(
            return_value={
                "/a.txt": {"success": True, "new_path": "/b.txt"},
                "/missing.txt": {"success": False, "error": "Source not found"},
            }
        )

        resp = client.post(
            "/rename-batch",
            json={
                "operations": [
                    {"source": "/a.txt", "destination": "/b.txt"},
                    {"source": "/missing.txt", "destination": "/new.txt"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        results = body["results"]
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert results[1]["error"] == "Source not found"

    def test_rename_batch_empty_operations(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Empty operations list returns empty results."""
        mock_fs.rename_batch = AsyncMock(return_value={})

        resp = client.post("/rename-batch", json={"operations": []})
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_rename_batch_exceeds_max(self, client: TestClient) -> None:
        """More than 50 operations returns 422 validation error."""
        ops = [{"source": f"/s{i}.txt", "destination": f"/d{i}.txt"} for i in range(51)]
        resp = client.post("/rename-batch", json={"operations": ops})
        assert resp.status_code == 422

    def test_rename_batch_permission_denied(self, client: TestClient, mock_fs: MagicMock) -> None:
        """403 when entire bulk operation is denied."""
        mock_fs.rename_batch = AsyncMock(side_effect=NexusPermissionError("Access denied"))
        resp = client.post(
            "/rename-batch",
            json={
                "operations": [{"source": "/a.txt", "destination": "/b.txt"}],
            },
        )
        assert resp.status_code == 403


# =============================================================================
# POST /copy-bulk
# =============================================================================


class TestCopyBulk:
    """Tests for POST /copy-bulk endpoint."""

    def test_copy_bulk_all_success(self, client: TestClient, mock_fs: MagicMock) -> None:
        """All copies succeed (small files)."""
        mock_fs.sys_stat = AsyncMock(return_value={"size": 100})
        mock_fs.sys_read = AsyncMock(return_value=b"content")
        mock_fs.write = AsyncMock(return_value={"size": 7})

        resp = client.post(
            "/copy-bulk",
            json={
                "operations": [
                    {"source": "/a.txt", "destination": "/a_copy.txt"},
                    {"source": "/b.txt", "destination": "/b_copy.txt"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 2
        assert all(r["success"] for r in body["results"])
        assert all(r["bytes_copied"] == 7 for r in body["results"])

    def test_copy_bulk_partial_failure(self, client: TestClient, mock_fs: MagicMock) -> None:
        """One copy fails (source not found), others succeed."""
        call_count = 0

        async def stat_side_effect(path, context=None):
            nonlocal call_count
            call_count += 1
            if "missing" in path:
                return None
            return {"size": 50}

        mock_fs.sys_stat = AsyncMock(side_effect=stat_side_effect)
        mock_fs.sys_read = AsyncMock(return_value=b"data")
        mock_fs.write = AsyncMock(return_value={"size": 4})

        resp = client.post(
            "/copy-bulk",
            json={
                "operations": [
                    {"source": "/good.txt", "destination": "/good_copy.txt"},
                    {"source": "/missing.txt", "destination": "/miss_copy.txt"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        results = body["results"]
        assert results[0]["success"] is True
        assert results[0]["bytes_copied"] == 4
        assert results[1]["success"] is False
        assert "not found" in results[1]["error"].lower()

    def test_copy_bulk_mixed_sizes(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Bulk copy with mixed small and large files uses appropriate paths."""
        call_count = 0

        async def stat_side_effect(path, context=None):
            nonlocal call_count
            call_count += 1
            if "large" in path:
                return {"size": 15 * 1024 * 1024}
            return {"size": 100}

        mock_fs.sys_stat = AsyncMock(side_effect=stat_side_effect)
        mock_fs.sys_read = AsyncMock(return_value=b"small data")
        mock_fs.write = AsyncMock(return_value={"size": 10})
        mock_fs.stream.return_value = iter([b"big"])
        mock_fs.write_stream.return_value = {"size": 15 * 1024 * 1024}

        resp = client.post(
            "/copy-bulk",
            json={
                "operations": [
                    {"source": "/small.txt", "destination": "/small_copy.txt"},
                    {"source": "/large.bin", "destination": "/large_copy.bin"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        results = body["results"]
        assert results[0]["success"] is True
        assert results[0]["bytes_copied"] == 10  # small file via read
        assert results[1]["success"] is True
        assert results[1]["bytes_copied"] == 15 * 1024 * 1024  # large file via stream

    def test_copy_bulk_empty_operations(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Empty operations list returns empty results."""
        resp = client.post("/copy-bulk", json={"operations": []})
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_copy_bulk_exceeds_max(self, client: TestClient) -> None:
        """More than 50 operations returns 422 validation error."""
        ops = [{"source": f"/s{i}.txt", "destination": f"/d{i}.txt"} for i in range(51)]
        resp = client.post("/copy-bulk", json={"operations": ops})
        assert resp.status_code == 422

    def test_copy_bulk_write_error_on_one(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Write failure on one operation does not block others."""
        mock_fs.sys_stat = AsyncMock(return_value={"size": 100})
        mock_fs.sys_read = AsyncMock(return_value=b"content")

        call_count = 0

        async def write_side_effect(path, buf, context=None):
            nonlocal call_count
            call_count += 1
            if "conflict" in path:
                raise FileExistsError("Already exists")
            return {"size": 7}

        mock_fs.write = AsyncMock(side_effect=write_side_effect)

        resp = client.post(
            "/copy-bulk",
            json={
                "operations": [
                    {"source": "/a.txt", "destination": "/conflict.txt"},
                    {"source": "/b.txt", "destination": "/ok.txt"},
                ],
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["success"] is False
        assert "Already exists" in results[0]["error"]
        assert results[1]["success"] is True
        assert results[1]["bytes_copied"] == 7
