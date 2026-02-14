"""EigenTrust and trust propagation — pure math functions.

Issue #1359 Phase 2: ~80 lines of core EigenTrust + helpers.
No ORM or service dependencies.

Algorithm:
    t(k+1) = (1 - alpha) * C^T @ t(k) + alpha * p
    where:
        C = row-normalized local trust matrix
        p = seed trust (prior / pre-trusted peers)
        alpha = weight of seed trust (0.5 default)
        Convergence when ||t(k+1) - t(k)||_1 < epsilon
"""

from __future__ import annotations

import numpy as np

from nexus.governance.models import GovernanceEdge


def eigentrust(
    local_trust: np.ndarray,
    seed_trust: np.ndarray | None = None,
    alpha: float = 0.5,
    max_iter: int = 100,
    epsilon: float = 1e-6,
) -> np.ndarray:
    """Compute global trust via EigenTrust power iteration.

    Args:
        local_trust: NxN matrix of local trust values (non-negative).
        seed_trust: N-vector of pre-trusted seed trust. If None, uniform.
        alpha: Weight of seed trust (0 = pure local, 1 = pure seed).
        max_iter: Maximum iterations before stopping.
        epsilon: Convergence threshold (L1 norm of change).

    Returns:
        N-vector of global trust scores (sums to 1.0).
    """
    n = local_trust.shape[0]

    if n == 0:
        return np.array([], dtype=np.float64)

    # Normalize local trust: row-normalize C
    c = _row_normalize(local_trust)

    # Default seed: uniform
    if seed_trust is None:
        p = np.ones(n, dtype=np.float64) / n
    else:
        p = seed_trust.copy()
        p_sum = p.sum()
        if p_sum > 0:
            p /= p_sum
        else:
            p = np.ones(n, dtype=np.float64) / n

    # Initial trust = seed
    t = p.copy()

    # Power iteration
    ct = c.T  # Transpose once
    for _ in range(max_iter):
        t_new = (1 - alpha) * (ct @ t) + alpha * p

        # Normalize to prevent drift
        t_sum = t_new.sum()
        if t_sum > 0:
            t_new /= t_sum

        # Convergence check
        if np.abs(t_new - t).sum() < epsilon:
            result: np.ndarray = t_new
            return result

        t = t_new

    final: np.ndarray = t
    return final


def build_local_trust_matrix(
    edges: list[GovernanceEdge],
    node_ids: list[str],
) -> np.ndarray:
    """Build a local trust matrix from governance edges.

    Args:
        edges: List of governance edges with weights.
        node_ids: Ordered list of node IDs (defines matrix indices).

    Returns:
        NxN numpy array where matrix[i][j] = trust from node_ids[i] to node_ids[j].
    """
    n = len(node_ids)
    matrix = np.zeros((n, n), dtype=np.float64)

    id_to_idx = {nid: i for i, nid in enumerate(node_ids)}

    for edge in edges:
        i = id_to_idx.get(edge.from_node)
        j = id_to_idx.get(edge.to_node)
        if i is not None and j is not None:
            matrix[i][j] += max(edge.weight, 0.0)

    return matrix


def detect_sybil_cluster(
    trust_scores: dict[str, float],
    threshold: float = 0.1,
) -> list[set[str]]:
    """Identify clusters of agents with suspiciously low trust scores.

    Agents with trust below threshold are grouped into a single cluster.
    In a real implementation, this would use graph clustering algorithms.

    Args:
        trust_scores: Mapping of agent_id → global trust score.
        threshold: Trust score below which an agent is suspicious.

    Returns:
        List of sets, each set is a cluster of suspicious agent IDs.
    """
    suspicious = {aid for aid, score in trust_scores.items() if score < threshold}

    if not suspicious:
        return []

    # Simple: return all low-trust agents as one cluster
    # Future: use graph connectivity for finer clustering
    return [suspicious]


def _row_normalize(matrix: np.ndarray) -> np.ndarray:
    """Row-normalize a matrix (each row sums to 1, or 0 if all zeros)."""
    result = matrix.copy().astype(np.float64)
    row_sums = result.sum(axis=1)
    # Avoid division by zero: rows with all zeros stay zero
    nonzero = row_sums > 0
    result[nonzero] = result[nonzero] / row_sums[nonzero, np.newaxis]
    return result
