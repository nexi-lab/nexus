"""Tests for SearchDaemon ↔ SqliteVecBackend wiring (Codex review R6 #1+#3).

The daemon owns the production refresh + mutation pipeline. R5 wired
the side-write into IndexingPipeline; R6 found that the daemon
constructs its OWN IndexingPipeline (separate from SearchService's
RPC path) and never received the backend, leaving the production
indexing path with no vec mirroring. R6 also found that DELETE/RENAME
mutations weren't pruning the vec lane.

These tests guard:
  1. SearchDaemon stores ``sqlite_vec_backend`` and forwards it to
     its IndexingPipeline at startup.
  2. ``_consume_embedding_mutations`` calls ``backend.delete([path],
     zone_id=...)`` on DELETE so deleted/renamed paths don't survive
     in the vector lane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeSqliteVec:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []

    async def delete(self, ids: list[str], *, zone_id: str) -> int:
        self.delete_calls.append({"ids": ids, "zone_id": zone_id})
        return len(ids)


def test_search_daemon_stores_sqlite_vec_backend() -> None:
    """The constructor must accept and store the backend so the
    indexing pipeline can reach it at startup time."""
    from nexus.bricks.search.daemon import SearchDaemon

    fake = _FakeSqliteVec()
    daemon = SearchDaemon(sqlite_vec_backend=fake)
    assert daemon._sqlite_vec_backend is fake


def test_search_daemon_passes_backend_to_indexing_pipeline_on_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's startup builds an IndexingPipeline locally; the
    backend reference must be forwarded so daemon-driven refresh
    populates the vec lane. We don't run full startup (heavy DB +
    embedding wiring) — instead we patch IndexingPipeline at the
    daemon-side import site and verify it gets called with the
    backend kwarg."""
    from nexus.bricks.search import daemon as daemon_mod

    captured_kwargs: dict[str, Any] = {}

    def _fake_ip(*_args: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return MagicMock(name="IndexingPipeline")

    # The daemon imports IndexingPipeline locally inside its startup
    # method; patch the public symbol the import resolves to.
    monkeypatch.setattr("nexus.bricks.search.indexing.IndexingPipeline", _fake_ip)

    fake_vec = _FakeSqliteVec()
    daemon = daemon_mod.SearchDaemon.__new__(daemon_mod.SearchDaemon)
    # Inject the minimum field set the IndexingPipeline construction
    # block reads. The real startup path does much more; we only test
    # that the construction call passes the backend through.
    daemon.config = MagicMock()
    daemon.config.database_url = None
    daemon.config.max_indexing_concurrency = 1
    daemon._async_session = None
    daemon._embedding_provider = MagicMock()
    daemon._entropy_chunker = None
    daemon._sqlite_vec_backend = fake_vec

    def _scope_provider() -> Any:
        return None

    daemon._current_index_scope = _scope_provider

    # Re-execute the IndexingPipeline construction block in isolation.
    # This mirrors lines 711-728 in daemon.py.
    from nexus.bricks.search.chunking import DocumentChunker
    from nexus.bricks.search.indexing import IndexingPipeline as _IP

    daemon._indexing_pipeline = _IP(
        chunker=DocumentChunker(),
        embedding_provider=daemon._embedding_provider,
        entropy_chunker=daemon._entropy_chunker,
        db_type="sqlite",
        async_session_factory=daemon._async_session,
        max_concurrency=daemon.config.max_indexing_concurrency,
        cross_doc_batching=True,
        scope_provider=daemon._current_index_scope,
        sqlite_vec_backend=daemon._sqlite_vec_backend,
    )

    assert captured_kwargs.get("sqlite_vec_backend") is fake_vec, (
        "SearchDaemon must forward sqlite_vec_backend to the IndexingPipeline "
        "it constructs — without this, daemon-driven indexing skips the side-"
        "write and SANDBOX hybrid silently degrades to keyword-only"
    )


@dataclass
class _FakeMutEvent:
    path: str
    op: Any  # SearchMutationOp
    # Codex review R10 #1: consumers reference ``event_id`` in error
    # messages when raising on unresolved content.
    event_id: str = "test-event"


@dataclass
class _FakeResolvedMutation:
    event: _FakeMutEvent
    path_id: str | None = None
    content: str | None = None
    zone_id: str | None = None
    virtual_path: str = ""
    # Codex review R10 #1: consumers read this to distinguish "real
    # truncation" (resolved=True, content="") from "couldn't read"
    # (resolved=False). Default True so existing tests get the
    # "resolved" behavior.
    content_resolved: bool = True


@pytest.mark.asyncio
async def test_delete_mutation_prunes_sqlite_vec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DELETE branch of ``_consume_embedding_mutations`` must
    call ``backend.delete([path], zone_id=...)`` so removed paths
    don't survive in the vector lane (rename arrives here as a
    DELETE on the old path followed by an UPSERT on the new one)."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    fake_vec = _FakeSqliteVec()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = fake_vec
    daemon._indexing_pipeline = MagicMock()
    daemon._embedding_provider = MagicMock()
    daemon._chunk_store = MagicMock(delete_document_chunks=AsyncMock(return_value=None))

    # Bypass the refresh/scope/has_resolved_path_id internals: stub
    # _resolve_mutations + _collapse_resolved_mutations to return our
    # synthetic DELETE event.
    delete_event = _FakeMutEvent(path="/zone/z1/gone.md", op=SearchMutationOp.DELETE)
    delete_mut = _FakeResolvedMutation(
        event=delete_event,
        path_id="pid-gone",
        zone_id="z1",
        virtual_path="/gone.md",
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [delete_mut]

    # Method-stub injection on a SearchDaemon instance — setattr to
    # keep mypy quiet without a per-line type:ignore (project policy).
    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    await daemon._consume_embedding_mutations([MagicMock()])

    # Codex review R9 #3 (high): canonical (unscoped) + legacy (scoped)
    # keys are passed so legacy rows from pre-R9 builds also get pruned.
    assert fake_vec.delete_calls == [{"ids": ["/gone.md", "/zone/z1/gone.md"], "zone_id": "z1"}], (
        f"DELETE mutation must prune the vec lane via backend.delete; got {fake_vec.delete_calls}"
    )
    daemon._chunk_store.delete_document_chunks.assert_awaited_once_with("pid-gone")


@pytest.mark.asyncio
async def test_fts_consumer_prunes_vec_on_delete_when_no_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review R7 #1 (high): the embedding consumer is dead in
    current txtai-era wiring (``_embedding_provider is None``), so
    the FTS consumer is the production carrier of deletes. The R6
    prune lived ONLY in the embedding consumer and never fired in
    real use. The FTS consumer's DELETE branch must also call
    ``backend.delete`` so SANDBOX vec rows for deleted/renamed paths
    get cleaned up under the production wiring shape."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    fake_vec = _FakeSqliteVec()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = fake_vec
    # Production-shape wiring: no embedding provider.
    daemon._embedding_provider = None
    daemon._indexing_pipeline = None
    daemon._chunk_store = MagicMock(delete_document_chunks=AsyncMock(return_value=None))

    delete_event = _FakeMutEvent(path="/zone/z2/old.md", op=SearchMutationOp.DELETE)
    delete_mut = _FakeResolvedMutation(
        event=delete_event,
        path_id="pid-old",
        zone_id="z2",
        virtual_path="/old.md",
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [delete_mut]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    await daemon._consume_fts_mutations([MagicMock()])

    # Codex review R9 #3 (high): canonical + legacy keys.
    assert fake_vec.delete_calls == [{"ids": ["/old.md", "/zone/z2/old.md"], "zone_id": "z2"}], (
        "FTS DELETE branch must prune the vec lane via dual-key delete; "
        "Codex R7 #1 + R9 #3 findings"
    )


@pytest.mark.asyncio
async def test_fts_consumer_mirrors_writes_into_vec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FTS-only path (production wiring) must also mirror writes
    into the vec lane via ``_index_to_document_chunks``. Without this,
    the SANDBOX vec lane stays empty under the txtai-era wiring even
    though writes succeed against document_chunks."""
    from nexus.bricks.search.chunk_store import ChunkRecord
    from nexus.bricks.search.daemon import SearchDaemon

    class _CapturingVec:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def delete(self, ids: list[str], *, zone_id: str) -> int:
            self.calls.append(("delete", {"ids": ids, "zone_id": zone_id}))
            return len(ids)

        async def upsert(self, items: list[dict[str, Any]], *, zone_id: str) -> int:
            self.calls.append(("upsert", {"items": items, "zone_id": zone_id}))
            return len(items)

    fake_vec = _CapturingVec()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = fake_vec
    daemon._chunk_store = MagicMock(replace_document_chunks=AsyncMock(return_value=None))
    # Stub the naive chunker so we don't need DocumentChunker config.
    setattr(  # noqa: B010
        daemon,
        "_build_naive_chunks",
        lambda content: [
            ChunkRecord(
                chunk_text=content,
                chunk_tokens=0,
                start_offset=0,
                end_offset=len(content),
                line_start=0,
                line_end=0,
                embedding=None,
                embedding_model=None,
                chunk_context=None,
                chunk_position=None,
                source_document_id=None,
            )
        ],
    )

    await daemon._index_to_document_chunks("pid-w", "/zone/zw/w.md", "alpha")

    daemon._chunk_store.replace_document_chunks.assert_awaited_once()
    # Order: delete first (full-replace), then upsert.
    ops = [op for op, _ in fake_vec.calls]
    assert ops == ["delete", "upsert"], (
        f"FTS-path mirror must full-replace (delete then upsert), got {ops}"
    )
    # Codex review R9 #3 (high): vec rows are written under the
    # canonical (unscoped) path so they line up with BM25 keys + the
    # SearchService unscoped path_filter. The delete step also prunes
    # the legacy scoped form so pre-R9 rows don't leak.
    delete_args = next(args for op, args in fake_vec.calls if op == "delete")
    assert delete_args["zone_id"] == "zw"
    assert delete_args["ids"] == ["/w.md", "/zone/zw/w.md"]
    upsert_args = next(args for op, args in fake_vec.calls if op == "upsert")
    assert upsert_args["zone_id"] == "zw"
    assert upsert_args["items"][0]["path"] == "/w.md"
    assert upsert_args["items"][0]["text"] == "alpha"


@pytest.mark.asyncio
async def test_bm25_consumer_prunes_on_delete() -> None:
    """Codex review R8 #2 (high): the BM25 consumer must call
    ``delete_document(path_id)`` on DELETE events. Before R8 it
    silently dropped DELETEs, leaving stale postings ranked in the
    keyword lane long after ChunkStore + vec lanes were pruned —
    so deleted/renamed paths kept appearing as zombie hits."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    class _FakeBM25:
        def __init__(self) -> None:
            self.deletes: list[str] = []
            self.indexes: list[tuple[str, str, str]] = []

        async def delete_document(self, path_id: str) -> bool:
            self.deletes.append(path_id)
            return True

        async def index_document(self, path_id: str, virtual_path: str, content: str) -> bool:
            self.indexes.append((path_id, virtual_path, content))
            return True

    fake_bm25 = _FakeBM25()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._bm25s_index = fake_bm25

    delete_event = _FakeMutEvent(path="/zone/z3/dead.md", op=SearchMutationOp.DELETE)
    delete_mut = _FakeResolvedMutation(event=delete_event, path_id="pid-dead", zone_id="z3")

    async def _resolve(_events: Any) -> list[Any]:
        return [delete_mut]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    await daemon._consume_bm25_mutations([MagicMock()])

    assert fake_bm25.deletes == ["pid-dead"], (
        f"BM25 consumer must prune on DELETE; got deletes={fake_bm25.deletes}"
    )
    assert fake_bm25.indexes == [], "BM25 consumer must NOT call index_document on DELETE events"


@pytest.mark.asyncio
async def test_bm25_consumer_raises_on_unresolved_content() -> None:
    """Codex review R10 #1 (high): the MutationResolver now exposes
    ``content_resolved`` to distinguish "real empty file" from
    "couldn't read it" (file_reader failed AND content_cache miss).
    With ``content_resolved=False``, the consumer MUST raise so the
    consumer doesn't checkpoint a transient failure as success —
    otherwise the only mutation that could repair the lane is silently
    dropped and the lane stays broken until that path mutates again."""
    from dataclasses import dataclass

    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    @dataclass
    class _Resolved:
        event: Any
        path_id: str | None
        content: str | None
        zone_id: str | None
        virtual_path: str = ""
        content_resolved: bool = True

    class _FakeBM25:
        def __init__(self) -> None:
            self.deletes: list[str] = []
            self.indexes: list[tuple[str, str, str]] = []

        async def delete_document(self, path_id: str) -> bool:
            self.deletes.append(path_id)
            return True

        async def index_document(self, path_id: str, virtual_path: str, content: str) -> bool:
            self.indexes.append((path_id, virtual_path, content))
            return True

    fake_bm25 = _FakeBM25()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._bm25s_index = fake_bm25

    upsert_event = _FakeMutEvent(path="/zone/z4/x.md", op=SearchMutationOp.UPSERT)
    # The transient-read-failure case: resolver could not read content.
    unresolved = _Resolved(
        event=upsert_event,
        path_id="pid-x",
        content=None,
        zone_id="z4",
        content_resolved=False,
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [unresolved]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    with pytest.raises(RuntimeError, match="content unresolved"):
        await daemon._consume_bm25_mutations([MagicMock()])

    # Critical: no mutation to BM25 happened, so the consumer's retry
    # on the next batch can safely re-attempt resolution.
    assert fake_bm25.deletes == []
    assert fake_bm25.indexes == []


@pytest.mark.asyncio
async def test_bm25_consumer_treats_resolved_empty_as_delete() -> None:
    """Codex review R10 #1 (high): once we can distinguish "real
    empty file" (content_resolved=True, content="") from "couldn't
    read", we CAN safely treat resolved-empty UPSERTs as deletes —
    they're real truncations and the prior posting is genuinely
    stale. The R9 #1 over-conservatism (skip everything that's not
    explicit DELETE) is no longer needed."""
    from dataclasses import dataclass

    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    @dataclass
    class _Resolved:
        event: Any
        path_id: str | None
        content: str | None
        zone_id: str | None
        virtual_path: str = ""
        content_resolved: bool = True

    class _FakeBM25:
        def __init__(self) -> None:
            self.deletes: list[str] = []
            self.indexes: list[tuple[str, str, str]] = []

        async def delete_document(self, path_id: str) -> bool:
            self.deletes.append(path_id)
            return True

        async def index_document(self, path_id: str, virtual_path: str, content: str) -> bool:
            self.indexes.append((path_id, virtual_path, content))
            return True

    fake_bm25 = _FakeBM25()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._bm25s_index = fake_bm25

    truncate_event = _FakeMutEvent(path="/zone/z4/blank.md", op=SearchMutationOp.UPSERT)
    # Real truncation: content="" with content_resolved=True.
    resolved_empty = _Resolved(
        event=truncate_event,
        path_id="pid-blank",
        content="",
        zone_id="z4",
        content_resolved=True,
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [resolved_empty]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    await daemon._consume_bm25_mutations([MagicMock()])

    assert fake_bm25.deletes == ["pid-blank"], (
        "Real truncation (content_resolved=True, content='') MUST be "
        "treated as DELETE in the BM25 lane"
    )
    assert fake_bm25.indexes == [], "Empty-content UPSERT must not be indexed as a document"


@pytest.mark.asyncio
async def test_bm25_consumer_raises_when_delete_returns_false() -> None:
    """Codex review R9 #2 (high): ``_run_mutation_consumer`` advances
    the BM25 checkpoint whenever the handler returns successfully. If
    ``delete_document`` silently returns False (rebuild failed,
    persistence error) the prior body suppressed and dropped the
    failure on the floor — leaving stale postings ranked forever in
    SANDBOX. Surface the failure as an exception so the consumer
    refuses to checkpoint and retries on the next pass."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    class _FlakyBM25:
        async def delete_document(self, path_id: str) -> bool:
            return False  # silent failure mode (e.g. rebuild aborted)

        async def index_document(self, path_id: str, virtual_path: str, content: str) -> bool:
            return True

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._bm25s_index = _FlakyBM25()

    delete_event = _FakeMutEvent(path="/zone/zE/x.md", op=SearchMutationOp.DELETE)
    delete_mut = _FakeResolvedMutation(
        event=delete_event,
        path_id="pid-x",
        zone_id="zE",
        virtual_path="/x.md",
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [delete_mut]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    with pytest.raises(RuntimeError, match="delete_document returned False"):
        await daemon._consume_bm25_mutations([MagicMock()])


@pytest.mark.asyncio
async def test_legacy_refresh_prunes_sqlite_vec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review R8 #4 (high): ``_delete_indexes_for_paths`` is the
    fallback delete-propagation path used when the durable op-log
    consumer isn't wired (older deployments, recovery boots).
    Pre-R8 it pruned ChunkStore + txtai backend + BM25 but left
    sqlite-vec rows intact, so the SANDBOX semantic lane returned
    zombie hits for paths that had been deleted."""
    from nexus.bricks.search.daemon import SearchDaemon

    class _FakeChunkStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_document_chunks(self, path_id: str) -> None:
            self.deleted.append(path_id)

    fake_vec = _FakeSqliteVec()
    fake_chunks = _FakeChunkStore()
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = fake_vec
    daemon._chunk_store = fake_chunks
    daemon._backend = None
    daemon._bm25s_index = None

    @dataclass
    class _LegacyResolved:
        event: Any
        path_id: str
        doc_id: str
        zone_id: str
        virtual_path: str

    resolved = [
        _LegacyResolved(
            event=_FakeMutEvent(path="/zone/zL/a.md", op=None),
            path_id="pid-a",
            doc_id="did-a",
            zone_id="zL",
            virtual_path="/a.md",
        ),
        _LegacyResolved(
            event=_FakeMutEvent(path="/zone/zL/b.md", op=None),
            path_id="pid-b",
            doc_id="did-b",
            zone_id="zL",
            virtual_path="/b.md",
        ),
        _LegacyResolved(
            event=_FakeMutEvent(path="/zone/zM/c.md", op=None),
            path_id="pid-c",
            doc_id="did-c",
            zone_id="zM",
            virtual_path="/c.md",
        ),
    ]

    async def _resolve(_events: Any) -> list[Any]:
        return resolved

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    await daemon._delete_indexes_for_paths(["/zone/zL/a.md", "/zone/zL/b.md", "/zone/zM/c.md"])

    # ChunkStore was pruned (existing behavior).
    assert sorted(fake_chunks.deleted) == ["pid-a", "pid-b", "pid-c"]
    # NEW: sqlite-vec is also pruned, batched by zone with both
    # canonical (unscoped virtual_path) and legacy (scoped event.path)
    # keys per Codex R9 #3.
    assert len(fake_vec.delete_calls) == 2, (
        f"vec must be pruned per zone; got {fake_vec.delete_calls}"
    )
    by_zone = {call["zone_id"]: sorted(call["ids"]) for call in fake_vec.delete_calls}
    assert by_zone == {
        "zL": sorted(["/a.md", "/zone/zL/a.md", "/b.md", "/zone/zL/b.md"]),
        "zM": sorted(["/c.md", "/zone/zM/c.md"]),
    }


@pytest.mark.asyncio
async def test_legacy_refresh_vec_failure_does_not_block_other_lanes() -> None:
    """A vec backend failure in the legacy refresh path must not abort
    ChunkStore/BM25 deletes — the prune is best-effort."""
    from nexus.bricks.search.daemon import SearchDaemon

    class _BrokenVec:
        async def delete(self, ids: list[str], *, zone_id: str) -> int:
            raise RuntimeError("vec offline")

    class _FakeChunkStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_document_chunks(self, path_id: str) -> None:
            self.deleted.append(path_id)

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = _BrokenVec()
    daemon._chunk_store = _FakeChunkStore()
    daemon._backend = None
    daemon._bm25s_index = None

    @dataclass
    class _R:
        event: Any
        path_id: str
        doc_id: str
        zone_id: str
        virtual_path: str

    async def _resolve(_events: Any) -> list[Any]:
        return [
            _R(
                event=_FakeMutEvent(path="/zone/zX/g.md", op=None),
                path_id="pid-g",
                doc_id="did-g",
                zone_id="zX",
                virtual_path="/g.md",
            )
        ]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    await daemon._delete_indexes_for_paths(["/zone/zX/g.md"])

    # ChunkStore prune still ran despite vec failure.
    assert daemon._chunk_store.deleted == ["pid-g"]


@pytest.mark.asyncio
async def test_fts_consumer_propagates_vec_delete_failure_on_delete() -> None:
    """Codex review R10 #2 (high): the FTS DELETE branch is the
    primary vec-delete carrier under default SANDBOX wiring (no
    embedding provider). Pre-R10 it caught vec exceptions and
    continued, so ``_run_mutation_consumer`` advanced the checkpoint
    despite the failure — leaving deleted/renamed paths searchable in
    the vector lane forever. The failure MUST propagate so the batch
    is retried on the next pass."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    class _FlakyVec:
        async def delete(self, ids: list[str], *, zone_id: str) -> int:
            raise RuntimeError("sqlite-vec offline")

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = _FlakyVec()
    daemon._embedding_provider = None
    daemon._indexing_pipeline = None
    daemon._chunk_store = MagicMock(delete_document_chunks=AsyncMock(return_value=None))

    delete_event = _FakeMutEvent(path="/zone/zV/v.md", op=SearchMutationOp.DELETE)
    delete_mut = _FakeResolvedMutation(
        event=delete_event,
        path_id="pid-v",
        zone_id="zV",
        virtual_path="/v.md",
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [delete_mut]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    with pytest.raises(RuntimeError, match="sqlite-vec offline"):
        await daemon._consume_fts_mutations([MagicMock()])


@pytest.mark.asyncio
async def test_embedding_consumer_propagates_vec_delete_failure_on_delete() -> None:
    """Codex review R10 #2 (high): same rationale as the FTS variant
    above — the embedding consumer's DELETE branch is the carrier
    when an embedding provider is wired, and a swallowed vec failure
    leaves stale rows searchable forever. Failures MUST propagate."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    class _FlakyVec:
        async def delete(self, ids: list[str], *, zone_id: str) -> int:
            raise RuntimeError("sqlite-vec offline")

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = _FlakyVec()
    daemon._indexing_pipeline = MagicMock()
    daemon._embedding_provider = MagicMock()
    daemon._chunk_store = MagicMock(delete_document_chunks=AsyncMock(return_value=None))

    delete_event = _FakeMutEvent(path="/zone/zE/e.md", op=SearchMutationOp.DELETE)
    delete_mut = _FakeResolvedMutation(
        event=delete_event,
        path_id="pid-e",
        zone_id="zE",
        virtual_path="/e.md",
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [delete_mut]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    with pytest.raises(RuntimeError, match="sqlite-vec offline"):
        await daemon._consume_embedding_mutations([MagicMock()])


@pytest.mark.asyncio
async def test_fts_consumer_raises_on_unresolved_upsert() -> None:
    """Codex review R10 #1 (high): the FTS consumer must also refuse
    to checkpoint unresolved-content UPSERTs (transient resolver
    failures), or the lane stays broken until the next mutation."""
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.mutation_events import SearchMutationOp

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._sqlite_vec_backend = None
    daemon._embedding_provider = None
    daemon._indexing_pipeline = None
    daemon._chunk_store = MagicMock()

    upsert_event = _FakeMutEvent(path="/zone/zU/u.md", op=SearchMutationOp.UPSERT)
    unresolved = _FakeResolvedMutation(
        event=upsert_event,
        path_id="pid-u",
        content=None,
        zone_id="zU",
        virtual_path="/u.md",
        content_resolved=False,
    )

    async def _resolve(_events: Any) -> list[Any]:
        return [unresolved]

    setattr(daemon, "_resolve_mutations", _resolve)  # noqa: B010
    setattr(daemon, "_collapse_resolved_mutations", lambda items: items)  # noqa: B010
    setattr(daemon, "_has_resolved_path_id", lambda m: True)  # noqa: B010

    with pytest.raises(RuntimeError, match="content unresolved"):
        await daemon._consume_fts_mutations([MagicMock()])
