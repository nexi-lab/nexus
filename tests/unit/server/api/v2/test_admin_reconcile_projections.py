"""POST /api/v2/admin/reconcile-projections repairs the projection from the kernel (#4738)."""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

from nexus.contracts.metadata import DT_REG
from nexus.storage.record_store import SQLAlchemyRecordStore

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")

_ADMIN = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "root",
    "zone_id": "eng",
    "zone_perms": [["eng", "rw"]],
    "is_admin": True,
}
_USER = {**_ADMIN, "subject_id": "alice", "is_admin": False}


@pytest.fixture
def record_store() -> Generator[SQLAlchemyRecordStore, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db")
        yield rs
        rs.close()


class _FakeFS:
    def __init__(self, record_store: SQLAlchemyRecordStore, entries: list[dict[str, Any]]) -> None:
        self.record_store = record_store
        self.entries = entries
        self.readdir_calls: list[tuple[str, dict[str, Any]]] = []

    def sys_readdir(self, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.readdir_calls.append((path, kwargs))
        return list(self.entries)


def _client(fs: Any, auth: dict[str, Any]) -> TestClient:
    from nexus.server.api.v2.dependencies import get_auth_result
    from nexus.server.api.v2.routers.replay import router

    app = FastAPI()
    app.state.nexus_fs = fs
    app.dependency_overrides[get_auth_result] = lambda: auth
    app.include_router(router)
    return TestClient(app)


def _entry(path: str, content_id: str) -> dict[str, Any]:
    return {
        "path": path,
        "size": 3,
        "content_id": content_id,
        "mime_type": "text/plain",
        "version": 1,
        "gen": 1,
        "entry_type": DT_REG,
        "zone_id": "root",
    }


def test_requires_admin(record_store: SQLAlchemyRecordStore) -> None:
    fs = _FakeFS(record_store, [])
    resp = _client(fs, _USER).post("/api/v2/admin/reconcile-projections", json={})
    assert resp.status_code == 403
    assert fs.readdir_calls == []


def test_reconcile_creates_missing_rows_in_the_token_zone(
    record_store: SQLAlchemyRecordStore,
) -> None:
    fs = _FakeFS(record_store, [_entry("/ws/a.txt", "ca"), _entry("/ws/b.txt", "cb")])
    resp = _client(fs, _ADMIN).post("/api/v2/admin/reconcile-projections", json={"prefix": "/ws"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["zone_id"] == "eng" and body["prefix"] == "/ws"
    assert body["scanned"] == 2 and body["created"] == 2 and body["errors"] == 0
    assert body["created_paths"] == ["/ws/a.txt", "/ws/b.txt"]
    assert body["dry_run"] is False and body["truncated"] is False
    # The kernel walk ran under the caller's context, recursively with details.
    path, kwargs = fs.readdir_calls[0]
    assert path == "/ws" and kwargs["recursive"] is True and kwargs["details"] is True
    assert kwargs["context"] is not None

    # Now in sync.
    again = _client(fs, _ADMIN).post(
        "/api/v2/admin/reconcile-projections", json={"prefix": "/ws", "dry_run": True}
    )
    assert again.json()["in_sync"] == 2 and again.json()["dry_run"] is True


def test_missing_prefix_is_404_not_500(record_store: SQLAlchemyRecordStore) -> None:
    from nexus.contracts.exceptions import NexusFileNotFoundError

    class _MissingFS(_FakeFS):
        def sys_readdir(self, path: str, **kwargs: Any) -> list[dict[str, Any]]:
            raise NexusFileNotFoundError(path)

    resp = _client(_MissingFS(record_store, []), _ADMIN).post(
        "/api/v2/admin/reconcile-projections", json={"prefix": "/nope"}
    )
    assert resp.status_code == 404, resp.text
    assert "/nope" in resp.json()["detail"]


def test_503_without_nexus_fs() -> None:
    from nexus.server.api.v2.dependencies import get_auth_result
    from nexus.server.api.v2.routers.replay import router

    app = FastAPI()
    app.state.nexus_fs = None
    app.dependency_overrides[get_auth_result] = lambda: _ADMIN
    app.include_router(router)
    resp = TestClient(app).post("/api/v2/admin/reconcile-projections", json={})
    assert resp.status_code == 503


def test_503_without_record_store() -> None:
    fs = SimpleNamespace(record_store=None)
    resp = _client(fs, _ADMIN).post("/api/v2/admin/reconcile-projections", json={})
    assert resp.status_code == 503
