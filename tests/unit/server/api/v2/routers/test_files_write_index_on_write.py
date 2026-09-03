"""Unit tests for the ``index`` option on POST /files/write + /files/batch/write (#4736).

Post-P12 a plain write never indexes.  ``index: true`` (or
``index: {"text": ...}``) makes the write and the index one call, and the
response says per file what happened — ``indexed`` with the plugin's
``index_seq``, ``skipped`` with a reason, or ``error``.  Search being
unavailable fails the request BEFORE the write.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
_MODIFIED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_MODIFIED_MS = int(_MODIFIED.timestamp() * 1000)
_INDEXED = {"indexed": 1, "skipped": [], "skipped_count": 0, "skipped_paths": [], "index_seq": 5}


def _b64(data: bytes | str) -> str:
    raw = data if isinstance(data, bytes) else data.encode()
    return base64.b64encode(raw).decode()


class _FakeFS:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []
        self.batches: list[list[tuple[str, bytes]]] = []

    def write(self, *, path: str, buf: bytes, **_: Any) -> dict[str, Any]:
        self.writes.append((path, buf))
        return {
            "content_id": f"cid-{len(self.writes)}",
            "version": 1,
            "size": len(buf),
            "modified_at": _MODIFIED,
        }

    def write_batch(self, files: list[tuple[str, bytes]], **_: Any) -> list[dict[str, Any]]:
        self.batches.append(list(files))
        return [
            {
                "path": path,
                "content_id": f"cid-{i}",
                "version": 1,
                "size": len(buf),
                "modified_at": _MODIFIED,
            }
            for i, (path, buf) in enumerate(files)
        ]


def _daemon(result: dict[str, Any] | None = None, *, error: Exception | None = None) -> MagicMock:
    daemon = MagicMock()
    if error is not None:
        daemon.index_documents = AsyncMock(side_effect=error)
    else:
        daemon.index_documents = AsyncMock(return_value=result if result is not None else _INDEXED)
    return daemon


def _client(fs: _FakeFS, daemon: Any | None) -> TestClient:
    from nexus.server.api.v2.routers.async_files import create_async_files_router
    from nexus.server.dependencies import get_auth_result, require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.dependency_overrides[get_auth_result] = lambda: _AUTH
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(create_async_files_router(nexus_fs=fs), prefix="/api/v2/files")
    return TestClient(app)


def _sent_docs(daemon: MagicMock) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    call = daemon.index_documents.await_args
    return list(call.args[0]), dict(call.kwargs)


# ── POST /files/write ─────────────────────────────────────────────────


def test_plain_write_does_not_index() -> None:
    fs, daemon = _FakeFS(), _daemon()
    resp = _client(fs, daemon).post(
        "/api/v2/files/write", json={"path": "/docs/a.md", "content": "hello"}
    )
    assert resp.status_code == 200
    assert resp.json()["index"] is None
    daemon.index_documents.assert_not_awaited()
    # #4740: the REST route writes into the caller's zone namespace
    # (auth zone "eng"), exactly like the RPC path; the index leg keeps the
    # caller-facing path (see the indexing tests below).
    assert fs.writes == [("/zone/eng/docs/a.md", b"hello")]


def test_index_true_indexes_the_written_text_in_the_same_call() -> None:
    fs, daemon = _FakeFS(), _daemon()
    resp = _client(fs, daemon).post(
        "/api/v2/files/write",
        json={"path": "/docs/a.md", "content": "hello world", "index": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content_id"] == "cid-1"
    assert body["index"]["status"] == "indexed"
    assert body["index"]["index_seq"] == 5
    docs, kwargs = _sent_docs(daemon)
    assert docs == [{"path": "/docs/a.md", "text": "hello world", "mtime_ms": _MODIFIED_MS}]
    assert kwargs == {"zone_id": "eng"}


def test_index_text_override_replaces_the_written_bytes() -> None:
    fs, daemon = _FakeFS(), _daemon()
    resp = _client(fs, daemon).post(
        "/api/v2/files/write",
        json={
            "path": "/docs/report.pdf",
            "content": _b64(b"\x00\x01\x02"),
            "encoding": "base64",
            "index": {"text": "extracted words"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["index"]["status"] == "indexed"
    docs, _ = _sent_docs(daemon)
    assert docs[0]["text"] == "extracted words"
    assert fs.writes == [("/zone/eng/docs/report.pdf", b"\x00\x01\x02")]


def test_binary_content_without_text_is_skipped_locally() -> None:
    fs, daemon = _FakeFS(), _daemon()
    resp = _client(fs, daemon).post(
        "/api/v2/files/write",
        json={
            "path": "/bin/blob",
            "content": _b64(b"\xff\xfe\x00"),
            "encoding": "base64",
            "index": True,
        },
    )
    assert resp.status_code == 200
    verdict = resp.json()["index"]
    assert verdict["status"] == "skipped"
    assert "UTF-8" in verdict["reason"]
    daemon.index_documents.assert_not_awaited()
    assert len(fs.writes) == 1, "the write itself still lands"


def test_index_requested_without_search_daemon_is_503_before_writing() -> None:
    fs = _FakeFS()
    resp = _client(fs, None).post(
        "/api/v2/files/write", json={"path": "/docs/a.md", "content": "x", "index": True}
    )
    assert resp.status_code == 503
    assert fs.writes == [], "nothing may be written when the requested index cannot happen"


def test_plugin_failure_after_the_write_is_reported_not_hidden() -> None:
    fs, daemon = _FakeFS(), _daemon(error=RuntimeError("plugin down"))
    resp = _client(fs, daemon).post(
        "/api/v2/files/write", json={"path": "/docs/a.md", "content": "x", "index": True}
    )
    assert resp.status_code == 200, "the write succeeded; only the index leg failed"
    verdict = resp.json()["index"]
    assert verdict["status"] == "error"
    assert "plugin down" in verdict["error"]
    assert len(fs.writes) == 1


def test_content_the_plugin_skips_is_reported_skipped() -> None:
    fs = _FakeFS()
    daemon = _daemon(
        {**_INDEXED, "indexed": 0, "skipped_count": 1, "skipped_paths": ["/docs/e.md"]}
    )
    resp = _client(fs, daemon).post(
        "/api/v2/files/write", json={"path": "/docs/e.md", "content": "   ", "index": True}
    )
    assert resp.status_code == 200
    verdict = resp.json()["index"]
    assert verdict["status"] == "skipped"
    assert "empty" in verdict["reason"]
    assert verdict["index_seq"] is None


# ── POST /files/batch/write ───────────────────────────────────────────


def test_batch_write_indexes_the_requested_files_in_one_plugin_call() -> None:
    fs, daemon = _FakeFS(), _daemon()
    resp = _client(fs, daemon).post(
        "/api/v2/files/batch/write",
        json={
            "index": True,
            "files": [
                {"path": "/b/text.md", "content_base64": _b64("alpha text")},
                {"path": "/b/blob.dat", "content_base64": _b64(b"\xff\xfe")},
                {"path": "/b/skip.md", "content_base64": _b64("not this one"), "index": False},
                {
                    "path": "/b/scan.pdf",
                    "content_base64": _b64(b"\x00"),
                    "index": {"text": "pdf words"},
                },
            ],
        },
    )
    assert resp.status_code == 200
    by_path = {r["path"]: r for r in resp.json()["results"]}
    assert by_path["/b/text.md"]["index"]["status"] == "indexed"
    assert by_path["/b/text.md"]["index"]["index_seq"] == 5
    assert by_path["/b/blob.dat"]["index"]["status"] == "skipped"
    assert "UTF-8" in by_path["/b/blob.dat"]["index"]["reason"]
    assert by_path["/b/skip.md"]["index"] is None
    assert by_path["/b/scan.pdf"]["index"]["status"] == "indexed"

    daemon.index_documents.assert_awaited_once()
    docs, kwargs = _sent_docs(daemon)
    assert [(d["path"], d["text"]) for d in docs] == [
        ("/b/text.md", "alpha text"),
        ("/b/scan.pdf", "pdf words"),
    ]
    assert all(d["mtime_ms"] == _MODIFIED_MS for d in docs)
    assert kwargs == {"zone_id": "eng"}
    assert len(fs.batches) == 1 and len(fs.batches[0]) == 4


def test_batch_write_without_index_never_reaches_the_plugin() -> None:
    fs, daemon = _FakeFS(), _daemon()
    resp = _client(fs, daemon).post(
        "/api/v2/files/batch/write",
        json={"files": [{"path": "/b/a.md", "content_base64": _b64("alpha")}]},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["index"] is None
    daemon.index_documents.assert_not_awaited()


def test_batch_write_index_without_search_daemon_is_503_before_writing() -> None:
    fs = _FakeFS()
    resp = _client(fs, None).post(
        "/api/v2/files/batch/write",
        json={"files": [{"path": "/b/a.md", "content_base64": _b64("alpha"), "index": True}]},
    )
    assert resp.status_code == 503
    assert fs.batches == []
