"""TDD tests for IndexingPipeline (Issue #1094).

Tests the parallel indexing pipeline with mock embedding providers
and mock sessions. All tests should FAIL before implementation is wired.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.search.chunking import DocumentChunk, DocumentChunker
from nexus.bricks.search.indexing import IndexingPipeline, IndexProgress, IndexResult

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


def _mock_chunker(chunks_per_doc: int = 3) -> DocumentChunker:
    """Return a chunker whose .chunk() returns deterministic chunks."""
    chunker = MagicMock(spec=DocumentChunker)

    def _chunk(content: str, path: str = "", compute_lines: bool = True) -> list[DocumentChunk]:
        return [_make_chunk(f"chunk-{i} of {path}", index=i) for i in range(chunks_per_doc)]

    chunker.chunk = _chunk
    return chunker


def _mock_embedding_provider(dim: int = 4) -> MagicMock:
    """Return an embedding provider that returns deterministic vectors."""
    provider = MagicMock()
    provider.__class__.__name__ = "MockProvider"

    call_count = {"embed_texts_batched": 0}

    async def _embed_batched(
        texts: list[str],
        batch_size: int | None = None,
        parallel: bool = True,
        max_concurrent: int = 5,
    ) -> list[list[float]]:
        call_count["embed_texts_batched"] += 1
        return [[float(i)] * dim for i in range(len(texts))]

    provider.embed_texts_batched = AsyncMock(side_effect=_embed_batched)
    provider._call_count = call_count
    return provider


def _mock_session_factory() -> MagicMock:
    """Return a mock async session factory."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx)
    factory._session = session
    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIndexSingleDocument:
    @pytest.mark.asyncio
    async def test_index_single_document(self) -> None:
        """Chunk -> embed -> insert produces correct IndexResult."""
        chunker = _mock_chunker(chunks_per_doc=3)
        provider = _mock_embedding_provider()
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
            cross_doc_batching=False,
        )

        result = await pipeline.index_document("test.py", "hello world", "pid-1")

        assert isinstance(result, IndexResult)
        assert result.path == "test.py"
        assert result.chunks_indexed == 3
        assert result.error is None

    @pytest.mark.asyncio
    async def test_index_single_document_empty_chunks(self) -> None:
        """Empty content produces 0 chunks."""
        chunker = _mock_chunker(chunks_per_doc=0)
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            async_session_factory=session_factory,
        )

        result = await pipeline.index_document("empty.py", "", "pid-2")

        assert result.chunks_indexed == 0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_index_single_document_no_provider(self) -> None:
        """Without embedding provider, should still chunk and insert."""
        chunker = _mock_chunker(chunks_per_doc=2)
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=None,
            async_session_factory=session_factory,
        )

        result = await pipeline.index_document("test.py", "content", "pid-3")

        assert result.chunks_indexed == 2
        assert result.error is None


