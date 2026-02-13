"""Affinity scoring for memory consolidation (SimpleMem-inspired).

This module implements semantic+temporal affinity scoring for intelligent
memory clustering, based on the SimpleMem paper's approach:

    affinity = beta * cos(v_i, v_j) + (1 - beta) * exp(-lambda * |t_i - t_j|)

Where:
- cos(v_i, v_j): Cosine similarity of memory embeddings (semantic)
- exp(-lambda * |t_i - t_j|): Temporal proximity with exponential decay
- beta: Balance factor (default 0.7, semantic-dominant)

Reference: SimpleMem: Efficient Lifelong Memory for LLM Agents
https://arxiv.org/html/2601.02553
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

# sklearn is imported lazily inside cluster_by_affinity() to avoid
# making it a hard dependency for other ACE components that don't need clustering.

if TYPE_CHECKING:
    pass


@dataclass
class AffinityConfig:
    """Configuration for affinity-based memory consolidation.

    Attributes:
        beta: Semantic similarity weight (0-1). Higher values prioritize
            semantic similarity over temporal proximity. Default 0.7 from
            SimpleMem paper.
        lambda_decay: Temporal decay rate. Controls how quickly temporal
            affinity decreases with time difference. Default 0.1.
        time_unit_hours: Time normalization factor in hours. Time differences
            are divided by this value before applying decay. Default 24.0 (1 day).
        cluster_threshold: Minimum affinity for clustering. Memory pairs with
            affinity below this threshold are placed in separate clusters.
            Default 0.85 from SimpleMem paper.
        linkage: Clustering linkage method. Options: 'average', 'complete',
            'single'. Default 'average' (works well with custom affinity).
        min_cluster_size: Minimum memories required to form a cluster worth
            consolidating. Default 2.
    """

    beta: float = 0.7
    lambda_decay: float = 0.1
    time_unit_hours: float = 24.0
    cluster_threshold: float = 0.85
    linkage: str = "average"
    min_cluster_size: int = 2

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        if not 0 <= self.beta <= 1:
            raise ValueError(f"beta must be in [0, 1], got {self.beta}")
        if self.lambda_decay < 0:
            raise ValueError(f"lambda_decay must be >= 0, got {self.lambda_decay}")
        if self.time_unit_hours <= 0:
            raise ValueError(f"time_unit_hours must be > 0, got {self.time_unit_hours}")
        if not 0 <= self.cluster_threshold <= 1:
            raise ValueError(f"cluster_threshold must be in [0, 1], got {self.cluster_threshold}")
        valid_linkages = {"average", "complete", "single"}
        if self.linkage not in valid_linkages:
            raise ValueError(f"linkage must be one of {valid_linkages}, got {self.linkage!r}")
        if self.min_cluster_size < 2:
            raise ValueError(f"min_cluster_size must be >= 2, got {self.min_cluster_size}")


@dataclass
class MemoryVector:
    """Memory with embedding and timestamp for clustering.

    Attributes:
        memory_id: Unique identifier for the memory.
        embedding: Vector embedding of memory content.
        created_at: Timestamp when memory was created.
        content: Optional text content of the memory.
        importance: Optional importance score (0-1).
        memory_type: Optional type classification.
    """

    memory_id: str
    embedding: list[float]
    created_at: datetime
    content: str | None = None
    importance: float | None = None
    memory_type: str | None = None

    def to_numpy(self) -> NDArray[np.floating]:
        """Convert embedding to numpy array."""
        return np.array(self.embedding, dtype=np.float64)


@dataclass
class ClusterResult:
    """Result of affinity-based clustering.

    Attributes:
        clusters: List of clusters, each containing memory IDs.
        affinity_matrix: Pairwise affinity scores between memories.
        memory_ids: Ordered list of memory IDs corresponding to matrix indices.
    """

    clusters: list[list[str]]
    affinity_matrix: NDArray[np.floating]
    memory_ids: list[str] = field(default_factory=list)

    @property
    def num_clusters(self) -> int:
        """Number of clusters formed."""
        return len(self.clusters)

    @property
    def cluster_sizes(self) -> list[int]:
        """Size of each cluster."""
        return [len(c) for c in self.clusters]


def compute_cosine_similarity(
    v_i: NDArray[np.floating],
    v_j: NDArray[np.floating],
) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        v_i: First vector.
        v_j: Second vector.

    Returns:
        Cosine similarity in range [-1, 1], or 0 if either vector is zero.
    """
    norm_i = np.linalg.norm(v_i)
    norm_j = np.linalg.norm(v_j)

    if norm_i == 0 or norm_j == 0:
        return 0.0

    return float(np.dot(v_i, v_j) / (norm_i * norm_j))


