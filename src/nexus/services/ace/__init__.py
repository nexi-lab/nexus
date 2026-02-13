"""ACE (Agentic Context Engineering) - Learning Engine.

Implements trajectory tracking, reflection, curation, and consolidation
for continuous agent learning.

Affinity-based consolidation (Issue #1026):
    The ConsolidationEngine now supports SimpleMem-inspired affinity scoring
    that combines semantic similarity and temporal proximity for smarter
    memory clustering. Use `consolidate_by_affinity_async()` for this approach.

Hierarchical memory abstraction (Issue #1029):
    The HierarchicalMemoryManager enables multi-level memory hierarchies
    where atomic memories are progressively consolidated into higher-level
    abstractions (atoms → clusters → abstracts).
"""

from nexus.services.ace.affinity import (
    AffinityConfig,
    ClusterResult,
    MemoryVector,
    cluster_by_affinity,
    compute_affinity,
    compute_affinity_matrix,
    get_cluster_statistics,
)
from nexus.services.ace.consolidation import ConsolidationEngine
from nexus.services.ace.curation import Curator
from nexus.services.ace.feedback import FeedbackManager
from nexus.services.ace.learning_loop import LearningLoop
from nexus.services.ace.memory_hierarchy import (
    HierarchicalMemoryManager,
    HierarchyLevel,
    HierarchyResult,
    HierarchyRetrievalResult,
    build_hierarchy,
)
from nexus.services.ace.playbook import PlaybookManager
from nexus.services.ace.reflection import Reflector
from nexus.services.ace.trajectory import TrajectoryManager

__all__ = [
    # Core ACE components
    "TrajectoryManager",
    "Reflector",
    "Curator",
    "PlaybookManager",
    "ConsolidationEngine",
    "FeedbackManager",
    "LearningLoop",
    # Affinity scoring (Issue #1026)
    "AffinityConfig",
    "MemoryVector",
    "ClusterResult",
    "compute_affinity",
    "compute_affinity_matrix",
    "cluster_by_affinity",
    "get_cluster_statistics",
    # Hierarchical memory (Issue #1029)
    "HierarchicalMemoryManager",
    "HierarchyLevel",
    "HierarchyResult",
    "HierarchyRetrievalResult",
    "build_hierarchy",
]
