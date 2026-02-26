"""Semantic search component factory (Issue #2075).

Centralizes creation of semantic search services (Seemann composition root
pattern). Both the NexusFS-path (``ainitialize_semantic_search``) and the
RPC-path (``initialize_semantic_search``) delegate here instead of duplicating
component wiring.

See also: docs/design/NEXUS-LEGO-ARCHITECTURE.md §2 (Factory Layer).
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.search.indexing import IndexingPipeline
    from nexus.bricks.search.indexing_service import IndexingService
    from nexus.bricks.search.pipeline_indexer import PipelineIndexer
    from nexus.bricks.search.query_service import QueryService

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
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    api_key: str | None = None,
    chunk_size: int = 1024,
    chunk_strategy: str = "semantic",
    cache_url: str | None = None,
    embedding_cache_ttl: int = 86400 * 3,
    # NexusFS-path extras (None on RPC path)
    nx: Any = None,
    # RPC-path extras for PipelineIndexer
    session_factory: Any = None,
    metadata: Any = None,
    file_reader: Any = None,
    file_lister: Any = None,
) -> SemanticSearchComponents:
    """Create all semantic search services.

    This is the single factory for both the NexusFS-path and the RPC-path.

    Args:
        record_store: RecordStoreABC providing engine and session_factory.
        embedding_provider: Provider name (e.g. "openai", "voyage").
        embedding_model: Model name for embeddings.
        api_key: API key for embedding provider.
        chunk_size: Chunk size in tokens.
        chunk_strategy: "fixed", "semantic", or "overlapping".
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
    from nexus.bricks.search.vector_db import VectorDatabase

    # --- Embedding provider ---
    emb_provider = None
    if embedding_provider:
        from nexus.bricks.search.embeddings import create_cached_embedding_provider
        from nexus.lib.env import get_dragonfly_url

        effective_cache_url = cache_url or get_dragonfly_url()
        emb_provider = await create_cached_embedding_provider(
            provider=embedding_provider,
            model=embedding_model,
            api_key=api_key,
            cache_url=effective_cache_url,
            cache_ttl=embedding_cache_ttl,
        )

    strategy_map = {
        "fixed": ChunkStrategy.FIXED,
        "semantic": ChunkStrategy.SEMANTIC,
        "overlapping": ChunkStrategy.OVERLAPPING,
    }
    chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

    # --- Core components ---
    engine = record_store.engine
    _is_pg = not str(engine.url).startswith("sqlite")
    vector_db = VectorDatabase(engine, is_postgresql=_is_pg)
    await asyncio.to_thread(vector_db.initialize)  # 14A: async init

    chunker = DocumentChunker(
        chunk_size=chunk_size,
        strategy=chunk_strat,
        overlap_size=128,
    )

    _sync_sf = record_store.session_factory
    _async_sf = None
    with contextlib.suppress(NotImplementedError, AttributeError):
        _async_sf = record_store.async_session_factory

    pipeline = IndexingPipeline(
        chunker=chunker,
        embedding_provider=emb_provider,
        db_type=vector_db.db_type,
        async_session_factory=_async_sf,
        max_concurrency=10,
        cross_doc_batching=True,
    )

    # --- ContextBuilder (optional, Issue #2036) ---
    _context_builder = None
    try:
        from nexus.services.llm.llm_context_builder import (
            AdaptiveRetrievalConfig,
            ContextBuilder,
        )

        _context_builder = ContextBuilder(adaptive_config=AdaptiveRetrievalConfig())
    except ImportError:
        pass  # optional dependency
    except Exception:
        logger.warning("Failed to create ContextBuilder", exc_info=True)

    # --- QueryService ---
    query_service = None
    if _sync_sf is not None:
        from nexus.bricks.search.query_service import QueryService

        query_service = QueryService(
            vector_db=vector_db,
            session_factory=_sync_sf,
            embedding_provider=emb_provider,
            context_builder=_context_builder,
        )

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
            vector_db=vector_db,
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
