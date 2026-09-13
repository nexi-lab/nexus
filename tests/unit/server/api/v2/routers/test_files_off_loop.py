"""Blocking VFS calls in the files router run off the event loop (#4777).

``fs.write`` and friends are synchronous kernel gRPC round-trips.  Running
them on the request event loop meant one slow write (e.g. while the search
plugin saturates the volume) stalled every other request in the process.
Every route must dispatch the call to a worker thread.
"""

from __future__ import annotations

import base64
import threading
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
_MODIFIED = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


class _ThreadRecordingFS:
    """Records the thread every VFS call ran on."""

    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {}

    def _note(self, op: str) -> None:
        self.calls.setdefault(op, []).append(threading.current_thread().name)

    def _meta(self, path: str) -> dict[str, Any]:
        return {
            "path": path,
            "content_id": "cid-1",
            "version": 1,
            "size": 5,
            "modified_at": _MODIFIED.isoformat(),
            "mime_type": "text/plain",
            "entry_type": 0,
        }

    def write(self, path: str, buf: bytes = b"", **_: Any) -> dict[str, Any]:
        self._note("write")
        return {**self._meta(path), "size": len(buf)}

    def read(self, path: str, *, return_metadata: bool = False, **_: Any) -> Any:
        self._note("read")
        return {"content": b"hello", **self._meta(path)} if return_metadata else b"hello"

    def sys_read(self, path: str, **_: Any) -> bytes:
        self._note("sys_read")
        return b"hello"

    def sys_stat(self, path: str, **_: Any) -> dict[str, Any]:
        self._note("sys_stat")
        return self._meta(path)

    def sys_unlink(self, path: str, **_: Any) -> dict[str, Any]:
        self._note("sys_unlink")
        return {"projection_seq": 1}

    def access(self, path: str, **_: Any) -> bool:
        self._note("access")
        return True

    def sys_readdir(self, path: str, **_: Any) -> list[dict[str, Any]]:
        self._note("sys_readdir")
        return [self._meta(f"{path.rstrip('/')}/a.md")]

    def mkdir(self, path: str, **_: Any) -> dict[str, Any]:
        self._note("mkdir")
        return {"path": path}

    def sys_rename(self, source: str, destination: str, **_: Any) -> dict[str, Any]:
        self._note("sys_rename")
        return {"projection_seq": 1}

    def write_batch(self, files: list[tuple[str, bytes]], **_: Any) -> list[dict[str, Any]]:
        self._note("write_batch")
        return [{**self._meta(p), "size": len(b)} for p, b in files]

    def read_batch(self, paths: list[str], **_: Any) -> list[dict[str, Any]]:
        self._note("read_batch")
        return [{"path": p, "content": b"hello", "success": True} for p in paths]

    def rename_batch(self, renames: list[tuple[str, str]], **_: Any) -> dict[str, Any]:
        self._note("rename_batch")
        return {src: {"success": True, "new_path": dst} for src, dst in renames}


def _client(fs: _ThreadRecordingFS) -> TestClient:
    from nexus.server.api.v2.routers.async_files import create_async_files_router
    from nexus.server.dependencies import get_auth_result, require_auth

    app = FastAPI()
    app.state.search_daemon = None
    app.dependency_overrides[get_auth_result] = lambda: _AUTH
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(create_async_files_router(nexus_fs=fs), prefix="/api/v2/files")

    # TestClient runs the app on a portal thread; this probe records the
    # event-loop thread's name so tests can assert VFS calls ran elsewhere.
    @app.get("/__probe_loop_thread")
    async def _probe() -> dict[str, str]:
        return {"thread": threading.current_thread().name}

    return TestClient(app)


def _loop_thread_name(client: TestClient) -> str:
    response = client.get("/__probe_loop_thread")
    assert response.status_code == 200
    return str(response.json()["thread"])


@pytest.mark.parametrize(
    ("op", "request_fn"),
    [
        (
            "write",
            lambda c: c.post("/api/v2/files/write", json={"path": "/d/a.md", "content": "hi"}),
        ),
        ("read", lambda c: c.get("/api/v2/files/read", params={"path": "/d/a.md"})),
        ("sys_unlink", lambda c: c.delete("/api/v2/files/delete", params={"path": "/d/a.md"})),
        ("access", lambda c: c.get("/api/v2/files/exists", params={"path": "/d/a.md"})),
        ("sys_readdir", lambda c: c.get("/api/v2/files/list", params={"path": "/d"})),
        ("mkdir", lambda c: c.post("/api/v2/files/mkdir", json={"path": "/d/new"})),
        ("sys_stat", lambda c: c.get("/api/v2/files/metadata", params={"path": "/d/a.md"})),
        (
            "sys_rename",
            lambda c: c.post(
                "/api/v2/files/rename", json={"source": "/d/a.md", "destination": "/d/b.md"}
            ),
        ),
        (
            "write_batch",
            lambda c: c.post(
                "/api/v2/files/batch/write",
                json={
                    "files": [
                        {"path": "/d/a.md", "content_base64": base64.b64encode(b"a").decode()}
                    ]
                },
            ),
        ),
        (
            "read_batch",
            lambda c: c.post("/api/v2/files/batch/read", json={"paths": ["/d/a.md"]}),
        ),
        (
            "rename_batch",
            lambda c: c.post(
                "/api/v2/files/rename-batch",
                json={"operations": [{"source": "/d/a.md", "destination": "/d/b.md"}]},
            ),
        ),
        (
            "write",
            lambda c: c.post(
                "/api/v2/files/copy", json={"source": "/d/a.md", "destination": "/d/b.md"}
            ),
        ),
    ],
)
def test_vfs_call_runs_off_the_event_loop(op: str, request_fn: Any) -> None:
    fs = _ThreadRecordingFS()
    with _client(fs) as client:
        loop_thread = _loop_thread_name(client)
        response = request_fn(client)
        assert response.status_code == 200, response.text

    assert fs.calls.get(op), f"{op} was never invoked; calls={fs.calls}"
    for thread_name in fs.calls[op]:
        assert thread_name != loop_thread, f"{op} ran on the event loop thread {loop_thread}"


def test_stream_read_runs_off_the_event_loop() -> None:
    fs = _ThreadRecordingFS()
    with _client(fs) as client:
        loop_thread = _loop_thread_name(client)
        response = client.get("/api/v2/files/stream", params={"path": "/d/a.md"})
        assert response.status_code == 200, response.text
        assert response.content == b"hello"

    for op in ("sys_stat", "sys_read"):
        assert fs.calls.get(op), f"{op} was never invoked; calls={fs.calls}"
        for thread_name in fs.calls[op]:
            assert thread_name != loop_thread, f"{op} ran on the event loop thread"
