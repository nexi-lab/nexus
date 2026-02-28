"""Unit tests for EvictionManager (Issue #2170).

Tests cover:
- No eviction at normal pressure
- Eviction at critical pressure transitions agents to SUSPENDED
- Cooldown prevents rapid eviction cycles
- Checkpoint written before transition
- StaleAgentError caught gracefully (skipped)
- InvalidTransitionError caught gracefully (agent reconnected)
- Watermark band stops eviction at low pressure
- Manual eviction via evict_agent()
- _build_checkpoint produces correct data
"""

import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.agent_types import AgentRecord, AgentState, EvictionReason
from nexus.contracts.qos import AgentQoS, QoSClass
from nexus.lib.performance_tuning import EvictionTuning
from nexus.system_services.agents.eviction_manager import EvictionManager
from nexus.system_services.agents.eviction_policy import LRUEvictionPolicy, QoSEvictionPolicy
from nexus.system_services.agents.resource_monitor import PressureLevel


def _make_agent(
    agent_id: str,
    last_heartbeat: datetime | None = None,
    generation: int = 1,
) -> AgentRecord:
    """Create a minimal AgentRecord for testing."""
    now = datetime.now(UTC)
    return AgentRecord(
        agent_id=agent_id,
        owner_id="test-owner",
        zone_id=None,
        name=None,
        state=AgentState.CONNECTED,
        generation=generation,
        last_heartbeat=last_heartbeat or (now - timedelta(hours=1)),
        metadata=types.MappingProxyType({}),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def tuning():
    """Create EvictionTuning for testing."""
    return EvictionTuning(
        memory_high_watermark_pct=85,
        memory_low_watermark_pct=75,
        max_active_agents=100,
        eviction_batch_size=5,
        checkpoint_timeout_seconds=5.0,
        eviction_cooldown_seconds=60,
        max_concurrent_transitions=10,
    )


@pytest.fixture
def mock_registry():
    """Create a mock AgentRegistry."""
    registry = MagicMock()
    registry.list_eviction_candidates.return_value = []
    registry.count_connected_agents.return_value = 0
    registry.batch_checkpoint.return_value = 0
    registry.transition.return_value = MagicMock()
    return registry


@pytest.fixture
def mock_monitor():
    """Create a mock ResourceMonitor."""
    monitor = AsyncMock()
    monitor.check_pressure.return_value = PressureLevel.NORMAL
    return monitor


@pytest.fixture
def policy():
    """Create an LRUEvictionPolicy."""
    return LRUEvictionPolicy()


@pytest.fixture
def manager(mock_registry, mock_monitor, policy, tuning):
    """Create an EvictionManager with mocked dependencies."""
    return EvictionManager(
        registry=mock_registry,
        monitor=mock_monitor,
        policy=policy,
        tuning=tuning,
    )


class TestEvictionManager:
    """Tests for EvictionManager.run_cycle()."""

    @pytest.mark.asyncio
    async def test_no_eviction_at_normal_pressure(self, manager, mock_monitor):
        """No eviction when resource pressure is NORMAL."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.reason is EvictionReason.NORMAL_PRESSURE

    @pytest.mark.asyncio
    async def test_eviction_at_critical_pressure(self, manager, mock_registry, mock_monitor):
        """Agents transitioned to SUSPENDED at critical pressure."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent(f"agent-{i}") for i in range(3)]
        mock_registry.list_eviction_candidates.return_value = agents

        result = await manager.run_cycle()

        assert result.evicted == 3
        assert result.reason is EvictionReason.PRESSURE_CRITICAL
        assert mock_registry.batch_checkpoint.call_count == 1
        assert mock_registry.transition.call_count == 3

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_eviction(self, manager, mock_registry, mock_monitor):
        """Second cycle within cooldown returns cooldown reason."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        # First cycle succeeds
        result1 = await manager.run_cycle()
        assert result1.evicted == 1

        # Second cycle hits cooldown
        result2 = await manager.run_cycle()
        assert result2.evicted == 0
        assert result2.reason is EvictionReason.COOLDOWN

    @pytest.mark.asyncio
    async def test_checkpoint_written_before_transition(self, manager, mock_registry, mock_monitor):
        """Checkpoint data is saved before state transition."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        call_order = []
        mock_registry.batch_checkpoint.side_effect = lambda *a, **kw: call_order.append(
            "checkpoint"
        )

        def transition_side_effect(*a, **kw):
            call_order.append("transition")
            return MagicMock()

        mock_registry.transition.side_effect = transition_side_effect

        await manager.run_cycle()

        assert call_order == ["checkpoint", "transition"]

    @pytest.mark.asyncio
    async def test_stale_generation_skipped(self, manager, mock_registry, mock_monitor):
        """StaleAgentError is caught and agent is skipped."""
        from nexus.system_services.agents.agent_registry import StaleAgentError

        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1"), _make_agent("agent-2")]
        mock_registry.list_eviction_candidates.return_value = agents

        # First agent raises StaleAgentError, second succeeds
        mock_registry.transition.side_effect = [
            StaleAgentError("agent-1", 1),
            MagicMock(),
        ]

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_agent_reconnected_between_detect_and_evict(
        self, manager, mock_registry, mock_monitor
    ):
        """InvalidTransitionError caught when agent reconnected."""
        from nexus.system_services.agents.agent_registry import InvalidTransitionError

        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        mock_registry.transition.side_effect = InvalidTransitionError(
            "agent-1", AgentState.IDLE, AgentState.SUSPENDED
        )

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_unexpected_exception_caught(self, manager, mock_registry, mock_monitor):
        """Unexpected Exception in transition is caught and agent is skipped."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1"), _make_agent("agent-2")]
        mock_registry.list_eviction_candidates.return_value = agents

        # First agent raises unexpected error, second succeeds
        mock_registry.transition.side_effect = [
            RuntimeError("DB connection lost"),
            MagicMock(),
        ]

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_no_candidates_returns_early(self, manager, mock_registry, mock_monitor):
        """When no eviction candidates are found, returns early."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        mock_registry.list_eviction_candidates.return_value = []

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.reason is EvictionReason.NO_CANDIDATES

    @pytest.mark.asyncio
    async def test_warning_pressure_triggers_eviction(self, manager, mock_registry, mock_monitor):
        """Warning pressure (not just critical) triggers eviction."""
        mock_monitor.check_pressure.return_value = PressureLevel.WARNING
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.reason is EvictionReason.PRESSURE_WARNING

    @pytest.mark.asyncio
    async def test_over_cap_triggers_eviction(self, manager, mock_registry, mock_monitor):
        """Agent cap exceeded at normal pressure triggers eviction."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        # Cap is 100 in fixture, report 101
        mock_registry.count_connected_agents.return_value = 101
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.reason is EvictionReason.OVER_AGENT_CAP

    @pytest.mark.asyncio
    async def test_over_cap_skips_pressure_recheck(self, manager, mock_registry, mock_monitor):
        """Over-cap eviction skips post-eviction pressure re-check."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        mock_registry.count_connected_agents.return_value = 101
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        result = await manager.run_cycle()

        assert result.post_pressure == "normal"
        # check_pressure called once (initial), not twice (no re-check)
        assert mock_monitor.check_pressure.call_count == 1

    @pytest.mark.asyncio
    async def test_checkpoint_timeout_aborts_cycle(self, manager, mock_registry, mock_monitor):
        """Checkpoint timeout returns early without evicting."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        # batch_checkpoint is called via asyncio.to_thread, mock blocks
        # Use threading.Event so we can cancel quickly instead of sleeping 60s
        import threading

        stop = threading.Event()
        mock_registry.batch_checkpoint.side_effect = lambda *a, **kw: stop.wait(timeout=60)

        # Override tuning to have very short timeout
        manager._tuning = EvictionTuning(
            memory_high_watermark_pct=85,
            memory_low_watermark_pct=75,
            max_active_agents=100,
            eviction_batch_size=5,
            checkpoint_timeout_seconds=0.01,  # 10ms timeout
            eviction_cooldown_seconds=60,
            max_concurrent_transitions=10,
        )

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.reason is EvictionReason.CHECKPOINT_TIMEOUT
        # Transition should NOT have been called
        mock_registry.transition.assert_not_called()


class TestBuildCheckpoint:
    """Tests for EvictionManager._build_checkpoint() (Issue #12A)."""

    def test_build_checkpoint_all_fields(self):
        """_build_checkpoint produces all 4 required fields."""
        now = datetime.now(UTC)
        agent = _make_agent("agent-1", last_heartbeat=now, generation=5)

        checkpoint = EvictionManager._build_checkpoint(agent)

        assert checkpoint["state"] == "CONNECTED"
        assert checkpoint["generation"] == 5
        assert checkpoint["last_heartbeat"] == now.isoformat()
        assert isinstance(checkpoint["evicted_at"], float)
        assert len(checkpoint) == 4

    def test_build_checkpoint_none_heartbeat(self):
        """_build_checkpoint handles None last_heartbeat."""
        now = datetime.now(UTC)
        agent = AgentRecord(
            agent_id="agent-1",
            owner_id="test-owner",
            zone_id=None,
            name=None,
            state=AgentState.CONNECTED,
            generation=1,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )

        checkpoint = EvictionManager._build_checkpoint(agent)

        assert checkpoint["last_heartbeat"] is None
        assert checkpoint["state"] == "CONNECTED"
        assert checkpoint["generation"] == 1


class TestManualEviction:
    """Tests for EvictionManager.evict_agent()."""

    @pytest.mark.asyncio
    async def test_evict_connected_agent(self, manager, mock_registry):
        """evict_agent() checkpoints and transitions a CONNECTED agent."""
        agent = _make_agent("agent-1")
        mock_registry.get.return_value = agent

        result = await manager.evict_agent("agent-1")

        assert result.evicted == 1
        assert result.reason is EvictionReason.MANUAL
        mock_registry.checkpoint.assert_called_once()
        mock_registry.transition.assert_called_once()

    @pytest.mark.asyncio
    async def test_evict_nonexistent_agent_raises(self, manager, mock_registry):
        """evict_agent() raises ValueError for nonexistent agent."""
        mock_registry.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await manager.evict_agent("no-such")

    @pytest.mark.asyncio
    async def test_evict_non_connected_agent_raises(self, manager, mock_registry):
        """evict_agent() raises ValueError for non-CONNECTED agent."""
        now = datetime.now(UTC)
        agent = AgentRecord(
            agent_id="agent-1",
            owner_id="test-owner",
            zone_id=None,
            name=None,
            state=AgentState.IDLE,
            generation=1,
            last_heartbeat=now,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        mock_registry.get.return_value = agent

        with pytest.raises(ValueError, match="not CONNECTED"):
            await manager.evict_agent("agent-1")


# ---------------------------------------------------------------------------
# QoS-specific tests (Issue #2171)
# ---------------------------------------------------------------------------


def _make_qos_agent(
    agent_id: str,
    last_heartbeat: datetime | None = None,
    generation: int = 1,
    eviction_class: QoSClass = QoSClass.STANDARD,
) -> AgentRecord:
    """Create an AgentRecord with QoS for testing."""
    now = datetime.now(UTC)
    return AgentRecord(
        agent_id=agent_id,
        owner_id="test-owner",
        zone_id=None,
        name=None,
        state=AgentState.CONNECTED,
        generation=generation,
        last_heartbeat=last_heartbeat or (now - timedelta(hours=1)),
        metadata=types.MappingProxyType({}),
        created_at=now,
        updated_at=now,
        qos=AgentQoS(eviction_class=eviction_class),
    )


class TestTriggerImmediateCycle:
    """Tests for EvictionManager.trigger_immediate_cycle()."""

    def test_sets_urgent_event(self, mock_registry, mock_monitor, tuning):
        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            registry=mock_registry,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        assert not mgr.urgent_event.is_set()
        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        assert mgr.urgent_event.is_set()

    @pytest.mark.asyncio
    async def test_preemption_evicts_spot_for_premium(self, mock_registry, mock_monitor, tuning):
        """When premium triggers preemption, only spot agents are evicted."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        mock_registry.count_connected_agents.return_value = 100  # at cap

        spot_agent = _make_qos_agent("spot-1", eviction_class=QoSClass.SPOT)
        premium_agent = _make_qos_agent("premium-1", eviction_class=QoSClass.PREMIUM)
        mock_registry.list_eviction_candidates.return_value = [spot_agent, premium_agent]

        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            registry=mock_registry,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        # Trigger preemption
        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        result = await mgr.run_cycle()

        assert result.evicted == 1
        # The transition should only be called for spot, not premium
        call_args = mock_registry.transition.call_args_list
        transitioned_ids = [c[0][0] for c in call_args]
        assert "spot-1" in transitioned_ids
        assert "premium-1" not in transitioned_ids

    @pytest.mark.asyncio
    async def test_preemption_skips_cooldown(self, mock_registry, mock_monitor, tuning):
        """Preemption trigger bypasses cooldown."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_qos_agent("agent-1", eviction_class=QoSClass.SPOT)]
        mock_registry.list_eviction_candidates.return_value = agents

        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            registry=mock_registry,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        # First cycle puts us in cooldown
        result1 = await mgr.run_cycle()
        assert result1.evicted == 1

        # Now trigger preemption — should bypass cooldown
        mock_registry.list_eviction_candidates.return_value = [
            _make_qos_agent("agent-2", eviction_class=QoSClass.SPOT)
        ]
        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        result2 = await mgr.run_cycle()
        assert result2.evicted == 1  # Not blocked by cooldown

    @pytest.mark.asyncio
    async def test_urgent_event_cleared_after_cycle(self, mock_registry, mock_monitor, tuning):
        """Urgent event is cleared after run_cycle consumes it."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        mock_registry.count_connected_agents.return_value = 100

        mock_registry.list_eviction_candidates.return_value = [
            _make_qos_agent("spot-1", eviction_class=QoSClass.SPOT)
        ]

        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            registry=mock_registry,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        assert mgr.urgent_event.is_set()

        await mgr.run_cycle()

        assert not mgr.urgent_event.is_set()


class TestQoSEvictionManagerIntegration:
    """Tests for QoS-aware eviction flow through EvictionManager."""

    @pytest.mark.asyncio
    async def test_context_passed_to_policy(self, mock_registry, mock_monitor, tuning):
        """EvictionContext is built and passed to policy.select_candidates."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL

        agents = [_make_qos_agent("spot-1", eviction_class=QoSClass.SPOT)]
        mock_registry.list_eviction_candidates.return_value = agents

        # Use a tracking mock policy
        tracking_policy = MagicMock()
        tracking_policy.select_candidates.return_value = agents

        mgr = EvictionManager(
            registry=mock_registry,
            monitor=mock_monitor,
            policy=tracking_policy,
            tuning=tuning,
        )

        await mgr.run_cycle()

        # Verify context was passed
        call_args = tracking_policy.select_candidates.call_args
        assert call_args.kwargs.get("context") is not None
        ctx = call_args.kwargs["context"]
        assert ctx.pressure is PressureLevel.CRITICAL
        assert ctx.trigger is EvictionReason.PRESSURE_CRITICAL
