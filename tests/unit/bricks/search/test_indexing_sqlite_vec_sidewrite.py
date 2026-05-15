"""Tests for the IndexingPipeline → SqliteVecBackend side-write
(Codex review R5 #2 — high).

The hybrid SANDBOX search lane reads from ``SearchService._sqlite_vec_
backend``, so unless the production indexing flow also writes there,
the vec lane is empty in real use and SANDBOX hybrid silently degrades
to keyword-only. These tests guard the wiring that mirrors every
``IndexingPipeline._bulk_insert`` into the local sqlite-vec backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.search.chunking import DocumentChunker
from nexus.bricks.search.indexing import IndexingPipeline


@dataclass
class _FakeChunk:
    """Looks chunk-shaped to ``_bulk_insert``."""

    text: str
    tokens: int = 0
    start_offset: int = 0
    end_offset: int = 0
    line_start: int = 0
    line_end: int = 0
    heading_prefix: str | None = None


@dataclass
class _FakeChunkedDoc:
    """Mimics ``_ChunkedDoc`` (private to indexing.py) so we can drive
    ``_bulk_insert`` without running phase 1."""

    path: str
    path_id: str
    chunks: list[_FakeChunk]
    chunk_texts: list[str] = field(default_factory=list)
    context_jsons: list[Any] = field(default_factory=list)
    context_positions: list[Any] = field(default_factory=list)
    source_document_id: str | None = None


class _FakeSqliteVec:
    """Records every ``upsert``/``delete`` call so the test can assert
    the side-write fires with the right shape and ordering."""

    def __init__(
        self,
        *,
        raise_on_upsert: Exception | None = None,
        raise_on_delete: Exception | None = None,
    ) -> None:
        self.upsert_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        # Captures the order operations arrived in, so tests can assert
        # delete-before-upsert (replace semantics).
        self.call_log: list[str] = []
        self._raise_upsert = raise_on_upsert
        self._raise_delete = raise_on_delete

    async def upsert(self, items: list[dict[str, Any]], *, zone_id: str) -> int:
        self.call_log.append("upsert")
        self.upsert_calls.append({"items": items, "zone_id": zone_id})
        if self._raise_upsert is not None:
            raise self._raise_upsert
        return len(items)

    async def delete(self, ids: list[str], *, zone_id: str) -> int:
        self.call_log.append("delete")
        self.delete_calls.append({"ids": ids, "zone_id": zone_id})
        if self._raise_delete is not None:
            raise self._raise_delete
        return len(ids)


def _make_pipeline(sqlite_vec: Any) -> IndexingPipeline:
    """Construct a pipeline wired to a fake sqlite-vec backend. The
    chunker / async_session_factory are unused in these tests because
    we drive ``_bulk_insert`` directly."""
    return IndexingPipeline(
        chunker=MagicMock(spec=DocumentChunker),
        embedding_provider=None,
        async_session_factory=MagicMock(name="async_sf"),
        sqlite_vec_backend=sqlite_vec,
    )


@pytest.mark.asyncio
async def test_bulk_insert_mirrors_chunks_into_sqlite_vec() -> None:
    """The headline R5 #2 fix: every successful ``_bulk_insert`` must
    also push the chunks into the sqlite-vec backend so the hybrid
    vector lane has data to fuse."""
    fake_vec = _FakeSqliteVec()
    pipeline = _make_pipeline(fake_vec)
    doc = _FakeChunkedDoc(
        path="/zone/z1/notes/x.md",
        path_id="pid-1",
        chunks=[_FakeChunk(text="hello world"), _FakeChunk(text="goodbye world")],
    )

    # Stub out the SQL ChunkStore write — we only care about the
    # side-write here. Patch ``replace_document_chunks`` on the
    # ChunkStore class used inside ``_bulk_insert``.
    with patch(
        "nexus.bricks.search.indexing.ChunkStore",
        return_value=MagicMock(replace_document_chunks=AsyncMock(return_value=None)),
    ):
        await pipeline._bulk_insert(doc)

    assert len(fake_vec.upsert_calls) == 1, (
        "every _bulk_insert must trigger exactly one sqlite-vec upsert"
    )
    call = fake_vec.upsert_calls[0]
    assert call["zone_id"] == "z1", (
        "zone_id must be derived from the indexed path so vec rows are "
        "filtered correctly at search time"
    )
    items = call["items"]
    # Codex review R9 #3 (high): vec rows are written under the
    # canonical (unscoped) virtual_path so they line up with BM25 keys
    # and the SearchService unscoped path_filter.
    assert [it["path"] for it in items] == ["/notes/x.md"] * 2
    assert [it["text"] for it in items] == ["hello world", "goodbye world"]
    assert [it["chunk_index"] for it in items] == [0, 1]


@pytest.mark.asyncio
async def test_bulk_insert_without_vec_backend_is_unchanged() -> None:
    """When no vec backend is wired (non-SANDBOX profiles or SANDBOX
    with vec opted out), ``_bulk_insert`` must remain a pure-SQL
    write — no surprise external calls."""
    pipeline = IndexingPipeline(
        chunker=MagicMock(spec=DocumentChunker),
        embedding_provider=None,
        async_session_factory=MagicMock(name="async_sf"),
        sqlite_vec_backend=None,
    )
    doc = _FakeChunkedDoc(
        path="/zone/z/x.md",
        path_id="pid",
        chunks=[_FakeChunk(text="t")],
    )

    chunk_store = MagicMock(replace_document_chunks=AsyncMock(return_value=None))
    with patch("nexus.bricks.search.indexing.ChunkStore", return_value=chunk_store):
        await pipeline._bulk_insert(doc)

    chunk_store.replace_document_chunks.assert_awaited_once()


@pytest.mark.asyncio
async def test_vec_backend_failure_does_not_break_primary_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Side-write failures must be best-effort — a flaky vec backend
    must not abort the primary BM25S/Txtai write. The user gets a
    ``semantic_degraded=True`` flag at search time anyway."""
    import logging

    fake_vec = _FakeSqliteVec(raise_on_upsert=RuntimeError("simulated vec outage"))
    pipeline = _make_pipeline(fake_vec)
    doc = _FakeChunkedDoc(
        path="/zone/z/x.md",
        path_id="pid",
        chunks=[_FakeChunk(text="t")],
    )

    chunk_store = MagicMock(replace_document_chunks=AsyncMock(return_value=None))
    caplog.set_level(logging.WARNING, logger="nexus.bricks.search.indexing")
    with patch("nexus.bricks.search.indexing.ChunkStore", return_value=chunk_store):
        # MUST NOT raise — the side-write is best-effort.
        await pipeline._bulk_insert(doc)

    chunk_store.replace_document_chunks.assert_awaited_once()
    # And the warning must explain what degraded.
    warns = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "sqlite-vec side-write failed" in r.getMessage()
    ]
    assert len(warns) == 1, (
        "vec failures must surface as exactly one WARNING per failed doc — "
        "silent failure would hide the degradation from operators"
    )


