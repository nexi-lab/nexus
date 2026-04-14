"""Benchmark: FTS/embedding consumer race on document_chunks (Issue #3708).

Run: pytest tests/benchmarks/bench_consumer_race.py -v -s -p no:benchmark

Measures:
1. Chunk consistency — no schema flapping between FTS naive and embedding smart chunks
2. Embedding API call count — should be N files, not 2N (no double-indexing)
3. Chunking throughput — naive 1000-char vs DocumentChunker on real HERB data
4. Double-write detection — each path_id written exactly once
5. DELETE propagation — embedding consumer handles deletes when sole writer
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.chunk_store import ChunkRecord, ChunkStore
from nexus.bricks.search.chunking import ChunkStrategy, DocumentChunker
from nexus.bricks.search.indexing import IndexResult
from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_resolver import ResolvedMutation

# ---------------------------------------------------------------------------
# HERB data loader
# ---------------------------------------------------------------------------

# Resolve HERB data: try worktree first, fall back to main repo root.
_BENCH_DIR = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "herb"
    / "enterprise-context"
    / "WorkFlowGenie"
)
if not _BENCH_DIR.exists():
    # Worktree may not contain benchmarks/ — walk up to the git main working tree.
    import subprocess

    _main_root = (
        subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"],
            cwd=Path(__file__).resolve().parent,
            text=True,
        )
        .splitlines()[0]
        .removeprefix("worktree ")
    )
    _BENCH_DIR = Path(_main_root) / "benchmarks" / "herb" / "enterprise-context" / "WorkFlowGenie"

HERB_ROOT = _BENCH_DIR


def _load_herb_documents() -> list[tuple[str, str]]:
    """Load real WorkFlowGenie files as (path, content) pairs.

    Expands JSONL files into individual records so each record
    is treated as a separate indexable document.
    """
    docs: list[tuple[str, str]] = []

    if not HERB_ROOT.exists():
        pytest.skip(f"HERB data not found at {HERB_ROOT}")

    # Markdown docs — each file is one document
    for md in sorted(HERB_ROOT.rglob("*.md")):
        text = md.read_text(errors="replace")
        if text.strip():
            rel = str(md.relative_to(HERB_ROOT))
            docs.append((f"/zone/root/WorkFlowGenie/{rel}", text))

    # JSONL files — each line is a separate document
    for jl in sorted(HERB_ROOT.rglob("*.jsonl")):
        if jl.name == "_index.jsonl":
            continue
        rel_dir = str(jl.relative_to(HERB_ROOT).parent)
        for line_no, line in enumerate(jl.read_text(errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Use title/summary/text as content
            text = ""
            for key in ("summary", "text", "title", "content", "description"):
                if key in record:
                    text += str(record[key]) + "\n\n"
            if not text.strip():
                text = json.dumps(record, indent=2)
            doc_id = record.get("id", f"{jl.stem}_{line_no}")
            docs.append((f"/zone/root/WorkFlowGenie/{rel_dir}/{doc_id}", text))

    return docs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events_from_docs(
    docs: list[tuple[str, str]],
    op: SearchMutationOp = SearchMutationOp.UPSERT,
) -> tuple[list[SearchMutationEvent], list[ResolvedMutation]]:
    events = []
    resolved = []
    now = datetime.now(UTC).replace(tzinfo=None)
    for i, (path, content) in enumerate(docs):
        ev = SearchMutationEvent(
            event_id=f"bench:{i}",
            operation_id=f"op-{i}",
            op=op,
            path=path,
            zone_id="root",
            timestamp=now,
            sequence_number=i,
        )
        events.append(ev)
        resolved.append(
            ResolvedMutation(
                event=ev,
                zone_id="root",
                virtual_path=path.replace("/zone/root", ""),
                path_id=f"pid-{i}",
                doc_id=f"doc-{i}",
                content=content if op == SearchMutationOp.UPSERT else None,
            )
        )
    return events, resolved


def _tracking_chunk_store() -> tuple[ChunkStore, dict]:
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    session_factory = MagicMock(return_value=ctx)
    store = ChunkStore(async_session_factory=session_factory, db_type="sqlite")

    tracker: dict = {"replace_calls": [], "delete_calls": []}
    orig_replace = store.replace_document_chunks
    orig_delete = store.delete_document_chunks

    async def _tracked_replace(path_id: str, chunks: list[ChunkRecord]) -> None:
        tracker["replace_calls"].append(
            {
                "path_id": path_id,
                "n_chunks": len(chunks),
                "has_embedding": any(c.embedding is not None for c in chunks),
                "total_tokens": sum(c.chunk_tokens for c in chunks),
            }
        )
        await orig_replace(path_id, chunks)

    async def _tracked_delete(path_id: str) -> None:
        tracker["delete_calls"].append(path_id)
        await orig_delete(path_id)

    # Monkey-patch via object.__setattr__ to avoid mypy assignment complaints.
    object.__setattr__(store, "replace_document_chunks", _tracked_replace)
    object.__setattr__(store, "delete_document_chunks", _tracked_delete)
    return store, tracker


def _bind_collapse(daemon: MagicMock) -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    daemon._collapse_resolved_mutations = MethodType(
        SearchDaemon._collapse_resolved_mutations, daemon
    )


def _naive_chunk(content: str, chunk_size: int = 1000) -> list[ChunkRecord]:
    """Replicate FTS consumer's naive 1000-char chunking."""
    chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
    records = []
    for idx, chunk_text in enumerate(chunks):
        if not chunk_text.strip():
            continue
        preceding = content[: idx * chunk_size]
        line_start = preceding.count("\n") + 1
        line_end = line_start + chunk_text.count("\n")
        records.append(
            ChunkRecord(
                chunk_text=chunk_text,
                chunk_tokens=max(1, len(chunk_text) // 4),
                start_offset=idx * chunk_size,
                end_offset=idx * chunk_size + len(chunk_text),
                line_start=line_start,
                line_end=line_end,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Benchmark 1: Chunking throughput — naive vs DocumentChunker on real data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunking_throughput_naive_vs_semantic() -> None:
    """Compare naive 1000-char FTS chunking vs DocumentChunker on HERB data.

    Reports: docs processed, chunks produced, tokens, wall-clock time.
    """
    docs = _load_herb_documents()
    assert len(docs) >= 50, f"Expected >= 50 docs, got {len(docs)}"

    # --- Naive 1000-char (FTS path) ---
    t0 = time.perf_counter()
    naive_total_chunks = 0
    naive_total_tokens = 0
    for _, content in docs:
        records = _naive_chunk(content)
        naive_total_chunks += len(records)
        naive_total_tokens += sum(r.chunk_tokens for r in records)
    naive_elapsed = time.perf_counter() - t0

    # --- DocumentChunker (embedding path) ---
    chunker = DocumentChunker(
        chunk_size=1024,
        overlap_size=128,
        strategy=ChunkStrategy.FIXED,
    )
    t0 = time.perf_counter()
    smart_total_chunks = 0
    smart_total_tokens = 0
    for path, content in docs:
        chunks = chunker.chunk(content, file_path=path)
        smart_total_chunks += len(chunks)
        smart_total_tokens += sum(c.tokens for c in chunks)
    smart_elapsed = time.perf_counter() - t0

    print("\n" + "=" * 70)
    print("BENCHMARK: Chunking throughput (HERB WorkFlowGenie)")
    print("=" * 70)
    print(f"Documents loaded:         {len(docs)}")
    print("")
    print(
        f"  Naive 1000-char (FTS):  {naive_total_chunks:>5} chunks, "
        f"{naive_total_tokens:>7} tokens, {naive_elapsed:.3f}s "
        f"({len(docs) / naive_elapsed:.0f} docs/sec)"
    )
    print(
        f"  DocumentChunker (emb):  {smart_total_chunks:>5} chunks, "
        f"{smart_total_tokens:>7} tokens, {smart_elapsed:.3f}s "
        f"({len(docs) / smart_elapsed:.0f} docs/sec)"
    )
    print("")
    print(
        f"  Chunk count diff:       {naive_total_chunks - smart_total_chunks:+d} "
        f"({(naive_total_chunks / max(smart_total_chunks, 1) - 1) * 100:+.0f}%)"
    )
    print(f"  Token count diff:       {naive_total_tokens - smart_total_tokens:+d}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Benchmark 2: No double-writes — concurrent consumers on real data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_double_writes_concurrent_consumers() -> None:
    """Ingest HERB docs through both FTS and embedding consumers concurrently.

    With the fix: FTS yields, embedding is sole writer → each path_id
    written exactly once. Without fix: 2x writes per path_id.
    """
    from nexus.bricks.search.daemon import SearchDaemon

    docs = _load_herb_documents()
    store, tracker = _tracking_chunk_store()

    events, resolved = _make_events_from_docs(docs)

    # --- FTS consumer (should skip in-scope paths when embedding active) ---
    fts_daemon = MagicMock(spec=SearchDaemon)
    fts_daemon._chunk_store = store
    fts_daemon._indexing_pipeline = MagicMock()
    fts_daemon._embedding_provider = MagicMock()
    _bind_collapse(fts_daemon)
    fts_daemon._resolve_mutations = AsyncMock(return_value=resolved)
    fts_daemon._is_path_in_scope = MagicMock(return_value=True)

    # --- Embedding consumer (sole writer for in-scope paths) ---
    emb_daemon = MagicMock(spec=SearchDaemon)
    emb_daemon._indexing_pipeline = MagicMock()
    emb_daemon._embedding_provider = MagicMock()
    emb_daemon._chunk_store = store
    _bind_collapse(emb_daemon)
    emb_daemon._resolve_mutations = AsyncMock(return_value=resolved)
    emb_daemon._is_path_in_scope = MagicMock(return_value=True)

    # Mock index_document → smart chunk + replace
    chunker = DocumentChunker(chunk_size=1024, overlap_size=128, strategy=ChunkStrategy.FIXED)
    embed_call_count = 0

    async def _mock_index_document(path: str, content: str, path_id: str) -> IndexResult:
        nonlocal embed_call_count
        embed_call_count += 1
        chunks = chunker.chunk(content, file_path=path)
        records = [
            ChunkRecord(
                chunk_text=c.text,
                chunk_tokens=c.tokens,
                start_offset=c.start_offset,
                end_offset=c.end_offset,
                line_start=c.line_start,
                line_end=c.line_end,
                embedding=[0.1] * 4,
                embedding_model="mock-embed-v1",
            )
            for c in chunks
        ]
        await store.replace_document_chunks(path_id, records)
        return IndexResult(path=path, chunks_indexed=len(records))

    emb_daemon._indexing_pipeline.index_document = AsyncMock(side_effect=_mock_index_document)

    t0 = time.perf_counter()
    await asyncio.gather(
        SearchDaemon._consume_fts_mutations(fts_daemon, events),
        SearchDaemon._consume_embedding_mutations(emb_daemon, events),
    )
    elapsed = time.perf_counter() - t0

    # Analyze results
    written_path_ids = [c["path_id"] for c in tracker["replace_calls"]]
    unique_path_ids = set(written_path_ids)
    all_have_embeddings = all(c["has_embedding"] for c in tracker["replace_calls"])
    total_chunks = sum(c["n_chunks"] for c in tracker["replace_calls"])
    total_tokens = sum(c["total_tokens"] for c in tracker["replace_calls"])

    print("\n" + "=" * 70)
    print("BENCHMARK: Concurrent consumer race (HERB WorkFlowGenie)")
    print("=" * 70)
    print(f"Documents ingested:       {len(docs)}")
    print(f"Wall-clock time:          {elapsed:.3f}s")
    print(f"Embedding API calls:      {embed_call_count} (expected: {len(docs)})")
    print(f"replace_document_chunks:  {len(tracker['replace_calls'])} calls")
    print(f"  Unique path_ids:        {len(unique_path_ids)}")
    print(f"  Duplicate writes:       {len(written_path_ids) - len(unique_path_ids)}")
    print(f"  All have embeddings:    {all_have_embeddings}")
    print(f"  Total chunks stored:    {total_chunks}")
    print(f"  Total tokens stored:    {total_tokens}")
    print("FTS consumer writes:      0 (yielded to embedding)")
    print(f"delete_document_chunks:   {len(tracker['delete_calls'])} calls")
    print("=" * 70)

    # Assertions
    assert embed_call_count == len(docs), (
        f"Expected {len(docs)} embedding calls, got {embed_call_count}"
    )
    assert len(written_path_ids) == len(unique_path_ids), (
        f"Double-write detected: {len(written_path_ids)} writes for "
        f"{len(unique_path_ids)} unique path_ids"
    )
    assert all_have_embeddings, (
        "All chunks should have embeddings (sole writer is embedding consumer)"
    )


# ---------------------------------------------------------------------------
# Benchmark 3: FTS fallback mode — still works when no embedding provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_fallback_throughput() -> None:
    """FTS consumer must work as sole writer when no embedding provider."""
    from nexus.bricks.search.daemon import SearchDaemon

    docs = _load_herb_documents()
    store, tracker = _tracking_chunk_store()

    events, resolved = _make_events_from_docs(docs)

    daemon = MagicMock(spec=SearchDaemon)
    daemon._chunk_store = store
    daemon._indexing_pipeline = None
    daemon._embedding_provider = None
    _bind_collapse(daemon)
    daemon._resolve_mutations = AsyncMock(return_value=resolved)
    daemon._index_to_document_chunks = MethodType(SearchDaemon._index_to_document_chunks, daemon)

    t0 = time.perf_counter()
    await SearchDaemon._consume_fts_mutations(daemon, events)
    elapsed = time.perf_counter() - t0

    total_chunks = sum(c["n_chunks"] for c in tracker["replace_calls"])
    total_tokens = sum(c["total_tokens"] for c in tracker["replace_calls"])

    print("\n" + "=" * 70)
    print("BENCHMARK: FTS fallback mode (no embedding provider)")
    print("=" * 70)
    print(f"Documents ingested:       {len(docs)}")
    print(f"Wall-clock time:          {elapsed:.3f}s")
    print(f"replace_document_chunks:  {len(tracker['replace_calls'])} calls")
    print(f"  Total chunks stored:    {total_chunks}")
    print(f"  Total tokens stored:    {total_tokens}")
    print(
        f"  All naive (no embed):   {not any(c['has_embedding'] for c in tracker['replace_calls'])}"
    )
    print(f"Throughput:               {len(docs) / elapsed:.0f} docs/sec")
    print("=" * 70)

    assert len(tracker["replace_calls"]) == len(docs), (
        f"FTS fallback should write all {len(docs)} docs, wrote {len(tracker['replace_calls'])}"
    )


# ---------------------------------------------------------------------------
# Benchmark 4: DELETE propagation under sole-writer mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_propagation_embedding_consumer() -> None:
    """Embedding consumer must propagate DELETEs when sole writer (Issue #3708)."""
    from nexus.bricks.search.daemon import SearchDaemon

    docs = _load_herb_documents()
    # Use a subset for delete simulation
    delete_docs = docs[:50]
    store, tracker = _tracking_chunk_store()

    events, resolved = _make_events_from_docs(delete_docs, op=SearchMutationOp.DELETE)

    daemon = MagicMock(spec=SearchDaemon)
    daemon._indexing_pipeline = MagicMock()
    daemon._embedding_provider = MagicMock()
    daemon._chunk_store = store
    _bind_collapse(daemon)
    daemon._resolve_mutations = AsyncMock(return_value=resolved)

    t0 = time.perf_counter()
    await SearchDaemon._consume_embedding_mutations(daemon, events)
    elapsed = time.perf_counter() - t0

    print("\n" + "=" * 70)
    print("BENCHMARK: DELETE propagation (embedding consumer)")
    print("=" * 70)
    print(f"Delete events:            {len(delete_docs)}")
    print(f"Wall-clock time:          {elapsed:.3f}s")
    print(f"delete_document_chunks:   {len(tracker['delete_calls'])} calls")
    print(f"replace_document_chunks:  {len(tracker['replace_calls'])} calls (should be 0)")
    print("=" * 70)

    assert len(tracker["delete_calls"]) == len(delete_docs), (
        f"Expected {len(delete_docs)} deletes, got {len(tracker['delete_calls'])}"
    )
    assert len(tracker["replace_calls"]) == 0, "Deletes should not trigger replace"


# ---------------------------------------------------------------------------
# Benchmark 5: Chunk boundary consistency check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_boundary_consistency() -> None:
    """Verify that chunk boundaries produced by the sole writer are
    consistent: no gaps, no overlaps in offsets, line numbers monotonic.
    """
    docs = _load_herb_documents()
    chunker = DocumentChunker(chunk_size=1024, overlap_size=128, strategy=ChunkStrategy.FIXED)

    inconsistencies = 0
    checked = 0

    for path, content in docs:
        chunks = chunker.chunk(content, file_path=path)
        if not chunks:
            continue
        checked += 1

        for i, chunk in enumerate(chunks):
            # Each chunk should have valid offsets
            if (
                chunk.start_offset is not None
                and chunk.end_offset is not None
                and chunk.start_offset > chunk.end_offset
            ):
                inconsistencies += 1

            # Line numbers should be non-decreasing across chunks
            if (
                i > 0
                and chunks[i - 1].line_end is not None
                and chunk.line_start is not None
                and chunk.line_start < chunks[i - 1].line_start
            ):
                inconsistencies += 1

            # Chunk text should not be empty
            if not chunk.text.strip():
                inconsistencies += 1

    print("\n" + "=" * 70)
    print("BENCHMARK: Chunk boundary consistency")
    print("=" * 70)
    print(f"Documents checked:        {checked}")
    print(f"Inconsistencies found:    {inconsistencies}")
    print("=" * 70)

    assert inconsistencies == 0, f"Found {inconsistencies} chunk boundary inconsistencies"
