"""Centralized search configuration (Issue #1520).

Provides a frozen SearchConfig dataclass and environment-variable helpers
to replace scattered os.environ.get() calls across search modules.

Usage::

    from nexus.bricks.search.config import search_config_from_env

    config = search_config_from_env()
    print(config.chunk_size)       # 1024
    print(config.fusion_method)    # "rrf"
"""

import os
from dataclasses import dataclass


def get_env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean from an environment variable.

    Truthy values: "true", "1", "yes" (case-insensitive).

    Args:
        key: Environment variable name.
        default: Default if not set.

    Returns:
        Parsed boolean.
    """
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes")


def get_env_float(key: str, default: float) -> float:
    """Read a float from an environment variable.

    Args:
        key: Environment variable name.
        default: Default if not set or unparseable.

    Returns:
        Parsed float.
    """
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def get_env_int(key: str, default: int) -> int:
    """Read an int from an environment variable.

    Args:
        key: Environment variable name.
        default: Default if not set or unparseable.

    Returns:
        Parsed int.
    """
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class SearchConfig:
    """Immutable search configuration.

    Centralizes all search-related settings that were previously scattered
    across multiple constructors and env-var reads.

    Frozen (immutable) to prevent accidental mutation after initialization.
    """

    # Chunking
    chunk_size: int = 1024
    chunk_strategy: str = "semantic"
    overlap_size: int = 128

    # Entropy filtering (Issue #1024)
    entropy_filtering: bool = False
    entropy_threshold: float = 0.35
    entropy_alpha: float = 0.5

    # Fusion (Issue #798)
    fusion_method: str = "rrf"
    fusion_alpha: float = 0.5
    rrf_k: int = 60

    # Embedding
    embedding_provider: str = "openai"
    embedding_model: str | None = None

    # Database pool (Issue #1520)
    pool_min_size: int = 10
    pool_max_size: int = 50
    pool_recycle: int = 1800  # 30 minutes

    # Search mode
    search_mode: str = "hybrid"

    # Contextual chunking (Issue #1192)
    contextual_chunking: bool = False

    # Ranking (Issue #1092)
    enable_attribute_boosting: bool = True

    # Indexing pipeline (Issue #1094)
    index_max_concurrency: int = 10  # Max concurrent document indexing
    index_batch_size: int = 100  # Embedding batch size
    index_cross_doc_batching: bool = True  # Enable cross-document batching
    index_max_embedding_concurrency: int = 5  # Max concurrent embedding API calls

    # Thread pool sizing (Issue #2188: centralized from module-level globals)
    vector_pool_workers: int = 2  # ThreadPoolExecutor for sync-to-async bridge
    ml_executor_workers: int = 4  # ThreadPoolExecutor for ML inference


def search_config_from_env() -> SearchConfig:
    """Build SearchConfig from environment variables.

    Env vars (all optional, with sane defaults):
        NEXUS_CHUNK_SIZE: Chunk size in tokens (default: 1024)
        NEXUS_CHUNK_STRATEGY: "fixed" | "semantic" | "overlapping"
        NEXUS_ENTROPY_FILTERING: Enable entropy filtering
        NEXUS_ENTROPY_THRESHOLD: Redundancy threshold (0.0-1.0)
        NEXUS_ENTROPY_ALPHA: Entity vs semantic novelty balance
        NEXUS_FUSION_METHOD: "rrf" | "weighted" | "rrf_weighted"
        NEXUS_FUSION_ALPHA: Semantic/keyword weight balance
        NEXUS_EMBEDDING_PROVIDER: "openai" | "voyage" | "fastembed"
        NEXUS_EMBEDDING_MODEL: Model name
        NEXUS_SEARCH_POOL_MIN: Min pool size
        NEXUS_SEARCH_POOL_MAX: Max pool size
        NEXUS_SEARCH_POOL_RECYCLE: Pool recycle seconds
        NEXUS_SEARCH_MODE: "hybrid" | "semantic" | "keyword"
        NEXUS_CONTEXTUAL_CHUNKING: Enable contextual chunking
        NEXUS_ATTRIBUTE_BOOSTING: Enable attribute ranking


    Returns:
        Frozen SearchConfig instance.
    """
    return SearchConfig(
        chunk_size=get_env_int("NEXUS_CHUNK_SIZE", 1024),
        chunk_strategy=os.environ.get("NEXUS_CHUNK_STRATEGY", "semantic"),
        entropy_filtering=get_env_bool("NEXUS_ENTROPY_FILTERING", False),
        entropy_threshold=get_env_float("NEXUS_ENTROPY_THRESHOLD", 0.35),
        entropy_alpha=get_env_float("NEXUS_ENTROPY_ALPHA", 0.5),
        fusion_method=os.environ.get("NEXUS_FUSION_METHOD", "rrf"),
        fusion_alpha=get_env_float("NEXUS_FUSION_ALPHA", 0.5),
        embedding_provider=os.environ.get("NEXUS_EMBEDDING_PROVIDER", "openai"),
        embedding_model=os.environ.get("NEXUS_EMBEDDING_MODEL") or None,
        pool_min_size=get_env_int("NEXUS_SEARCH_POOL_MIN", 10),
        pool_max_size=get_env_int("NEXUS_SEARCH_POOL_MAX", 50),
        pool_recycle=get_env_int("NEXUS_SEARCH_POOL_RECYCLE", 1800),
        search_mode=os.environ.get("NEXUS_SEARCH_MODE", "hybrid"),
        contextual_chunking=get_env_bool("NEXUS_CONTEXTUAL_CHUNKING", False),
        enable_attribute_boosting=get_env_bool("NEXUS_ATTRIBUTE_BOOSTING", True),
        # Indexing pipeline (Issue #1094)
        index_max_concurrency=get_env_int("NEXUS_INDEX_MAX_CONCURRENCY", 10),
        index_batch_size=get_env_int("NEXUS_INDEX_BATCH_SIZE", 100),
        index_cross_doc_batching=get_env_bool("NEXUS_INDEX_CROSS_DOC_BATCHING", True),
        index_max_embedding_concurrency=get_env_int("NEXUS_INDEX_MAX_EMBEDDING_CONCURRENCY", 5),
        # Thread pools (Issue #2188)
        vector_pool_workers=get_env_int("NEXUS_VDB_WORKERS", 2),
        ml_executor_workers=get_env_int("NEXUS_ML_WORKERS", 4),
    )
