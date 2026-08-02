"""Unit tests for the covered-version consumer gate (Issue #4566 follow-up).

Explicit ``POST /search/index`` calls stamp ``file_paths.indexed_content_id``
after a successful write. The mutation consumers must skip an UPSERT whose
row's CURRENT ``content_id`` already matches that marker — otherwise the
consumer's blob-bytes indexing clobbers the richer caller-provided text
(PDF extraction, OCR output, sidecar markdown). A real content change
advances ``content_id``, the marker stops matching, and consumers index the
new version normally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.daemon import DaemonStats, SearchDaemon
from nexus.bricks.search.mutation_events import SearchMutationOp

# =============================================================================
# Fakes
# =============================================================================


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _Session:
    def __init__(self, row: tuple[Any, ...] | None, *, raise_on_execute: bool = False) -> None:
        self._row = row
        self._raise = raise_on_execute
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        if self._raise:
            raise RuntimeError("db down")
        self.calls.append(dict(params or {}))
        return _Result(self._row)


@dataclass
class _FakeMutEvent:
    path: str
    op: Any
    event_id: str = "test-event"


@dataclass
class _FakeResolvedMutation:
    event: _FakeMutEvent
    path_id: str | None = None
    content: str | None = None
    zone_id: str | None = None
    virtual_path: str = ""
    content_resolved: bool = True


def _covered_daemon(row: tuple[Any, ...] | None, *, raise_on_execute: bool = False) -> SearchDaemon:
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon.stats = DaemonStats()
    daemon._async_session = lambda: _Session(row, raise_on_execute=raise_on_execute)
    return daemon


def _upsert_mutation(path: str = "/zone/z1/doc.pdf") -> _FakeResolvedMutation:
    return _FakeResolvedMutation(
        event=_FakeMutEvent(path=path, op=SearchMutationOp.UPSERT),
        path_id="pid-1",
        content="raw blob bytes",
        zone_id="z1",
        virtual_path="/doc.pdf",
    )


def _stub_resolution(daemon: SearchDaemon, mutations: list[Any]) -> None:
    async def _resolve(_consumer: Any, _events: Any) -> list[Any]:
        return mutations

    # setattr keeps mypy quiet without a type:ignore (project policy).
    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010
    setattr(daemon, "_is_path_in_scope", lambda p: True)  # noqa: B010


# =============================================================================
# _upsert_covered_by_index truth table
# =============================================================================


class TestUpsertCoveredByIndex:
    @pytest.mark.asyncio
    async def test_covered_when_marker_matches_current_content(self) -> None:
        daemon = _covered_daemon(("cid-1", "cid-1"))
        assert await daemon._upsert_covered_by_index("pid-1") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            ("cid-2", "cid-1"),  # content advanced past the marker
            ("cid-1", None),  # never stamped
            (None, None),  # no content at all
            (None, "cid-1"),  # marker without content (defensive)
            None,  # row deleted under us
        ],
    )
    async def test_not_covered(self, row: tuple[Any, ...] | None) -> None:
        daemon = _covered_daemon(row)
        assert await daemon._upsert_covered_by_index("pid-1") is False

    @pytest.mark.asyncio
    async def test_no_session_is_not_covered(self) -> None:
        daemon = SearchDaemon.__new__(SearchDaemon)
        daemon.stats = DaemonStats()
        daemon._async_session = None
        assert await daemon._upsert_covered_by_index("pid-1") is False

    @pytest.mark.asyncio
    async def test_lookup_error_fails_open(self) -> None:
        """DB errors must not block consumer indexing (pre-gate behavior)."""
        daemon = _covered_daemon(("cid-1", "cid-1"), raise_on_execute=True)
        assert await daemon._upsert_covered_by_index("pid-1") is False


# =============================================================================
# Consumer wiring
# =============================================================================


class TestEmbeddingConsumerGate:
    @pytest.mark.asyncio
    async def test_covered_upsert_skips_pipeline(self) -> None:
        daemon = _covered_daemon(("cid-1", "cid-1"))
        daemon._indexing_pipeline = MagicMock(index_document=AsyncMock())
        daemon._embedding_provider = MagicMock()
        daemon._chunk_store = MagicMock(replace_document_chunks=AsyncMock())
        daemon._sqlite_vec_backend = None
        _stub_resolution(daemon, [_upsert_mutation()])

        await daemon._consume_embedding_mutations([MagicMock()])

        daemon._indexing_pipeline.index_document.assert_not_called()
        daemon._chunk_store.replace_document_chunks.assert_not_called()
        assert daemon.stats.mutation_upserts_skipped_covered == 1

    @pytest.mark.asyncio
    async def test_uncovered_upsert_reaches_pipeline(self) -> None:
        daemon = _covered_daemon(("cid-2", "cid-1"))
        pipeline_result = MagicMock(error=None, chunks_indexed=1)
        daemon._indexing_pipeline = MagicMock(
            index_document=AsyncMock(return_value=pipeline_result)
        )
        daemon._embedding_provider = MagicMock()
        daemon._chunk_store = MagicMock(replace_document_chunks=AsyncMock())
        daemon._sqlite_vec_backend = None
        _stub_resolution(daemon, [_upsert_mutation()])

        await daemon._consume_embedding_mutations([MagicMock()])

        daemon._indexing_pipeline.index_document.assert_awaited_once()
        assert daemon.stats.mutation_upserts_skipped_covered == 0


class TestFtsConsumerGate:
    def _fts_daemon(self, row: tuple[Any, ...] | None) -> tuple[SearchDaemon, list[Any]]:
        daemon = _covered_daemon(row)
        # Embedding inactive → FTS consumer is the chunk writer.
        daemon._indexing_pipeline = None
        daemon._embedding_provider = None
        daemon._chunk_store = MagicMock(delete_document_chunks=AsyncMock())
        daemon._sqlite_vec_backend = None
        _stub_resolution(daemon, [_upsert_mutation()])
        chunk_writes: list[Any] = []

        async def _record_chunks(path_id: str, path: str, content: str) -> None:
            chunk_writes.append((path_id, path, content))

        setattr(daemon, "_index_to_document_chunks", _record_chunks)  # noqa: B010
        return daemon, chunk_writes

    @pytest.mark.asyncio
    async def test_covered_upsert_skips_chunk_write(self) -> None:
        daemon, chunk_writes = self._fts_daemon(("cid-1", "cid-1"))
        await daemon._consume_fts_mutations([MagicMock()])
        assert chunk_writes == []
        assert daemon.stats.mutation_upserts_skipped_covered == 1

    @pytest.mark.asyncio
    async def test_uncovered_upsert_writes_chunks(self) -> None:
        daemon, chunk_writes = self._fts_daemon(("cid-2", "cid-1"))
        await daemon._consume_fts_mutations([MagicMock()])
        assert chunk_writes == [("pid-1", "/zone/z1/doc.pdf", "raw blob bytes")]
        assert daemon.stats.mutation_upserts_skipped_covered == 0
