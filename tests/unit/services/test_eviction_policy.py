"""Unit tests for EvictionPolicy (Issues #2170, #2171).

Tests cover:
- LRU selects oldest first, respects batch_size, handles None heartbeats
- LRUEvictionPolicy satisfies EvictionPolicy protocol
- QoSEvictionPolicy: spot-first ordering, preemption filtering,
  fallback to LRU within same class, mixed QoS ordering

Post-AgentRegistry deletion: tests use AgentDescriptor instead of AgentRecord.
"""

from datetime import UTC, datetime, timedelta

from nexus.contracts.agent_types import EvictionReason
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.process_types import (
    AgentDescriptor,
    AgentKind,
    AgentState,
    ExternalProcessInfo,
)
from nexus.contracts.qos import EvictionContext, QoSClass
from nexus.services.agents.eviction_policy import (
    EvictionPolicy,
    LRUEvictionPolicy,
    QoSEvictionPolicy,
)
from nexus.services.agents.resource_monitor import PressureLevel


def _make_agent(
    agent_id: str,
    last_heartbeat: datetime | None = None,
    eviction_class: QoSClass = QoSClass.STANDARD,
) -> AgentDescriptor:
    """Create a minimal AgentDescriptor for testing."""
    now = datetime.now(UTC)
    return AgentDescriptor(
        pid=agent_id,
        ppid=None,
        name=agent_id,
        owner_id="test-owner",
        zone_id=ROOT_ZONE_ID,
        kind=AgentKind.UNMANAGED,
        state=AgentState.BUSY,
        generation=1,
        created_at=now,
        updated_at=now,
        external_info=ExternalProcessInfo(
            connection_id=f"conn-{agent_id}",
            last_heartbeat=last_heartbeat,
        ),
        labels={"eviction_class": eviction_class.value},
    )


# ---------------------------------------------------------------------------
# LRUEvictionPolicy
# ---------------------------------------------------------------------------


class TestLRUEvictionPolicy:
    """Tests for LRUEvictionPolicy."""

    def test_lru_selects_oldest_first(self):
        now = datetime.now(UTC)
        agents = [
            _make_agent("old", last_heartbeat=now - timedelta(hours=2)),
            _make_agent("medium", last_heartbeat=now - timedelta(hours=1)),
            _make_agent("new", last_heartbeat=now),
        ]

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=2)

        assert len(selected) == 2
        assert selected[0].pid == "old"
        assert selected[1].pid == "medium"

    def test_lru_respects_batch_size(self):
        now = datetime.now(UTC)
        agents = [_make_agent(f"agent-{i}", last_heartbeat=now) for i in range(10)]

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=3)

        assert len(selected) == 3

    def test_lru_handles_null_heartbeats(self):
        now = datetime.now(UTC)
        agents = [
            _make_agent("no-heartbeat-1", last_heartbeat=None),
            _make_agent("no-heartbeat-2", last_heartbeat=None),
            _make_agent("old", last_heartbeat=now - timedelta(hours=1)),
            _make_agent("new", last_heartbeat=now),
        ]

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=2)

        assert len(selected) == 2
        assert selected[0].pid == "no-heartbeat-1"
        assert selected[1].pid == "no-heartbeat-2"

    def test_lru_empty_list(self):
        policy = LRUEvictionPolicy()
        selected = policy.select_candidates([], batch_size=10)
        assert selected == []

    def test_lru_satisfies_protocol(self):
        assert isinstance(LRUEvictionPolicy(), EvictionPolicy)

    def test_lru_accepts_context_without_error(self):
        """LRU policy accepts context param but ignores it."""
        now = datetime.now(UTC)
        agents = [_make_agent("agent-1", last_heartbeat=now)]
        ctx = EvictionContext(
            pressure=PressureLevel.CRITICAL,
            trigger=EvictionReason.PRESSURE_CRITICAL,
        )

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=1, context=ctx)
        assert len(selected) == 1


# ---------------------------------------------------------------------------
# QoSEvictionPolicy
# ---------------------------------------------------------------------------


