"""Unit tests for SkeletonIndexer DB write behavior."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[4] / "src/nexus/bricks/search/skeleton_indexer.py"
_SPEC = importlib.util.spec_from_file_location("_test_skeleton_indexer_module", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_SKELETON_INDEXER_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SKELETON_INDEXER_MODULE)
SkeletonIndexer = _SKELETON_INDEXER_MODULE.SkeletonIndexer


class _NoopReader:
    async def read_head(self, virtual_path: str, max_bytes: int) -> bytes:
        return b""


class _NoopBM25:
    async def upsert_skeleton(
        self,
        doc_id: str,
        virtual_path: str,
        title: str | None,
        zone_id: str,
        *,
        path_id: str | None = None,
    ) -> None:
        pass

    async def delete_skeleton(self, doc_id: str, zone_id: str) -> None:
        pass


class _FakeInsert:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def values(self, **kwargs: Any) -> "_FakeInsert":
        self._captured["values"] = kwargs
        return self

    def on_conflict_do_update(
        self,
        *,
        index_elements: list[str],
        set_: dict[str, Any],
    ) -> "_FakeInsert":
        self._captured["index_elements"] = index_elements
        self._captured["set"] = set_
        return self


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.committed = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass

    async def execute(self, stmt: Any) -> None:
        self.executed.append(stmt)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_postgres_upsert_uses_naive_utc_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres TIMESTAMP WITHOUT TIME ZONE rejects tz-aware datetimes via asyncpg."""
    captured: dict[str, Any] = {}

    def _fake_pg_insert(_model: Any) -> _FakeInsert:
        return _FakeInsert(captured)

    monkeypatch.setattr("sqlalchemy.dialects.postgresql.insert", _fake_pg_insert)

    fake_session = _FakeSession()
    indexer = SkeletonIndexer(
        file_reader=_NoopReader(),
        bm25=_NoopBM25(),
        async_session_factory=lambda: fake_session,
    )

    await indexer._upsert_db_row_pg(
        path_id="path-1",
        zone_id="root",
        title="Title",
        content_id="abc123",
    )

    inserted_at = captured["values"]["indexed_at"]
    updated_at = captured["set"]["indexed_at"]

    assert inserted_at.tzinfo is None
    assert updated_at.tzinfo is None
    assert fake_session.committed is True
