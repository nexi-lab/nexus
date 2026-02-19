"""Unit tests for EvictionManager (Issue #2170).

Tests cover:
- No eviction at normal pressure
- Eviction at critical pressure transitions agents to SUSPENDED
- Cooldown prevents rapid eviction cycles
- Checkpoint written before transition
- StaleAgentError caught gracefully (skipped)
- InvalidTransitionError caught gracefully (agent reconnected)
- Watermark band stops eviction at low pressure
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.agent_types import AgentRecord, AgentState
from nexus.core.performance_tuning import EvictionTuning
from nexus.services.agents.eviction_manager import EvictionManager
from nexus.services.agents.eviction_policy import LRUEvictionPolicy
from nexus.services.agents.resource_monitor import PressureLevel


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
        assert result.reason == "normal_pressure"

    @pytest.mark.asyncio
    async def test_eviction_at_critical_pressure(self, manager, mock_registry, mock_monitor):
        """Agents transitioned to SUSPENDED at critical pressure."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent(f"agent-{i}") for i in range(3)]
        mock_registry.list_eviction_candidates.return_value = agents

        result = await manager.run_cycle()

        assert result.evicted == 3
        assert "critical" in result.reason
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
        assert result2.reason == "cooldown"

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
        from nexus.services.agents.agent_registry import StaleAgentError

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
        from nexus.services.agents.agent_registry import InvalidTransitionError

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
    async def test_no_candidates_returns_early(self, manager, mock_registry, mock_monitor):
        """When no eviction candidates are found, returns early."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        mock_registry.list_eviction_candidates.return_value = []

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.reason == "no_candidates"

    @pytest.mark.asyncio
    async def test_warning_pressure_triggers_eviction(self, manager, mock_registry, mock_monitor):
        """Warning pressure (not just critical) triggers eviction."""
        mock_monitor.check_pressure.return_value = PressureLevel.WARNING
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert "warning" in result.reason

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
        assert result.reason == "over_agent_cap"

    @pytest.mark.asyncio
    async def test_checkpoint_timeout_aborts_cycle(self, manager, mock_registry, mock_monitor):
        """Checkpoint timeout returns early without evicting."""
        import asyncio

        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        agents = [_make_agent("agent-1")]
        mock_registry.list_eviction_candidates.return_value = agents

        # Simulate slow checkpoint by making batch_checkpoint block
        async def _slow_checkpoint(*_a, **_kw):
            await asyncio.sleep(60)

        # batch_checkpoint is called via asyncio.to_thread, but since we mock it,
        # we need to make the sync version sleep — use side_effect with time.sleep
        import time

        mock_registry.batch_checkpoint.side_effect = lambda *a, **kw: time.sleep(60)

        # Override tuning to have very short timeout
        manager._tuning = EvictionTuning(
            memory_high_watermark_pct=85,
            memory_low_watermark_pct=75,
            max_active_agents=100,
            eviction_batch_size=5,
            checkpoint_timeout_seconds=0.01,  # 10ms timeout
            eviction_cooldown_seconds=60,
        )

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.reason == "checkpoint_timeout"
        # Transition should NOT have been called
        mock_registry.transition.assert_not_called()