class TestQoSEvictionPolicy:
    """Tests for QoSEvictionPolicy."""

    def test_satisfies_protocol(self):
        assert isinstance(QoSEvictionPolicy(), EvictionPolicy)

    def test_spot_evicted_before_standard(self):
        """Spot agents are evicted before standard agents."""
        now = datetime.now(UTC)
        agents = [
            _make_agent(
                "std-1", last_heartbeat=now - timedelta(hours=2), eviction_class=QoSClass.STANDARD
            ),
            _make_agent("spot-1", last_heartbeat=now, eviction_class=QoSClass.SPOT),
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=1)

        assert len(selected) == 1
        assert selected[0].pid == "spot-1"

    def test_standard_evicted_before_premium(self):
        """Standard agents are evicted before premium agents."""
        now = datetime.now(UTC)
        agents = [
            _make_agent(
                "premium-1",
                last_heartbeat=now - timedelta(hours=5),
                eviction_class=QoSClass.PREMIUM,
            ),
            _make_agent("std-1", last_heartbeat=now, eviction_class=QoSClass.STANDARD),
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=1)

        assert selected[0].pid == "std-1"

    def test_within_class_oldest_first(self):
        """Within same QoS class, oldest heartbeat is evicted first."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("spot-new", last_heartbeat=now, eviction_class=QoSClass.SPOT),
            _make_agent(
                "spot-old", last_heartbeat=now - timedelta(hours=2), eviction_class=QoSClass.SPOT
            ),
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=1)

        assert selected[0].pid == "spot-old"

    def test_none_heartbeat_evicted_first_within_class(self):
        """Agents with None heartbeat are evicted first within same class."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("spot-active", last_heartbeat=now, eviction_class=QoSClass.SPOT),
            _make_agent("spot-no-hb", last_heartbeat=None, eviction_class=QoSClass.SPOT),
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=1)

        assert selected[0].pid == "spot-no-hb"

    def test_full_ordering_spot_standard_premium(self):
        """Full ordering: spot (oldest first) -> standard -> premium."""
        now = datetime.now(UTC)
        agents = [
            _make_agent(
                "premium-1",
                last_heartbeat=now - timedelta(hours=10),
                eviction_class=QoSClass.PREMIUM,
            ),
            _make_agent(
                "std-old", last_heartbeat=now - timedelta(hours=5), eviction_class=QoSClass.STANDARD
            ),
            _make_agent("std-new", last_heartbeat=now, eviction_class=QoSClass.STANDARD),
            _make_agent(
                "spot-old", last_heartbeat=now - timedelta(hours=3), eviction_class=QoSClass.SPOT
            ),
            _make_agent(
                "spot-new", last_heartbeat=now - timedelta(hours=1), eviction_class=QoSClass.SPOT
            ),
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=10)

        ids = [a.pid for a in selected]
        assert ids == ["spot-old", "spot-new", "std-old", "std-new", "premium-1"]

    def test_batch_size_limits_output(self):
        """Batch size limits number of selected agents."""
        now = datetime.now(UTC)
        agents = [
            _make_agent(f"spot-{i}", last_heartbeat=now, eviction_class=QoSClass.SPOT)
            for i in range(10)
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=3)

        assert len(selected) == 3

    def test_empty_list(self):
        policy = QoSEvictionPolicy()
        selected = policy.select_candidates([], batch_size=10)
        assert selected == []

    def test_preemption_only_evicts_lower_class(self):
        """Preemption with PREMIUM requesting only evicts SPOT and STANDARD."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("spot-1", last_heartbeat=now, eviction_class=QoSClass.SPOT),
            _make_agent("std-1", last_heartbeat=now, eviction_class=QoSClass.STANDARD),
            _make_agent("premium-1", last_heartbeat=now, eviction_class=QoSClass.PREMIUM),
        ]

        ctx = EvictionContext(
            pressure=PressureLevel.NORMAL,
            trigger=EvictionReason.OVER_AGENT_CAP,
            requesting_agent_qos=QoSClass.PREMIUM,
        )

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=10, context=ctx)

        ids = [a.pid for a in selected]
        assert "spot-1" in ids
        assert "std-1" in ids
        assert "premium-1" not in ids

    def test_preemption_standard_only_evicts_spot(self):
        """Preemption with STANDARD requesting only evicts SPOT."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("spot-1", last_heartbeat=now, eviction_class=QoSClass.SPOT),
            _make_agent("std-1", last_heartbeat=now, eviction_class=QoSClass.STANDARD),
            _make_agent("premium-1", last_heartbeat=now, eviction_class=QoSClass.PREMIUM),
        ]

        ctx = EvictionContext(
            pressure=PressureLevel.NORMAL,
            trigger=EvictionReason.OVER_AGENT_CAP,
            requesting_agent_qos=QoSClass.STANDARD,
        )

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=10, context=ctx)

        ids = [a.pid for a in selected]
        assert ids == ["spot-1"]

    def test_preemption_spot_evicts_nobody(self):
        """Preemption with SPOT requesting evicts nobody (no lower class)."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("spot-1", last_heartbeat=now, eviction_class=QoSClass.SPOT),
            _make_agent("std-1", last_heartbeat=now, eviction_class=QoSClass.STANDARD),
        ]

        ctx = EvictionContext(
            pressure=PressureLevel.NORMAL,
            trigger=EvictionReason.OVER_AGENT_CAP,
            requesting_agent_qos=QoSClass.SPOT,
        )

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=10, context=ctx)

        assert selected == []

    def test_no_context_evicts_all_by_priority(self):
        """Without context, all agents are eligible ordered by priority."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("premium-1", last_heartbeat=now, eviction_class=QoSClass.PREMIUM),
            _make_agent("spot-1", last_heartbeat=now, eviction_class=QoSClass.SPOT),
        ]

        policy = QoSEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=10)

        assert selected[0].pid == "spot-1"
        assert selected[1].pid == "premium-1"
