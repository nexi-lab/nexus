from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _Result:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _RecordingAsyncSession:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def __aenter__(self) -> "_RecordingAsyncSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        self.statements.append((sql, dict(params or {})))
        if "SELECT path_id FROM file_paths" in sql:
            return _Result(("existing-path-id",))
        return _Result()

    async def commit(self) -> None:
        self.commits += 1


class _RecordingPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def index_document(self, path: str, content: str, path_id: str) -> SimpleNamespace:
        self.calls.append((path, content, path_id))
        return SimpleNamespace(error=None)


@pytest.mark.asyncio
async def test_index_documents_preserves_existing_file_metadata() -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    session = _RecordingAsyncSession()
    pipeline = _RecordingPipeline()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._async_session = lambda: session
    daemon._indexing_pipeline = pipeline

    count = await daemon._index_documents_on_current_loop(
        [
            {
                "id": "/workspace/docs.md",
                "path": "/workspace/docs.md",
                "text": "manual excerpt that may differ from the file bytes",
            }
        ]
    )

    assert count == 1
    assert pipeline.calls == [
        (
            "/workspace/docs.md",
            "manual excerpt that may differ from the file bytes",
            "existing-path-id",
        )
    ]

    update_statements = [
        (sql, params)
        for sql, params in session.statements
        if sql.lstrip().upper().startswith("UPDATE file_paths".upper())
    ]
    assert update_statements
    update_sql, update_params = update_statements[0]
    assert "content_id" not in update_sql
    assert "size_bytes" not in update_sql
    assert "content_id" not in update_params
    assert "size_bytes" not in update_params
