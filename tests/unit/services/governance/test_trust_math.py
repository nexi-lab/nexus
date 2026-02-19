"""Unit tests for EigenTrust and trust propagation pure math functions.

Tests eigentrust, build_local_trust_matrix, detect_sybil_cluster,
and the internal _row_normalize helper.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus.services.governance.models import GovernanceEdge
from nexus.services.governance.trust_math import (
    build_local_trust_matrix,
    detect_sybil_cluster,
    eigentrust,
)

# ---------------------------------------------------------------------------
# eigentrust
# ---------------------------------------------------------------------------


class TestEigentrust:
    """Tests for the EigenTrust power iteration."""

    def test_empty_matrix(self) -> None:
        result = eigentrust(np.array([]).reshape(0, 0))
        assert len(result) == 0

    def test_single_node(self) -> None:
        local_trust = np.array([[1.0]])
        result = eigentrust(local_trust)
        assert len(result) == 1
        assert result[0] == pytest.approx(1.0)

    def test_uniform_trust(self) -> None:
        # 3x3 matrix where everyone trusts everyone equally
        local_trust = np.ones((3, 3))
        result = eigentrust(local_trust)
        assert len(result) == 3
        assert result.sum() == pytest.approx(1.0)
        # Symmetric trust should yield uniform global trust
        for val in result:
            assert val == pytest.approx(1.0 / 3.0, abs=1e-5)

    def test_trust_sums_to_one(self) -> None:
        local_trust = np.array(
            [
                [0, 2, 1],
                [1, 0, 3],
                [4, 1, 0],
            ],
            dtype=np.float64,
        )
        result = eigentrust(local_trust)
        assert result.sum() == pytest.approx(1.0)

    def test_seed_trust_bias(self) -> None:
        # Node 0 is pre-trusted (high seed)
        local_trust = np.ones((3, 3))
        seed = np.array([10.0, 1.0, 1.0])
        result = eigentrust(local_trust, seed_trust=seed, alpha=0.9)
        # With high alpha and strong seed for node 0, node 0 should dominate
        assert result[0] > result[1]
        assert result[0] > result[2]

    def test_zero_seed_falls_back_to_uniform(self) -> None:
        local_trust = np.ones((3, 3))
        seed = np.array([0.0, 0.0, 0.0])
        result = eigentrust(local_trust, seed_trust=seed)
        assert result.sum() == pytest.approx(1.0)
        # Falls back to uniform seed, and with uniform local trust -> uniform result
        for val in result:
            assert val == pytest.approx(1.0 / 3.0, abs=1e-4)

    def test_alpha_zero_pure_local(self) -> None:
        # alpha=0 means pure local trust, no seed
        local_trust = np.array(
            [
                [0, 5, 0],
                [0, 0, 5],
                [5, 0, 0],
            ],
            dtype=np.float64,
        )
        result = eigentrust(local_trust, alpha=0.0)
        assert result.sum() == pytest.approx(1.0)

    def test_alpha_one_pure_seed(self) -> None:
        # alpha=1 means pure seed trust
        local_trust = np.array(
            [
                [0, 1, 0],
                [0, 0, 1],
                [1, 0, 0],
            ],
            dtype=np.float64,
        )
        seed = np.array([1.0, 0.0, 0.0])
        result = eigentrust(local_trust, seed_trust=seed, alpha=1.0)
        assert result[0] == pytest.approx(1.0, abs=1e-5)

    def test_convergence_within_max_iter(self) -> None:
        local_trust = np.array(
            [
                [0, 3, 1],
                [2, 0, 2],
                [1, 1, 0],
            ],
            dtype=np.float64,
        )
        # With max_iter=1, may not converge but should still return valid vector
        result = eigentrust(local_trust, max_iter=1)
        assert result.sum() == pytest.approx(1.0)

    def test_all_zeros_matrix(self) -> None:
        local_trust = np.zeros((3, 3))
        result = eigentrust(local_trust)
        assert result.sum() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# build_local_trust_matrix
# ---------------------------------------------------------------------------


class TestBuildLocalTrustMatrix:
    """Tests for build_local_trust_matrix."""

    def test_empty_edges(self) -> None:
        matrix = build_local_trust_matrix([], ["a", "b", "c"])
        assert matrix.shape == (3, 3)
        assert matrix.sum() == 0.0

    def test_empty_node_ids(self) -> None:
        matrix = build_local_trust_matrix([], [])
        assert matrix.shape == (0, 0)

    def test_single_edge(self) -> None:
        edge = GovernanceEdge(
            edge_id="e1",
            from_node="a",
            to_node="b",
            zone_id="z1",
            weight=5.0,
        )
        matrix = build_local_trust_matrix([edge], ["a", "b"])
        assert matrix[0, 1] == pytest.approx(5.0)
        assert matrix[1, 0] == pytest.approx(0.0)

    def test_multiple_edges_accumulate(self) -> None:
        edges = [
            GovernanceEdge(edge_id="e1", from_node="a", to_node="b", zone_id="z1", weight=3.0),
            GovernanceEdge(edge_id="e2", from_node="a", to_node="b", zone_id="z1", weight=2.0),
        ]
        matrix = build_local_trust_matrix(edges, ["a", "b"])
        assert matrix[0, 1] == pytest.approx(5.0)

    def test_negative_weight_clamped_to_zero(self) -> None:
        edge = GovernanceEdge(
            edge_id="e1",
            from_node="a",
            to_node="b",
            zone_id="z1",
            weight=-3.0,
        )
        matrix = build_local_trust_matrix([edge], ["a", "b"])
        assert matrix[0, 1] == pytest.approx(0.0)

    def test_unknown_nodes_ignored(self) -> None:
        edge = GovernanceEdge(
            edge_id="e1",
            from_node="x",  # Not in node_ids
            to_node="y",  # Not in node_ids
            zone_id="z1",
            weight=5.0,
        )
        matrix = build_local_trust_matrix([edge], ["a", "b"])
        assert matrix.sum() == 0.0


# ---------------------------------------------------------------------------
# detect_sybil_cluster
# ---------------------------------------------------------------------------


class TestDetectSybilCluster:
    """Tests for detect_sybil_cluster."""

    def test_no_suspicious_agents(self) -> None:
        scores = {"a1": 0.5, "a2": 0.6, "a3": 0.7}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert clusters == []

    def test_all_below_threshold(self) -> None:
        scores = {"a1": 0.01, "a2": 0.02, "a3": 0.03}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert len(clusters) == 1
        assert clusters[0] == {"a1", "a2", "a3"}

    def test_some_below_threshold(self) -> None:
        scores = {"a1": 0.05, "a2": 0.5, "a3": 0.01}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert len(clusters) == 1
        assert clusters[0] == {"a1", "a3"}

    def test_empty_scores(self) -> None:
        clusters = detect_sybil_cluster({})
        assert clusters == []

    def test_default_threshold(self) -> None:
        scores = {"a1": 0.05}
        clusters = detect_sybil_cluster(scores)
        assert len(clusters) == 1
        assert "a1" in clusters[0]
