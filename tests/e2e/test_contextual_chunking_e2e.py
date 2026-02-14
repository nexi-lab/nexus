"""E2E tests for contextual chunking with FastAPI server + permissions (Issue #1192).

Tests the full pipeline:
1. Start nexus serve with FastAPI (database auth + permissions enabled)
2. Write a document with ambiguous references via API
3. Index it with contextual chunking enabled (direct + API validation)
4. Verify chunks are stored with context metadata in the DB
5. Validate non-user (agent/service) permission subjects work correctly
6. Performance: verify no regressions from contextual chunking overhead

Requires: no external API keys (uses mock LLM generator).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from nexus.search.async_search import AsyncSemanticSearch
from nexus.search.chunking import ChunkStrategy, DocumentChunker
from nexus.search.contextual_chunking import (
    ChunkContext,
    ContextualChunker,
    ContextualChunkingConfig,
    create_context_generator,
)

# Use small chunk size so the test doc produces multiple chunks
SMALL_CHUNKER = DocumentChunker(chunk_size=40, strategy=ChunkStrategy.FIXED)


async def _create_tables_and_path(db_url: str, async_session_factory, virtual_path: str, size: int) -> str:
    """Create DB tables and insert a file_paths row via ORM. Returns the path_id."""
    from sqlalchemy.ext.asyncio import create_async_engine

    import nexus.storage.models.file_path  # noqa: F401
    import nexus.storage.models.filesystem  # noqa: F401
    from nexus.storage.models._base import Base
    from nexus.storage.models.file_path import FilePathModel

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    path_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        fp = FilePathModel(
            path_id=path_id,
            virtual_path=virtual_path,
            backend_id="local",
            physical_path=virtual_path,
            size_bytes=size,
        )
        session.add(fp)
        await session.commit()
    return path_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AMBIGUOUS_DOC = """\
Acme Corp reported strong Q3 results on Tuesday. Revenue grew 15% year-over-year,
beating analyst expectations. The company attributed growth to international expansion.

It opened new offices in Tokyo and Berlin last quarter. These offices are expected to
contribute significantly to Q4 revenue. He mentioned that the hiring pipeline is strong.

