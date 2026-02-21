"""Unit tests for EigenTrust and trust propagation pure math functions.

Issue #2129 §9A: Tests for eigentrust, build_local_trust_matrix,
detect_sybil_cluster, and _row_normalize.
"""

import numpy as np
import pytest
from scipy import sparse

from nexus.bricks.governance.models import EdgeType, GovernanceEdge
from nexus.bricks.governance.trust_math import (
    _row_normalize,
    build_local_trust_matrix,
    detect_sybil_cluster,
    eigentrust,
)

# ── eigentrust ──────────────────────────────────────────────────────────


class TestEigentrust:
    def test_empty_matrix(self) -> None:
        m = np.zeros((0, 0))
        result = eigentrust(m)
        assert len(result) == 0

    def test_single_node(self) -> None:
        m = np.array([[1.0]])
        result = eigentrust(m)
        assert len(result) == 1
        assert result[0] == pytest.approx(1.0)

    def test_two_nodes_convergence(self) -> None:
        m = np.array([[0.0, 1.0], [1.0, 0.0]])
        result = eigentrust(m)
        assert len(result) == 2
        assert sum(result) == pytest.approx(1.0)
        # Symmetric → equal trust
        assert result[0] == pytest.approx(result[1], abs=0.01)

    def test_alpha_zero_pure_local(self) -> None:
        # Mutual trust → even split with alpha=0
        m = np.array([[0.0, 1.0], [1.0, 0.0]])
        result = eigentrust(m, alpha=0.0)
        assert sum(result) == pytest.approx(1.0)

    def test_alpha_one_pure_seed(self) -> None:
        m = np.array([[0.0, 1.0], [1.0, 0.0]])
        seed = np.array([0.8, 0.2])
        result = eigentrust(m, seed_trust=seed, alpha=1.0)
        assert result[0] == pytest.approx(0.8, abs=0.01)
        assert result[1] == pytest.approx(0.2, abs=0.01)

    def test_sparse_matrix_input(self) -> None:
        m = sparse.lil_matrix((3, 3))
        m[0, 1] = 1.0
        m[1, 2] = 1.0
        m[2, 0] = 1.0
        result = eigentrust(m)
        assert len(result) == 3
        assert sum(result) == pytest.approx(1.0)


# ── build_local_trust_matrix ────────────────────────────────────────────


class TestBuildLocalTrustMatrix:
    def test_empty_edges(self) -> None:
        matrix = build_local_trust_matrix([], ["a", "b"])
        assert matrix.shape == (2, 2)
        assert matrix.nnz == 0  # sparse → zero non-zeros

    def test_single_edge(self) -> None:
        edges = [
            GovernanceEdge(
                edge_id="e1",
                from_node="a",
                to_node="b",
                zone_id="z1",
                edge_type=EdgeType.TRANSACTION,
                weight=2.5,
            )
        ]
        matrix = build_local_trust_matrix(edges, ["a", "b"])
        dense = matrix.toarray()
        assert dense[0, 1] == pytest.approx(2.5)
        assert dense[1, 0] == 0.0

    def test_negative_weights_clamped(self) -> None:
        edges = [
            GovernanceEdge(
                edge_id="e1",
                from_node="a",
                to_node="b",
                zone_id="z1",
                weight=-5.0,
            )
        ]
        matrix = build_local_trust_matrix(edges, ["a", "b"])
        dense = matrix.toarray()
        assert dense[0, 1] == 0.0  # max(-5, 0) = 0

    def test_max_nodes_cap(self) -> None:
        nodes = [f"n{i}" for i in range(100)]
        matrix = build_local_trust_matrix([], nodes, max_nodes=10)
        assert matrix.shape == (10, 10)


# ── detect_sybil_cluster ───────────────────────────────────────────────


class TestDetectSybilCluster:
    def test_no_suspicious_agents(self) -> None:
        scores = {"a": 0.5, "b": 0.8, "c": 0.3}
        assert detect_sybil_cluster(scores, threshold=0.1) == []

    def test_all_suspicious(self) -> None:
        scores = {"a": 0.01, "b": 0.02, "c": 0.03}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert len(clusters) == 1
        assert clusters[0] == {"a", "b", "c"}

    def test_threshold_boundary(self) -> None:
        scores = {"a": 0.1, "b": 0.09}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert len(clusters) == 1
        assert clusters[0] == {"b"}  # 0.09 < 0.1

    def test_empty_scores(self) -> None:
        assert detect_sybil_cluster({}) == []


# ── _row_normalize ──────────────────────────────────────────────────────


class TestRowNormalize:
    def test_zero_rows_stay_zero(self) -> None:
        m = np.array([[0.0, 0.0], [1.0, 3.0]])
        result = _row_normalize(m)
        assert result[0, 0] == 0.0
        assert result[0, 1] == 0.0
        assert result[1, 0] == pytest.approx(0.25)
        assert result[1, 1] == pytest.approx(0.75)

    def test_proper_normalization(self) -> None:
        m = np.array([[2.0, 2.0], [1.0, 3.0]])
        result = _row_normalize(m)
        for i in range(2):
            assert result[i].sum() == pytest.approx(1.0)

    def test_sparse_normalization(self) -> None:
        m = sparse.lil_matrix((2, 2))
        m[0, 1] = 4.0
        m[1, 0] = 2.0
        m[1, 1] = 2.0
        result = _row_normalize(m)
        dense = result.toarray()
        assert dense[0, 1] == pytest.approx(1.0)  # only non-zero in row
        assert dense[1].sum() == pytest.approx(1.0)
