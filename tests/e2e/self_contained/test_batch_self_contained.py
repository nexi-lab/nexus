"""Self-contained E2E tests for batch endpoint (Issue #1242).

Uses FastAPI TestClient with mock NexusFS — no Rust extension needed.
Validates full router wiring, sequential execution, and performance.
"""

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.routers.batch import create_batch_router


def _make_mock_fs() -> MagicMock:
    """Create a mock NexusFS with realistic behaviors."""
    fs = MagicMock()
    # Simulate realistic read latency
    fs.read.return_value = b"hello world content here"
    fs.write.return_value = {
        "etag": "e3b0c44298fc1c14",
        "version": 1,
        "size": 24,
        "modified_at": "2026-02-17T00:00:00Z",
        "path": "/test.txt",
    }
    fs.delete.return_value = {"deleted": True, "path": "/test.txt"}
    fs.exists.return_value = True
    fs.list.return_value = ["/file1.txt", "/file2.txt", "/dir/"]
    fs.mkdir.return_value = None

    meta = MagicMock()
    meta.path = "/test.txt"
    meta.size = 24
    meta.etag = "e3b0c44298fc1c14"
    meta.version = 1
    meta.is_dir = False
    meta.created_at = None
    meta.modified_at = None
    fs.get_metadata.return_value = meta
    return fs


def _make_mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = "e2e-test-user"
    ctx.zone_id = ROOT_ZONE_ID
    ctx.groups = ["testers"]
    ctx.is_admin = False
    ctx.is_system = False
    return ctx


@pytest.fixture()
def mock_fs() -> MagicMock:
    return _make_mock_fs()


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    app = FastAPI()
    router = create_batch_router(
        nexus_fs=mock_fs,
        get_context_override=lambda: _make_mock_context(),
    )
    app.include_router(router, prefix="/api/v2")
    return TestClient(app)


class TestBatchSelfContainedE2E:
    """Full-stack E2E tests using in-process TestClient."""

    def test_all_seven_operations_wired(self, client: TestClient) -> None:
        """All 7 VFS operation types work through the full router stack."""
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/test.txt"},
                    {"op": "write", "path": "/new.txt", "content": "hello"},
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

        # Verify each op type returned correct status
        expected_statuses = [200, 201, 200, 200, 200, 200, 201]
        for i, expected in enumerate(expected_statuses):
            assert data["results"][i]["status"] == expected, (
                f"Op {i} ({data['results'][i]}) expected {expected}"
            )

    def test_sequential_write_then_read(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Write then read in single batch verifies sequential execution."""
        execution_order: list[str] = []

        def tracking_write(*args: Any, **kwargs: Any) -> dict:
            execution_order.append("write")
            return {
                "etag": "new",
                "version": 1,
                "size": 5,
                "modified_at": "2026-02-17T00:00:00Z",
                "path": "/data.txt",
            }

        def tracking_read(*args: Any, **kwargs: Any) -> bytes:
            execution_order.append("read")
            return b"hello"

        mock_fs.write.side_effect = tracking_write
        mock_fs.read.side_effect = tracking_read

        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "write", "path": "/data.txt", "content": "hello"},
                    {"op": "read", "path": "/data.txt"},
                ],
            },
        )
        assert response.status_code == 200
        assert execution_order == ["write", "read"]

    def test_stop_on_error_cascading(self, client: TestClient, mock_fs: MagicMock) -> None:
        """stop_on_error cascades 424 to all remaining operations."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        mock_fs.read.side_effect = NexusFileNotFoundError(path="/missing.txt")

        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/missing.txt"},
                    {"op": "exists", "path": "/a.txt"},
                    {"op": "exists", "path": "/b.txt"},
                    {"op": "exists", "path": "/c.txt"},
                ],
                "stop_on_error": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 404
        assert data["results"][1]["status"] == 424
        assert data["results"][2]["status"] == 424
        assert data["results"][3]["status"] == 424

    def test_partial_failure_continues(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Partial failure without stop_on_error continues processing."""
        from nexus.contracts.exceptions import NexusPermissionError

        call_count = 0

        def permission_gated_read(path: str, **kwargs: Any) -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise NexusPermissionError("Access denied")
            return b"content"

        mock_fs.read.side_effect = permission_gated_read

        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/a.txt"},
                    {"op": "read", "path": "/b.txt"},
                    {"op": "read", "path": "/c.txt"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 200
        assert data["results"][1]["status"] == 403
        assert data["results"][2]["status"] == 200

    def test_response_format_matches_spec(self, client: TestClient) -> None:
        """Response format matches the issue #1242 specification."""
        response = client.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/test.txt"},
                    {"op": "stat", "path": "/test.txt"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Top-level structure
        assert "results" in data
        assert isinstance(data["results"], list)

        # Each result has index, status, and either data or error
        for result in data["results"]:
            assert "index" in result
            assert "status" in result
            assert isinstance(result["index"], int)
            assert isinstance(result["status"], int)
            # Successful ops have data, failed ops have error
            if result["status"] < 400:
                assert result["data"] is not None


class TestBatchPerformanceBenchmark:
    """In-process performance benchmark: batch vs individual requests."""

    def test_batch_reduces_framework_overhead(self, client: TestClient) -> None:
        """Batch of 20 reads has less total framework overhead than 20 individual requests.

        This measures the per-request FastAPI/Pydantic overhead eliminated by batching.
        """
        n_ops = 20

        # Measure N individual read requests
        start = time.perf_counter()
        for _ in range(n_ops):
            resp = client.post(
                "/api/v2/batch",
                json={"operations": [{"op": "read", "path": "/test.txt"}]},
            )
            assert resp.status_code == 200
        individual_time = time.perf_counter() - start

        # Measure 1 batch of N reads
        start = time.perf_counter()
        resp = client.post(
            "/api/v2/batch",
            json={
                "operations": [{"op": "read", "path": "/test.txt"} for _ in range(n_ops)],
            },
        )
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == n_ops
        batch_time = time.perf_counter() - start

        # Batch should be at least 2x faster (eliminates 19 request/response cycles)
        speedup = individual_time / batch_time
        assert speedup > 2.0, (
            f"Batch ({batch_time:.4f}s) not 2x faster than "
            f"individual ({individual_time:.4f}s), speedup={speedup:.2f}x"
        )
