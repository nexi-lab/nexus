"""DELETE /files/delete: ``recursive`` removes a directory tree; a non-empty
directory without it is a 409, not a 500.

Before, the route always called ``sys_unlink(path)``: a directory holding
files could not be removed at all, and the kernel's "Directory not empty"
surfaced as an opaque 500.
"""

from __future__ import annotations

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
    "zone_id": "root",
    "zone_perms": [["root", "rw"]],
    "is_admin": True,
}


class _FakeFS:
    """Directory /ws/a holds files; the kernel refuses it without recursive."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def sys_unlink(self, path: str, *, recursive: bool = False, **_: Any) -> dict[str, Any]:
        self.calls.append((path, recursive))
        if path == "/ws/a" and not recursive:
            raise RuntimeError('RPC error [-32603]: IOError("Directory not empty: /ws/a")')
        return {"projection_seq": 1}


def _client(fs: _FakeFS) -> TestClient:
    from nexus.server.api.v2.routers.async_files import create_async_files_router
    from nexus.server.dependencies import get_auth_result, require_auth

    app = FastAPI()
    app.state.search_daemon = None
    app.dependency_overrides[get_auth_result] = lambda: _AUTH
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(create_async_files_router(nexus_fs=fs), prefix="/api/v2/files")
    return TestClient(app)


def test_non_empty_directory_without_recursive_is_a_conflict() -> None:
    fs = _FakeFS()
    resp = _client(fs).delete("/api/v2/files/delete?path=/ws/a")
    assert resp.status_code == 409, resp.text
    assert "Directory not empty" in resp.json()["detail"]
    assert fs.calls == [("/ws/a", False)]


def test_recursive_deletes_the_tree() -> None:
    fs = _FakeFS()
    resp = _client(fs).delete("/api/v2/files/delete?path=/ws/a&recursive=true")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True
    assert fs.calls == [("/ws/a", True)]


def test_plain_file_delete_is_unchanged() -> None:
    fs = _FakeFS()
    assert _client(fs).delete("/api/v2/files/delete?path=/ws/b.txt").status_code == 200
    assert fs.calls == [("/ws/b.txt", False)]


def test_recursive_cannot_join_a_transaction() -> None:
    fs = _FakeFS()
    resp = _client(fs).delete("/api/v2/files/delete?path=/ws/a&recursive=true&transaction_id=t1")
    assert resp.status_code == 400
    assert fs.calls == []