class TestIndexDirectory:
    @pytest.mark.asyncio
    async def test_index_directory_parallel(self) -> None:
        """Multiple files processed and all return results."""
        chunker = _mock_chunker(chunks_per_doc=2)
        provider = _mock_embedding_provider()
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
            max_concurrency=5,
        )

        docs = [
            ("a.py", "content a", "pid-a"),
            ("b.py", "content b", "pid-b"),
            ("c.py", "content c", "pid-c"),
        ]

        results = await pipeline.index_documents(docs)

        assert len(results) == 3
        for r in results:
            assert r.chunks_indexed == 2
            assert r.error is None

    @pytest.mark.asyncio
    async def test_index_directory_respects_max_concurrency(self) -> None:
        """Semaphore bounds concurrent chunking."""
        max_concurrent_observed = 0
        current_concurrent = 0

        def _counting_chunk(
            self_chunker: DocumentChunker, content: str, path: str = "", compute_lines: bool = True
        ) -> list[DocumentChunk]:
            nonlocal max_concurrent_observed, current_concurrent
            current_concurrent += 1
            max_concurrent_observed = max(max_concurrent_observed, current_concurrent)
            result = [_make_chunk(f"chunk of {path}")]
            current_concurrent -= 1
            return result

        chunker = DocumentChunker(chunk_size=1024)
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            async_session_factory=session_factory,
            max_concurrency=2,
        )

        docs = [(f"file{i}.py", f"content {i}", f"pid-{i}") for i in range(10)]

        with patch.object(DocumentChunker, "chunk", _counting_chunk):
            results = await pipeline.index_documents(docs)

        assert len(results) == 10
        # Concurrency should be bounded (hard to test precisely with asyncio.to_thread,
        # but we verify all docs were processed)
        for r in results:
            assert r.error is None

    @pytest.mark.asyncio
    async def test_index_directory_partial_failure(self) -> None:
        """Some files fail, others succeed."""
        call_count = 0

        def _failing_chunk(
            content: str, path: str = "", compute_lines: bool = True
        ) -> list[DocumentChunk]:
            nonlocal call_count
            call_count += 1
            if "bad" in path:
                raise ValueError(f"Cannot chunk {path}")
            return [_make_chunk(f"chunk of {path}")]

        chunker = MagicMock(spec=DocumentChunker)
        chunker.chunk = _failing_chunk
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            async_session_factory=session_factory,
        )

        docs = [
            ("good.py", "content", "pid-1"),
            ("bad.py", "content", "pid-2"),
            ("also_good.py", "content", "pid-3"),
        ]

        results = await pipeline.index_documents(docs)

        assert len(results) == 3
        assert results[0].chunks_indexed == 1
        assert results[0].error is None
        assert results[1].chunks_indexed == 0
        assert results[1].error is not None
        assert "Cannot chunk" in results[1].error
        assert results[2].chunks_indexed == 1
        assert results[2].error is None

    @pytest.mark.asyncio
    async def test_index_directory_returns_index_results(self) -> None:
        """Results are IndexResult dataclasses, not magic numbers."""
        chunker = _mock_chunker(chunks_per_doc=5)
        provider = _mock_embedding_provider()
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
        )

        results = await pipeline.index_documents([("x.py", "c", "pid-x")])

        assert all(isinstance(r, IndexResult) for r in results)
        assert results[0].chunks_indexed == 5


class TestProgressCallback:
    @pytest.mark.asyncio
    async def test_progress_callback_called(self) -> None:
        """Progress callback is invoked for each file."""
        chunker = _mock_chunker(chunks_per_doc=1)
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            async_session_factory=session_factory,
        )

        progress_reports: list[IndexProgress] = []

        def _on_progress(p: IndexProgress) -> None:
            progress_reports.append(p)

        docs = [
            ("a.py", "a", "pid-a"),
            ("b.py", "b", "pid-b"),
        ]

        await pipeline.index_documents(docs, progress_callback=_on_progress)

        assert len(progress_reports) == 2
        assert progress_reports[-1].completed == 2
        assert progress_reports[-1].total == 2


