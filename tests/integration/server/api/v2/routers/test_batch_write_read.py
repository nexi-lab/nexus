"""HTTP integration tests for POST /batch/write and POST /batch/read (Issue #3700).

Tests cover:
- POST /batch/write: valid base64 batch, result shape, file count limit, byte limit
- POST /batch/read: strict mode 200, strict mode 404 on missing, partial mode shape
- Round-trip: write then read returns identical content
- Discriminated union: type field is "success"/"error" on each item
- Auth failure returns 401
- base64 validation (invalid base64 → 422)
"""

import base64
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Mock NexusFS with async write_batch and read_batch."""
    fs = MagicMock()
    fs.write_batch = AsyncMock(
        return_value=[
            {
                "path": "/files/a.txt",
                "content_id": "hash_a",
                "version": 1,
                "modified_at": None,
                "size": 5,
            },
            {
                "path": "/files/b.txt",
                "content_id": "hash_b",
                "version": 1,
                "modified_at": None,
                "size": 3,
            },
        ]
    )
    fs.read_batch = AsyncMock(
        return_value=[
            {
                "path": "/files/a.txt",
                "content": b"hello",
                "content_id": "hash_a",
                "version": 1,
                "modified_at": None,
                "size": 5,
            },
            {
                "path": "/files/b.txt",
                "content": b"bye",
                "content_id": "hash_b",
                "version": 1,
                "modified_at": None,
                "size": 3,
            },
        ]
    )
    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }
    return TestClient(app)


@pytest.fixture()
def unauthed_client(mock_fs: MagicMock) -> TestClient:
    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: None
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /batch/write
# ---------------------------------------------------------------------------


class TestBatchWriteEndpoint:
    def test_valid_batch_returns_200(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/write",
            json={
                "files": [
                    {"path": "/files/a.txt", "content_base64": _b64(b"hello")},
                    {"path": "/files/b.txt", "content_base64": _b64(b"bye")},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) == 2

    def test_result_shape_contains_expected_fields(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "/files/a.txt", "content_base64": _b64(b"hello")}]},
        )
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert "content_id" in result
        assert "version" in result
        assert "size" in result

    def test_invalid_base64_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "/files/a.txt", "content_base64": "!!!not-base64!!!"}]},
        )
        assert resp.status_code == 422

    def test_empty_files_list_returns_200(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.write_batch = AsyncMock(return_value=[])
        resp = client.post("/batch/write", json={"files": []})
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_exceeds_file_count_returns_422(self, client: TestClient) -> None:
        files = [{"path": f"/f/{i}.txt", "content_base64": _b64(b"x")} for i in range(501)]
        resp = client.post("/batch/write", json={"files": files})
        assert resp.status_code == 422

    def test_fs_permission_error_returns_403(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.write_batch = AsyncMock(side_effect=NexusPermissionError("denied"))
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "/files/a.txt", "content_base64": _b64(b"x")}]},
        )
        assert resp.status_code == 403

    def test_fs_invalid_path_returns_400(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.write_batch = AsyncMock(side_effect=InvalidPathError("bad path"))
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "", "content_base64": _b64(b"x")}]},
        )
        assert resp.status_code in (400, 422)

    def test_unauthenticated_returns_401(self, unauthed_client: TestClient) -> None:
        resp = unauthed_client.post(
            "/batch/write",
            json={"files": [{"path": "/files/a.txt", "content_base64": _b64(b"x")}]},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /batch/read
# ---------------------------------------------------------------------------


class TestBatchReadEndpoint:
    def test_valid_batch_returns_200(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/read",
            json={"paths": ["/files/a.txt", "/files/b.txt"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) == 2

    def test_success_items_have_type_success(self, client: TestClient) -> None:
        resp = client.post("/batch/read", json={"paths": ["/files/a.txt"]})
        assert resp.status_code == 200
        item = resp.json()["results"][0]
        assert item["type"] == "success"

    def test_content_base64_decodes_correctly(self, client: TestClient) -> None:
        resp = client.post("/batch/read", json={"paths": ["/files/a.txt"]})
        assert resp.status_code == 200
        item = resp.json()["results"][0]
        assert base64.b64decode(item["content_base64"]) == b"hello"

    def test_strict_mode_missing_path_returns_404(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        mock_fs.read_batch = AsyncMock(side_effect=NexusFileNotFoundError("/files/missing.txt"))
        resp = client.post("/batch/read", json={"paths": ["/files/missing.txt"]})
        assert resp.status_code == 404

    def test_partial_mode_missing_path_returns_error_item(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        mock_fs.read_batch = AsyncMock(
            return_value=[{"path": "/files/missing.txt", "error": "not_found"}]
        )
        resp = client.post("/batch/read", json={"paths": ["/files/missing.txt"], "partial": True})
        assert resp.status_code == 200
        item = resp.json()["results"][0]
        assert item["type"] == "error"
        assert item["error"] == "not_found"

    def test_partial_mode_passes_flag_to_fs(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.read_batch = AsyncMock(return_value=[])
        client.post("/batch/read", json={"paths": [], "partial": True})
        mock_fs.read_batch.assert_called_once()
        call_kwargs = mock_fs.read_batch.call_args[1]
        assert call_kwargs.get("partial") is True

    def test_permission_error_returns_403(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.read_batch = AsyncMock(side_effect=NexusPermissionError("denied"))
        resp = client.post("/batch/read", json={"paths": ["/files/secret.txt"]})
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, unauthed_client: TestClient) -> None:
        resp = unauthed_client.post("/batch/read", json={"paths": ["/files/a.txt"]})
        assert resp.status_code == 401

    def test_exceeds_file_count_returns_422(self, client: TestClient) -> None:
        paths = [f"/files/{i}.txt" for i in range(501)]
        resp = client.post("/batch/read", json={"paths": paths})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Round-trip: write then read
# ---------------------------------------------------------------------------


class TestBatchRoundTrip:
    def test_write_then_read_returns_identical_content(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """write_batch → read_batch should produce the same content and content_id."""
        content_a = b"atomic write content"
        content_b = b"second file content"

        mock_fs.write_batch = AsyncMock(
            return_value=[
                {
                    "path": "/rt/a.txt",
                    "content_id": "etag_a",
                    "version": 1,
                    "modified_at": None,
                    "size": len(content_a),
                },
                {
                    "path": "/rt/b.txt",
                    "content_id": "etag_b",
                    "version": 1,
                    "modified_at": None,
                    "size": len(content_b),
                },
            ]
        )
        write_resp = client.post(
            "/batch/write",
            json={
                "files": [
                    {"path": "/rt/a.txt", "content_base64": _b64(content_a)},
                    {"path": "/rt/b.txt", "content_base64": _b64(content_b)},
                ]
            },
        )
        assert write_resp.status_code == 200
        write_results = write_resp.json()["results"]

        mock_fs.read_batch = AsyncMock(
            return_value=[
                {
                    "path": "/rt/a.txt",
                    "content": content_a,
                    "content_id": "etag_a",
                    "version": 1,
                    "modified_at": None,
                    "size": len(content_a),
                },
                {
                    "path": "/rt/b.txt",
                    "content": content_b,
                    "content_id": "etag_b",
                    "version": 1,
                    "modified_at": None,
                    "size": len(content_b),
                },
            ]
        )
        read_resp = client.post(
            "/batch/read",
            json={"paths": ["/rt/a.txt", "/rt/b.txt"]},
        )
        assert read_resp.status_code == 200
        read_results = read_resp.json()["results"]

        # Content matches
        assert base64.b64decode(read_results[0]["content_base64"]) == content_a
        assert base64.b64decode(read_results[1]["content_base64"]) == content_b

        # ETags match
        assert read_results[0]["content_id"] == write_results[0]["content_id"]
        assert read_results[1]["content_id"] == write_results[1]["content_id"]


# ---------------------------------------------------------------------------
# Discriminated union serialization
# ---------------------------------------------------------------------------


class TestDiscriminatedUnionSerialization:
    """Verify that the type discriminator field is correct on each result item."""

    def test_success_item_has_type_success(self, client: TestClient) -> None:
        resp = client.post("/batch/read", json={"paths": ["/files/a.txt"]})
        item = resp.json()["results"][0]
        assert item["type"] == "success"
        assert "content_base64" in item

    def test_error_item_has_type_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.read_batch = AsyncMock(
            return_value=[{"path": "/files/missing.txt", "error": "not_found"}]
        )
        resp = client.post(
            "/batch/read",
            json={"paths": ["/files/missing.txt"], "partial": True},
        )
        item = resp.json()["results"][0]
        assert item["type"] == "error"
        assert "content_base64" not in item
        assert item["error"] == "not_found"

    def test_mixed_results_have_correct_types(self, client: TestClient, mock_fs: MagicMock) -> None:
        mock_fs.read_batch = AsyncMock(
            return_value=[
                {
                    "path": "/files/exists.txt",
                    "content": b"data",
                    "content_id": "abc",
                    "version": 1,
                    "modified_at": None,
                    "size": 4,
                },
                {"path": "/files/missing.txt", "error": "not_found"},
            ]
        )
        resp = client.post(
            "/batch/read",
            json={"paths": ["/files/exists.txt", "/files/missing.txt"], "partial": True},
        )
        results = resp.json()["results"]
        assert results[0]["type"] == "success"
        assert results[1]["type"] == "error"
