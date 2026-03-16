"""Unit tests for QoS contract types (Issue #2171).

Tests cover:
- QoSClass enum values and ordering
- EVICTION_ORDER mapping correctness
- QoSClassConfig frozen dataclass
- QoSTuning.for_class() lookup and error handling
- AgentQoS defaults, overrides, serialization (to_dict/from_dict)
- EvictionContext construction
"""

import pytest

from nexus.contracts.agent_types import EvictionReason
from nexus.contracts.qos import (
    EVICTION_ORDER,
    AgentQoS,
    EvictionContext,
    QoSClass,
    QoSClassConfig,
    QoSTuning,
)
from nexus.system_services.agents.resource_monitor import PressureLevel

# ---------------------------------------------------------------------------
# QoSClass enum
# ---------------------------------------------------------------------------


class TestQoSClass:
    def test_enum_values(self):
        assert QoSClass.PREMIUM == "premium"
        assert QoSClass.STANDARD == "standard"
        assert QoSClass.SPOT == "spot"

    def test_enum_from_string(self):
        assert QoSClass("premium") is QoSClass.PREMIUM
        assert QoSClass("standard") is QoSClass.STANDARD
        assert QoSClass("spot") is QoSClass.SPOT

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            QoSClass("invalid")


# ---------------------------------------------------------------------------
# EVICTION_ORDER
# ---------------------------------------------------------------------------


class TestEvictionOrder:
    def test_spot_lowest(self):
        assert EVICTION_ORDER[QoSClass.SPOT] == 0

    def test_standard_middle(self):
        assert EVICTION_ORDER[QoSClass.STANDARD] == 1

    def test_premium_highest(self):
        assert EVICTION_ORDER[QoSClass.PREMIUM] == 2

    def test_monotonicity(self):
        """Spot < Standard < Premium."""
        assert (
            EVICTION_ORDER[QoSClass.SPOT]
            < EVICTION_ORDER[QoSClass.STANDARD]
            < EVICTION_ORDER[QoSClass.PREMIUM]
        )


# ---------------------------------------------------------------------------
# QoSClassConfig
# ---------------------------------------------------------------------------


class TestQoSClassConfig:
    def test_fields(self):
        config = QoSClassConfig(
            max_concurrent_tasks=5, scheduling_weight=1, eviction_priority=0, preemptible=True
        )
        assert config.max_concurrent_tasks == 5
        assert config.scheduling_weight == 1
        assert config.eviction_priority == 0
        assert config.preemptible is True


# ---------------------------------------------------------------------------
# QoSTuning
# ---------------------------------------------------------------------------


@pytest.fixture
def tuning():
    return QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=20, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=10, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=5, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
    )


class TestQoSTuning:
    def test_for_class_premium(self, tuning):
        config = tuning.for_class(QoSClass.PREMIUM)
        assert config.max_concurrent_tasks == 20
        assert config.scheduling_weight == 3

    def test_for_class_standard(self, tuning):
        config = tuning.for_class(QoSClass.STANDARD)
        assert config.max_concurrent_tasks == 10

    def test_for_class_spot(self, tuning):
        config = tuning.for_class(QoSClass.SPOT)
        assert config.max_concurrent_tasks == 5
        assert config.preemptible is True


# ---------------------------------------------------------------------------
# AgentQoS
# ---------------------------------------------------------------------------


class TestAgentQoS:
    def test_defaults(self):
        qos = AgentQoS()
        assert qos.scheduling_class is QoSClass.STANDARD
        assert qos.eviction_class is QoSClass.STANDARD
        assert qos.max_concurrent_override is None
        assert qos.scheduling_weight_override is None

    def test_resolve_max_concurrent_from_class(self, tuning):
        qos = AgentQoS(scheduling_class=QoSClass.PREMIUM)
        assert qos.resolve_max_concurrent(tuning) == 20

    def test_resolve_max_concurrent_override(self, tuning):
        qos = AgentQoS(scheduling_class=QoSClass.PREMIUM, max_concurrent_override=42)
        assert qos.resolve_max_concurrent(tuning) == 42

    def test_resolve_scheduling_weight_from_class(self, tuning):
        qos = AgentQoS(scheduling_class=QoSClass.PREMIUM)
        assert qos.resolve_scheduling_weight(tuning) == 3

    def test_resolve_scheduling_weight_override(self, tuning):
        qos = AgentQoS(scheduling_class=QoSClass.STANDARD, scheduling_weight_override=7)
        assert qos.resolve_scheduling_weight(tuning) == 7

    def test_to_dict(self):
        qos = AgentQoS(
            scheduling_class=QoSClass.PREMIUM,
            eviction_class=QoSClass.STANDARD,
            max_concurrent_override=15,
        )
        d = qos.to_dict()
        assert d["scheduling_class"] == "premium"
        assert d["eviction_class"] == "standard"
        assert d["max_concurrent_override"] == 15
        assert d["scheduling_weight_override"] is None

    def test_from_dict_full(self):
        data = {
            "scheduling_class": "premium",
            "eviction_class": "spot",
            "max_concurrent_override": 25,
            "scheduling_weight_override": 5,
        }
        qos = AgentQoS.from_dict(data)
        assert qos.scheduling_class is QoSClass.PREMIUM
        assert qos.eviction_class is QoSClass.SPOT
        assert qos.max_concurrent_override == 25
        assert qos.scheduling_weight_override == 5

    def test_from_dict_defaults(self):
        qos = AgentQoS.from_dict({})
        assert qos.scheduling_class is QoSClass.STANDARD
        assert qos.eviction_class is QoSClass.STANDARD
        assert qos.max_concurrent_override is None

    def test_roundtrip(self):
        original = AgentQoS(
            scheduling_class=QoSClass.SPOT,
            eviction_class=QoSClass.PREMIUM,
            max_concurrent_override=8,
            scheduling_weight_override=2,
        )
        restored = AgentQoS.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# EvictionContext
# ---------------------------------------------------------------------------


class TestEvictionContext:
    def test_construction(self):
        ctx = EvictionContext(
            pressure=PressureLevel.CRITICAL,
            trigger=EvictionReason.PRESSURE_CRITICAL,
            requesting_agent_qos=QoSClass.PREMIUM,
        )
        assert ctx.pressure is PressureLevel.CRITICAL
        assert ctx.trigger is EvictionReason.PRESSURE_CRITICAL
        assert ctx.requesting_agent_qos is QoSClass.PREMIUM

    def test_default_requesting_qos_is_none(self):
        ctx = EvictionContext(
            pressure=PressureLevel.NORMAL,
            trigger=EvictionReason.OVER_AGENT_CAP,
        )
        assert ctx.requesting_agent_qos is None