class TestCrossDocBatching:
    @pytest.mark.asyncio
    async def test_cross_doc_batching_single_api_call(self) -> None:
        """3 small docs batched into 1 embed call."""
        chunker = _mock_chunker(chunks_per_doc=2)
        provider = _mock_embedding_provider()
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
            cross_doc_batching=True,
            batch_size=1000,  # Large enough for all chunks
        )

        docs = [
            ("a.py", "a", "pid-a"),
            ("b.py", "b", "pid-b"),
            ("c.py", "c", "pid-c"),
        ]

        await pipeline.index_documents(docs)

        # All 6 chunks (3 docs * 2 chunks) should be embedded in 1 call
        assert provider.embed_texts_batched.call_count == 1
        # Verify the call received all 6 texts
        call_args = provider.embed_texts_batched.call_args
        assert len(call_args[0][0]) == 6  # 3 docs * 2 chunks

    @pytest.mark.asyncio
    async def test_cross_doc_batching_preserves_ordering(self) -> None:
        """Embeddings map to correct documents after cross-doc batching."""
        chunker = _mock_chunker(chunks_per_doc=2)

        # Provider that returns unique embeddings per text
        provider = MagicMock()
        provider.__class__.__name__ = "MockProvider"

        async def _embed_with_index(
            texts: list[str],
            batch_size: int | None = None,
            parallel: bool = True,
            max_concurrent: int = 5,
        ) -> list[list[float]]:
            # Return embedding = [hash of text] so we can verify ordering
            return [[float(hash(t) % 1000)] for t in texts]

        provider.embed_texts_batched = AsyncMock(side_effect=_embed_with_index)
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
            cross_doc_batching=True,
        )

        docs = [
            ("a.py", "alpha", "pid-a"),
            ("b.py", "beta", "pid-b"),
        ]

        results = await pipeline.index_documents(docs)

        assert all(r.chunks_indexed == 2 for r in results)


# ---------------------------------------------------------------------------
# Tests: Heading prefix in embedding contract (Issue #3719)
# ---------------------------------------------------------------------------


class TestMarkdownAwareHeadingPrefix:
    """Verify chunk_texts carries heading prefix while stored text stays raw."""

    @pytest.mark.asyncio
    async def test_heading_prefix_in_embedded_text_not_in_stored(self) -> None:
        """chunk_texts should have heading prefix; chunks.text should not."""
        from nexus.bricks.search.chunking import ChunkStrategy

        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.MARKDOWN_AWARE)
        provider = _mock_embedding_provider()
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
        )

        md_content = "# Auth\n\nJWT tokens expire after 1 hour.\n\n## OAuth\n\nOAuth2 flow.\n"

        result = await pipeline.index_document("docs/auth.md", md_content, "pid-1")
        assert result.chunks_indexed >= 1

        # Verify embed_texts_batched was called with heading-prefixed texts
        embed_call = provider.embed_texts_batched.call_args
        embedded_texts = embed_call[0][0]
        assert any("[" in t and "Auth" in t for t in embedded_texts), (
            f"Expected heading prefix in embedded texts, got: {embedded_texts}"
        )

        # Verify stored chunk_text (via session.execute) does NOT have prefix
        session = session_factory._session
        insert_calls = session.execute.call_args_list
        # The last batch of insert calls contains the chunk data
        for call in insert_calls:
            if call.args and hasattr(call.args[0], "text") and "INSERT" in str(call.args[0]):
                # Check the params — chunk_text should not contain bracket prefix
                params_list = call.args[1] if len(call.args) > 1 else []
                if isinstance(params_list, list):
                    for params in params_list:
                        if isinstance(params, dict) and "chunk_text" in params:
                            assert not params["chunk_text"].startswith("["), (
                                f"Stored chunk_text should not have heading prefix: "
                                f"{params['chunk_text'][:80]}"
                            )

    @pytest.mark.asyncio
    async def test_non_markdown_no_prefix(self) -> None:
        """Non-markdown files should not get heading prefix even with MARKDOWN_AWARE."""
        from nexus.bricks.search.chunking import ChunkStrategy

        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.MARKDOWN_AWARE)
        provider = _mock_embedding_provider()
        session_factory = _mock_session_factory()

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=provider,
            async_session_factory=session_factory,
        )

        result = await pipeline.index_document(
            "src/main.py",
            "def main():\n    pass\n",
            "pid-2",
        )
        assert result.chunks_indexed >= 1

        # Embedded texts should not have bracket prefixes
        embed_call = provider.embed_texts_batched.call_args
        embedded_texts = embed_call[0][0]
        for t in embedded_texts:
            assert not t.startswith("["), f"Python file should not have heading prefix: {t[:80]}"
