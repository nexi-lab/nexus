"""Tests for POST /api/v2/admin/reindex search-refresh side-effect (#4241).

Reindex previously rebuilt only the aspect store. Operators ran
``nexus reindex --target all`` per the Docker entrypoint's v2→v3 reset
message, saw ``processed=N, errors=0``, and concluded the BM25/vector
index was repopulated — but search still returned 0 rows. This module
locks in the fix: after the MCL replay, reindex must drive
``search_daemon.notify_file_change`` for every processed path AND
stamp ``stats.last_index_refresh`` so /api/v2/search/stats reflects
the activity.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    get_auth_result,
    get_operation_logger,
)
from nexus.server.api.v2.routers.replay import router as replay_router


def _row(path: str, *, seq: int, change: str = "upsert") -> Any:
    """Build a minimal MCL row stand-in (matches OperationLogModel fields used)."""
    return SimpleNamespace(
        path=path,
        sequence_number=seq,
        entity_urn=f"urn:nexus:file:default:abc{seq}",
        aspect_name="file_metadata",
        change_type=change,
        metadata_snapshot=None,
        zone_id="root",
        created_at=None,
        operation_type="write",
    )


def _make_app(*, rows: list[Any], search_daemon: Any | None) -> FastAPI:
    """Build a FastAPI app with the replay router and stubbed deps."""
    app = FastAPI()

    # Stub search daemon on app.state.
    app.state.search_daemon = search_daemon

    # Stub op_logger: replay_changes returns the rows, session is a no-op MagicMock
    # whose .execute(...).scalar_one() returns total count.
    session = MagicMock()
    scalar = MagicMock()
    scalar.scalar_one = MagicMock(return_value=len(rows))
    session.execute = MagicMock(return_value=scalar)
    session.commit = MagicMock()

    op_logger = MagicMock()
    op_logger.session = session
    op_logger.replay_changes = MagicMock(return_value=iter(rows))

    # Dependency overrides.
    async def _fake_get_operation_logger() -> Any:
        return op_logger, "root"

    async def _fake_get_auth_result() -> dict[str, Any]:
        return {"is_admin": True, "subject_id": "admin", "zone_id": "root"}

    app.dependency_overrides[get_operation_logger] = _fake_get_operation_logger
    app.dependency_overrides[get_auth_result] = _fake_get_auth_result
    app.include_router(replay_router)
    return app


def test_reindex_all_calls_search_notify_per_path() -> None:
    """``target=all`` drives search_daemon.notify_file_change for every
    processed path so the BM25/vector index sees the new state (#4241).
    """
    rows = [_row("/repro/a.md", seq=1), _row("/repro/b.md", seq=2)]

    daemon = MagicMock()
    daemon.notify_file_change = AsyncMock()
    daemon.stats = SimpleNamespace(last_index_refresh=None)

    # Stub _MCLProcessor.process so we don't need a live aspect store.
    import nexus.cli.commands.reindex as reindex_mod

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reindex_mod._MCLProcessor, "process", lambda self, row: None)
        app = _make_app(rows=rows, search_daemon=daemon)
        with TestClient(app) as client:
            resp = client.post("/api/v2/admin/reindex", json={"target": "all"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processed"] == 2
    # Round-1 review (codex MEDIUM): we now report *enqueued* paths, not
    # completed indexing — notify_file_change only wakes the consumer.
    assert body["search_paths_enqueued"] == 2
    assert body["search_refresh_enqueued_at"] is not None
    notified = [call.args for call in daemon.notify_file_change.await_args_list]
    assert ("/repro/a.md", "update") in notified
    assert ("/repro/b.md", "update") in notified
    # Round-1 fix: reindex MUST NOT pre-stamp stats.last_index_refresh —
    # that field is the consumer's to write on actual indexing completion.
    assert daemon.stats.last_index_refresh is None


def test_reindex_search_target_also_refreshes() -> None:
    """``target=search`` is the obvious subset and must refresh too."""
    rows = [_row("/x.md", seq=5)]
    daemon = MagicMock()
    daemon.notify_file_change = AsyncMock()
    daemon.stats = SimpleNamespace(last_index_refresh=None)

    import nexus.cli.commands.reindex as reindex_mod

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reindex_mod._MCLProcessor, "process", lambda self, row: None)
        app = _make_app(rows=rows, search_daemon=daemon)
        with TestClient(app) as client:
            resp = client.post("/api/v2/admin/reindex", json={"target": "search"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["search_paths_enqueued"] == 1
    daemon.notify_file_change.assert_awaited_once_with("/x.md", "update")


def test_reindex_versions_target_does_not_refresh_search() -> None:
    """``target=versions`` is unrelated to the search index and must NOT
    poke notify_file_change."""
    rows = [_row("/x.md", seq=5)]
    daemon = MagicMock()
    daemon.notify_file_change = AsyncMock()
    daemon.stats = SimpleNamespace(last_index_refresh=None)

    import nexus.cli.commands.reindex as reindex_mod

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reindex_mod._MCLProcessor, "process", lambda self, row: None)
        app = _make_app(rows=rows, search_daemon=daemon)
        with TestClient(app) as client:
            resp = client.post("/api/v2/admin/reindex", json={"target": "versions"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["search_paths_enqueued"] == 0
    assert body["search_refresh_enqueued_at"] is None
    daemon.notify_file_change.assert_not_awaited()


def test_reindex_delete_event_propagates_as_delete() -> None:
    """A ``change_type=delete`` MCL row must drive a delete refresh,
    not an update — otherwise BM25 keeps a tombstoned entry."""
    rows = [_row("/gone.md", seq=10, change="delete")]
    daemon = MagicMock()
    daemon.notify_file_change = AsyncMock()
    daemon.stats = SimpleNamespace(last_index_refresh=None)

    import nexus.cli.commands.reindex as reindex_mod

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reindex_mod._MCLProcessor, "process", lambda self, row: None)
        app = _make_app(rows=rows, search_daemon=daemon)
        with TestClient(app) as client:
            resp = client.post("/api/v2/admin/reindex", json={"target": "all"})

    assert resp.status_code == 200, resp.text
    daemon.notify_file_change.assert_awaited_once_with("/gone.md", "delete")


def test_reindex_dry_run_does_not_refresh() -> None:
    """``dry_run=true`` must NOT touch the search daemon."""
    rows = [_row("/x.md", seq=1)]
    daemon = MagicMock()
    daemon.notify_file_change = AsyncMock()
    daemon.stats = SimpleNamespace(last_index_refresh=None)

    app = _make_app(rows=rows, search_daemon=daemon)
    with TestClient(app) as client:
        resp = client.post("/api/v2/admin/reindex", json={"target": "all", "dry_run": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    daemon.notify_file_change.assert_not_awaited()


def test_reindex_partial_enqueue_failure_surfaces_in_response() -> None:
    """Round-4 review (codex MEDIUM): when some notify_file_change calls
    raise, the response must report search_enqueue_errors and list the
    failed paths — otherwise operators see processed=N, errors=0 and a
    lower enqueued count, and miss that part of the replay never
    reached the search index.
    """
    rows = [
        _row("/good1.md", seq=1),
        _row("/bad.md", seq=2),
        _row("/good2.md", seq=3),
    ]

    async def _flaky_notify(path: str, change: str) -> None:
        if path == "/bad.md":
            raise RuntimeError("backend down")

    daemon = MagicMock()
    daemon.notify_file_change = AsyncMock(side_effect=_flaky_notify)
    daemon.stats = SimpleNamespace(last_index_refresh=None)

    import nexus.cli.commands.reindex as reindex_mod

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reindex_mod._MCLProcessor, "process", lambda self, row: None)
        app = _make_app(rows=rows, search_daemon=daemon)
        with TestClient(app) as client:
            resp = client.post("/api/v2/admin/reindex", json={"target": "all"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 2 succeeded, 1 failed at enqueue stage.
    assert body["search_paths_enqueued"] == 2, body
    assert body["search_enqueue_errors"] == 1, body
    assert "/bad.md" in body["search_enqueue_failed_paths"], body


def test_reindex_without_search_daemon_still_succeeds() -> None:
    """A deployment without a search daemon (e.g., sandbox profile) must
    not 500. The aspect-store rebuild is the primary contract; search
    refresh is best-effort."""
    rows = [_row("/x.md", seq=1)]

    import nexus.cli.commands.reindex as reindex_mod

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reindex_mod._MCLProcessor, "process", lambda self, row: None)
        app = _make_app(rows=rows, search_daemon=None)
        with TestClient(app) as client:
            resp = client.post("/api/v2/admin/reindex", json={"target": "all"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processed"] == 1
    assert body["search_paths_enqueued"] == 0
    assert body["search_refresh_enqueued_at"] is None