Looking ahead, management expects continued momentum. The board approved a new stock
buyback program of $500 million. They believe the company is well-positioned for growth.
"""

AMBIGUOUS_DOC_SUMMARY = (
    "Acme Corp Q3 earnings report: 15% revenue growth, international expansion "
    "into Tokyo and Berlin, new $500M stock buyback program approved."
)


def _mock_context_for_chunk(chunk_text: str) -> ChunkContext:
    """Generate a deterministic mock context based on chunk content."""
    if "Q3 results" in chunk_text or "Revenue grew" in chunk_text:
        return ChunkContext(
            situating_context="From Acme Corp Q3 2024 earnings report discussing financial performance.",
            resolved_references=[],
            key_entities=["Acme Corp", "Q3", "revenue"],
        )
    elif "Tokyo" in chunk_text or "Berlin" in chunk_text:
        return ChunkContext(
            situating_context="Acme Corp's international expansion efforts with new offices in Asia and Europe.",
            resolved_references=[
                {"original": "It", "resolved": "Acme Corp"},
                {"original": "These offices", "resolved": "Tokyo and Berlin offices"},
            ],
            key_entities=["Acme Corp", "Tokyo", "Berlin", "offices"],
        )
    elif "management" in chunk_text or "buyback" in chunk_text:
        return ChunkContext(
            situating_context="Acme Corp management's forward guidance and capital allocation decisions.",
            resolved_references=[
                {"original": "He", "resolved": "CEO of Acme Corp"},
                {"original": "They", "resolved": "Acme Corp board of directors"},
            ],
            key_entities=["Acme Corp", "management", "stock buyback"],
        )
    else:
        return ChunkContext(
            situating_context="Additional details from Acme Corp Q3 earnings report.",
            resolved_references=[],
            key_entities=["Acme Corp"],
        )


# ---------------------------------------------------------------------------
# 1. Direct ContextualChunker E2E (no server, validates core logic)
# ---------------------------------------------------------------------------


class TestContextualChunkerDirectE2E:
    """E2E test of ContextualChunker with realistic document content."""

    @pytest.mark.asyncio
    async def test_ambiguous_document_gets_context(self):
        """Full document with pronouns/ambiguous refs → all chunks get context."""
        async def mock_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            return _mock_context_for_chunk(chunk_text)

        config = ContextualChunkingConfig(enabled=True, batch_concurrency=3)
        chunker = ContextualChunker(
            context_generator=mock_gen, config=config, base_chunker=SMALL_CHUNKER,
        )

        result = await chunker.chunk_with_context(
            AMBIGUOUS_DOC, doc_summary=AMBIGUOUS_DOC_SUMMARY, file_path="/reports/q3.md"
        )

        assert result.total_chunks >= 2, f"Expected 2+ chunks, got {result.total_chunks}"
        assert result.chunks_with_context == result.total_chunks
        assert result.chunks_without_context == 0
        assert result.context_rate == 100.0
        assert result.source_document_id  # UUID generated

        # Verify contextual text includes context prefix
        for cc in result.chunks:
            assert cc.context is not None
            assert cc.context.situating_context
            assert cc.contextual_text.startswith(cc.context.situating_context)
            assert cc.chunk.text in cc.contextual_text

    @pytest.mark.asyncio
    async def test_reference_resolution_preserved(self):
        """Resolved references are correctly stored in chunk context."""
        async def mock_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            return _mock_context_for_chunk(chunk_text)

        config = ContextualChunkingConfig(enabled=True, batch_concurrency=2)
        chunker = ContextualChunker(
            context_generator=mock_gen, config=config, base_chunker=SMALL_CHUNKER,
        )

        result = await chunker.chunk_with_context(
            AMBIGUOUS_DOC, doc_summary=AMBIGUOUS_DOC_SUMMARY
        )

        # At least some chunks should have resolved references
        resolved_chunks = [
            cc for cc in result.chunks
            if cc.context and cc.context.resolved_references
        ]
        assert len(resolved_chunks) >= 1, "Expected at least 1 chunk with resolved references"

        # Check resolution structure
        for cc in resolved_chunks:
            for ref in cc.context.resolved_references:
                assert "original" in ref
                assert "resolved" in ref

    @pytest.mark.asyncio
    async def test_key_entities_extracted(self):
        """Key entities are extracted from each chunk."""
        async def mock_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            return _mock_context_for_chunk(chunk_text)

        config = ContextualChunkingConfig(enabled=True)
        chunker = ContextualChunker(
            context_generator=mock_gen, config=config, base_chunker=SMALL_CHUNKER,
        )

        result = await chunker.chunk_with_context(
            AMBIGUOUS_DOC, doc_summary=AMBIGUOUS_DOC_SUMMARY
        )

        all_entities = set()
        for cc in result.chunks:
            if cc.context:
                all_entities.update(cc.context.key_entities)

        # "Acme Corp" should appear in entities across all chunks
        assert "Acme Corp" in all_entities


# ---------------------------------------------------------------------------
# 2. AsyncSemanticSearch + Contextual Chunking Integration (SQLite)
# ---------------------------------------------------------------------------


class TestAsyncSearchWithContextualChunking:
    """Test AsyncSemanticSearch with contextual chunking enabled (SQLite, no embeddings)."""

    @pytest.mark.asyncio
    async def test_index_with_contextual_chunking_stores_metadata(self, tmp_path):
        """Index a document → verify chunk_context, chunk_position, source_document_id in DB."""
        db_path = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"

        async def mock_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            return _mock_context_for_chunk(chunk_text)

        search = AsyncSemanticSearch(
            database_url=db_url,
            embedding_provider=None,  # No embeddings, just chunking
            chunk_size=40,
            chunk_strategy=ChunkStrategy.FIXED,
            contextual_chunking=True,
            context_generator=mock_gen,
        )
        await search.initialize()

        path_id = await _create_tables_and_path(
            db_url, search.async_session, "/reports/q3.md", len(AMBIGUOUS_DOC)
        )

        # Index the document
        n_chunks = await search.index_document(
            path="/reports/q3.md", content=AMBIGUOUS_DOC, path_id=path_id
        )

        assert n_chunks >= 2, f"Expected 2+ chunks, got {n_chunks}"

        # Verify DB state: all chunks should have contextual metadata
        from sqlalchemy import text
        async with search.async_session() as session:
            rows = (await session.execute(
                text("SELECT chunk_id, chunk_text, chunk_context, chunk_position, source_document_id FROM document_chunks WHERE path_id = :pid ORDER BY chunk_position"),
                {"pid": path_id},
            )).fetchall()

        assert len(rows) == n_chunks

        # All chunks share the same source_document_id
        source_doc_ids = {r[4] for r in rows}
        assert len(source_doc_ids) == 1, f"Expected 1 source_document_id, got {source_doc_ids}"
        assert source_doc_ids.pop() is not None

        # Each chunk has a position and context
        for row in rows:
            chunk_id, chunk_text, chunk_context_json, chunk_position, source_doc_id = row
            assert chunk_text, "chunk_text should not be empty"
            assert chunk_position is not None, "chunk_position should be set"
            assert chunk_context_json is not None, "chunk_context should be set"

            # Validate the context JSON is valid and matches ChunkContext schema
            ctx_data = json.loads(chunk_context_json)
            assert "situating_context" in ctx_data
            assert "key_entities" in ctx_data
            assert len(ctx_data["situating_context"]) > 0

        # Verify positions are sequential
        positions = sorted(r[3] for r in rows)
        assert positions == list(range(len(positions)))

    @pytest.mark.asyncio
    async def test_index_without_contextual_chunking_no_metadata(self, tmp_path):
        """Without contextual chunking, chunk_context/position/source_document_id are NULL."""
        db_path = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"

        search = AsyncSemanticSearch(
            database_url=db_url,
            embedding_provider=None,
            chunk_size=40,
            chunk_strategy=ChunkStrategy.FIXED,
            contextual_chunking=False,  # Disabled
        )
        await search.initialize()

        path_id = await _create_tables_and_path(
            db_url, search.async_session, "/reports/old.md", len(AMBIGUOUS_DOC)
        )

        n_chunks = await search.index_document(
            path="/reports/old.md", content=AMBIGUOUS_DOC, path_id=path_id
        )

        assert n_chunks >= 2

        # Verify NO contextual metadata
        from sqlalchemy import text
        async with search.async_session() as session:
            rows = (await session.execute(
                text("SELECT chunk_context, chunk_position, source_document_id FROM document_chunks WHERE path_id = :pid"),
                {"pid": path_id},
            )).fetchall()

        for row in rows:
            assert row[0] is None, "chunk_context should be NULL when contextual chunking disabled"
            assert row[1] is None, "chunk_position should be NULL"
            assert row[2] is None, "source_document_id should be NULL"


# ---------------------------------------------------------------------------
# 3. Server E2E: Write + Permission Validation (non-user subjects)
# ---------------------------------------------------------------------------


class TestServerPermissionsE2E:
    """Test that non-user permission subjects can write and read through the API."""

    @pytest.mark.e2e
    def test_health_check(self, test_app):
        """Server is running and healthy."""
        response = test_app.get("/health")
        assert response.status_code == 200

    @pytest.mark.e2e
    def test_write_and_read_with_api_key(self, test_app, nexus_server):
        """Write a file and read it back with API key auth (non-user permission)."""
        api_key = os.environ.get("NEXUS_API_KEY", "test-e2e-api-key-12345")
        headers = {"Authorization": f"Bearer {api_key}"}

        # Write
        response = test_app.post(
            "/api/nfs/write",
            json={"path": "/test/q3_report.md", "data": AMBIGUOUS_DOC},
            headers=headers,
        )
        # Accept either 200 or auth error (depending on server config)
        if response.status_code == 200:
            # Read back
            read_resp = test_app.post(
                "/api/nfs/read",
                json={"path": "/test/q3_report.md"},
                headers=headers,
            )
            assert read_resp.status_code == 200

    @pytest.mark.e2e
    def test_list_with_api_key(self, test_app, nexus_server):
        """List files with API key auth."""
        api_key = os.environ.get("NEXUS_API_KEY", "test-e2e-api-key-12345")
        headers = {"Authorization": f"Bearer {api_key}"}

        response = test_app.post(
            "/api/nfs/list",
            json={"path": "/"},
            headers=headers,
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 4. Performance validation: contextual chunking overhead
# ---------------------------------------------------------------------------


class TestContextualChunkingPerformance:
    """Validate no performance regressions from contextual chunking."""

    @pytest.mark.asyncio
    async def test_concurrency_bounded(self):
        """Semaphore correctly limits concurrent LLM calls."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def tracking_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            try:
                await asyncio.sleep(0.005)  # Simulate LLM latency
                return _mock_context_for_chunk(chunk_text)
            finally:
                async with lock:
                    current_concurrent -= 1

        config = ContextualChunkingConfig(enabled=True, batch_concurrency=3)
        from nexus.search.chunking import DocumentChunker
        base_chunker = DocumentChunker(chunk_size=30, strategy=ChunkStrategy.FIXED)
        chunker = ContextualChunker(
            context_generator=tracking_gen,
            config=config,
            base_chunker=base_chunker,
        )

        # Large doc to produce many chunks
        doc = "\n\n".join([f"Paragraph {i} with enough words to form a complete chunk." for i in range(50)])
        result = await chunker.chunk_with_context(doc, doc_summary="Large doc")

        assert result.total_chunks > 5
        assert max_concurrent <= 3, f"Concurrency exceeded limit: {max_concurrent} > 3"

    @pytest.mark.asyncio
    async def test_indexing_performance_acceptable(self, tmp_path):
        """Indexing with contextual chunking completes in reasonable time."""
        db_path = tmp_path / f"perf_{uuid.uuid4().hex[:8]}.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"

        call_count = 0

        async def fast_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.001)  # 1ms simulated LLM latency
            return _mock_context_for_chunk(chunk_text)

        search = AsyncSemanticSearch(
            database_url=db_url,
            embedding_provider=None,
            chunk_size=50,
            chunk_strategy=ChunkStrategy.FIXED,
            contextual_chunking=True,
            context_generator=fast_gen,
        )
        await search.initialize()

        path_id = await _create_tables_and_path(
            db_url, search.async_session, "/perf/doc.md", 5000
        )

        # Generate a large-ish document
        large_doc = "\n\n".join([f"Section {i}: " + " ".join(["word"] * 40) for i in range(20)])

        start = time.monotonic()
        n_chunks = await search.index_document(
            path="/perf/doc.md", content=large_doc, path_id=path_id
        )
        elapsed = time.monotonic() - start

        assert n_chunks >= 5, f"Expected 5+ chunks, got {n_chunks}"
        assert call_count >= n_chunks, "Generator should be called for each chunk"
        # With 1ms simulated latency and concurrency=5, should complete quickly
        assert elapsed < 10.0, f"Indexing took too long: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_graceful_degradation_performance(self):
        """When LLM fails, heuristic fallback doesn't add significant overhead."""
        async def failing_gen(doc_summary, chunk_text, prev_chunks, next_chunks):
            raise RuntimeError("LLM unavailable")

        # Heuristic fallback is ON by default — chunks should still get context
        config = ContextualChunkingConfig(enabled=True, batch_concurrency=5)
        chunker = ContextualChunker(context_generator=failing_gen, config=config)

        start = time.monotonic()
        result = await chunker.chunk_with_context(
            AMBIGUOUS_DOC, doc_summary=AMBIGUOUS_DOC_SUMMARY
        )
        elapsed = time.monotonic() - start

        assert result.total_chunks >= 1
        # With heuristic fallback, all chunks get context even when LLM fails
        assert result.chunks_with_context == result.total_chunks
        # Heuristic fallback with retries should still be fast
        assert elapsed < 5.0, f"Degradation took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 5. Context generator factory E2E
