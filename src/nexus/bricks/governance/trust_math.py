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

from typing import TYPE_CHECKING, Any, cast

from nexus.bricks.governance.models import GovernanceEdge

if TYPE_CHECKING:
    import numpy as np
    from scipy import sparse

_np: Any | None
_sparse: Any | None

try:
    import numpy as _np_mod
    from scipy import sparse as _sparse_mod
except ImportError:  # pragma: no cover - exercised when governance extra is absent
    _np = None
    _sparse = None
else:
    _np = _np_mod
    _sparse = _sparse_mod


def _require_governance_math() -> tuple[Any, Any]:
    """Return (numpy, scipy.sparse) or raise a clear install hint."""
    if _np is None or _sparse is None:
        raise RuntimeError(
            "Governance math dependencies are not installed. "
            "Install with: pip install 'nexus-ai-fs[governance]'"
        )
    return _np, _sparse


def eigentrust(
    local_trust: np.ndarray | sparse.spmatrix,
    seed_trust: np.ndarray | None = None,
    alpha: float = 0.5,
    max_iter: int = 100,
    epsilon: float = 1e-6,
) -> np.ndarray:
    """Compute global trust via EigenTrust power iteration.

    Args:
        local_trust: NxN matrix of local trust values (non-negative).
            Accepts both dense ndarray and scipy sparse matrices.
        seed_trust: N-vector of pre-trusted seed trust. If None, uniform.
        alpha: Weight of seed trust (0 = pure local, 1 = pure seed).
        max_iter: Maximum iterations before stopping.
        epsilon: Convergence threshold (L1 norm of change).

    Returns:
        N-vector of global trust scores (sums to 1.0).
    """
    np_mod, sparse_mod = _require_governance_math()

    n = local_trust.shape[0]

    if n == 0:
        return cast("np.ndarray", np_mod.array([], dtype=np_mod.float64))

    # Normalize local trust: row-normalize C
    c = _row_normalize(local_trust)

    # Default seed: uniform
    if seed_trust is None:
        p = np_mod.ones(n, dtype=np_mod.float64) / n
    else:
        p = seed_trust.copy()
        p_sum = p.sum()
        if p_sum > 0:
            p /= p_sum
        else:
            p = np_mod.ones(n, dtype=np_mod.float64) / n

    # Initial trust = seed
    t = p.copy()

    # Power iteration — transpose once
    ct = c.T.tocsr() if isinstance(c, sparse_mod.spmatrix) else c.T

    for _ in range(max_iter):
        t_new = (1 - alpha) * (ct @ t) + alpha * p

        # Ensure dense for normalization
        if sparse_mod.issparse(t_new):
            t_new = np_mod.asarray(t_new).flatten()

        # Normalize to prevent drift
        t_sum = t_new.sum()
        if t_sum > 0:
            t_new /= t_sum

        # Convergence check
        if np_mod.abs(t_new - t).sum() < epsilon:
            result = cast("np.ndarray", t_new)
            return result

        t = t_new

    final = cast("np.ndarray", t)
    return final


def build_local_trust_matrix(
    edges: list[GovernanceEdge],
    node_ids: list[str],
    max_nodes: int = 10_000,
) -> sparse.lil_matrix:
    """Build a local trust matrix from governance edges.

    Uses scipy sparse matrices to handle large graphs efficiently
    without OOM on dense NxN allocations (Issue #2129 §15).

    Args:
        edges: List of governance edges with weights.
        node_ids: Ordered list of node IDs (defines matrix indices).
        max_nodes: Safety cap on matrix dimension.

    Returns:
        NxN sparse matrix where matrix[i][j] = trust from node_ids[i] to node_ids[j].
    """
    _np_mod, sparse_mod = _require_governance_math()

    n = min(len(node_ids), max_nodes)
    matrix = sparse_mod.lil_matrix((n, n), dtype=float)

    id_to_idx = {nid: i for i, nid in enumerate(node_ids[:n])}

    for edge in edges:
        i = id_to_idx.get(edge.from_node)
        j = id_to_idx.get(edge.to_node)
        if i is not None and j is not None:
            matrix[i, j] += max(edge.weight, 0.0)

    return matrix


def detect_sybil_cluster(
    trust_scores: dict[str, float],
    threshold: float = 0.1,
) -> list[set[str]]:
    """Identify clusters of agents with suspiciously low trust scores.

    Agents with trust below threshold are grouped into a single cluster.
    In a real implementation, this would use graph clustering algorithms.

    Args:
        trust_scores: Mapping of agent_id -> global trust score.
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


def _row_normalize(matrix: np.ndarray | sparse.spmatrix) -> np.ndarray | sparse.spmatrix:
    """Row-normalize a matrix (each row sums to 1, or 0 if all zeros)."""
    np_mod, sparse_mod = _require_governance_math()

    if isinstance(matrix, sparse_mod.spmatrix):
        mat = matrix.tocsr().astype(np_mod.float64, copy=True)
        row_sums = np_mod.asarray(mat.sum(axis=1)).flatten()
        nonzero = row_sums > 0
        # Scale rows in-place
        for i in range(mat.shape[0]):
            if nonzero[i]:
                mat[i] /= row_sums[i]
        return mat

    result = matrix.copy().astype(np_mod.float64)
    row_sums = result.sum(axis=1)
    nonzero = row_sums > 0
    result[nonzero] = result[nonzero] / row_sums[nonzero, np_mod.newaxis]
    return result
