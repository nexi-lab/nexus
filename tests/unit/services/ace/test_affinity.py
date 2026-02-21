"""Unit tests for affinity scoring pure math functions.

Tests compute_cosine_similarity, compute_temporal_proximity,
compute_affinity, compute_affinity_matrix, AffinityConfig validation,
and cluster_by_affinity.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest


def _sklearn_available() -> bool:
    """Check if sklearn is installed."""
    try:
        import sklearn  # noqa: F401

        return True
    except ImportError:
        return False


from nexus.services.ace.affinity import (  # noqa: E402
    AffinityConfig,
    ClusterResult,
    MemoryVector,
    compute_affinity,
    compute_affinity_matrix,
    compute_cosine_similarity,
    compute_temporal_proximity,
    get_cluster_statistics,
)

# ---------------------------------------------------------------------------
# AffinityConfig validation
# ---------------------------------------------------------------------------


class TestAffinityConfig:
    """Tests for AffinityConfig parameter validation."""

    def test_valid_defaults(self) -> None:
        config = AffinityConfig()
        assert config.beta == 0.7
        assert config.lambda_decay == 0.1
        assert config.time_unit_hours == 24.0
        assert config.cluster_threshold == 0.85
        assert config.linkage == "average"
        assert config.min_cluster_size == 2

    def test_beta_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="beta"):
            AffinityConfig(beta=1.5)

    def test_beta_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="beta"):
            AffinityConfig(beta=-0.1)

    def test_lambda_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="lambda_decay"):
            AffinityConfig(lambda_decay=-1.0)

    def test_time_unit_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="time_unit_hours"):
            AffinityConfig(time_unit_hours=0.0)

    def test_cluster_threshold_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="cluster_threshold"):
            AffinityConfig(cluster_threshold=2.0)

    def test_invalid_linkage_raises(self) -> None:
        with pytest.raises(ValueError, match="linkage"):
            AffinityConfig(linkage="ward")

    def test_min_cluster_size_below_two_raises(self) -> None:
        with pytest.raises(ValueError, match="min_cluster_size"):
            AffinityConfig(min_cluster_size=1)

    def test_all_valid_linkages(self) -> None:
        for linkage in ("average", "complete", "single"):
            config = AffinityConfig(linkage=linkage)
            assert config.linkage == linkage


# ---------------------------------------------------------------------------
# compute_cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    """Tests for compute_cosine_similarity."""

    def test_identical_vectors(self) -> None:
        v = np.array([1.0, 2.0, 3.0])
        assert compute_cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        v1 = np.array([1.0, 0.0])
        v2 = np.array([0.0, 1.0])
        assert compute_cosine_similarity(v1, v2) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        v1 = np.array([1.0, 0.0])
        v2 = np.array([-1.0, 0.0])
        assert compute_cosine_similarity(v1, v2) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self) -> None:
        v1 = np.array([0.0, 0.0, 0.0])
        v2 = np.array([1.0, 2.0, 3.0])
        assert compute_cosine_similarity(v1, v2) == 0.0

    def test_both_zero_vectors(self) -> None:
        v = np.array([0.0, 0.0])
        assert compute_cosine_similarity(v, v) == 0.0

    def test_similar_vectors(self) -> None:
        v1 = np.array([1.0, 0.0, 0.0])
        v2 = np.array([0.9, 0.1, 0.0])
        sim = compute_cosine_similarity(v1, v2)
        assert 0.9 < sim <= 1.0


# ---------------------------------------------------------------------------
# compute_temporal_proximity
# ---------------------------------------------------------------------------


class TestTemporalProximity:
    """Tests for compute_temporal_proximity."""

    def test_same_time_returns_one(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        assert compute_temporal_proximity(t, t) == pytest.approx(1.0)

    def test_large_time_diff_near_zero(self) -> None:
        t1 = datetime(2025, 1, 1, tzinfo=UTC)
        t2 = t1 + timedelta(days=365)
        result = compute_temporal_proximity(t1, t2)
        assert result < 0.01  # Very small for large time diff

    def test_symmetry(self) -> None:
        t1 = datetime(2025, 1, 1, tzinfo=UTC)
        t2 = t1 + timedelta(hours=12)
        assert compute_temporal_proximity(t1, t2) == pytest.approx(
            compute_temporal_proximity(t2, t1)
        )

    def test_higher_decay_faster_falloff(self) -> None:
        t1 = datetime(2025, 1, 1, tzinfo=UTC)
        t2 = t1 + timedelta(hours=24)
        prox_slow = compute_temporal_proximity(t1, t2, lambda_decay=0.1)
        prox_fast = compute_temporal_proximity(t1, t2, lambda_decay=1.0)
        assert prox_fast < prox_slow

    def test_result_in_zero_one_range(self) -> None:
        t1 = datetime(2025, 1, 1, tzinfo=UTC)
        t2 = t1 + timedelta(hours=6)
        result = compute_temporal_proximity(t1, t2)
        assert 0.0 < result <= 1.0


# ---------------------------------------------------------------------------
# compute_affinity
# ---------------------------------------------------------------------------


class TestComputeAffinity:
    """Tests for the combined affinity function."""

    def test_identical_memories_high_affinity(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        v = [1.0, 0.0, 0.0]
        affinity = compute_affinity(v, v, t, t)
        assert affinity == pytest.approx(1.0)

    def test_dissimilar_memories_lower_affinity(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        affinity = compute_affinity(v1, v2, t, t)
        # Cosine sim = 0 -> normalized = 0.5, temporal = 1.0
        # affinity = 0.7 * 0.5 + 0.3 * 1.0 = 0.65
        assert affinity == pytest.approx(0.65)

    def test_custom_config(self) -> None:
        t1 = datetime(2025, 1, 1, tzinfo=UTC)
        t2 = t1 + timedelta(hours=12)
        v1 = [1.0, 0.0]
        v2 = [1.0, 0.0]
        config = AffinityConfig(beta=0.5, lambda_decay=0.1)
        affinity = compute_affinity(v1, v2, t1, t2, config)
        # Cosine = 1.0 -> normalized = 1.0
        # beta=0.5 * 1.0 + 0.5 * temporal
        assert 0.5 < affinity <= 1.0

    def test_accepts_numpy_arrays(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        v1 = np.array([1.0, 0.0])
        v2 = np.array([0.0, 1.0])
        affinity = compute_affinity(v1, v2, t, t)
        assert isinstance(affinity, float)

    def test_default_config_used(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        v = [1.0, 0.0]
        affinity = compute_affinity(v, v, t, t, config=None)
        assert affinity == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_affinity_matrix
# ---------------------------------------------------------------------------


class TestComputeAffinityMatrix:
    """Tests for compute_affinity_matrix."""

    def _make_memory(self, mid: str, embedding: list[float], t: datetime) -> MemoryVector:
        return MemoryVector(memory_id=mid, embedding=embedding, created_at=t)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compute_affinity_matrix([])

    def test_single_memory(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [self._make_memory("m1", [1.0, 0.0], t)]
        matrix = compute_affinity_matrix(memories)
        assert matrix.shape == (1, 1)
        assert matrix[0, 0] == pytest.approx(1.0)

    def test_two_identical_memories(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [1.0, 0.0], t),
            self._make_memory("m2", [1.0, 0.0], t),
        ]
        matrix = compute_affinity_matrix(memories)
        assert matrix.shape == (2, 2)
        assert matrix[0, 1] == pytest.approx(1.0)
        assert matrix[1, 0] == pytest.approx(1.0)

    def test_symmetry(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [1.0, 0.0, 0.0], t),
            self._make_memory("m2", [0.0, 1.0, 0.0], t + timedelta(hours=6)),
            self._make_memory("m3", [0.0, 0.0, 1.0], t + timedelta(hours=12)),
        ]
        matrix = compute_affinity_matrix(memories)
        np.testing.assert_array_almost_equal(matrix, matrix.T)

    def test_diagonal_is_one(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [1.0, 0.0], t),
            self._make_memory("m2", [0.0, 1.0], t + timedelta(hours=24)),
        ]
        matrix = compute_affinity_matrix(memories)
        for i in range(matrix.shape[0]):
            assert matrix[i, i] == pytest.approx(1.0)

    def test_zero_embedding_handled(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [0.0, 0.0], t),
            self._make_memory("m2", [1.0, 0.0], t),
        ]
        # Should not raise, uses fallback for zero vectors
        matrix = compute_affinity_matrix(memories)
        assert matrix.shape == (2, 2)


# ---------------------------------------------------------------------------
# MemoryVector
# ---------------------------------------------------------------------------


class TestMemoryVector:
    """Tests for MemoryVector dataclass."""

    def test_to_numpy(self) -> None:
        mv = MemoryVector("m1", [1.0, 2.0, 3.0], datetime(2025, 1, 1, tzinfo=UTC))
        arr = mv.to_numpy()
        assert isinstance(arr, np.ndarray)
        np.testing.assert_array_equal(arr, [1.0, 2.0, 3.0])

    def test_optional_fields(self) -> None:
        mv = MemoryVector("m1", [1.0], datetime(2025, 1, 1, tzinfo=UTC))
        assert mv.content is None
        assert mv.importance is None
        assert mv.memory_type is None


# ---------------------------------------------------------------------------
# ClusterResult
# ---------------------------------------------------------------------------


class TestClusterResult:
    """Tests for ClusterResult dataclass."""

    def test_num_clusters(self) -> None:
        cr = ClusterResult(
            clusters=[["m1", "m2"], ["m3"]],
            affinity_matrix=np.eye(3),
        )
        assert cr.num_clusters == 2

    def test_cluster_sizes(self) -> None:
        cr = ClusterResult(
            clusters=[["m1", "m2"], ["m3"]],
            affinity_matrix=np.eye(3),
        )
        assert cr.cluster_sizes == [2, 1]

    def test_empty_clusters(self) -> None:
        cr = ClusterResult(clusters=[], affinity_matrix=np.array([]))
        assert cr.num_clusters == 0
        assert cr.cluster_sizes == []


# ---------------------------------------------------------------------------
# cluster_by_affinity
# ---------------------------------------------------------------------------


class TestClusterByAffinity:
    """Tests for cluster_by_affinity (requires sklearn)."""

    def _make_memory(self, mid: str, embedding: list[float], t: datetime) -> MemoryVector:
        return MemoryVector(memory_id=mid, embedding=embedding, created_at=t)

    def test_fewer_than_two_raises(self) -> None:
        from nexus.services.ace.affinity import cluster_by_affinity

        t = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="at least 2"):
            cluster_by_affinity([self._make_memory("m1", [1.0], t)])

    @pytest.mark.skipif(
        not _sklearn_available(),
        reason="sklearn not installed",
    )
    def test_two_similar_memories_cluster_together(self) -> None:
        from nexus.services.ace.affinity import cluster_by_affinity

        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [1.0, 0.0, 0.0], t),
            self._make_memory("m2", [0.99, 0.01, 0.0], t),
        ]
        config = AffinityConfig(cluster_threshold=0.5)
        result = cluster_by_affinity(memories, config)
        assert result.num_clusters >= 1
        assert len(result.memory_ids) == 2

    @pytest.mark.skipif(
        not _sklearn_available(),
        reason="sklearn not installed",
    )
    def test_dissimilar_memories_may_form_separate_clusters(self) -> None:
        from nexus.services.ace.affinity import cluster_by_affinity

        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [1.0, 0.0, 0.0], t),
            self._make_memory("m2", [0.0, 1.0, 0.0], t + timedelta(days=30)),
            self._make_memory("m3", [0.0, 0.0, 1.0], t + timedelta(days=60)),
        ]
        config = AffinityConfig(cluster_threshold=0.99, min_cluster_size=2)
        result = cluster_by_affinity(memories, config)
        # With high threshold and distant timestamps, may get fewer clusters
        assert isinstance(result, ClusterResult)


# ---------------------------------------------------------------------------
# get_cluster_statistics
# ---------------------------------------------------------------------------


class TestGetClusterStatistics:
    """Tests for get_cluster_statistics."""

    def _make_memory(self, mid: str, embedding: list[float], t: datetime) -> MemoryVector:
        return MemoryVector(memory_id=mid, embedding=embedding, created_at=t)

    def test_basic_statistics(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [
            self._make_memory("m1", [1.0, 0.0], t),
            self._make_memory("m2", [0.9, 0.1], t + timedelta(hours=2)),
        ]
        affinity_matrix = compute_affinity_matrix(memories)
        cluster_result = ClusterResult(
            clusters=[["m1", "m2"]],
            affinity_matrix=affinity_matrix,
            memory_ids=["m1", "m2"],
        )

        stats = get_cluster_statistics(memories, cluster_result)
        assert len(stats) == 1
        assert stats[0]["size"] == 2
        assert "avg_affinity" in stats[0]
        assert "min_affinity" in stats[0]
        assert stats[0]["time_span_hours"] == pytest.approx(2.0)

    def test_single_memory_cluster_skipped(self) -> None:
        t = datetime(2025, 1, 1, tzinfo=UTC)
        memories = [self._make_memory("m1", [1.0], t)]
        cluster_result = ClusterResult(
            clusters=[["m1"]],
            affinity_matrix=np.eye(1),
            memory_ids=["m1"],
        )
        stats = get_cluster_statistics(memories, cluster_result)
        # Single-memory clusters are skipped (< 2 memories)
        assert len(stats) == 0
