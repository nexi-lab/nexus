"""Vector clock for causal ordering in edge split-brain scenarios.

Provides a frozen ``VectorClock`` dataclass that tracks per-node logical
timestamps. Used by ``ConflictDetector`` to determine whether two operations
are causally ordered or truly concurrent.

Issue #1707: Edge split-brain resilience.
"""

import json
from dataclasses import dataclass, field
from enum import Enum


class CausalOrder(Enum):
    """Result of comparing two vector clocks."""

    BEFORE = "before"
    AFTER = "after"
    CONCURRENT = "concurrent"
    EQUAL = "equal"


@dataclass(frozen=True, slots=True)
class VectorClock:
    """Immutable vector clock for causal ordering.

    Each node_id maps to a monotonically increasing counter.
    """

    counters: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Defensive copy to prevent external mutation of the frozen clock."""
        object.__setattr__(self, "counters", dict(self.counters))

    def increment(self, node_id: str) -> "VectorClock":
        """Return a new VectorClock with the given node's counter incremented."""
        new_counters = dict(self.counters)
        new_counters[node_id] = new_counters.get(node_id, 0) + 1
        return VectorClock(counters=new_counters)

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Return a new VectorClock that is the pointwise max of both clocks."""
        all_nodes = set(self.counters) | set(other.counters)
        merged = {
            node: max(self.counters.get(node, 0), other.counters.get(node, 0)) for node in all_nodes
        }
        return VectorClock(counters=merged)

    def compare(self, other: "VectorClock") -> CausalOrder:
        """Compare this clock with another.

        Returns:
            BEFORE  — self happened-before other
            AFTER   — self happened-after other
            EQUAL   — identical clocks
            CONCURRENT — neither dominates (true concurrency)
        """
        all_nodes = set(self.counters) | set(other.counters)
        if not all_nodes:
            return CausalOrder.EQUAL

        self_le = True  # self <= other on all nodes
        other_le = True  # other <= self on all nodes

        for node in all_nodes:
            s = self.counters.get(node, 0)
            o = other.counters.get(node, 0)
            if s > o:
                self_le = False  # self is NOT <= other on this node
            if o > s:
                other_le = False  # other is NOT <= self on this node

        if self_le and other_le:
            return CausalOrder.EQUAL
        if self_le:
            return CausalOrder.BEFORE
        if other_le:
            return CausalOrder.AFTER
        return CausalOrder.CONCURRENT

    def to_json(self) -> str:
        """Serialize to JSON string for storage."""
        return json.dumps(self.counters, sort_keys=True)

    @classmethod
    def from_json(cls, data: str) -> "VectorClock":
        """Deserialize from JSON string."""
        counters = json.loads(data)
        if not isinstance(counters, dict):
            raise ValueError(f"Expected dict, got {type(counters).__name__}")
        return cls(counters={k: int(v) for k, v in counters.items()})

    def __bool__(self) -> bool:
        """Return True if the clock has any non-zero counters."""
        return bool(self.counters)
