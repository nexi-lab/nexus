"""Parallel indexing pipeline for embedding generation (Issue #1094).

Two-phase pipeline for 15-30x faster bulk indexing:
  Phase 1: Chunk documents in parallel (asyncio.to_thread for CPU-bound work)
  Phase 2: Batch embed across all documents, then bulk insert to DB

Features:
- Semaphore-bounded parallel document processing
- Cross-document embedding batching (fewer API calls)
- Bulk DB inserts (executemany for SQLite, batched INSERT for PG)
- Configurable concurrency and progress reporting
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nexus.bricks.search.chunk_store import ChunkRecord, ChunkStore
from nexus.bricks.search.chunking import DocumentChunker, EntropyAwareChunker
from nexus.bricks.search.contextual_chunking import (
    ContextualChunker,
    ContextualChunkResult,
)

# Removed: txtai handles this (Issue #2663)
# from nexus.bricks.search.embeddings import EmbeddingProvider
try:
    from nexus.bricks.search.embeddings import EmbeddingProvider
except ImportError:
    EmbeddingProvider = Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    """Result of indexing a single document."""

    path: str
    chunks_indexed: int
    error: str | None = None


@dataclass(frozen=True)
class IndexProgress:
    """Progress report for bulk indexing."""

    completed: int
    total: int
    current_path: str | None = None
    errors: int = 0


@dataclass
class _ChunkedDoc:
    """Internal: result of phase-1 chunking for a single document."""

    path: str
    path_id: str
    chunks: list[Any]
    chunk_texts: list[str]
    contextual_result: ContextualChunkResult | None = None
    source_document_id: str | None = None
    context_jsons: list[str | None] = field(default_factory=list)
    context_positions: list[int | None] = field(default_factory=list)


class IndexingPipeline:
    """Parallel indexing pipeline for embedding generation.

    Two-phase pipeline:
      Phase 1: Chunk documents in parallel (asyncio.to_thread for CPU-bound work)
      Phase 2: Batch embed across all documents, then bulk insert to DB
    """

    def __init__(
        self,
        *,
        chunker: DocumentChunker,
        embedding_provider: EmbeddingProvider | None = None,
        entropy_chunker: EntropyAwareChunker | None = None,
        contextual_chunker: ContextualChunker | None = None,
        db_type: str = "sqlite",
        async_session_factory: Any | None = None,
        max_concurrency: int = 10,
        batch_size: int = 100,
        max_embedding_concurrency: int = 5,
        cross_doc_batching: bool = True,
    ):
        self._chunker = chunker
        self._embedding_provider = embedding_provider
        self._entropy_chunker = entropy_chunker
        self._contextual_chunker = contextual_chunker
        self._db_type = db_type
        self._async_session_factory = async_session_factory
        self._max_concurrency = max_concurrency
        self._batch_size = batch_size
        self._max_embedding_concurrency = max_embedding_concurrency
        self._cross_doc_batching = cross_doc_batching

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_document(
        self,
        path: str,
        content: str,
        path_id: str,
    ) -> IndexResult:
        """Index a single document through the pipeline.

        Args:
            path: Virtual path of the document.
            content: Document text content.
            path_id: Path ID from file_paths table.

        Returns:
            IndexResult with chunk count or error.
        """
        results = await self.index_documents([(path, content, path_id)])
        return results[0]

    async def index_documents(
        self,
        documents: list[tuple[str, str, str]],
        progress_callback: Callable[[IndexProgress], None] | None = None,
    ) -> list[IndexResult]:
        """Index multiple documents with parallelism and cross-doc batching.

        Args:
            documents: List of (path, content, path_id) tuples.
            progress_callback: Optional callback for progress reporting.

        Returns:
            List of IndexResult (one per document, same order as input).
        """
        if not documents:
            return []

        total = len(documents)
        sem = asyncio.Semaphore(self._max_concurrency)
        completed = 0
        errors = 0

        # Phase 1: Chunk all documents in parallel (semaphore-bounded)
        async def _chunk_one(path: str, content: str, path_id: str) -> _ChunkedDoc | IndexResult:
            nonlocal completed, errors
            async with sem:
                try:
                    doc = await self._chunk_document(path, content, path_id)
                    return doc
                except Exception as exc:
                    errors += 1
                    return IndexResult(path=path, chunks_indexed=0, error=str(exc))
                finally:
                    completed += 1
                    if progress_callback:
                        progress_callback(
                            IndexProgress(
                                completed=completed,
                                total=total,
                                current_path=path,
                                errors=errors,
                            )
                        )

        phase1 = await asyncio.gather(*[_chunk_one(p, c, pid) for p, c, pid in documents])

        # Separate successful chunks from errors
        chunked_docs: list[_ChunkedDoc] = []
        results_map: dict[str, IndexResult] = {}
        doc_order = [p for p, _, _ in documents]

        for item in phase1:
            if isinstance(item, IndexResult):
                results_map[item.path] = item
            else:
                if not item.chunks:
                    results_map[item.path] = IndexResult(path=item.path, chunks_indexed=0)
                else:
                    chunked_docs.append(item)

        # Phase 2: Cross-doc embedding + bulk insert
        if chunked_docs and self._embedding_provider:
            if self._cross_doc_batching:
                await self._embed_cross_doc(chunked_docs)
            else:
                await self._embed_per_doc(chunked_docs)

        # Phase 3: Bulk insert each document
        for doc in chunked_docs:
            try:
                await self._bulk_insert(doc)
                results_map[doc.path] = IndexResult(path=doc.path, chunks_indexed=len(doc.chunks))
            except Exception as exc:
                logger.error("Bulk insert failed for %s: %s", doc.path, exc)
                results_map[doc.path] = IndexResult(path=doc.path, chunks_indexed=0, error=str(exc))

        # Return results in input order
        return [results_map.get(p, IndexResult(path=p, chunks_indexed=0)) for p in doc_order]

    # ------------------------------------------------------------------
    # Phase 1: Chunking
    # ------------------------------------------------------------------

    async def _chunk_document(self, path: str, content: str, path_id: str) -> _ChunkedDoc:
        """Chunk a single document (CPU-bound work offloaded to thread)."""
        contextual_result: ContextualChunkResult | None = None
        source_document_id: str | None = None
        context_jsons: list[str | None] = []
        context_positions: list[int | None] = []

        if self._contextual_chunker is not None:
            doc_summary = (
                content[:500].rsplit(". ", 1)[0] + "." if ". " in content[:500] else content[:500]
            )
            contextual_result = await self._contextual_chunker.chunk_with_context(
                document=content,
                doc_summary=doc_summary,
                file_path=path,
                compute_lines=True,
            )
            source_document_id = contextual_result.source_document_id
            chunks = [cc.chunk for cc in contextual_result.chunks]
            chunk_texts = [cc.contextual_text for cc in contextual_result.chunks]
            for cc in contextual_result.chunks:
                context_positions.append(cc.position)
                context_jsons.append(
                    cc.context.model_dump_json() if cc.context is not None else None
                )
        elif self._entropy_chunker is not None:
            entropy_result = await self._entropy_chunker.chunk_with_filtering(
                content, path, compute_lines=True
            )
            chunks = entropy_result.chunks
            chunk_texts = [c.text for c in chunks]
        else:
            # CPU-bound: offload to thread (Issue #1094 / decision 14)
            chunks = await asyncio.to_thread(self._chunker.chunk, content, path)
            chunk_texts = [c.text for c in chunks]

        # Issue #3719: When chunks carry heading_prefix (markdown-aware strategy),
        # prepend it to chunk_texts for embedding enrichment while keeping
        # chunk.text raw for storage.  Skipped when contextual chunking is
        # active because LLM-generated context is strictly richer.
        if contextual_result is None:
            chunk_texts = [
                f"{c.heading_prefix} {t}" if getattr(c, "heading_prefix", None) else t
                for c, t in zip(chunks, chunk_texts, strict=True)
            ]

        return _ChunkedDoc(
            path=path,
            path_id=path_id,
            chunks=chunks,
            chunk_texts=chunk_texts,
            contextual_result=contextual_result,
            source_document_id=source_document_id,
            context_jsons=context_jsons,
            context_positions=context_positions,
        )

    # ------------------------------------------------------------------
    # Phase 2: Embedding
    # ------------------------------------------------------------------

    async def _embed_cross_doc(self, docs: list[_ChunkedDoc]) -> None:
        """Batch embed across all documents, then split back per-document."""
        if not self._embedding_provider:
            return

        # Flatten all chunk texts and record boundaries
        all_texts: list[str] = []
        boundaries: list[int] = []  # cumulative lengths
        for doc in docs:
            all_texts.extend(doc.chunk_texts)
            boundaries.append(len(all_texts))

        if not all_texts:
            return

        logger.info(
            "[INDEXING] Cross-doc batching: %d texts across %d documents",
            len(all_texts),
            len(docs),
        )

        all_embeddings = await self._embedding_provider.embed_texts_batched(
            all_texts,
            batch_size=self._batch_size,
            parallel=True,
            max_concurrent=self._max_embedding_concurrency,
        )

        # Split embeddings back to per-document
        start = 0
        for i, doc in enumerate(docs):
            end = boundaries[i]
            doc._embeddings = all_embeddings[start:end]  # type: ignore[attr-defined]
            start = end

    async def _embed_per_doc(self, docs: list[_ChunkedDoc]) -> None:
        """Embed each document independently (no cross-doc batching)."""
        if not self._embedding_provider:
            return

        sem = asyncio.Semaphore(self._max_embedding_concurrency)

        async def _embed_one(doc: _ChunkedDoc) -> None:
            async with sem:
                if doc.chunk_texts:
                    doc._embeddings = await self._embedding_provider.embed_texts_batched(  # type: ignore[union-attr, attr-defined]
                        doc.chunk_texts,
                        batch_size=self._batch_size,
                        parallel=True,
                        max_concurrent=self._max_embedding_concurrency,
                    )

        await asyncio.gather(*[_embed_one(d) for d in docs])

    # ------------------------------------------------------------------
    # Phase 3: Bulk insert
    # ------------------------------------------------------------------

    async def _bulk_insert(self, doc: _ChunkedDoc) -> None:
        """Bulk insert chunks + embeddings for one document."""
        if self._async_session_factory is None:
            raise RuntimeError("async_session_factory required for bulk insert")

        embeddings: list[list[float]] | None = getattr(doc, "_embeddings", None)
        embedding_model = (
            self._embedding_provider.__class__.__name__ if self._embedding_provider else None
        )
        chunk_store = ChunkStore(
            async_session_factory=self._async_session_factory,
            db_type=self._db_type,
        )
        records = [
            ChunkRecord(
                chunk_text=chunk.text,
                chunk_tokens=chunk.tokens,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                embedding=embeddings[i] if embeddings else None,
                embedding_model=embedding_model,
                chunk_context=doc.context_jsons[i] if doc.context_jsons else None,
                chunk_position=doc.context_positions[i] if doc.context_positions else None,
                source_document_id=doc.source_document_id,
            )
            for i, chunk in enumerate(doc.chunks)
        ]
        await chunk_store.replace_document_chunks(doc.path_id, records)
