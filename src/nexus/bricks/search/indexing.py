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
from dataclasses import dataclass
from typing import Any

from nexus.bricks.search.chunk_store import ChunkRecord, ChunkStore
from nexus.bricks.search.chunking import DocumentChunker, EntropyAwareChunker
from nexus.bricks.search.mutation_events import extract_zone_id, strip_zone_prefix

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
        db_type: str = "sqlite",
        async_session_factory: Any | None = None,
        max_concurrency: int = 10,
        batch_size: int = 100,
        max_embedding_concurrency: int = 5,
        cross_doc_batching: bool = True,
        sqlite_vec_backend: Any | None = None,
    ):
        self._chunker = chunker
        self._embedding_provider = embedding_provider
        self._entropy_chunker = entropy_chunker
        self._db_type = db_type
        self._async_session_factory = async_session_factory
        self._max_concurrency = max_concurrency
        self._batch_size = batch_size
        self._max_embedding_concurrency = max_embedding_concurrency
        self._cross_doc_batching = cross_doc_batching
        # Codex review R5 (high): SANDBOX-only side-write into the local
        # ``SqliteVecBackend`` so the hybrid path's vector lane is
        # actually populated by normal indexing. Without this, the
        # ``_sqlite_vec_backend`` wired into ``SearchService`` only ever
        # sees rows that someone manually upserted — production
        # indexing flows through ``_bulk_insert`` (BM25S/Txtai pillar)
        # and the vec lane stays empty, silently degrading SANDBOX
        # hybrid-by-default to keyword-only.
        self._sqlite_vec_backend = sqlite_vec_backend

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

    async def prune_vec_rows(self, path: str) -> None:
        """Prune sqlite-vec rows for a single ``path``.

        Codex review R9 #4 (high): IndexingService has bypass branches
        (successful empty parse, parse-error stale-chunk delete) that
        clear ``document_chunks`` directly without going through this
        pipeline — so the side-write into the SANDBOX vec lane never
        runs and stale rows survive. Expose a tiny helper so those
        branches can pull the vec lane along without each one having
        to know about the backend wiring.

        Codex review R10 #3 (medium): callers run this INSIDE their
        SQL CAS transaction so a vec failure rolls back the chunk
        delete + hash advance, leaving the lanes consistent and the
        CAS unsatisfied for the next tick to retry. We therefore do
        NOT swallow exceptions here — let them propagate so the
        transaction surfaces the failure.
        """
        if self._sqlite_vec_backend is None:
            return
        zone_id = extract_zone_id(path)
        canonical = strip_zone_prefix(path) if path.startswith("/zone/") else path
        keys = [canonical]
        if path != canonical:
            keys.append(path)
        await self._sqlite_vec_backend.delete(keys, zone_id=zone_id)

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
            Paths that are outside the current index scope (Issue #3698)
            are returned with ``chunks_indexed=0`` and no error, and they
            never reach the chunker or the embedding provider.
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

        chunked_docs: list[_ChunkedDoc] = []
        results_map: dict[str, IndexResult] = {}
        original_order: list[str] = [p for p, _, _ in documents]

        # Codex review R7 #2 (high): zero-chunk docs (empty file,
        # parser returned nothing) skip _bulk_insert entirely — but
        # the caller's intent was a REPLACE, not a no-op. Without
        # explicit prune, prior vec rows for the same path survive
        # with stale text. Track the empty-result paths so we can
        # delete their vec rows below before returning the zero-chunk
        # IndexResult.
        empty_replace_by_zone: dict[str, list[str]] = {}
        # Codex review R8 #3 (high): also need to clear the canonical
        # ``document_chunks`` rows for zero-chunk replacements. The R7
        # branch only pruned the vec lane, leaving the BM25/FTS-lane
        # ChunkStore rows intact — so deleted/truncated docs still
        # surfaced in keyword search. Capture path_ids so we can
        # delete via the same ChunkStore the bulk-insert path uses.
        empty_replace_path_ids: list[str] = []
        for item in phase1:
            if isinstance(item, IndexResult):
                results_map[item.path] = item
            elif not item.chunks:
                results_map[item.path] = IndexResult(path=item.path, chunks_indexed=0)
                if self._sqlite_vec_backend is not None:
                    try:
                        zid = extract_zone_id(item.path)
                        empty_replace_by_zone.setdefault(zid, []).append(item.path)
                    except ValueError:
                        # Bad path shape — already surfaced upstream.
                        pass
                if item.path_id:
                    empty_replace_path_ids.append(item.path_id)
            else:
                chunked_docs.append(item)

        if self._sqlite_vec_backend is not None and empty_replace_by_zone:
            for zone_id, empty_paths in empty_replace_by_zone.items():
                try:
                    # Codex review R9 #3 (high): dual-key prune.
                    empty_keys: list[str] = []
                    for p in empty_paths:
                        canon = strip_zone_prefix(p) if p.startswith("/zone/") else p
                        if canon not in empty_keys:
                            empty_keys.append(canon)
                        if p != canon and p not in empty_keys:
                            empty_keys.append(p)
                    await self._sqlite_vec_backend.delete(empty_keys, zone_id=zone_id)
                except Exception as exc:
                    logger.warning(
                        "[IndexingPipeline] sqlite-vec empty-replace prune failed "
                        "for zone=%s paths=%s: %s",
                        zone_id,
                        empty_paths,
                        exc,
                    )

        if empty_replace_path_ids and self._async_session_factory is not None:
            try:
                empty_chunk_store = ChunkStore(
                    async_session_factory=self._async_session_factory,
                    db_type=self._db_type,
                )
                for path_id in empty_replace_path_ids:
                    await empty_chunk_store.delete_document_chunks(path_id)
            except Exception as exc:
                logger.warning(
                    "[IndexingPipeline] document_chunks empty-replace prune "
                    "failed for %d path_id(s): %s",
                    len(empty_replace_path_ids),
                    exc,
                )

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

        # Return results in the caller's original input order (captured
        # before scope filtering reduced the working list).
        return [results_map.get(p, IndexResult(path=p, chunks_indexed=0)) for p in original_order]

    # ------------------------------------------------------------------
    # Phase 1: Chunking
    # ------------------------------------------------------------------

    async def _chunk_document(self, path: str, content: str, path_id: str) -> _ChunkedDoc:
        """Chunk a single document (CPU-bound work offloaded to thread).

        LLM-driven contextual chunking used to live here as a third
        branch; the Rust nexus-search-plugin now owns that at its own
        indexing layer (see contextual_chunker.rs in the plugin), so
        this pipeline no longer duplicates the LLM round-trip.
        """
        if self._entropy_chunker is not None:
            entropy_result = await self._entropy_chunker.chunk_with_filtering(
                content, path, compute_lines=True
            )
            chunks = entropy_result.chunks
            chunk_texts = [c.text for c in chunks]
        else:
            # CPU-bound: offload to thread (Issue #1094 / decision 14)
            chunks = await asyncio.to_thread(self._chunker.chunk, content, path)
            chunk_texts = [c.text for c in chunks]

        # Issue #3719: When chunks carry heading_prefix (markdown-aware
        # strategy), prepend it to chunk_texts for embedding enrichment
        # while keeping chunk.text raw for storage.
        chunk_texts = [
            f"{c.heading_prefix} {t}" if getattr(c, "heading_prefix", None) else t
            for c, t in zip(chunks, chunk_texts, strict=True)
        ]

        return _ChunkedDoc(
            path=path,
            path_id=path_id,
            chunks=chunks,
            chunk_texts=chunk_texts,
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
                heading_prefix=chunk.heading_prefix,
                embedding=embeddings[i] if embeddings else None,
                embedding_model=embedding_model,
                # chunk_context / chunk_position / source_document_id
                # default to None on ChunkRecord.  They used to be
                # populated by the Python contextual-chunking branch;
                # the Rust nexus-search-plugin now owns that at its
                # own indexing layer and does not write back to this
                # Python ChunkStore, so leaving them None here is a
                # clean pass-through.
            )
            for i, chunk in enumerate(doc.chunks)
        ]
        await chunk_store.replace_document_chunks(doc.path_id, records)
        # Codex review R5+R6 (high): mirror the chunks into the
        # SANDBOX local sqlite-vec backend so the hybrid path's vector
        # lane has real data to fuse. ``upsert`` only replaces rows
        # whose ``(zone_id, path, chunk_index)`` matches an incoming
        # tuple — so if a document shrinks (5 chunks → 3), chunks 3
        # and 4 would survive with stale text and corrupt search
        # results. ``ChunkStore.replace_document_chunks`` (above) has
        # full-replace semantics; we mirror that contract by deleting
        # ALL rows for ``(zone_id, path)`` before the upsert. Best-
        # effort: a backend failure must not abort the primary write.
        if self._sqlite_vec_backend is not None and doc.chunks:
            try:
                zone_id = extract_zone_id(doc.path)
                # Codex review R9 #3 (high): canonical key = unscoped
                # virtual_path. Vec rows MUST agree with BM25 keys,
                # IndexingPipeline writers, and SearchService's
                # unscoped path_filter — otherwise scoped vs unscoped
                # doppelgängers leak into hybrid results and break
                # path-prefix filtering.
                canonical_path = (
                    strip_zone_prefix(doc.path) if doc.path.startswith("/zone/") else doc.path
                )
                # Full-replace: drop every existing chunk for this
                # path, then insert the current set. The SqliteVec
                # ``delete`` API takes "ids" but treats them as paths
                # and deletes every row for the (zone_id, path) tuple.
                # Pass BOTH the canonical and the original (possibly
                # scoped) form so legacy rows are also pruned.
                delete_keys = [canonical_path]
                if doc.path != canonical_path:
                    delete_keys.append(doc.path)
                await self._sqlite_vec_backend.delete(delete_keys, zone_id=zone_id)
                items = [
                    {
                        "path": canonical_path,
                        "text": chunk.text,
                        "chunk_index": i,
                        "chunk_tokens": chunk.tokens,
                        "line_start": chunk.line_start,
                        "line_end": chunk.line_end,
                        "heading_prefix": chunk.heading_prefix,
                    }
                    for i, chunk in enumerate(doc.chunks)
                ]
                await self._sqlite_vec_backend.upsert(items, zone_id=zone_id)
            except Exception as exc:
                # Vec backend is supplementary — log loudly but don't
                # break the primary index path. The hybrid lane will
                # surface ``semantic_degraded=True`` on search if the
                # vec lane is empty, so users still get a clear signal.
                logger.warning(
                    "[IndexingPipeline] sqlite-vec side-write failed for %s "
                    "(hybrid lane will degrade to keyword-only for this doc): %s",
                    doc.path,
                    exc,
                )
