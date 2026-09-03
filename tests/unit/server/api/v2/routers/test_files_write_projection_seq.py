"""POST /files/write and /files/batch/write return ``projection_seq`` (#4738)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "rw"]],
    "is_admin": True,
}
_MODIFIED = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class _FakeFS:
    def __init__(self, *, seq: int | None = 42) -> None:
        self.seq = seq

    def sys_unlink(self, path: str, **_: Any) -> dict[str, Any]:
        return {"projection_seq": self.seq}

    def sys_rename(self, source: str, destination: str, **_: Any) -> dict[str, Any]:
        return {"projection_seq": self.seq}

    def mkdir(self, path: str, **_: Any) -> dict[str, Any]:
        return {"path": path, "projection_seq": self.seq}

    def rename_batch(self, renames: list[tuple[str, str]], **_: Any) -> dict[str, Any]:
        return {
            src: {
                "success": True,
                "new_path": dst,
                "projection_seq": (self.seq + i) if self.seq is not None else None,
            }
            for i, (src, dst) in enumerate(renames)
        }

    def write(self, *, path: str, buf: bytes, **_: Any) -> dict[str, Any]:
        return {
            "content_id": "cid-1",
            "version": 3,
            "size": len(buf),
            "modified_at": _MODIFIED,
            "projection_seq": self.seq,
        }

    def write_batch(self, files: list[tuple[str, bytes]], **_: Any) -> list[dict[str, Any]]:
        return [
            {
                "path": path,
                "content_id": f"cid-{i}",
                "version": 1,
                "size": len(buf),
                "modified_at": _MODIFIED,
                "projection_seq": (self.seq + i) if self.seq is not None else None,
            }
            for i, (path, buf) in enumerate(files)
        ]


def _client(fs: _FakeFS) -> TestClient:
    from nexus.server.api.v2.routers.async_files import create_async_files_router
    from nexus.server.dependencies import get_auth_result, require_auth

    app = FastAPI()
    app.state.search_daemon = None
    app.dependency_overrides[get_auth_result] = lambda: _AUTH
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(create_async_files_router(nexus_fs=fs), prefix="/api/v2/files")
    return TestClient(app)


def test_write_response_carries_projection_seq() -> None:
    resp = _client(_FakeFS(seq=42)).post(
        "/api/v2/files/write", json={"path": "/docs/a.md", "content": "hello"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["projection_seq"] == 42
    assert body["content_id"] == "cid-1"


def test_write_response_projection_seq_is_null_when_unconfirmed() -> None:
    resp = _client(_FakeFS(seq=None)).post(
        "/api/v2/files/write", json={"path": "/docs/a.md", "content": "hello"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["projection_seq"] is None


def test_batch_write_results_carry_per_item_projection_seq() -> None:
    files = [
        {"path": "/docs/a.md", "content_base64": base64.b64encode(b"a").decode()},
        {"path": "/docs/b.md", "content_base64": base64.b64encode(b"b").decode()},
    ]
    resp = _client(_FakeFS(seq=10)).post("/api/v2/files/batch/write", json={"files": files})
    assert resp.status_code == 200, resp.text
    assert [r["projection_seq"] for r in resp.json()["results"]] == [10, 11]


def test_delete_response_carries_projection_seq() -> None:
    resp = _client(_FakeFS(seq=7)).delete("/api/v2/files/delete?path=/docs/a.md")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "deleted": True,
        "path": "/docs/a.md",
        "revision": None,
        "projection_seq": 7,
    }


def test_rename_response_carries_projection_seq() -> None:
    resp = _client(_FakeFS(seq=8)).post(
        "/api/v2/files/rename", json={"source": "/docs/a.md", "destination": "/docs/b.md"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["projection_seq"] == 8 and resp.json()["success"] is True


def test_mkdir_response_carries_projection_seq() -> None:
    resp = _client(_FakeFS(seq=9)).post("/api/v2/files/mkdir", json={"path": "/docs/new"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"created": True, "path": "/docs/new", "projection_seq": 9}


def test_rename_batch_results_carry_per_item_projection_seq() -> None:
    resp = _client(_FakeFS(seq=20)).post(
        "/api/v2/files/rename-batch",
        json={
            "operations": [
                {"source": "/a", "destination": "/b"},
                {"source": "/c", "destination": "/d"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert [r["projection_seq"] for r in resp.json()["results"]] == [20, 21]


def test_mutation_responses_report_null_when_unconfirmed() -> None:
    client = _client(_FakeFS(seq=None))
    assert client.delete("/api/v2/files/delete?path=/x").json()["projection_seq"] is None
    assert client.post("/api/v2/files/mkdir", json={"path": "/d"}).json()["projection_seq"] is None
    body = client.post("/api/v2/files/rename", json={"source": "/a", "destination": "/b"}).json()
    assert body["projection_seq"] is None
