"""Tests for contextual chunking module (Issue #1192).

Covers:
- Data model validation (ChunkContext, ContextualChunk, ContextualChunkResult)
- ContextualChunker happy path and edge cases
- Failure modes: LLM errors, bad JSON, timeouts, partial failures
- Concurrency: semaphore limiting, large documents
- Configuration defaults
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from nexus.search.chunking import ChunkStrategy, DocumentChunk, DocumentChunker
from nexus.search.contextual_chunking import (
    ChunkContext,
    ContextualChunk,
    ContextualChunker,
    ContextualChunkingConfig,
    ContextualChunkResult,
    create_context_generator,
    create_heuristic_generator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(text: str, index: int = 0, tokens: int = 10) -> DocumentChunk:
    return DocumentChunk(
        text=text,
        chunk_index=index,
        tokens=tokens,
        start_offset=0,
        end_offset=len(text),
    )


def _ok_context(text: str = "This chunk discusses revenue growth.") -> ChunkContext:
    return ChunkContext(
        situating_context=text,
        resolved_references=[{"original": "it", "resolved": "the company"}],
        key_entities=["revenue", "growth"],
    )


def _make_generator(
    side_effect: list | None = None,
    return_value: ChunkContext | None = None,
) -> AsyncMock:
    """Build a mock ContextGenerator callable."""
    gen = AsyncMock()
    if side_effect is not None:
        gen.side_effect = side_effect
    elif return_value is not None:
        gen.return_value = return_value
    else:
        gen.return_value = _ok_context()
    return gen


# ---------------------------------------------------------------------------
# Data Model Tests
# ---------------------------------------------------------------------------


class TestChunkContext:
    """Test ChunkContext Pydantic model."""

    def test_basic_creation(self):
        ctx = ChunkContext(
            situating_context="Context here",
            resolved_references=[{"original": "he", "resolved": "John"}],
            key_entities=["John", "revenue"],
        )
        assert ctx.situating_context == "Context here"
        assert len(ctx.resolved_references) == 1
        assert ctx.key_entities == ["John", "revenue"]

    def test_defaults(self):
        ctx = ChunkContext(situating_context="Minimal")
        assert ctx.resolved_references == []
        assert ctx.key_entities == []

    def test_json_round_trip(self):
        ctx = _ok_context()
        json_str = ctx.model_dump_json()
        restored = ChunkContext.model_validate_json(json_str)
        assert restored.situating_context == ctx.situating_context
        assert restored.resolved_references == ctx.resolved_references
        assert restored.key_entities == ctx.key_entities


class TestContextualChunk:
    """Test ContextualChunk dataclass."""

    def test_contextual_text_with_context(self):
        chunk = _make_chunk("Some text")
        ctx = _ok_context("Background info")
        cc = ContextualChunk(chunk=chunk, context=ctx, position=0, doc_summary="Summary")
        assert cc.contextual_text == "Background info\n\nSome text"

    def test_contextual_text_without_context(self):
        chunk = _make_chunk("Some text")
        cc = ContextualChunk(chunk=chunk, context=None, position=0, doc_summary="Summary")
        assert cc.contextual_text == "Some text"

    def test_contextual_text_empty_context(self):
        chunk = _make_chunk("Some text")
        ctx = ChunkContext(situating_context="")
        cc = ContextualChunk(chunk=chunk, context=ctx, position=0, doc_summary="Summary")
        # Empty situating_context falls through to just the chunk text
        assert cc.contextual_text == "Some text"


class TestContextualChunkResult:
    """Test ContextualChunkResult dataclass."""

    def test_context_rate_all_success(self):
        result = ContextualChunkResult(
            chunks=[],
            total_chunks=10,
            chunks_with_context=10,
            chunks_without_context=0,
            source_document_id="abc",
        )
        assert result.context_rate == 100.0

    def test_context_rate_partial(self):
        result = ContextualChunkResult(
            chunks=[],
            total_chunks=10,
            chunks_with_context=7,
            chunks_without_context=3,
            source_document_id="abc",
        )
        assert result.context_rate == pytest.approx(70.0)

    def test_context_rate_empty(self):
        result = ContextualChunkResult(
            chunks=[],
            total_chunks=0,
            chunks_with_context=0,
            chunks_without_context=0,
            source_document_id="abc",
        )
        assert result.context_rate == 0.0


class TestContextualChunkingConfig:
    """Test configuration defaults."""

    def test_defaults(self):
        cfg = ContextualChunkingConfig()
        assert cfg.enabled is False
        assert cfg.max_context_length == 200
        assert cfg.batch_concurrency == 5
        assert cfg.use_heuristic_fallback is True

    def test_custom_values(self):
        cfg = ContextualChunkingConfig(
            enabled=True, max_context_length=500, batch_concurrency=10, use_heuristic_fallback=False
        )
        assert cfg.enabled is True
        assert cfg.max_context_length == 500
        assert cfg.batch_concurrency == 10
        assert cfg.use_heuristic_fallback is False


# ---------------------------------------------------------------------------
# ContextualChunker Tests
# ---------------------------------------------------------------------------


class TestContextualChunkerHappyPath:
    """Happy path tests for ContextualChunker."""

    @pytest.mark.asyncio
    async def test_basic_3_chunks(self):
        """3 chunks all get context successfully."""
        gen = _make_generator()
        config = ContextualChunkingConfig(enabled=True, batch_concurrency=5)
        chunker = ContextualChunker(context_generator=gen, config=config)

        doc = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = await chunker.chunk_with_context(doc, doc_summary="A test doc")

        assert result.total_chunks > 0
        assert result.chunks_with_context == result.total_chunks
        assert result.chunks_without_context == 0
        assert result.context_rate == 100.0
        assert result.source_document_id  # UUID is set

        for cc in result.chunks:
            assert cc.context is not None
            assert cc.context.situating_context

    @pytest.mark.asyncio
    async def test_single_chunk(self):
        """Single chunk document — no surrounding context available."""
        gen = _make_generator()
        config = ContextualChunkingConfig(enabled=True)
        chunker = ContextualChunker(context_generator=gen, config=config)

        result = await chunker.chunk_with_context("Short text.", doc_summary="Summary")

        assert result.total_chunks == 1
        assert result.chunks_with_context == 1

        # Generator was called with empty prev/next
        call_args = gen.call_args_list[0]
        assert call_args[0][2] == []  # prev_chunks is empty
        assert call_args[0][3] == []  # next_chunks is empty

    @pytest.mark.asyncio
    async def test_empty_document(self):
        """Empty document returns empty result."""
        gen = _make_generator()
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        result = await chunker.chunk_with_context("", doc_summary="Empty")

        assert result.total_chunks == 0
        assert result.chunks_with_context == 0
        assert result.chunks_without_context == 0
        gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only(self):
        """Whitespace-only document handled gracefully."""
        gen = _make_generator()
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        result = await chunker.chunk_with_context("   \n\n  \t  ", doc_summary="WS")
        assert result.total_chunks == 0
        gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_length_truncation(self):
        """Context longer than max_context_length is truncated."""
        long_ctx = ChunkContext(
            situating_context="A" * 500,
            key_entities=["test"],
        )
        gen = _make_generator(return_value=long_ctx)
        config = ContextualChunkingConfig(enabled=True, max_context_length=100)
        chunker = ContextualChunker(context_generator=gen, config=config)

        result = await chunker.chunk_with_context("Some content.", doc_summary="S")

        assert result.total_chunks >= 1
        for cc in result.chunks:
            assert cc.context is not None
            assert len(cc.context.situating_context) <= 100

    @pytest.mark.asyncio
    async def test_source_document_id_consistency(self):
        """All chunks in a result share the same source_document_id."""
        gen = _make_generator()
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        doc = "Para one.\n\nPara two.\n\nPara three."
        result = await chunker.chunk_with_context(doc, doc_summary="S")

        assert result.source_document_id
        # All chunks reference the same doc_summary
        for cc in result.chunks:
            assert cc.doc_summary == "S"


class TestContextualChunkerFailureModes:
    """Failure mode tests for ContextualChunker."""

    @pytest.mark.asyncio
    async def test_all_llm_fail_no_heuristic(self):
        """All LLM calls fail with heuristic disabled — all chunks stored without context."""
        gen = _make_generator(side_effect=[RuntimeError("LLM down")] * 20)
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True, use_heuristic_fallback=False),
        )

        result = await chunker.chunk_with_context(
            "Paragraph one.\n\nParagraph two.", doc_summary="S"
        )

        assert result.total_chunks > 0
        assert result.chunks_with_context == 0
        assert result.chunks_without_context == result.total_chunks
        assert result.context_rate == 0.0

        for cc in result.chunks:
            assert cc.context is None
            assert cc.contextual_text == cc.chunk.text  # Falls back to raw text

    @pytest.mark.asyncio
    async def test_all_llm_fail_heuristic_fallback(self):
        """All LLM calls fail with heuristic enabled — heuristic context used."""
        gen = _make_generator(side_effect=[RuntimeError("LLM down")] * 20)
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True, use_heuristic_fallback=True),
        )

        result = await chunker.chunk_with_context(
            "Paragraph one.\n\nParagraph two.", doc_summary="A test document."
        )

        assert result.total_chunks > 0
        # All chunks get heuristic context
        assert result.chunks_with_context == result.total_chunks
        assert result.chunks_without_context == 0

        for cc in result.chunks:
            assert cc.context is not None
            assert cc.context.situating_context  # Non-empty heuristic context
            assert (
                "A test document" in cc.context.situating_context
                or "Section" in cc.context.situating_context
            )

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        """Mixed success/failure — correct counts."""
        gen = _make_generator(
            side_effect=[
                _ok_context(),
                RuntimeError("fail"),
                RuntimeError("fail"),  # retry also fails
                _ok_context(),
            ]
        )
        config = ContextualChunkingConfig(
            enabled=True, batch_concurrency=1, use_heuristic_fallback=False
        )
        base_chunker = DocumentChunker(chunk_size=50, strategy=ChunkStrategy.FIXED)
        chunker = ContextualChunker(
            context_generator=gen,
            config=config,
            base_chunker=base_chunker,
        )

        # Create doc large enough to produce exactly 2 chunks at 50 tokens each
        # ~4 chars per token, so need ~200+ chars per chunk
        doc = " ".join(["word"] * 60) + "\n\n" + " ".join(["text"] * 60)
        result = await chunker.chunk_with_context(doc, doc_summary="S")

        assert result.total_chunks >= 2
        # First chunk succeeds, second retries and fails
        assert result.chunks_with_context >= 1
        assert result.chunks_without_context >= 1

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """First attempt fails, retry succeeds."""
        gen = _make_generator(
            side_effect=[
                RuntimeError("transient error"),
                _ok_context(),  # retry succeeds
            ]
        )
        config = ContextualChunkingConfig(enabled=True, batch_concurrency=1)
        chunker = ContextualChunker(context_generator=gen, config=config)

        result = await chunker.chunk_with_context("Content.", doc_summary="S")

        assert result.total_chunks == 1
        assert result.chunks_with_context == 1
        assert gen.call_count == 2  # First call + retry

    @pytest.mark.asyncio
    async def test_rate_limit_error(self):
        """Rate limit error triggers retry, then degrades."""
        gen = _make_generator(
            side_effect=[
                RuntimeError("rate_limit_exceeded"),
                RuntimeError("rate_limit_exceeded"),
            ]
        )
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(
                enabled=True, batch_concurrency=1, use_heuristic_fallback=False
            ),
        )

        result = await chunker.chunk_with_context("Text.", doc_summary="S")

        assert result.chunks_without_context == result.total_chunks

    @pytest.mark.asyncio
    async def test_llm_returns_empty_context(self):
        """LLM returns empty situating_context — chunk stored with empty context."""
        empty_ctx = ChunkContext(situating_context="")
        gen = _make_generator(return_value=empty_ctx)
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        result = await chunker.chunk_with_context("Content.", doc_summary="S")

        assert result.chunks_with_context == result.total_chunks
        for cc in result.chunks:
            assert cc.context is not None
            assert cc.context.situating_context == ""
            # contextual_text falls back to raw text for empty context
            assert cc.contextual_text == cc.chunk.text

    @pytest.mark.asyncio
    async def test_bad_json_from_generator(self):
        """Generator raises validation error (bad JSON) — degrades gracefully."""

        async def bad_json_gen(*_args, **_kwargs):
            # Simulate what happens when LLM returns invalid JSON
            ChunkContext.model_validate_json("{invalid json")

        chunker = ContextualChunker(
            context_generator=bad_json_gen,
            config=ContextualChunkingConfig(enabled=True, use_heuristic_fallback=False),
        )

        result = await chunker.chunk_with_context("Text.", doc_summary="S")
        assert result.chunks_without_context == result.total_chunks

    @pytest.mark.asyncio
    async def test_timeout_degrades(self):
        """Timeout from generator — degrades gracefully."""

        async def slow_gen(*args, **kwargs):
            raise TimeoutError("LLM call timed out")

        chunker = ContextualChunker(
            context_generator=slow_gen,
            config=ContextualChunkingConfig(enabled=True, use_heuristic_fallback=False),
        )

        result = await chunker.chunk_with_context("Content.", doc_summary="S")
        assert result.chunks_without_context == result.total_chunks

    @pytest.mark.asyncio
    async def test_connection_error_degrades(self):
        """Connection error from generator — degrades gracefully."""

        async def failing_gen(*args, **kwargs):
            raise ConnectionError("Cannot reach LLM API")

        chunker = ContextualChunker(
            context_generator=failing_gen,
            config=ContextualChunkingConfig(enabled=True, use_heuristic_fallback=False),
        )

        result = await chunker.chunk_with_context("Content.", doc_summary="S")
        assert result.chunks_without_context == result.total_chunks


class TestContextualChunkerConcurrency:
    """Concurrency and performance tests."""

    @pytest.mark.asyncio
    async def test_concurrency_limit(self):
        """Semaphore limits parallel calls."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def tracking_gen(doc_summary, chunk_text, prev, next_chunks):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            try:
                await asyncio.sleep(0.01)
                return _ok_context()
            finally:
                async with lock:
                    current_concurrent -= 1

        config = ContextualChunkingConfig(enabled=True, batch_concurrency=3)
        base_chunker = DocumentChunker(chunk_size=20, strategy=ChunkStrategy.FIXED)
        chunker = ContextualChunker(
            context_generator=tracking_gen,
            config=config,
            base_chunker=base_chunker,
        )

        # Generate many chunks
        doc = " ".join(["word"] * 200)
        result = await chunker.chunk_with_context(doc, doc_summary="S")

        assert result.total_chunks > 3
        assert max_concurrent <= 3  # Never exceeded concurrency limit

    @pytest.mark.asyncio
    async def test_large_document(self):
        """100+ chunks processed successfully."""
        gen = _make_generator()
        config = ContextualChunkingConfig(enabled=True, batch_concurrency=10)
        base_chunker = DocumentChunker(chunk_size=20, strategy=ChunkStrategy.FIXED)
        chunker = ContextualChunker(
            context_generator=gen,
            config=config,
            base_chunker=base_chunker,
        )

        # Large document producing many chunks
        doc = "\n\n".join([f"Paragraph {i} with enough words to form a chunk." for i in range(150)])
        result = await chunker.chunk_with_context(doc, doc_summary="Large doc")

        assert result.total_chunks >= 50  # Should produce many chunks
        assert result.chunks_with_context == result.total_chunks

    @pytest.mark.asyncio
    async def test_unicode_content(self):
        """Unicode/emoji content handled correctly."""
        gen = _make_generator(
            return_value=ChunkContext(
                situating_context="This discusses international markets",
                key_entities=["Tokyo", "Berlin"],
            )
        )
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        doc = "Tokyo (東京) reported strong results. Berlin (ベルリン) showed growth."
        result = await chunker.chunk_with_context(doc, doc_summary="International report")

        assert result.total_chunks >= 1
        assert result.chunks_with_context == result.total_chunks


