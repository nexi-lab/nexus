"""Reusable graph topology fixtures for governance tests.

Issue #1359: Provides standard graph patterns for testing
collusion detection, ring detection, and Sybil analysis.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nexus.governance.models import EdgeType, GovernanceEdge


def _edge(
    from_node: str, to_node: str, weight: float = 1.0, zone_id: str = "zone-1"
) -> GovernanceEdge:
    """Helper to create a test edge."""
    return GovernanceEdge(
        edge_id=f"e-{from_node}-{to_node}",
        from_node=from_node,
        to_node=to_node,
        zone_id=zone_id,
        edge_type=EdgeType.TRANSACTION,
        weight=weight,
        created_at=datetime.now(UTC),
    )


def clean_chain(n: int = 4, zone_id: str = "zone-1") -> list[GovernanceEdge]:
    """A→B→C→D — no rings, linear chain.

    Args:
        n: Number of nodes in the chain.
    """
    nodes = [f"agent-{i}" for i in range(n)]
    return [_edge(nodes[i], nodes[i + 1], zone_id=zone_id) for i in range(n - 1)]


def simple_ring(n: int = 3, zone_id: str = "zone-1") -> list[GovernanceEdge]:
    """A→B→C→A — simple cycle.

    Args:
        n: Number of nodes in the ring (minimum 3).
    """
    nodes = [f"agent-{i}" for i in range(n)]
    edges = [_edge(nodes[i], nodes[(i + 1) % n], zone_id=zone_id) for i in range(n)]
    return edges


def complex_ring(zone_id: str = "zone-1") -> list[GovernanceEdge]:
    """Multiple overlapping rings.

    Ring 1: A→B→C→A
    Ring 2: C→D→E→C
    """
    return [
        _edge("A", "B", zone_id=zone_id),
        _edge("B", "C", zone_id=zone_id),
        _edge("C", "A", zone_id=zone_id),
        _edge("C", "D", zone_id=zone_id),
        _edge("D", "E", zone_id=zone_id),
        _edge("E", "C", zone_id=zone_id),
    ]


def star_topology(
    hub: str = "hub", n_spokes: int = 5, zone_id: str = "zone-1"
) -> list[GovernanceEdge]:
    """Hub connected to all spokes (no rings)."""
    spokes = [f"spoke-{i}" for i in range(n_spokes)]
    edges: list[GovernanceEdge] = []
    for spoke in spokes:
        edges.append(_edge(hub, spoke, zone_id=zone_id))
        edges.append(_edge(spoke, hub, zone_id=zone_id))
    return edges


def sybil_cluster(
    n_honest: int = 3,
    n_sybils: int = 5,
    zone_id: str = "zone-1",
) -> list[GovernanceEdge]:
    """Cluster of Sybils with shared principal.

    Honest agents: h-0, h-1, h-2 (all connected)
    Sybils: s-0, s-1, s-2, s-3, s-4 (all connected to each other, weakly to honest)
    """
    honest = [f"h-{i}" for i in range(n_honest)]
    sybils = [f"s-{i}" for i in range(n_sybils)]

    edges: list[GovernanceEdge] = []

    # Honest agents interconnected with high weight
    for i in range(n_honest):
        for j in range(i + 1, n_honest):
            edges.append(_edge(honest[i], honest[j], weight=5.0, zone_id=zone_id))
            edges.append(_edge(honest[j], honest[i], weight=5.0, zone_id=zone_id))

    # Sybils interconnected with high weight (suspicious)
    for i in range(n_sybils):
        for j in range(i + 1, n_sybils):
            edges.append(_edge(sybils[i], sybils[j], weight=3.0, zone_id=zone_id))
            edges.append(_edge(sybils[j], sybils[i], weight=3.0, zone_id=zone_id))

    # Weak connections from one sybil to honest (attack vector)
    edges.append(_edge(sybils[0], honest[0], weight=0.1, zone_id=zone_id))

    return edges


def mixed_graph(zone_id: str = "zone-1") -> list[GovernanceEdge]:
    """Mix of clean agents + fraud ring + Sybils."""
    edges: list[GovernanceEdge] = []

    # Clean chain: c-0 → c-1 → c-2
    edges.extend(clean_chain(3, zone_id=zone_id))

    # Fraud ring: f-0 → f-1 → f-2 → f-0
    fraud_nodes = ["f-0", "f-1", "f-2"]
    for i in range(3):
        edges.append(_edge(fraud_nodes[i], fraud_nodes[(i + 1) % 3], weight=8.0, zone_id=zone_id))

    # Weak link between clean and fraud
    edges.append(_edge("agent-2", "f-0", weight=0.5, zone_id=zone_id))

    return edges


def large_graph(
    n_nodes: int = 100, n_edges: int = 500, zone_id: str = "zone-1"
) -> list[GovernanceEdge]:
    """Random graph for performance testing."""
    import random

    random.seed(42)  # Reproducible
    nodes = [f"node-{i}" for i in range(n_nodes)]
    edges: list[GovernanceEdge] = []

    for _ in range(n_edges):
        src = random.choice(nodes)
        dst = random.choice(nodes)
        if src != dst:
            weight = round(random.uniform(0.1, 10.0), 2)
            edges.append(_edge(src, dst, weight=weight, zone_id=zone_id))

    return edges
