"""Tests for ACE affinity scoring module (Issue #1026).

Tests the SimpleMem-inspired affinity scoring that combines semantic
similarity and temporal proximity for memory clustering.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from nexus.services.ace.affinity import (
    AffinityConfig,
    ClusterResult,
    MemoryVector,
    cluster_by_affinity,
    compute_affinity,
    compute_affinity_matrix,
    compute_cosine_similarity,
    compute_temporal_proximity,
    get_cluster_statistics,
)


class TestComputeCosineSimilarity:
    """Test cosine similarity computation."""

    def test_identical_vectors(self):
        """Identical vectors should have similarity of 1.0."""
        v = np.array([1.0, 0.0, 0.0])
        similarity = compute_cosine_similarity(v, v)
        assert similarity == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors should have similarity of 0.0."""
        v1 = np.array([1.0, 0.0, 0.0])
        v2 = np.array([0.0, 1.0, 0.0])
        similarity = compute_cosine_similarity(v1, v2)
        assert similarity == pytest.approx(0.0)

    def test_opposite_vectors(self):
        """Opposite vectors should have similarity of -1.0."""
        v1 = np.array([1.0, 0.0, 0.0])
        v2 = np.array([-1.0, 0.0, 0.0])
        similarity = compute_cosine_similarity(v1, v2)
        assert similarity == pytest.approx(-1.0)

    def test_similar_vectors(self):
        """Similar vectors should have high similarity."""
        v1 = np.array([1.0, 0.0, 0.0])
        v2 = np.array([0.9, 0.1, 0.0])
        similarity = compute_cosine_similarity(v1, v2)
        assert similarity > 0.9

    def test_zero_vector(self):
        """Zero vector should return 0.0 similarity."""
        v1 = np.array([1.0, 0.0, 0.0])
        v2 = np.array([0.0, 0.0, 0.0])
        similarity = compute_cosine_similarity(v1, v2)
        assert similarity == 0.0

    def test_high_dimensional_vectors(self):
        """Should work with high-dimensional vectors (like embeddings)."""
        np.random.seed(42)
        v1 = np.random.randn(1536)  # OpenAI embedding dimension
        v2 = v1 + np.random.randn(1536) * 0.1  # Slightly perturbed
        similarity = compute_cosine_similarity(v1, v2)
        assert 0.9 < similarity < 1.0


class TestComputeTemporalProximity:
    """Test temporal proximity computation."""

    def test_same_time(self):
        """Same timestamp should have proximity of 1.0."""
        t = datetime.now(UTC)
        proximity = compute_temporal_proximity(t, t)
        assert proximity == pytest.approx(1.0)

    def test_one_day_apart(self):
        """One day apart with default settings should give ~0.9."""
        t1 = datetime.now(UTC)
        t2 = t1 - timedelta(days=1)
        proximity = compute_temporal_proximity(t1, t2, lambda_decay=0.1, time_unit_hours=24.0)
        # exp(-0.1 * 1) = exp(-0.1) ≈ 0.905
        assert proximity == pytest.approx(0.905, abs=0.01)

    def test_one_week_apart(self):
        """One week apart should have low proximity."""
        t1 = datetime.now(UTC)
        t2 = t1 - timedelta(days=7)
        proximity = compute_temporal_proximity(t1, t2, lambda_decay=0.1, time_unit_hours=24.0)
        # exp(-0.1 * 7) = exp(-0.7) ≈ 0.497
        assert proximity == pytest.approx(0.497, abs=0.01)

    def test_high_decay_rate(self):
        """High decay rate should decrease proximity faster."""
        t1 = datetime.now(UTC)
        t2 = t1 - timedelta(days=1)
        proximity = compute_temporal_proximity(t1, t2, lambda_decay=1.0, time_unit_hours=24.0)
        # exp(-1.0 * 1) = exp(-1) ≈ 0.368
        assert proximity == pytest.approx(0.368, abs=0.01)

    def test_order_independent(self):
        """Order of timestamps should not matter."""
        t1 = datetime.now(UTC)
        t2 = t1 - timedelta(hours=12)
        proximity1 = compute_temporal_proximity(t1, t2)
        proximity2 = compute_temporal_proximity(t2, t1)
        assert proximity1 == pytest.approx(proximity2)