@pytest.mark.asyncio
async def test_empty_doc_does_not_call_upsert() -> None:
    """A doc with zero chunks (e.g. empty file) has nothing to mirror.
    Avoid a no-op upsert call to keep the side-write traffic correlated
    1:1 with actual indexable content."""
    fake_vec = _FakeSqliteVec()
    pipeline = _make_pipeline(fake_vec)
    doc = _FakeChunkedDoc(path="/zone/z/empty.md", path_id="pid", chunks=[])

    with patch(
        "nexus.bricks.search.indexing.ChunkStore",
        return_value=MagicMock(replace_document_chunks=AsyncMock(return_value=None)),
    ):
        await pipeline._bulk_insert(doc)

    assert fake_vec.upsert_calls == []
    assert fake_vec.delete_calls == []


@pytest.mark.asyncio
async def test_bulk_insert_uses_full_replace_semantics() -> None:
    """Codex review R6 #2 (high): ``upsert`` only replaces rows whose
    ``(zone_id, path, chunk_index)`` matches an incoming tuple, so a
    doc that shrinks (5 chunks → 3) would leave chunks 3 and 4
    stranded with stale text. ``ChunkStore.replace_document_chunks``
    has full-replace semantics; the side-write must mirror that
    contract by deleting all rows for ``(zone_id, path)`` BEFORE the
    upsert. This test guards the delete-before-upsert ordering."""
    fake_vec = _FakeSqliteVec()
    pipeline = _make_pipeline(fake_vec)
    doc = _FakeChunkedDoc(
        path="/zone/z/shrunk.md",
        path_id="pid",
        chunks=[_FakeChunk(text="only chunk after shrink")],
    )

    with patch(
        "nexus.bricks.search.indexing.ChunkStore",
        return_value=MagicMock(replace_document_chunks=AsyncMock(return_value=None)),
    ):
        await pipeline._bulk_insert(doc)

    # Order matters: delete must precede upsert.
    assert fake_vec.call_log == ["delete", "upsert"], (
        f"side-write must full-replace (delete then upsert), got {fake_vec.call_log}. "
        f"Without the delete, a doc shrinking from N chunks to fewer would leave "
        f"stale higher-index chunks searchable in the vec lane."
    )
    # Delete targets the canonical (unscoped) path AND the legacy
    # scoped form so pre-R9 rows are also pruned (Codex R9 #3).
    assert fake_vec.delete_calls == [{"ids": ["/shrunk.md", "/zone/z/shrunk.md"], "zone_id": "z"}]
    assert fake_vec.upsert_calls[0]["zone_id"] == "z"


