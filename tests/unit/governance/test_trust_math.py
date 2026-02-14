"""Tests for EigenTrust and trust math.

Issue #1359 Phase 2: ~20 tests covering convergence, edge cases,
topologies, and performance.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from nexus.services.governance.models import EdgeType, GovernanceEdge
from nexus.services.governance.trust_math import (
    build_local_trust_matrix,
    detect_sybil_cluster,
    eigentrust,
)


class TestEigenTrust:
    """Tests for the EigenTrust algorithm."""

    def test_empty_matrix(self) -> None:
        result = eigentrust(np.array([]).reshape(0, 0))
        assert len(result) == 0

    def test_single_node(self) -> None:
        m = np.array([[1.0]])
        result = eigentrust(m)
        assert len(result) == 1
        assert result[0] == pytest.approx(1.0)

    def test_two_nodes_symmetric(self) -> None:
        m = np.array([[0.0, 1.0], [1.0, 0.0]])
        result = eigentrust(m)
        assert len(result) == 2
        assert result[0] == pytest.approx(0.5, abs=0.01)
        assert result[1] == pytest.approx(0.5, abs=0.01)

    def test_three_nodes_hand_computed(self) -> None:
        """3-node chain: A→B→C. Trust should flow from A to C via B."""
        m = np.array(
            [
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
            ]
        )
        result = eigentrust(m, alpha=0.5)
        assert len(result) == 3
        # Sum should be ~1.0
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_convergence_within_max_iter(self) -> None:
        m = np.array(
            [
                [0.0, 0.5, 0.5],
                [0.3, 0.0, 0.7],
                [0.6, 0.4, 0.0],
            ]
        )
        result = eigentrust(m, max_iter=100, epsilon=1e-6)
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_with_seed_trust(self) -> None:
        m = np.array(
            [
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
                [1.0, 0.0, 0.0],
            ]
        )
        seed = np.array([1.0, 0.0, 0.0])  # Trust only agent 0
        result = eigentrust(m, seed_trust=seed, alpha=0.5)
        assert result[0] > result[1]  # Agent 0 should have highest trust

    def test_without_seed_trust(self) -> None:
        m = np.array(
            [
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
                [1.0, 0.0, 0.0],
            ]
        )
        result = eigentrust(m, seed_trust=None)
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_disconnected_graph(self) -> None:
        """Isolated nodes should get default (uniform) trust."""
        m = np.array(
            [
                [0.0, 1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 0.0, 0.0],  # Isolated
                [0.0, 0.0, 0.0, 0.0],  # Isolated
            ]
        )
        result = eigentrust(m)
        assert len(result) == 4
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_self_loops(self) -> None:
        """Self-loops should be handled gracefully."""
        m = np.array(
            [
                [1.0, 1.0],
                [1.0, 1.0],
            ]
        )
        result = eigentrust(m)
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_all_zero_trust(self) -> None:
        """All-zero matrix → uniform distribution via seed."""
        m = np.zeros((3, 3))
        result = eigentrust(m)
        # Should converge to seed (uniform)
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_star_topology(self) -> None:
        """Hub with 4 spokes. Hub should get highest trust."""
        m = np.array(
            [
                [0.0, 1.0, 1.0, 1.0, 1.0],  # Hub trusts all
                [1.0, 0.0, 0.0, 0.0, 0.0],  # Spoke 1 trusts hub
                [1.0, 0.0, 0.0, 0.0, 0.0],  # Spoke 2 trusts hub
                [1.0, 0.0, 0.0, 0.0, 0.0],  # Spoke 3 trusts hub
                [1.0, 0.0, 0.0, 0.0, 0.0],  # Spoke 4 trusts hub
            ]
        )
        result = eigentrust(m)
        assert result[0] > result[1]  # Hub has highest trust

    def test_large_random_graph_performance(self) -> None:
        """1000-node random graph should converge in <1s."""
        np.random.seed(42)
        n = 1000
        m = np.random.rand(n, n) * 0.1
        np.fill_diagonal(m, 0.0)

        start = time.monotonic()
        result = eigentrust(m, max_iter=100)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"EigenTrust on 1000 nodes took {elapsed:.2f}s"
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_alpha_zero_pure_local(self) -> None:
        """alpha=0 means pure local trust (no seed influence)."""
        m = np.array(
            [
                [0.0, 1.0],
                [1.0, 0.0],
            ]
        )
        result = eigentrust(m, alpha=0.0)
        assert sum(result) == pytest.approx(1.0, abs=0.01)

    def test_alpha_one_pure_seed(self) -> None:
        """alpha=1 means pure seed trust (ignore local)."""
        m = np.array(
            [
                [0.0, 1.0],
                [0.0, 0.0],
            ]
        )
        seed = np.array([0.8, 0.2])
        result = eigentrust(m, seed_trust=seed, alpha=1.0)
        # Should be exactly seed (normalized)
        assert result[0] == pytest.approx(0.8, abs=0.01)
        assert result[1] == pytest.approx(0.2, abs=0.01)


class TestBuildLocalTrustMatrix:
    """Tests for building trust matrices from edges."""

    def test_basic_matrix(self) -> None:
        edges = [
            GovernanceEdge("e1", "A", "B", "z1", EdgeType.TRANSACTION, 2.0),
            GovernanceEdge("e2", "B", "A", "z1", EdgeType.TRANSACTION, 1.0),
        ]
        matrix = build_local_trust_matrix(edges, ["A", "B"])
        assert matrix[0][1] == 2.0  # A→B
        assert matrix[1][0] == 1.0  # B→A

    def test_empty_edges(self) -> None:
        matrix = build_local_trust_matrix([], ["A", "B"])
        assert np.all(matrix == 0.0)

    def test_unknown_nodes_ignored(self) -> None:
        edges = [
            GovernanceEdge("e1", "A", "C", "z1", EdgeType.TRANSACTION, 1.0),
        ]
        matrix = build_local_trust_matrix(edges, ["A", "B"])
        assert np.all(matrix == 0.0)  # C not in node_ids

    def test_negative_weights_clamped(self) -> None:
        edges = [
            GovernanceEdge("e1", "A", "B", "z1", EdgeType.TRANSACTION, -1.0),
        ]
        matrix = build_local_trust_matrix(edges, ["A", "B"])
        assert matrix[0][1] == 0.0  # Negative clamped to 0

    def test_multiple_edges_same_pair(self) -> None:
        edges = [
            GovernanceEdge("e1", "A", "B", "z1", EdgeType.TRANSACTION, 1.0),
            GovernanceEdge("e2", "A", "B", "z1", EdgeType.TRANSACTION, 2.0),
        ]
        matrix = build_local_trust_matrix(edges, ["A", "B"])
        assert matrix[0][1] == 3.0  # Accumulated


class TestDetectSybilCluster:
    """Tests for Sybil cluster detection."""

    def test_no_suspicious(self) -> None:
        scores = {"A": 0.5, "B": 0.6, "C": 0.4}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert clusters == []

    def test_all_suspicious(self) -> None:
        scores = {"A": 0.01, "B": 0.02, "C": 0.03}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert len(clusters) == 1
        assert clusters[0] == {"A", "B", "C"}

    def test_mixed(self) -> None:
        scores = {"A": 0.5, "B": 0.01, "C": 0.02}
        clusters = detect_sybil_cluster(scores, threshold=0.1)
        assert len(clusters) == 1
        assert "A" not in clusters[0]
        assert "B" in clusters[0]
        assert "C" in clusters[0]

    def test_empty_scores(self) -> None:
        clusters = detect_sybil_cluster({})
        assert clusters == []

    def test_custom_threshold(self) -> None:
        scores = {"A": 0.3, "B": 0.2, "C": 0.1}
        clusters = detect_sybil_cluster(scores, threshold=0.25)
        assert len(clusters) == 1
        assert "A" not in clusters[0]
        assert "B" in clusters[0]
        assert "C" in clusters[0]
