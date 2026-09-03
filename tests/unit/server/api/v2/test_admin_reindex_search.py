"""POST /api/v2/admin/reindex drives the search plugin synchronously (#4241 / #4736).

Pre-fix the route sent ``NotifyFileChange(update)`` per replayed path — a
no-op ack post-P12 — and reported the count as ``search_paths_enqueued``.
Now deletes evict, the rest are read through the VFS and indexed via
``IndexDocuments``, and the response says per path what happened.
"""

from __future__ import annotations

from types import SimpleNamespace
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

_ADMIN = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "root",
    "zone_id": "eng",
    "zone_perms": [["eng", "rw"]],
    "is_admin": True,
}


class _FakeSession:
    def commit(self) -> None:  # the route commits after the replay loop
        pass

    def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(scalar_one=lambda: 3)


class _FakeOpLogger:
    def __init__(self, rows: list[Any]) -> None:
        self.session = _FakeSession()
        self._rows = rows

    def replay_changes(self, **_: Any):
        yield from self._rows


class _FakeFS:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def read(self, path: str, *, context: Any = None, **_: Any) -> bytes:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        if path not in self.files:
            raise NexusFileNotFoundError(path)
        return self.files[path]

    def sys_stat(self, path: str, *, context: Any = None, **_: Any) -> dict[str, Any]:
        return {"modified_at_ms": 1_700_000_000_000}


def _row(seq: int, path: str, change: str, *, aspect: str = "file_metadata") -> SimpleNamespace:
    return SimpleNamespace(
        sequence_number=seq,
        path=path,
        change_type=change,
        entity_urn=f"urn:nexus:file:{path}",
        aspect_name=aspect,
        metadata_snapshot={},
        zone_id="eng",
    )


def _client(
    monkeypatch: pytest.MonkeyPatch, *, rows: list[Any], daemon: Any, fs: Any
) -> TestClient:
    from nexus.server.api.v2.dependencies import get_auth_result, get_operation_logger
    from nexus.server.api.v2.routers.replay import router

    # The aspect-store rebuild is not under test — make the processor a no-op.
    class _NoopProcessor:
        def __init__(self, session: Any, target: str) -> None:
            pass

        def process(self, row: Any) -> None:
            pass

    monkeypatch.setattr("nexus.cli.commands.reindex._MCLProcessor", _NoopProcessor)

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.nexus_fs = fs
    app.dependency_overrides[get_auth_result] = lambda: _ADMIN
    app.dependency_overrides[get_operation_logger] = lambda: (_FakeOpLogger(rows), "eng")
    app.include_router(router)
    return TestClient(app)


def _daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(
        return_value={"indexed": 2, "skipped": [], "skipped_paths": [], "index_seq": 9}
    )
    daemon.notify_file_change = AsyncMock(return_value={"status": "accepted", "index_seq": 10})
    return daemon


def test_reindex_indexes_and_evicts_synchronously_and_reports_per_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row(1, "/docs/a.md", "upsert"),
        _row(2, "/docs/b.md", "upsert"),
        _row(3, "/docs/gone.md", "delete"),
        _row(4, "/docs/missing.md", "upsert"),
        # Aspect rows carry the entity URN in ``path`` — aspect-store
        # work, not a document.  They must not reach the plugin or count
        # as a search failure (seen live: schema_metadata / lineage rows
        # from `nexus demo init` reported as 5 index errors).
        _row(5, "urn:nexus:file:root:97fa8b50b0c8606e94ff2f57f0aa3873", "upsert", aspect="lineage"),
    ]
    daemon = _daemon()
    fs = _FakeFS({"/docs/a.md": b"alpha", "/docs/b.md": b"bravo"})

    resp = _client(monkeypatch, rows=rows, daemon=daemon, fs=fs).post(
        "/api/v2/admin/reindex", json={"target": "search"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processed"] == 5
    assert body["search_paths_indexed"] == 2
    assert body["search_paths_deleted"] == 1
    assert body["search_paths_skipped"] == 0
    assert body["search_index_errors"] == 1
    assert body["search_index_failed_paths"] == ["/docs/missing.md"]
    assert "404" in body["search_index_error"]
    assert body["search_index_seq"] == 10, "highest plugin seq across index + evict"
    assert isinstance(body["search_indexed_at"], float)
    for stale in ("search_paths_enqueued", "search_refresh_enqueued_at", "search_enqueue_errors"):
        assert stale not in body, f"{stale} described a queue that does not exist"

    # Text went to the plugin in the token zone with the stat mtime; the
    # delete was an evict, not an update ack.
    docs = daemon.index_documents.await_args.args[0]
    assert [(d["path"], d["text"], d["mtime_ms"]) for d in docs] == [
        ("/docs/a.md", "alpha", 1_700_000_000_000),
        ("/docs/b.md", "bravo", 1_700_000_000_000),
    ]
    assert daemon.index_documents.await_args.kwargs == {"zone_id": "eng"}
    daemon.notify_file_change.assert_awaited_once_with("/docs/gone.md", "delete", zone_id="eng")


def test_reindex_without_search_daemon_fails_every_path_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_row(1, "/docs/a.md", "upsert"), _row(2, "/docs/b.md", "upsert")]

    resp = _client(monkeypatch, rows=rows, daemon=None, fs=_FakeFS({})).post(
        "/api/v2/admin/reindex", json={"target": "all"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["search_paths_indexed"] == 0
    assert body["search_index_errors"] == 2
    assert sorted(body["search_index_failed_paths"]) == ["/docs/a.md", "/docs/b.md"]
    assert "search unavailable" in body["search_index_error"]


def test_versions_target_never_touches_search(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _daemon()
    resp = _client(
        monkeypatch, rows=[_row(1, "/docs/a.md", "upsert")], daemon=daemon, fs=_FakeFS({})
    ).post("/api/v2/admin/reindex", json={"target": "versions"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["search_paths_indexed"] == 0 and body["search_index_errors"] == 0
    daemon.index_documents.assert_not_awaited()
    daemon.notify_file_change.assert_not_awaited()


def test_dry_run_reports_total_only(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _daemon()
    resp = _client(
        monkeypatch, rows=[_row(1, "/docs/a.md", "upsert")], daemon=daemon, fs=_FakeFS({})
    ).post("/api/v2/admin/reindex", json={"target": "search", "dry_run": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True and body["total"] == 3
    assert body["search_paths_indexed"] == 0
    daemon.index_documents.assert_not_awaited()