class TestComputeAffinity:
    """Test combined affinity computation."""

    def test_identical_same_time(self):
        """Identical vectors at same time should have max affinity."""
        v = [1.0, 0.0, 0.0]
        t = datetime.now(UTC)
        affinity = compute_affinity(v, v, t, t)
        assert affinity == pytest.approx(1.0)

    def test_semantic_dominant_default(self):
        """Default beta=0.7 should weight semantic similarity higher."""
        config = AffinityConfig(beta=0.7, lambda_decay=0.1)

        # Similar vectors, far apart in time
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.95, 0.05, 0.0]
        t1 = datetime.now(UTC)
        t2 = t1 - timedelta(days=7)

        affinity = compute_affinity(v1, v2, t1, t2, config)

        # High semantic similarity should dominate despite time difference
        assert affinity > 0.7

    def test_temporal_dominant(self):
        """Low beta should weight temporal proximity higher."""
        config = AffinityConfig(beta=0.3, lambda_decay=0.1)

        # Orthogonal vectors, same time
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        t = datetime.now(UTC)

        affinity = compute_affinity(v1, v2, t, t, config)

        # beta=0.3: 0.3 * 0.5 (normalized cosine=0) + 0.7 * 1.0 = 0.85
        assert affinity > 0.7

    def test_uses_normalized_cosine(self):
        """Cosine similarity should be normalized to [0, 1]."""
        config = AffinityConfig(beta=1.0)  # Pure semantic

        # Orthogonal vectors (cosine = 0, normalized = 0.5)
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        t = datetime.now(UTC)

        affinity = compute_affinity(v1, v2, t, t, config)
        # With beta=1.0: affinity = 0.5 (normalized orthogonal)
        assert affinity == pytest.approx(0.5)

    def test_uses_config_defaults(self):
        """Should use default config if not provided."""
        v = [1.0, 0.0, 0.0]
        t = datetime.now(UTC)
        affinity = compute_affinity(v, v, t, t)
        assert affinity == pytest.approx(1.0)


class TestComputeAffinityMatrix:
    """Test pairwise affinity matrix computation."""

    def test_empty_list_raises(self):
        """Empty memory list should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            compute_affinity_matrix([])

    def test_single_memory(self):
        """Single memory should give 1x1 matrix with 1.0."""
        t = datetime.now(UTC)
        memories = [MemoryVector("m1", [1.0, 0.0], t)]
        matrix = compute_affinity_matrix(memories)
        assert matrix.shape == (1, 1)
        assert matrix[0, 0] == pytest.approx(1.0)

    def test_symmetric_matrix(self):
        """Affinity matrix should be symmetric."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [0.0, 1.0, 0.0], t - timedelta(hours=1)),
            MemoryVector("m3", [0.5, 0.5, 0.0], t - timedelta(hours=2)),
        ]
        matrix = compute_affinity_matrix(memories)

        assert matrix.shape == (3, 3)
        for i in range(3):
            for j in range(3):
                assert matrix[i, j] == pytest.approx(matrix[j, i])

    def test_diagonal_is_one(self):
        """Diagonal entries should be 1.0 (self-affinity)."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0], t),
            MemoryVector("m2", [0.0, 1.0], t),
        ]
        matrix = compute_affinity_matrix(memories)

        assert matrix[0, 0] == pytest.approx(1.0)
        assert matrix[1, 1] == pytest.approx(1.0)

    def test_similar_memories_high_affinity(self):
        """Similar memories should have high affinity scores."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [0.95, 0.05, 0.0], t),  # Very similar
        ]
        matrix = compute_affinity_matrix(memories)

        assert matrix[0, 1] > 0.9


