"""GovernanceSnapshot — pre-loaded governance data for batch analysis (Issue #2129 §13B).

Avoids double-loading edges in ``CollusionService.compute_fraud_scores()``
by bundling edges + node IDs + optional pre-built graph into a single
frozen dataclass.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nexus.bricks.governance.models import GovernanceEdge

if TYPE_CHECKING:
    import networkx as nx


@dataclass(frozen=True)
class GovernanceSnapshot:
    """Pre-loaded governance data for batch analysis."""

    edges: list[GovernanceEdge]
    node_ids: list[str]
    graph: "nx.DiGraph | None" = field(default=None, compare=False)
