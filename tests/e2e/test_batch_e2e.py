"""End-to-end tests for batch endpoint (Issue #1242).

Starts a real Nexus server with permissions enabled and validates:
1. Authenticated batch operations
2. Sequential dependency (write then read)
3. Mixed valid/invalid operations
4. stop_on_error with failure
5. Performance: 20-op batch vs 20 individual requests
6. Stat metadata correctness

Requires: Rust Raft extension (maturin develop -m rust/raft/Cargo.toml)
"""

import time

import httpx
import pytest

# Skip all tests if Raft metastore isn't available (requires Rust build)
try:
    from nexus.storage.raft_metadata_store import RaftMetadataStore  # noqa: F401

    _has_raft = True
except (ImportError, OSError):
    _has_raft = False

pytestmark = pytest.mark.skipif(
    not _has_raft, reason="Raft metastore not available (build with maturin)"
)


@pytest.fixture()
def auth_headers(nexus_server: dict) -> dict[str, str]:
    """Get auth headers using the test API key."""
    return {"Authorization": "Bearer test-e2e-api-key-12345"}


class TestBatchE2EAuth:
    def test_batch_read_with_auth(
        self, test_app: httpx.Client, auth_headers: dict[str, str]
    ) -> None:
        """Batch read operations pass auth context through correctly."""
        for i in range(3):
            test_app.put(
                f"/api/v1/files/batch_test_{i}.txt",
                content=f"content {i}",
                headers=auth_headers,
            )
        response = test_app.post(
            "/api/v2/batch",
            json={
                "operations": [{"op": "read", "path": f"/batch_test_{i}.txt"} for i in range(3)],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 3
        for result in data["results"]:
            assert result["status"] == 200


class TestBatchE2ESequential:
    def test_write_then_read_sequential(
        self, test_app: httpx.Client, auth_headers: dict[str, str]
    ) -> None:
        """Write and immediately read the same file in a single batch."""
        response = test_app.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "write", "path": "/seq_test.txt", "content": "sequential write"},
                    {"op": "read", "path": "/seq_test.txt"},
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 201
        assert data["results"][1]["status"] == 200


class TestBatchE2EMixed:
    def test_mixed_valid_invalid_operations(
        self, test_app: httpx.Client, auth_headers: dict[str, str]
    ) -> None:
        """Valid ops succeed while invalid ops fail, all in one batch."""
        test_app.put("/api/v1/files/mixed_test.txt", content="exists", headers=auth_headers)
        response = test_app.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/mixed_test.txt"},
                    {"op": "read", "path": "/nonexistent.txt"},
                    {"op": "exists", "path": "/mixed_test.txt"},
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 200
        assert data["results"][1]["status"] == 404
        assert data["results"][2]["status"] == 200


class TestBatchE2EStopOnError:
    def test_stop_on_error_halts_batch(
        self, test_app: httpx.Client, auth_headers: dict[str, str]
    ) -> None:
        """stop_on_error=true halts the batch on first failure."""
        response = test_app.post(
            "/api/v2/batch",
            json={
                "operations": [
                    {"op": "read", "path": "/does_not_exist.txt"},
                    {"op": "exists", "path": "/anything.txt"},
                ],
                "stop_on_error": True,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"][0]["status"] == 404
        assert data["results"][1]["status"] == 424


class TestBatchE2EPerformance:
    def test_batch_faster_than_individual(
        self, test_app: httpx.Client, auth_headers: dict[str, str]
    ) -> None:
        """Batch of 20 reads should be faster than 20 individual requests."""
        for i in range(20):
            test_app.put(
                f"/api/v1/files/perf_{i}.txt",
                content=f"perf content {i}",
                headers=auth_headers,
            )

        start = time.perf_counter()
        for i in range(20):
            resp = test_app.get(
                f"/api/v2/files/read?path=/perf_{i}.txt",
                headers=auth_headers,
            )
            assert resp.status_code == 200
        individual_time = time.perf_counter() - start

        start = time.perf_counter()
        resp = test_app.post(
            "/api/v2/batch",
            json={
                "operations": [{"op": "read", "path": f"/perf_{i}.txt"} for i in range(20)],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        batch_time = time.perf_counter() - start

        speedup = individual_time / batch_time
        assert speedup > 1.5, (
            f"Batch ({batch_time:.3f}s) not faster than "
            f"individual ({individual_time:.3f}s), speedup={speedup:.2f}x"
        )


class TestBatchE2EStat:
    def test_stat_returns_file_metadata(
        self, test_app: httpx.Client, auth_headers: dict[str, str]
    ) -> None:
        """Stat operation returns size, version, path info."""
        test_app.put("/api/v1/files/stat_test.txt", content="hello stat", headers=auth_headers)
        response = test_app.post(
            "/api/v2/batch",
            json={"operations": [{"op": "stat", "path": "/stat_test.txt"}]},
            headers=auth_headers,
        )
        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["status"] == 200
        assert result["data"]["size"] > 0
        assert result["data"]["path"] == "/stat_test.txt"