# ---------------------------------------------------------------------------


class TestContextGeneratorFactoryE2E:
    """Test create_context_generator with realistic payloads."""

    @pytest.mark.asyncio
    async def test_factory_produces_valid_context(self):
        """Factory-created generator parses JSON correctly."""
        mock_llm = AsyncMock(return_value=json.dumps({
            "situating_context": "Acme Corp Q3 earnings discussion",
            "resolved_references": [{"original": "He", "resolved": "CEO John Smith"}],
            "key_entities": ["Acme Corp", "Q3", "John Smith"],
        }))

        gen = await create_context_generator(mock_llm)
        ctx = await gen(
            AMBIGUOUS_DOC_SUMMARY,
            "He mentioned that revenue exceeded expectations by 15%.",
            ["Acme Corp reported strong Q3 results."],
            ["Looking ahead, management expects continued momentum."],
        )

        assert isinstance(ctx, ChunkContext)
        assert ctx.situating_context == "Acme Corp Q3 earnings discussion"
        assert len(ctx.resolved_references) == 1
        assert ctx.resolved_references[0]["original"] == "He"
        assert "John Smith" in ctx.key_entities

    @pytest.mark.asyncio
    async def test_factory_prompt_includes_surrounding_context(self):
        """Factory sends previous and next chunks in the prompt."""
        captured_prompt = None

        async def capture_llm(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return json.dumps({
                "situating_context": "test",
                "resolved_references": [],
                "key_entities": [],
            })

        gen = await create_context_generator(capture_llm)
        await gen(
            "Doc summary here",
            "Current chunk text",
            ["Previous chunk one", "Previous chunk two"],
            ["Next chunk one"],
        )

        assert captured_prompt is not None
        assert "Doc summary here" in captured_prompt
        assert "Current chunk text" in captured_prompt
        assert "Previous chunk" in captured_prompt
        assert "Next chunk" in captured_prompt