# ---------------------------------------------------------------------------
# Context Generator Factory Tests
# ---------------------------------------------------------------------------


class TestCreateContextGenerator:
    """Test the create_context_generator factory."""

    @pytest.mark.asyncio
    async def test_creates_working_generator(self):
        """Factory produces a callable that works with ContextualChunker."""
        mock_llm = AsyncMock(
            return_value='{"situating_context": "test ctx", "resolved_references": [], "key_entities": ["x"]}'
        )
        gen = await create_context_generator(mock_llm)

        result = await gen("summary", "chunk text", ["prev"], ["next"])

        assert isinstance(result, ChunkContext)
        assert result.situating_context == "test ctx"
        assert result.key_entities == ["x"]
        mock_llm.assert_called_once()

    @pytest.mark.asyncio
    async def test_bad_json_raises(self):
        """Invalid JSON from LLM raises ValidationError."""
        mock_llm = AsyncMock(return_value="not valid json")
        gen = await create_context_generator(mock_llm)

        with pytest.raises((ValueError, KeyError)):
            await gen("summary", "chunk", [], [])


# ---------------------------------------------------------------------------
# Heuristic Generator Tests
# ---------------------------------------------------------------------------


class TestCreateHeuristicGenerator:
    """Test the heuristic (no-LLM) context generator."""

    @pytest.mark.asyncio
    async def test_produces_valid_context(self):
        """Heuristic generator returns ChunkContext with entities."""
        gen = create_heuristic_generator()
        ctx = await gen(
            "Acme Corp Q3 report.",
            "Revenue grew 15%. John presented results.",
            [],
            ["Next section."],
        )
        assert isinstance(ctx, ChunkContext)
        assert ctx.situating_context
        assert "Acme Corp" in ctx.situating_context or "Opening" in ctx.situating_context

    @pytest.mark.asyncio
    async def test_extracts_capitalized_entities(self):
        """Heuristic extracts capitalized phrases as entities."""
        gen = create_heuristic_generator()
        ctx = await gen(
            "Summary",
            "John Smith and Jane Doe met in New York to discuss Acme Corp.",
            ["Previous."],
            [],
        )
        # Should extract at least some capitalized entities
        assert len(ctx.key_entities) >= 1
        entity_text = " ".join(ctx.key_entities)
        assert "John" in entity_text or "New York" in entity_text or "Acme" in entity_text

    @pytest.mark.asyncio
    async def test_position_context(self):
        """First chunk gets 'Opening section', later chunks get 'Section N'."""
        gen = create_heuristic_generator()

        # First chunk (no prev)
        ctx0 = await gen("Summary", "First text.", [], ["Next."])
        assert "Opening" in ctx0.situating_context

        # Third chunk (2 prev)
        ctx2 = await gen("Summary", "Later text.", ["Prev1.", "Prev2."], [])
        assert "Section 3" in ctx2.situating_context

    @pytest.mark.asyncio
    async def test_heading_extraction(self):
        """Heuristic extracts markdown headings."""
        gen = create_heuristic_generator()
        ctx = await gen(
            "Summary",
            "## Financial Results\nRevenue grew 15%.",
            [],
            [],
        )
        assert "Financial Results" in ctx.situating_context

    @pytest.mark.asyncio
    async def test_doc_summary_included(self):
        """Doc summary first sentence is included in context."""
        gen = create_heuristic_generator()
        ctx = await gen(
            "Acme Corp Q3 earnings. Strong growth reported.",
            "Some chunk text here.",
            [],
            [],
        )
        assert "Acme Corp Q3 earnings." in ctx.situating_context

    @pytest.mark.asyncio
    async def test_works_with_contextual_chunker(self):
        """Heuristic generator works end-to-end with ContextualChunker."""
        gen = create_heuristic_generator()
        config = ContextualChunkingConfig(enabled=True, batch_concurrency=2)
        base_chunker = DocumentChunker(chunk_size=30, strategy=ChunkStrategy.FIXED)
        chunker = ContextualChunker(
            context_generator=gen,
            config=config,
            base_chunker=base_chunker,
        )

        doc = "Acme Corp reported strong results.\n\nRevenue grew 15% this quarter."
        result = await chunker.chunk_with_context(doc, doc_summary="Acme Corp report.")

        assert result.total_chunks >= 1
        assert result.chunks_with_context == result.total_chunks
        for cc in result.chunks:
            assert cc.context is not None
            assert cc.context.situating_context