@pytest.mark.asyncio
async def test_descoped_paths_are_pruned_from_vec() -> None:
    """Codex review R7 #3 (high): when a previously-indexed path
    becomes out-of-scope, the scope filter drops it from indexing
    BUT its old vec rows survive — so it stays searchable in the
    SANDBOX hybrid lane long after admin policy excluded it.
    ``index_documents`` must call ``backend.delete`` for de-scoped
    paths so the vec lane mirrors the scope decision."""
    fake_vec = _FakeSqliteVec()
    pipeline = IndexingPipeline(
        chunker=MagicMock(spec=DocumentChunker),
        embedding_provider=None,
        async_session_factory=MagicMock(name="async_sf"),
        sqlite_vec_backend=fake_vec,
        # Scope filter that rejects everything → all paths are descoped.
        scope_provider=lambda: _ALL_OUT_OF_SCOPE,
    )

    results = await pipeline.index_documents(
        [
            ("/zone/za/banned.md", "content", "pid-a"),
            ("/zone/zb/banned2.md", "content", "pid-b"),
        ]
    )

    # Every doc came back as zero-chunk (correct).
    assert all(r.chunks_indexed == 0 for r in results)
    # Each de-scoped path's prior vec rows were pruned. Calls are
    # batched per-zone, so we get one delete call per zone (2 total).
    # Codex review R9 #3 (high): canonical (unscoped) AND legacy
    # (scoped) keys are both passed so pre-R9 rows are pruned too.
    by_zone = {call["zone_id"]: set(call["ids"]) for call in fake_vec.delete_calls}
    assert by_zone == {
        "za": {"/banned.md", "/zone/za/banned.md"},
        "zb": {"/banned2.md", "/zone/zb/banned2.md"},
    }, f"de-scoped paths must be pruned from vec; got {fake_vec.delete_calls}"


@pytest.mark.asyncio
async def test_zero_chunk_doc_prunes_vec() -> None:
    """Codex review R7 #2 (high): an in-scope doc that produces
    zero chunks (file shrunk to empty, parser returned nothing) is
    a REPLACE, not a no-op — old vec rows must be pruned. The
    standard delete-before-upsert in ``_bulk_insert`` doesn't fire
    because phase 3 only runs for non-empty chunked_docs."""
    fake_vec = _FakeSqliteVec()
    pipeline = IndexingPipeline(
        chunker=MagicMock(spec=DocumentChunker),
        embedding_provider=None,
        async_session_factory=MagicMock(name="async_sf"),
        sqlite_vec_backend=fake_vec,
    )

    # Patch _chunk_document to return an in-scope doc with zero chunks.
    async def _empty_chunk(_self: Any, path: str, _content: str, path_id: str) -> Any:
        # Mirrors the _ChunkedDoc shape but with empty chunks.
        return _FakeChunkedDoc(path=path, path_id=path_id, chunks=[])

    with patch.object(IndexingPipeline, "_chunk_document", _empty_chunk):
        results = await pipeline.index_documents([("/zone/zc/empty.md", "", "pid-empty")])

    assert results[0].chunks_indexed == 0
    # Vec prune fired exactly once for the empty replacement, with both
    # canonical (unscoped) AND legacy (scoped) keys per Codex R9 #3.
    assert fake_vec.delete_calls == [
        {"ids": ["/empty.md", "/zone/zc/empty.md"], "zone_id": "zc"}
    ], (
        f"in-scope zero-chunk doc must trigger a vec prune (replace = "
        f"delete on empty); got {fake_vec.delete_calls}"
    )
    # No upsert — there's nothing to insert.
    assert fake_vec.upsert_calls == []


# Sentinel used by the descope test; treated as "everything is out
# of scope" by the patched is_path_indexed below. Must be defined
# AFTER the imports at module top (no forward ref tricks).
class _AllOutOfScope:
    """Stand-in for IndexScope that rejects every path."""


_ALL_OUT_OF_SCOPE = _AllOutOfScope()


