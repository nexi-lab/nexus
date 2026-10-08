"""Python result shapes for the typed SearchService protocol."""

from datetime import UTC, datetime
from typing import Any

from nexus.grpc.search.v1 import search_pb2


def semantic_hit(result: search_pb2.QueryResult) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "path": result.path,
        "chunk_text": result.chunk_text,
        "score": round(result.score, 4),
        "chunk_index": result.chunk_index,
    }
    if result.HasField("title_score"):
        hit["title_score"] = round(result.title_score, 4)
    return hit


def search_stats(response: search_pb2.StatsResponse) -> dict[str, Any]:
    timestamp = response.last_successful_index_at_ms
    backend = response.backend or "rust-plugin"
    return {
        "fts_doc_count": response.fts_doc_count,
        "fts_path_count": response.fts_path_count,
        "ann_chunk_count": response.ann_chunk_count,
        "parked_count": response.parked_count,
        "backend": backend,
        "embedding_model": response.embedding_model or None,
        "vector_backend": "hnsw-in-process" if response.embedding_model else None,
        "indexing_in_progress": response.indexing_in_progress,
        "last_index_seq": response.last_index_seq,
        "pending": response.pending,
        "last_successful_index_at": (
            datetime.fromtimestamp(timestamp / 1000.0, tz=UTC).isoformat() if timestamp else None
        ),
        "last_index_refresh": timestamp / 1000.0 if timestamp else None,
    }