def compute_temporal_proximity(
    t_i: datetime,
    t_j: datetime,
    lambda_decay: float = 0.1,
    time_unit_hours: float = 24.0,
) -> float:
    """Compute temporal proximity with exponential decay.

    Args:
        t_i: First timestamp.
        t_j: Second timestamp.
        lambda_decay: Decay rate (higher = faster decay).
        time_unit_hours: Time normalization factor in hours.

    Returns:
        Temporal proximity in range (0, 1], where 1 = same time.
    """
    # Calculate time difference in hours
    time_diff_seconds = abs((t_i - t_j).total_seconds())
    time_diff_hours = time_diff_seconds / 3600.0

    # Normalize by time unit (e.g., 24h = 1 unit)
    time_diff_normalized = time_diff_hours / time_unit_hours

    # Exponential decay: e^(-lambda * t)
    return float(np.exp(-lambda_decay * time_diff_normalized))


def compute_affinity(
    v_i: list[float] | NDArray[np.floating],
    v_j: list[float] | NDArray[np.floating],
    t_i: datetime,
    t_j: datetime,
    config: AffinityConfig | None = None,
) -> float:
    """Compute affinity between two memories.

    Affinity combines semantic similarity and temporal proximity:
        affinity = beta * cos(v_i, v_j) + (1 - beta) * exp(-lambda * |t_i - t_j|)

    Args:
        v_i: Embedding vector of first memory.
        v_j: Embedding vector of second memory.
        t_i: Timestamp of first memory.
        t_j: Timestamp of second memory.
        config: Optional configuration. Uses defaults if not provided.

    Returns:
        Affinity score in range [0, 1] (assuming normalized embeddings).

    Example:
        >>> from datetime import datetime, timedelta
        >>> config = AffinityConfig(beta=0.7, lambda_decay=0.1)
        >>> v1 = [1.0, 0.0, 0.0]
        >>> v2 = [0.9, 0.1, 0.0]
        >>> t1 = datetime.now()
        >>> t2 = t1 - timedelta(hours=12)
        >>> affinity = compute_affinity(v1, v2, t1, t2, config)
        >>> print(f"Affinity: {affinity:.3f}")
    """
    if config is None:
        config = AffinityConfig()

    # Convert to numpy arrays if needed
    v_i_np = np.array(v_i, dtype=np.float64) if isinstance(v_i, list) else v_i
    v_j_np = np.array(v_j, dtype=np.float64) if isinstance(v_j, list) else v_j

    # Compute semantic similarity (cosine)
    semantic_sim = compute_cosine_similarity(v_i_np, v_j_np)

    # Normalize cosine similarity to [0, 1] range
    # Cosine is in [-1, 1], we map to [0, 1]
    semantic_sim_normalized = (semantic_sim + 1.0) / 2.0

    # Compute temporal proximity
    temporal_prox = compute_temporal_proximity(
        t_i, t_j, config.lambda_decay, config.time_unit_hours
    )

    # Combined affinity
    affinity = config.beta * semantic_sim_normalized + (1 - config.beta) * temporal_prox

    return affinity


