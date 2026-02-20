"""Agent QoS classes for scheduling, eviction, and resource allocation (Issue #2171).

Defines three QoS classes (premium/standard/spot) with K8s-style separation
of scheduling priority from eviction priority, per-class resource guarantees
with per-agent overrides, and eviction context for preemption.

Types:
- QoSClass: StrEnum for the three QoS tiers.
- EVICTION_ORDER: Maps QoSClass to eviction priority (lower = evicted first).
- QoSClassConfig: Per-class resource limits and scheduling weights.
- QoSTuning: Composite config for all three classes.
- AgentQoS: Per-agent QoS assignment with optional overrides.
- EvictionContext: Context passed to eviction policy for preemption decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from nexus.contracts.agent_types import EvictionReason


class PressureLevel(Enum):
    """System resource pressure classification (Issue #2170).

    Moved from ``services.agents.resource_monitor`` to contracts tier
    so that EvictionContext can reference it without a tier violation
    (Issue #2171). Re-exported from resource_monitor for backward compat.
    """

    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class QoSClass(StrEnum):
    """Agent QoS classification.

    Three tiers with distinct resource guarantees:
    - PREMIUM: Reserved resources, last to evict, highest scheduling weight.
    - STANDARD: Default tier, burstable resources.
    - SPOT: Opportunistic, first to evict, preemptible by higher classes.
    """

    PREMIUM = "premium"
    STANDARD = "standard"
    SPOT = "spot"


# Eviction order: lower value = evicted first.
EVICTION_ORDER: dict[QoSClass, int] = {
    QoSClass.SPOT: 0,
    QoSClass.STANDARD: 1,
    QoSClass.PREMIUM: 2,
}


@dataclass(frozen=True)
class QoSClassConfig:
    """Per-class resource limits and scheduling parameters.

    Attributes:
        max_concurrent_tasks: Maximum concurrent tasks for agents in this class.
        scheduling_weight: WFQ weight for fair-share scheduling (higher = more share).
        eviction_priority: Eviction resistance (higher = harder to evict).
        preemptible: Whether agents in this class can be preempted by higher classes.
    """

    max_concurrent_tasks: int
    scheduling_weight: int
    eviction_priority: int
    preemptible: bool


@dataclass(frozen=True)
class QoSTuning:
    """Composite QoS configuration for all three classes.

    Wired via DI from ProfileTuning; each deployment profile defines
    different resource limits per class.
    """

    premium: QoSClassConfig
    standard: QoSClassConfig
    spot: QoSClassConfig

    def for_class(self, cls: QoSClass) -> QoSClassConfig:
        """Look up the config for a given QoS class.

        Args:
            cls: The QoS class to look up.

        Returns:
            QoSClassConfig for the requested class.

        Raises:
            ValueError: If cls is not a valid QoSClass.
        """
        configs = {
            QoSClass.PREMIUM: self.premium,
            QoSClass.STANDARD: self.standard,
            QoSClass.SPOT: self.spot,
        }
        config = configs.get(cls)
        if config is None:
            raise ValueError(f"Unknown QoS class: {cls!r}")
        return config


@dataclass(frozen=True)
class AgentQoS:
    """Per-agent QoS assignment with optional overrides.

    K8s-style separation: scheduling_class controls fair-share weight,
    eviction_class controls eviction ordering. They default to the same
    value but can be set independently.

    Override fields (max_concurrent_override, scheduling_weight_override)
    take precedence over class defaults when set.

    Attributes:
        scheduling_class: QoS class for scheduling weight lookup.
        eviction_class: QoS class for eviction priority ordering.
        max_concurrent_override: Per-agent override for max concurrent tasks.
        scheduling_weight_override: Per-agent override for scheduling weight.
    """

    scheduling_class: QoSClass = QoSClass.STANDARD
    eviction_class: QoSClass = QoSClass.STANDARD
    max_concurrent_override: int | None = None
    scheduling_weight_override: int | None = None

    def resolve_max_concurrent(self, tuning: QoSTuning) -> int:
        """Resolve effective max concurrent tasks.

        Per-agent override takes precedence over class default.

        Args:
            tuning: QoSTuning with per-class defaults.

        Returns:
            Effective max concurrent tasks for this agent.
        """
        if self.max_concurrent_override is not None:
            return self.max_concurrent_override
        return tuning.for_class(self.scheduling_class).max_concurrent_tasks

    def resolve_scheduling_weight(self, tuning: QoSTuning) -> int:
        """Resolve effective scheduling weight.

        Per-agent override takes precedence over class default.

        Args:
            tuning: QoSTuning with per-class defaults.

        Returns:
            Effective scheduling weight for this agent.
        """
        if self.scheduling_weight_override is not None:
            return self.scheduling_weight_override
        return tuning.for_class(self.scheduling_class).scheduling_weight

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict for JSON storage.

        Returns:
            Dict with string keys suitable for JSON serialization.
        """
        return {
            "scheduling_class": str(self.scheduling_class),
            "eviction_class": str(self.eviction_class),
            "max_concurrent_override": self.max_concurrent_override,
            "scheduling_weight_override": self.scheduling_weight_override,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AgentQoS:
        """Deserialize from a plain dict (JSON storage).

        Args:
            data: Dict with scheduling_class, eviction_class, and optional overrides.

        Returns:
            AgentQoS instance.
        """
        return cls(
            scheduling_class=QoSClass(str(data.get("scheduling_class", "standard"))),
            eviction_class=QoSClass(str(data.get("eviction_class", "standard"))),
            max_concurrent_override=cast("int | None", data.get("max_concurrent_override")),
            scheduling_weight_override=cast("int | None", data.get("scheduling_weight_override")),
        )


@dataclass(frozen=True)
class EvictionContext:
    """Context for eviction policy decisions (Issue #2171).

    Passed to EvictionPolicy.select_candidates() to enable QoS-aware
    eviction and preemption decisions.

    Attributes:
        pressure: Current system resource pressure level.
        trigger: Why this eviction cycle was triggered.
        requesting_agent_qos: QoS class of the agent requesting resources
            (for preemption scenarios). None if not a preemption trigger.
    """

    pressure: PressureLevel
    trigger: EvictionReason
    requesting_agent_qos: QoSClass | None = None