@pytest.fixture(autouse=True)
def _patch_scope_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the scope predicate treat ``_ALL_OUT_OF_SCOPE`` as
    rejecting every path. Other scope objects fall through unchanged."""
    from nexus.bricks.search import indexing as ind

    real_is_path_indexed = ind.is_path_indexed

    def _fake(scope: Any, zone_id: str, virtual_path: str) -> bool:
        if scope is _ALL_OUT_OF_SCOPE:
            return False
        return real_is_path_indexed(scope, zone_id, virtual_path)

    monkeypatch.setattr(ind, "is_path_indexed", _fake)


@pytest.mark.asyncio
async def test_zero_chunk_doc_prunes_chunk_store() -> None:
    """Codex review R8 #3 (high): in-scope zero-chunk replacement
    must also clear ``document_chunks`` rows for the path_id. The
    R7 fix only pruned the vec lane, leaving keyword-lane FTS rows
    intact — so deleted/truncated docs kept ranking under BM25."""
    fake_vec = _FakeSqliteVec()
    pipeline = IndexingPipeline(
        chunker=MagicMock(spec=DocumentChunker),
        embedding_provider=None,
        async_session_factory=MagicMock(name="async_sf"),
        sqlite_vec_backend=fake_vec,
    )

    async def _empty_chunk(_self: Any, path: str, _content: str, path_id: str) -> Any:
        return _FakeChunkedDoc(path=path, path_id=path_id, chunks=[])

    captured_store = MagicMock(delete_document_chunks=AsyncMock(return_value=None))
    with (
        patch.object(IndexingPipeline, "_chunk_document", _empty_chunk),
        patch("nexus.bricks.search.indexing.ChunkStore", return_value=captured_store),
    ):
        results = await pipeline.index_documents(
            [
                ("/zone/zc/a.md", "", "pid-a"),
                ("/zone/zc/b.md", "", "pid-b"),
            ]
        )

    assert all(r.chunks_indexed == 0 for r in results)
    # Both path_ids were pruned from the canonical chunk store.
    delete_calls = captured_store.delete_document_chunks.await_args_list
    pruned_ids = sorted(call.args[0] for call in delete_calls)
    assert pruned_ids == ["pid-a", "pid-b"], (
        f"zero-chunk replacement must prune document_chunks for every empty "
        f"path_id; got pruned={pruned_ids}"
    )


@pytest.mark.asyncio
async def test_zero_chunk_chunk_store_failure_does_not_abort_indexing() -> None:
    """ChunkStore prune is best-effort — a failure must not abort
    the indexing batch (other docs still need their results)."""
    fake_vec = _FakeSqliteVec()
    pipeline = IndexingPipeline(
        chunker=MagicMock(spec=DocumentChunker),
        embedding_provider=None,
        async_session_factory=MagicMock(name="async_sf"),
        sqlite_vec_backend=fake_vec,
    )

    async def _empty_chunk(_self: Any, path: str, _content: str, path_id: str) -> Any:
        return _FakeChunkedDoc(path=path, path_id=path_id, chunks=[])

    broken_store = MagicMock(
        delete_document_chunks=AsyncMock(side_effect=RuntimeError("chunk-store down"))
    )
    with (
        patch.object(IndexingPipeline, "_chunk_document", _empty_chunk),
        patch("nexus.bricks.search.indexing.ChunkStore", return_value=broken_store),
    ):
        results = await pipeline.index_documents([("/zone/zc/a.md", "", "pid-a")])

    # Result still surfaces; chunks_indexed=0 reflects the empty doc.
    assert results[0].chunks_indexed == 0
    # Vec prune still fired even though chunk-store prune failed.
    assert fake_vec.delete_calls == [{"ids": ["/a.md", "/zone/zc/a.md"], "zone_id": "zc"}]


@pytest.mark.asyncio
async def test_delete_failure_skips_upsert() -> None:
    """If the delete-prune step fails, the upsert MUST be skipped (any
    other ordering would mix new chunks with stale shrink-survivors).
    The whole side-write degrades cleanly to a logged warning so the
    primary write isn't aborted."""
    fake_vec = _FakeSqliteVec(raise_on_delete=RuntimeError("delete boom"))
    pipeline = _make_pipeline(fake_vec)
    doc = _FakeChunkedDoc(path="/zone/z/x.md", path_id="pid", chunks=[_FakeChunk(text="t")])

    chunk_store = MagicMock(replace_document_chunks=AsyncMock(return_value=None))
    with patch("nexus.bricks.search.indexing.ChunkStore", return_value=chunk_store):
        await pipeline._bulk_insert(doc)  # MUST NOT raise

    # Primary BM25S/Txtai write fired regardless.
    chunk_store.replace_document_chunks.assert_awaited_once()
    # Upsert must NOT have run after the delete failed.
    assert fake_vec.upsert_calls == [], (
        "delete failure must short-circuit the side-write — running the upsert "
        "anyway would leave the vec lane in an inconsistent state"
    )