def compute_affinity_matrix(
    memories: list[MemoryVector],
    config: AffinityConfig | None = None,
) -> NDArray[np.floating]:
    """Compute pairwise affinity matrix for memories.

    Uses vectorized operations for both semantic similarity (sklearn
    cosine_similarity matrix) and temporal proximity (numpy broadcasting)
    for 10-50x speedup over pairwise loops when n > 50.

    Args:
        memories: List of MemoryVector objects with embeddings and timestamps.
        config: Optional configuration. Uses defaults if not provided.

    Returns:
        Symmetric NxN affinity matrix where entry (i,j) is affinity between
        memories[i] and memories[j]. Diagonal entries are 1.0.

    Raises:
        ValueError: If memories list is empty.
    """
    if not memories:
        raise ValueError("Cannot compute affinity matrix for empty memory list")

    if config is None:
        config = AffinityConfig()

    n = len(memories)

    # Stack embeddings into (n, d) matrix
    embedding_matrix = np.array([m.to_numpy() for m in memories], dtype=np.float64)

    # --- Vectorized semantic similarity ---
    # Compute norms for zero-vector handling
    norms = np.linalg.norm(embedding_matrix, axis=1)
    has_zero = np.any(norms == 0)

    if has_zero:
        # Fall back to safe per-pair computation for rows with zero vectors
        semantic_matrix = np.zeros((n, n), dtype=np.float64)
        for i in range(n):
            for j in range(i, n):
                if norms[i] == 0 or norms[j] == 0:
                    semantic_matrix[i, j] = 0.0
                else:
                    semantic_matrix[i, j] = float(
                        np.dot(embedding_matrix[i], embedding_matrix[j]) / (norms[i] * norms[j])
                    )
                semantic_matrix[j, i] = semantic_matrix[i, j]
    else:
        # Fast path: sklearn vectorized cosine similarity
        try:
            from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine

            semantic_matrix = sklearn_cosine(embedding_matrix)
        except ImportError:
            # Fallback: manual vectorized computation
            normalized = embedding_matrix / norms[:, np.newaxis]
            semantic_matrix = normalized @ normalized.T

    # Normalize cosine similarity from [-1, 1] to [0, 1]
    semantic_normalized = (semantic_matrix + 1.0) / 2.0

    # --- Vectorized temporal proximity ---
    # Convert timestamps to hours since epoch for vectorized computation
    epoch = memories[0].created_at
    hours = np.array(
        [(m.created_at - epoch).total_seconds() / 3600.0 for m in memories],
        dtype=np.float64,
    )

    # Pairwise absolute time differences via broadcasting: |t_i - t_j|
    time_diff_matrix = np.abs(hours[:, np.newaxis] - hours[np.newaxis, :])

    # Normalize by time_unit_hours and apply exponential decay
    temporal_matrix = np.exp(-config.lambda_decay * time_diff_matrix / config.time_unit_hours)

    # --- Combined affinity ---
    affinity_matrix = config.beta * semantic_normalized + (1 - config.beta) * temporal_matrix

    # Force diagonal to 1.0 (perfect self-affinity)
    np.fill_diagonal(affinity_matrix, 1.0)

    return affinity_matrix


