"""Performance benchmarks for IndexingPipeline (Issue #1094).

Measures:
- Parallel vs sequential throughput (docs/sec)
- Cross-doc batching API call reduction
- Bulk insert throughput
"""


import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.search.chunking import DocumentChunk, DocumentChunker
from nexus.search.indexing import IndexingPipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(text: str, index: int = 0) -> DocumentChunk:
    return DocumentChunk(
        text=text,
        chunk_index=index,
        tokens=len(text.split()),
        start_offset=0,
        end_offset=len(text),
        line_start=1,
        line_end=1,
    )


def _fast_chunker(chunks_per_doc: int = 5) -> DocumentChunker:
    chunker = MagicMock(spec=DocumentChunker)

    def _chunk(content: str, path: str = "", compute_lines: bool = True) -> list[DocumentChunk]:
        return [_make_chunk(f"chunk-{i}", index=i) for i in range(chunks_per_doc)]

    chunker.chunk = _chunk
    return chunker


def _fast_provider() -> MagicMock:
    provider = MagicMock()
    provider.__class__.__name__ = "BenchProvider"
    call_count = {"n": 0}

    async def _embed(
        texts: list[str],
        batch_size: int | None = None,
        parallel: bool = True,
        max_concurrent: int = 5,
    ) -> list[list[float]]:
        call_count["n"] += 1
        return [[0.1] * 4 for _ in texts]

    provider.embed_texts_batched = AsyncMock(side_effect=_embed)
    provider._call_count = call_count
    return provider


def _fast_session_factory() -> MagicMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_vs_sequential_throughput() -> None:
    """Parallel pipeline should process more docs/sec than sequential."""
    n_docs = 50
    docs = [(f"file{i}.py", f"content {i}", f"pid-{i}") for i in range(n_docs)]

    chunker = _fast_chunker(chunks_per_doc=3)
    provider = _fast_provider()

    # Parallel (cross-doc batching)
    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=_fast_session_factory(),
        max_concurrency=10,
        cross_doc_batching=True,
    )

    start = time.perf_counter()
    results = await pipeline.index_documents(docs)
    parallel_time = time.perf_counter() - start

    assert all(r.chunks_indexed == 3 for r in results)
    parallel_throughput = n_docs / parallel_time

    # Sequential (1 concurrency, no cross-doc batching)
    provider2 = _fast_provider()
    pipeline_seq = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider2,
        async_session_factory=_fast_session_factory(),
        max_concurrency=1,
        cross_doc_batching=False,
    )

    start = time.perf_counter()
    results_seq = await pipeline_seq.index_documents(docs)
    seq_time = time.perf_counter() - start

    assert all(r.chunks_indexed == 3 for r in results_seq)
    seq_throughput = n_docs / seq_time

    # Log for visibility
    print(f"\nParallel: {parallel_throughput:.0f} docs/sec ({parallel_time:.3f}s)")
    print(f"Sequential: {seq_throughput:.0f} docs/sec ({seq_time:.3f}s)")
    print(f"Speedup: {parallel_throughput / max(seq_throughput, 0.1):.1f}x")


@pytest.mark.asyncio
async def test_cross_doc_batching_fewer_api_calls() -> None:
    """Cross-doc batching should use fewer embed_texts_batched calls."""
    n_docs = 20
    docs = [(f"file{i}.py", f"content {i}", f"pid-{i}") for i in range(n_docs)]

    chunker = _fast_chunker(chunks_per_doc=3)

    # With cross-doc batching: 1 call
    provider_cross = _fast_provider()
    pipeline_cross = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider_cross,
        async_session_factory=_fast_session_factory(),
        cross_doc_batching=True,
        batch_size=10000,
    )
    await pipeline_cross.index_documents(docs)
    cross_calls = provider_cross.embed_texts_batched.call_count

    # Without cross-doc batching: N calls (1 per doc)
    provider_per = _fast_provider()
    pipeline_per = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider_per,
        async_session_factory=_fast_session_factory(),
        cross_doc_batching=False,
    )
    await pipeline_per.index_documents(docs)
    per_calls = provider_per.embed_texts_batched.call_count

    print(f"\nCross-doc calls: {cross_calls}")
    print(f"Per-doc calls: {per_calls}")
    print(f"Reduction: {per_calls / max(cross_calls, 1):.0f}x fewer API calls")

    assert cross_calls < per_calls
    assert cross_calls == 1  # All texts in one batch


@pytest.mark.asyncio
async def test_bulk_insert_throughput() -> None:
    """Measure chunks/sec for bulk insert (mock session)."""
    n_docs = 100
    chunks_per_doc = 10

    docs = [(f"file{i}.py", f"content {i}", f"pid-{i}") for i in range(n_docs)]

    chunker = _fast_chunker(chunks_per_doc=chunks_per_doc)
    provider = _fast_provider()
    session_factory = _fast_session_factory()

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=session_factory,
        cross_doc_batching=True,
    )

    start = time.perf_counter()
    results = await pipeline.index_documents(docs)
    elapsed = time.perf_counter() - start

    total_chunks = sum(r.chunks_indexed for r in results)
    throughput = total_chunks / elapsed

    print(f"\nBulk insert: {total_chunks} chunks in {elapsed:.3f}s")
    print(f"Throughput: {throughput:.0f} chunks/sec")

    assert total_chunks == n_docs * chunks_per_doc