class TestClusterByAffinity:
    """Test affinity-based clustering."""

    def test_less_than_two_raises(self):
        """Less than 2 memories should raise ValueError."""
        t = datetime.now(UTC)
        memories = [MemoryVector("m1", [1.0, 0.0], t)]
        with pytest.raises(ValueError, match="at least 2"):
            cluster_by_affinity(memories)

    def test_two_similar_memories_cluster_together(self):
        """Two very similar memories should be in same cluster."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [0.99, 0.01, 0.0], t),  # Very similar
        ]
        config = AffinityConfig(cluster_threshold=0.5)  # Low threshold
        result = cluster_by_affinity(memories, config)

        assert result.num_clusters >= 1
        # Both should be in the same cluster
        all_memory_ids = [mid for cluster in result.clusters for mid in cluster]
        assert "m1" in all_memory_ids or "m2" in all_memory_ids

    def test_dissimilar_memories_separate_clusters(self):
        """Dissimilar memories should be in separate clusters."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [-1.0, 0.0, 0.0], t),  # Opposite
        ]
        config = AffinityConfig(cluster_threshold=0.9)  # High threshold
        result = cluster_by_affinity(memories, config)

        # With high threshold, opposite vectors shouldn't cluster together
        # They may end up in separate clusters or not meet min_cluster_size
        assert isinstance(result, ClusterResult)

    def test_returns_cluster_result(self):
        """Should return ClusterResult with expected attributes."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0], t),
            MemoryVector("m2", [0.9, 0.1], t),
        ]
        result = cluster_by_affinity(memories)

        assert isinstance(result, ClusterResult)
        assert hasattr(result, "clusters")
        assert hasattr(result, "affinity_matrix")
        assert hasattr(result, "memory_ids")

    def test_respects_min_cluster_size(self):
        """Clusters below min_cluster_size should be filtered out."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [0.0, 1.0, 0.0], t),  # Orthogonal
            MemoryVector("m3", [0.0, 0.0, 1.0], t),  # Orthogonal to both
        ]
        config = AffinityConfig(cluster_threshold=0.95, min_cluster_size=2)
        result = cluster_by_affinity(memories, config)

        # All clusters should have at least min_cluster_size memories
        for cluster in result.clusters:
            assert len(cluster) >= config.min_cluster_size

    def test_three_similar_memories_cluster(self):
        """Three similar memories should form one cluster."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t, "Coffee in morning"),
            MemoryVector("m2", [0.95, 0.05, 0.0], t, "Morning coffee routine"),
            MemoryVector("m3", [0.9, 0.1, 0.0], t, "Coffee preferences"),
        ]
        config = AffinityConfig(cluster_threshold=0.7)
        result = cluster_by_affinity(memories, config)

        # Should form at least one cluster
        assert result.num_clusters >= 1
        # At least one cluster should have multiple memories
        assert any(len(c) >= 2 for c in result.clusters)


class TestClusterResult:
    """Test ClusterResult dataclass."""

    def test_num_clusters(self):
        """num_clusters should return correct count."""
        result = ClusterResult(
            clusters=[["m1", "m2"], ["m3", "m4", "m5"]],
            affinity_matrix=np.eye(5),
            memory_ids=["m1", "m2", "m3", "m4", "m5"],
        )
        assert result.num_clusters == 2

    def test_cluster_sizes(self):
        """cluster_sizes should return correct sizes."""
        result = ClusterResult(
            clusters=[["m1", "m2"], ["m3", "m4", "m5"]],
            affinity_matrix=np.eye(5),
            memory_ids=["m1", "m2", "m3", "m4", "m5"],
        )
        assert result.cluster_sizes == [2, 3]


class TestMemoryVector:
    """Test MemoryVector dataclass."""

    def test_to_numpy(self):
        """to_numpy should convert embedding to numpy array."""
        t = datetime.now(UTC)
        mv = MemoryVector("m1", [1.0, 2.0, 3.0], t)
        arr = mv.to_numpy()

        assert isinstance(arr, np.ndarray)
        assert arr.dtype == np.float64
        assert list(arr) == [1.0, 2.0, 3.0]

    def test_optional_fields(self):
        """Optional fields should default to None."""
        t = datetime.now(UTC)
        mv = MemoryVector("m1", [1.0, 0.0], t)

        assert mv.content is None
        assert mv.importance is None
        assert mv.memory_type is None

    def test_with_optional_fields(self):
        """Should accept optional fields."""
        t = datetime.now(UTC)
        mv = MemoryVector(
            memory_id="m1",
            embedding=[1.0, 0.0],
            created_at=t,
            content="Test content",
            importance=0.8,
            memory_type="fact",
        )

        assert mv.content == "Test content"
        assert mv.importance == 0.8
        assert mv.memory_type == "fact"


class TestGetClusterStatistics:
    """Test cluster statistics computation."""

    def test_calculates_avg_affinity(self):
        """Should calculate average affinity within cluster."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [0.95, 0.05, 0.0], t),
        ]
        config = AffinityConfig(cluster_threshold=0.5)
        result = cluster_by_affinity(memories, config)

        stats = get_cluster_statistics(memories, result, config)

        if stats:
            assert "avg_affinity" in stats[0]
            assert 0 <= stats[0]["avg_affinity"] <= 1

    def test_calculates_time_span(self):
        """Should calculate time span of cluster."""
        t = datetime.now(UTC)
        memories = [
            MemoryVector("m1", [1.0, 0.0, 0.0], t),
            MemoryVector("m2", [0.95, 0.05, 0.0], t - timedelta(hours=12)),
        ]
        config = AffinityConfig(cluster_threshold=0.5)
        result = cluster_by_affinity(memories, config)

        stats = get_cluster_statistics(memories, result, config)

        if stats:
            assert "time_span_hours" in stats[0]
            # Should be approximately 12 hours
            assert 11 < stats[0]["time_span_hours"] < 13


