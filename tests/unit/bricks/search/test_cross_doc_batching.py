"""Parametrized tests for cross-document embedding batching (Issue #1094).

Verifies that the IndexingPipeline correctly batches embeddings across
multiple documents and assigns them back to the correct documents.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.chunking import DocumentChunk, DocumentChunker
from nexus.bricks.search.indexing import IndexingPipeline

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


def _mock_chunker_varied(chunk_counts: dict[str, int]) -> DocumentChunker:
    """Chunker that returns N chunks per path (keyed by path)."""
    chunker = MagicMock(spec=DocumentChunker)

    def _chunk(content: str, path: str = "", compute_lines: bool = True) -> list[DocumentChunk]:
        n = chunk_counts.get(path, 1)
        return [_make_chunk(f"{path}-chunk-{i}", index=i) for i in range(n)]

    chunker.chunk = _chunk
    return chunker


def _mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.__class__.__name__ = "MockProvider"
    call_log: list[int] = []

    async def _embed(
        texts: list[str],
        batch_size: int | None = None,
        parallel: bool = True,
        max_concurrent: int = 5,
    ) -> list[list[float]]:
        call_log.append(len(texts))
        return [[float(i + 1)] for i in range(len(texts))]

    provider.embed_texts_batched = AsyncMock(side_effect=_embed)
    provider._call_log = call_log
    return provider


def _mock_session_factory() -> MagicMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_single_doc() -> None:
    """1 doc with N chunks -> 1 embed call."""
    chunker = _mock_chunker_varied({"only.py": 5})
    provider = _mock_provider()

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=_mock_session_factory(),
        cross_doc_batching=True,
        batch_size=1000,
    )

    results = await pipeline.index_documents([("only.py", "content", "pid-1")])

    assert results[0].chunks_indexed == 5
    assert provider.embed_texts_batched.call_count == 1
    assert provider._call_log == [5]


@pytest.mark.asyncio
async def test_batch_two_docs() -> None:
    """2 docs -> embeddings assigned to correct doc."""
    chunker = _mock_chunker_varied({"a.py": 3, "b.py": 2})
    provider = _mock_provider()

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=_mock_session_factory(),
        cross_doc_batching=True,
        batch_size=1000,
    )

    results = await pipeline.index_documents(
        [
            ("a.py", "alpha", "pid-a"),
            ("b.py", "beta", "pid-b"),
        ]
    )

    assert results[0].chunks_indexed == 3
    assert results[1].chunks_indexed == 2
    # Single batch call with 5 texts
    assert provider._call_log == [5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunk_counts,expected_total",
    [
        ({"a.py": 0, "b.py": 1, "c.py": 5, "d.py": 50}, 56),
        ({"x.py": 1}, 1),
        ({"a.py": 10, "b.py": 10}, 20),
    ],
    ids=["varied-sizes", "single-chunk", "equal-sizes"],
)
async def test_batch_many_docs_varied_sizes(
    chunk_counts: dict[str, int],
    expected_total: int,
) -> None:
    """Docs with 0, 1, 5, 50 chunks all handled correctly."""
    chunker = _mock_chunker_varied(chunk_counts)
    provider = _mock_provider()

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=_mock_session_factory(),
        cross_doc_batching=True,
        batch_size=10000,
    )

    docs = [(path, "content", f"pid-{path}") for path in chunk_counts]
    results = await pipeline.index_documents(docs)

    total_indexed = sum(r.chunks_indexed for r in results)
    assert total_indexed == expected_total


@pytest.mark.asyncio
async def test_batch_empty_doc_skipped() -> None:
    """0-chunk doc doesn't generate embed call; other docs still processed."""
    chunker = _mock_chunker_varied({"empty.py": 0, "real.py": 3})
    provider = _mock_provider()

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=_mock_session_factory(),
        cross_doc_batching=True,
        batch_size=1000,
    )

    results = await pipeline.index_documents(
        [
            ("empty.py", "", "pid-e"),
            ("real.py", "data", "pid-r"),
        ]
    )

    assert results[0].chunks_indexed == 0
    assert results[1].chunks_indexed == 3
    # Only the 3 texts from real.py should be embedded
    assert provider._call_log == [3]


@pytest.mark.asyncio
async def test_batch_exceeds_batch_size() -> None:
    """When total texts exceed batch_size, provider handles multiple batches."""
    # 3 docs * 5 chunks = 15 texts, batch_size=10 -> provider handles splitting
    chunker = _mock_chunker_varied({"a.py": 5, "b.py": 5, "c.py": 5})
    provider = _mock_provider()

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=provider,
        async_session_factory=_mock_session_factory(),
        cross_doc_batching=True,
        batch_size=10,  # Provider will split internally
    )

    results = await pipeline.index_documents(
        [
            ("a.py", "a", "pid-a"),
            ("b.py", "b", "pid-b"),
            ("c.py", "c", "pid-c"),
        ]
    )

    assert all(r.chunks_indexed == 5 for r in results)
    # embed_texts_batched called once with all 15 texts (provider splits internally)
    assert provider.embed_texts_batched.call_count == 1
    assert provider._call_log == [15]
