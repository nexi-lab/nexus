"""E2E tests for POST /batch/write and POST /batch/read with a real NexusFS kernel.

Uses FastAPI TestClient wired to a real NexusFS (SQLite + CASLocalBackend) — no mocks.
Validates that the full HTTP → router → NexusFS → Rust storage stack works end-to-end.

Issue #3700: Expose write_batch + read_batch via HTTP.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._sqlite_meta import SQLiteMetastore
from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_fs(tmp_path: Path) -> NexusFS:
    """Real NexusFS kernel: SQLite metastore + CASLocalBackend, permissions off."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "meta.db")
    metastore = SQLiteMetastore(db_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )
    kernel._init_cred = OperationContext(
        user_id="e2e-user",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )
    kernel.sys_setattr("/files", entry_type=DT_MOUNT, backend=backend)
    metastore.put(_make_mount_entry("/files", backend.name))

    return kernel


@pytest.fixture()
def client(real_fs: NexusFS) -> TestClient:
    """TestClient wired to the real NexusFS kernel."""
    app = FastAPI()
    router = create_async_files_router(nexus_fs=real_fs)
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /batch/write — real storage
# ---------------------------------------------------------------------------


class TestBatchWriteRealStorage:
    """write_batch hits real SQLite + CASLocalBackend."""

    def test_write_single_file_returns_200(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "/files/hello.txt", "content_base64": _b64(b"hello")}]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert result["path"] == "/files/hello.txt"
        assert result["size"] == 5
        assert result["version"] >= 1
        assert result["content_id"] is not None

    def test_write_multiple_files_atomic(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/write",
            json={
                "files": [
                    {"path": "/files/a.txt", "content_base64": _b64(b"alpha")},
                    {"path": "/files/b.txt", "content_base64": _b64(b"beta")},
                    {"path": "/files/c.txt", "content_base64": _b64(b"gamma")},
                ]
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 3
        paths = {r["path"] for r in results}
        assert paths == {"/files/a.txt", "/files/b.txt", "/files/c.txt"}

    def test_write_empty_content(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "/files/empty.txt", "content_base64": _b64(b"")}]},
        )
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["size"] == 0

    def test_write_binary_content(self, client: TestClient) -> None:
        binary = bytes(range(256))
        resp = client.post(
            "/batch/write",
            json={"files": [{"path": "/files/binary.bin", "content_base64": _b64(binary)}]},
        )
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["size"] == 256


# ---------------------------------------------------------------------------
# POST /batch/read — real storage
# ---------------------------------------------------------------------------


class TestBatchReadRealStorage:
    """read_batch hits real SQLite + CASLocalBackend after writing data."""

    def test_read_existing_file_returns_correct_content(self, client: TestClient) -> None:
        content = b"read-back content"
        # Write first
        client.post(
            "/batch/write",
            json={"files": [{"path": "/files/read1.txt", "content_base64": _b64(content)}]},
        )
        # Read back
        resp = client.post("/batch/read", json={"paths": ["/files/read1.txt"]})
        assert resp.status_code == 200
        item = resp.json()["results"][0]
        assert item["type"] == "success"
        assert base64.b64decode(item["content_base64"]) == content

    def test_read_missing_file_strict_returns_404(self, client: TestClient) -> None:
        resp = client.post("/batch/read", json={"paths": ["/files/does-not-exist.txt"]})
        assert resp.status_code == 404

    def test_read_missing_file_partial_returns_error_item(self, client: TestClient) -> None:
        resp = client.post(
            "/batch/read",
            json={"paths": ["/files/ghost.txt"], "partial": True},
        )
        assert resp.status_code == 200
        item = resp.json()["results"][0]
        assert item["type"] == "error"
        assert "error" in item

    def test_read_preserves_order(self, client: TestClient) -> None:
        # Write three files
        files = [
            ("/files/ord_c.txt", b"ccc"),
            ("/files/ord_a.txt", b"aaa"),
            ("/files/ord_b.txt", b"bbb"),
        ]
        client.post(
            "/batch/write",
            json={"files": [{"path": p, "content_base64": _b64(c)} for p, c in files]},
        )
        # Read in reverse order
        paths = ["/files/ord_b.txt", "/files/ord_c.txt", "/files/ord_a.txt"]
        resp = client.post("/batch/read", json={"paths": paths})
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert base64.b64decode(results[0]["content_base64"]) == b"bbb"
        assert base64.b64decode(results[1]["content_base64"]) == b"ccc"
        assert base64.b64decode(results[2]["content_base64"]) == b"aaa"


# ---------------------------------------------------------------------------
# Round-trip: write then read
# ---------------------------------------------------------------------------


class TestBatchRoundTripRealStorage:
    """write_batch → read_batch with real persistence."""

    def test_write_then_read_content_matches(self, client: TestClient) -> None:
        content_a = b"round-trip file A"
        content_b = b"round-trip file B"

        write_resp = client.post(
            "/batch/write",
            json={
                "files": [
                    {"path": "/files/rt_a.txt", "content_base64": _b64(content_a)},
                    {"path": "/files/rt_b.txt", "content_base64": _b64(content_b)},
                ]
            },
        )
        assert write_resp.status_code == 200
        write_results = write_resp.json()["results"]

        read_resp = client.post(
            "/batch/read",
            json={"paths": ["/files/rt_a.txt", "/files/rt_b.txt"]},
        )
        assert read_resp.status_code == 200
        read_results = read_resp.json()["results"]

        assert base64.b64decode(read_results[0]["content_base64"]) == content_a
        assert base64.b64decode(read_results[1]["content_base64"]) == content_b

        # ETags from write must match ETags from read
        assert read_results[0]["content_id"] == write_results[0]["content_id"]
        assert read_results[1]["content_id"] == write_results[1]["content_id"]

    def test_overwrite_updates_content(self, client: TestClient) -> None:
        path = "/files/overwrite.txt"
        client.post(
            "/batch/write",
            json={"files": [{"path": path, "content_base64": _b64(b"version one")}]},
        )
        client.post(
            "/batch/write",
            json={"files": [{"path": path, "content_base64": _b64(b"version two")}]},
        )

        read_resp = client.post("/batch/read", json={"paths": [path]})
        item = read_resp.json()["results"][0]
        assert base64.b64decode(item["content_base64"]) == b"version two"

    def test_partial_mixed_hit_and_miss(self, client: TestClient) -> None:
        client.post(
            "/batch/write",
            json={"files": [{"path": "/files/partial_hit.txt", "content_base64": _b64(b"found")}]},
        )

        resp = client.post(
            "/batch/read",
            json={
                "paths": ["/files/partial_hit.txt", "/files/partial_miss.txt"],
                "partial": True,
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["type"] == "success"
        assert base64.b64decode(results[0]["content_base64"]) == b"found"
        assert results[1]["type"] == "error"