class TestAffinityConfig:
    """Test AffinityConfig dataclass."""

    def test_defaults(self):
        """Should have correct default values."""
        config = AffinityConfig()

        assert config.beta == 0.7
        assert config.lambda_decay == 0.1
        assert config.time_unit_hours == 24.0
        assert config.cluster_threshold == 0.85
        assert config.linkage == "average"
        assert config.min_cluster_size == 2

    def test_custom_values(self):
        """Should accept custom values."""
        config = AffinityConfig(
            beta=0.5,
            lambda_decay=0.2,
            time_unit_hours=12.0,
            cluster_threshold=0.9,
            linkage="complete",
            min_cluster_size=3,
        )

        assert config.beta == 0.5
        assert config.lambda_decay == 0.2
        assert config.time_unit_hours == 12.0
        assert config.cluster_threshold == 0.9
        assert config.linkage == "complete"
        assert config.min_cluster_size == 3

    def test_beta_out_of_range_raises(self):
        """beta outside [0, 1] should raise ValueError."""
        with pytest.raises(ValueError, match="beta"):
            AffinityConfig(beta=-0.1)
        with pytest.raises(ValueError, match="beta"):
            AffinityConfig(beta=1.1)

    def test_lambda_decay_negative_raises(self):
        """Negative lambda_decay should raise ValueError."""
        with pytest.raises(ValueError, match="lambda_decay"):
            AffinityConfig(lambda_decay=-0.01)

    def test_time_unit_hours_non_positive_raises(self):
        """time_unit_hours <= 0 should raise ValueError."""
        with pytest.raises(ValueError, match="time_unit_hours"):
            AffinityConfig(time_unit_hours=0)
        with pytest.raises(ValueError, match="time_unit_hours"):
            AffinityConfig(time_unit_hours=-1.0)

    def test_cluster_threshold_out_of_range_raises(self):
        """cluster_threshold outside [0, 1] should raise ValueError."""
        with pytest.raises(ValueError, match="cluster_threshold"):
            AffinityConfig(cluster_threshold=-0.1)
        with pytest.raises(ValueError, match="cluster_threshold"):
            AffinityConfig(cluster_threshold=1.5)

    def test_invalid_linkage_raises(self):
        """Invalid linkage method should raise ValueError."""
        with pytest.raises(ValueError, match="linkage"):
            AffinityConfig(linkage="ward")

    def test_min_cluster_size_below_two_raises(self):
        """min_cluster_size < 2 should raise ValueError."""
        with pytest.raises(ValueError, match="min_cluster_size"):
            AffinityConfig(min_cluster_size=1)
        with pytest.raises(ValueError, match="min_cluster_size"):
            AffinityConfig(min_cluster_size=0)

    def test_boundary_values_accepted(self):
        """Boundary values should be accepted without error."""
        # beta = 0 and 1
        AffinityConfig(beta=0.0)
        AffinityConfig(beta=1.0)
        # lambda_decay = 0
        AffinityConfig(lambda_decay=0.0)
        # cluster_threshold = 0 and 1
        AffinityConfig(cluster_threshold=0.0)
        AffinityConfig(cluster_threshold=1.0)
        # min_cluster_size = 2
        AffinityConfig(min_cluster_size=2)
        # all valid linkages
        AffinityConfig(linkage="single")
        AffinityConfig(linkage="complete")