def cluster_by_affinity(
    memories: list[MemoryVector],
    config: AffinityConfig | None = None,
) -> ClusterResult:
    """Cluster memories by combined semantic and temporal affinity.

    Uses agglomerative (hierarchical) clustering with a precomputed affinity
    matrix. Memories with high affinity (semantic similarity AND temporal
    proximity) are grouped together.

    Args:
        memories: List of MemoryVector objects with embeddings and timestamps.
        config: Optional configuration. Uses defaults if not provided.

    Returns:
        ClusterResult with clusters, affinity matrix, and memory IDs.

    Raises:
        ValueError: If memories list has fewer than 2 items.

    Example:
        >>> memories = [
        ...     MemoryVector("m1", [1, 0, 0], datetime.now(), "Coffee"),
        ...     MemoryVector("m2", [0.9, 0.1, 0], datetime.now(), "Tea"),
        ...     MemoryVector("m3", [0, 1, 0], datetime.now(), "Weather"),
        ... ]
        >>> result = cluster_by_affinity(memories)
        >>> print(f"Formed {result.num_clusters} clusters")
    """
    if len(memories) < 2:
        raise ValueError("Need at least 2 memories for clustering")

    if config is None:
        config = AffinityConfig()

    # Compute affinity matrix
    affinity_matrix = compute_affinity_matrix(memories, config)

    # Convert affinity to distance (AgglomerativeClustering uses distance)
    # Distance = 1 - Affinity (higher affinity = lower distance)
    distance_matrix = 1.0 - affinity_matrix

    # Lazy import sklearn to avoid making it a hard dependency
    try:
        from sklearn.cluster import AgglomerativeClustering
    except ImportError as e:
        raise ImportError(
            "sklearn is required for affinity-based clustering. "
            "Install with: pip install scikit-learn"
        ) from e

    # Agglomerative clustering with precomputed distance
    clustering = AgglomerativeClustering(
        n_clusters=None,
        metric="precomputed",
        linkage=config.linkage,
        distance_threshold=1.0 - config.cluster_threshold,  # Convert to distance
    )

    labels = clustering.fit_predict(distance_matrix)

    # Group memories by cluster label
    clusters_dict: dict[int, list[str]] = {}
    for idx, label in enumerate(labels):
        if label not in clusters_dict:
            clusters_dict[label] = []
        clusters_dict[label].append(memories[idx].memory_id)

    # Filter out clusters below minimum size
    clusters = [
        cluster for cluster in clusters_dict.values() if len(cluster) >= config.min_cluster_size
    ]

    # Also include memory IDs for reference
    memory_ids = [m.memory_id for m in memories]

    return ClusterResult(
        clusters=clusters,
        affinity_matrix=affinity_matrix,
        memory_ids=memory_ids,
    )


def get_cluster_statistics(
    memories: list[MemoryVector],
    cluster_result: ClusterResult,
    config: AffinityConfig | None = None,
) -> list[dict]:
    """Get statistics for each cluster.

    Args:
        memories: Original list of MemoryVector objects.
        cluster_result: Result from cluster_by_affinity().
        config: Optional configuration.

    Returns:
        List of dicts with cluster statistics including:
        - memory_ids: IDs of memories in cluster
        - size: Number of memories
        - avg_affinity: Average pairwise affinity within cluster
        - min_affinity: Minimum pairwise affinity
        - time_span_hours: Time span from earliest to latest memory
    """
    if config is None:
        config = AffinityConfig()

    # Create lookup for memory by ID
    memory_lookup = {m.memory_id: m for m in memories}
    id_to_idx = {mid: idx for idx, mid in enumerate(cluster_result.memory_ids)}

    stats = []
    for cluster_ids in cluster_result.clusters:
        cluster_memories = [memory_lookup[mid] for mid in cluster_ids if mid in memory_lookup]

        if len(cluster_memories) < 2:
            continue

        # Calculate pairwise affinities within cluster
        cluster_indices = [id_to_idx[mid] for mid in cluster_ids if mid in id_to_idx]
        affinities = []
        for i, idx_i in enumerate(cluster_indices):
            for idx_j in cluster_indices[i + 1 :]:
                affinities.append(cluster_result.affinity_matrix[idx_i, idx_j])

        # Calculate time span
        timestamps = [m.created_at for m in cluster_memories]
        time_span = (max(timestamps) - min(timestamps)).total_seconds() / 3600.0

        stats.append(
            {
                "memory_ids": cluster_ids,
                "size": len(cluster_ids),
                "avg_affinity": float(np.mean(affinities)) if affinities else 1.0,
                "min_affinity": float(np.min(affinities)) if affinities else 1.0,
                "time_span_hours": time_span,
            }
        )

    return stats
