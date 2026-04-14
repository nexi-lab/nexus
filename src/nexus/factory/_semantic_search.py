"""Semantic search component factory (Issue #2075).

Centralizes creation of semantic search services (Seemann composition root
pattern). Both the NexusFS-path (``ainitialize_semantic_search``) and the
RPC-path (``initialize_semantic_search``) delegate here instead of duplicating
component wiring.

See also: docs/design/NEXUS-LEGO-ARCHITECTURE.md §2 (Factory Layer).
"""

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.search.indexing import IndexingPipeline
    from nexus.bricks.search.indexing_service import IndexingService
    from nexus.bricks.search.pipeline_indexer import PipelineIndexer

    # Removed: txtai handles this (Issue #2663)
    # from nexus.bricks.search.query_service import QueryService
    QueryService = Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SemanticSearchComponents:
    """All components produced by the semantic search factory."""

    query_service: "QueryService | None"
    indexing_service: "IndexingService | None"
    pipeline_indexer: "PipelineIndexer | None"
    indexing_pipeline: "IndexingPipeline | None"


async def create_semantic_search_components(
    *,
    record_store: Any,
    chunk_size: int = 1024,
    chunk_strategy: str = "semantic",
    # NexusFS-path extras (None on RPC path)
    nx: Any = None,
    # RPC-path extras for PipelineIndexer
    session_factory: Any = None,
    metadata: Any = None,
    file_reader: Any = None,
    file_lister: Any = None,
    # Legacy params absorbed for backward compat (txtai handles these now)
    **_kwargs: Any,
) -> SemanticSearchComponents:
    """Create all semantic search services.

    This is the single factory for both the NexusFS-path and the RPC-path.

    Args:
        record_store: RecordStoreABC providing engine and session_factory.
        embedding_provider: Provider name (e.g. "openai", "voyage").
        embedding_model: Model name for embeddings.
        api_key: API key for embedding provider.
        chunk_size: Chunk size in tokens.
        chunk_strategy: "fixed", "semantic", "overlapping", or "markdown_aware".
        cache_url: Redis/Dragonfly URL for embedding cache.
        embedding_cache_ttl: Cache TTL in seconds (default: 3 days).
        nx: NexusFS instance (NexusFS-path only).
        session_factory: DB session factory for PipelineIndexer (RPC-path).
        metadata: MetastoreABC for PipelineIndexer (RPC-path).
        file_reader: File read callable for PipelineIndexer (RPC-path).
        file_lister: File list callable for PipelineIndexer (RPC-path).

    Returns:
        SemanticSearchComponents with all created services.
    """
    from nexus.bricks.search.chunking import ChunkStrategy, DocumentChunker
    from nexus.bricks.search.indexing import IndexingPipeline

    # Removed: txtai handles this (Issue #2663)
    # from nexus.bricks.search.vector_db import VectorDatabase

    # --- Embedding provider ---
    # Removed: txtai handles this (Issue #2663)
    emb_provider = None

    strategy_map = {
        "fixed": ChunkStrategy.FIXED,
        "semantic": ChunkStrategy.SEMANTIC,
        "overlapping": ChunkStrategy.OVERLAPPING,
        "markdown_aware": ChunkStrategy.MARKDOWN_AWARE,
    }
    chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

    # --- Core components ---
    # Removed: txtai handles this (Issue #2663)
    # VectorDatabase is no longer available; vector_db set to None.
    vector_db = None

    chunker = DocumentChunker(
        chunk_size=chunk_size,
        strategy=chunk_strat,
        overlap_size=128,
    )

    _sync_sf = record_store.session_factory
    _async_sf = None
    with contextlib.suppress(NotImplementedError, AttributeError):
        _async_sf = record_store.async_session_factory

    # Determine db_type from engine URL (vector_db removed, Issue #2663)
    engine = record_store.engine
    _is_pg = not str(engine.url).startswith("sqlite")
    _db_type = "postgresql" if _is_pg else "sqlite"

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=emb_provider,
        db_type=_db_type,
        async_session_factory=_async_sf,
        max_concurrency=10,
        cross_doc_batching=True,
    )

    # --- QueryService ---
    # Removed: txtai handles this (Issue #2663)
    query_service = None

    # --- IndexingService (NexusFS-path only) ---
    indexing_service = None
    if nx is not None and _sync_sf is not None:
        from nexus.bricks.search.indexing_service import IndexingService
        from nexus.factory.adapters import _NexusFSFileReader

        _file_reader = _NexusFSFileReader(nx)
        indexing_service = IndexingService(
            pipeline=pipeline,
            file_reader=_file_reader,
            session_factory=_sync_sf,
            vector_db=vector_db,  # None — txtai handles this (Issue #2663)
            embedding_provider=emb_provider,
        )

    # --- PipelineIndexer (RPC-path only) ---
    pipeline_indexer = None
    if session_factory is not None and metadata is not None and file_reader is not None:
        from nexus.bricks.search.pipeline_indexer import PipelineIndexer

        pipeline_indexer = PipelineIndexer(
            pipeline=pipeline,
            session_factory=session_factory,
            metadata=metadata,
            file_reader=file_reader,
            file_lister=file_lister,
        )

    return SemanticSearchComponents(
        query_service=query_service,
        indexing_service=indexing_service,
        pipeline_indexer=pipeline_indexer,
        indexing_pipeline=pipeline,
    )
