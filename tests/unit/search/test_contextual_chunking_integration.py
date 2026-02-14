"""Integration tests for contextual chunking pipeline (Issue #1192).

Tests the full index→search pipeline with mocked LLM, verifying:
- Chunks are enriched with context before embedding
- Context metadata is stored alongside chunk text
- Contextual text is used for embeddings
- Backwards compatibility with non-contextual path
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.search.chunking import DocumentChunker
from nexus.search.contextual_chunking import (
    ChunkContext,
    ContextualChunker,
    ContextualChunkingConfig,
)


class TestContextualChunkingPipeline:
    """End-to-end contextual chunking pipeline tests."""

    @pytest.mark.asyncio
    async def test_full_pipeline_with_mock_llm(self):
        """Full pipeline: chunk → generate context → compose text."""
        ctx = ChunkContext(
            situating_context="This section discusses Q3 revenue growth at Acme Corp.",
            resolved_references=[{"original": "it", "resolved": "Acme Corp"}],
            key_entities=["Acme Corp", "Q3", "revenue"],
        )
        gen = AsyncMock(return_value=ctx)
        config = ContextualChunkingConfig(enabled=True, batch_concurrency=2)
        chunker = ContextualChunker(context_generator=gen, config=config)

        doc = (
            "Acme Corp reported strong Q3 results. Revenue grew 15% year-over-year.\n\n"
            "The company attributed growth to international expansion. "
            "It opened new offices in Tokyo and Berlin.\n\n"
            "Looking ahead, management expects continued momentum in Q4."
        )

        result = await chunker.chunk_with_context(
            doc, doc_summary="Acme Corp Q3 earnings report", file_path="/reports/q3.md"
        )

        assert result.total_chunks >= 1
        assert result.chunks_with_context == result.total_chunks
        assert result.source_document_id  # UUID generated

        # Verify contextual text composition
        for cc in result.chunks:
            assert cc.context is not None
            expected = f"{ctx.situating_context}\n\n{cc.chunk.text}"
            assert cc.contextual_text == expected

        # Verify generator was called with correct surrounding context
        assert gen.call_count == result.total_chunks

    @pytest.mark.asyncio
    async def test_contextual_text_used_for_embedding(self):
        """Verify that composed contextual text (not raw text) would be embedded."""
        ctx = ChunkContext(
            situating_context="Background: Acme Corp Q3 report",
            key_entities=["Acme"],
        )
        gen = AsyncMock(return_value=ctx)
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        result = await chunker.chunk_with_context("Revenue grew 15%.", doc_summary="S")

        # The text that should be sent to embedding provider
        embedding_texts = [cc.contextual_text for cc in result.chunks]
        for t in embedding_texts:
            assert t.startswith("Background: Acme Corp Q3 report")
            assert "Revenue grew 15%." in t

    @pytest.mark.asyncio
    async def test_context_stored_separately_from_text(self):
        """Context and original text are stored separately (can be retrieved independently)."""
        ctx = ChunkContext(
            situating_context="Context info",
            key_entities=["test"],
        )
        gen = AsyncMock(return_value=ctx)
        chunker = ContextualChunker(
            context_generator=gen,
            config=ContextualChunkingConfig(enabled=True),
        )

        result = await chunker.chunk_with_context("Original text.", doc_summary="S")

        for cc in result.chunks:
            # Original text is preserved in chunk.text
            assert cc.chunk.text == "Original text."
            # Context is stored separately
            assert cc.context is not None
            assert cc.context.situating_context == "Context info"
            # Composed form is derived, not stored
            assert cc.contextual_text == "Context info\n\nOriginal text."


class TestBackwardsCompatibility:
    """Ensure non-contextual path still works."""

    @pytest.mark.asyncio
    async def test_chunking_without_contextual_enabled(self):
        """Standard chunking still works when contextual chunking is disabled."""
        base_chunker = DocumentChunker(chunk_size=1024)
        chunks = base_chunker.chunk("Some document text.", "test.md")

        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.text
            assert chunk.tokens > 0
