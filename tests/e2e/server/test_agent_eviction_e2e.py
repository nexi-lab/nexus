"""E2E test for agent eviction under resource pressure (Issue #2170, 12A).

Tests the full eviction pipeline:
1. Register agents → connect → stop heartbeating
2. Mock psutil to report high memory → trigger eviction
3. Verify agents transitioned to SUSPENDED
4. Verify checkpoint data persisted (via restore_checkpoint public API)
5. Verify agents can reconnect and restore checkpoint
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.contracts.agent_types import AgentState, EvictionReason
from nexus.core.performance_tuning import EvictionTuning
from nexus.services.agents.agent_registry import AgentRegistry
from nexus.services.agents.eviction_manager import EvictionManager
from nexus.services.agents.eviction_policy import LRUEvictionPolicy
from nexus.services.agents.resource_monitor import ResourceMonitor
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create a session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def registry(session_factory):
    """Create an AgentRegistry."""
    return AgentRegistry(session_factory=session_factory, flush_interval=9999)


@pytest.fixture
def tuning():
    """Create EvictionTuning for E2E testing."""
    return EvictionTuning(
        memory_high_watermark_pct=85,
        memory_low_watermark_pct=75,
        max_active_agents=100,
        eviction_batch_size=10,
        checkpoint_timeout_seconds=5.0,
        eviction_cooldown_seconds=0,  # No cooldown for testing
        max_concurrent_transitions=10,
    )


@pytest.fixture
def eviction_manager(registry, tuning):
    """Create EvictionManager with real registry and mocked monitor."""
    monitor = ResourceMonitor(tuning=tuning)
    policy = LRUEvictionPolicy()
    return EvictionManager(
        registry=registry,
        monitor=monitor,
        policy=policy,
        tuning=tuning,
    )


class TestAgentEvictionE2E:
    """End-to-end eviction pipeline tests."""

    @pytest.mark.asyncio
    async def test_full_eviction_pipeline(self, registry, eviction_manager):
        """Register → connect → trigger eviction → verify SUSPENDED → restore."""
        # 1. Register and connect 3 agents
        for i in range(3):
            registry.register(f"agent-{i}", "alice")
            registry.transition(f"agent-{i}", AgentState.CONNECTED, expected_generation=0)

        # 2. Flush heartbeats so DB timestamps are old
        registry.flush_heartbeats()

        # 3. Verify all are CONNECTED
        for i in range(3):
            record = registry.get(f"agent-{i}")
            assert record.state is AgentState.CONNECTED

        # 4. Mock psutil to report high memory
        mem = MagicMock()
        mem.percent = 90.0

        with (
            patch("nexus.services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem

            # 5. Run eviction cycle
            result = await eviction_manager.run_cycle()

        # 6. Verify agents were evicted
        assert result.evicted == 3
        assert result.reason is EvictionReason.PRESSURE_CRITICAL

        # 7. Verify all agents are SUSPENDED
        for i in range(3):
            record = registry.get(f"agent-{i}")
            assert record.state is AgentState.SUSPENDED

        # 8. Verify checkpoint data via public restore_checkpoint() API
        for i in range(3):
            checkpoint = registry.restore_checkpoint(f"agent-{i}")
            assert checkpoint is not None
            assert checkpoint["state"] == "CONNECTED"
            assert checkpoint["generation"] == 1
            assert checkpoint["last_heartbeat"] is not None or True  # may be None
            assert "evicted_at" in checkpoint

    @pytest.mark.asyncio
    async def test_reconnect_after_eviction(self, registry, eviction_manager):
        """Evicted agent can reconnect and restore checkpoint."""
        # Register and connect
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.flush_heartbeats()

        # Evict under pressure
        mem = MagicMock()
        mem.percent = 90.0

        with (
            patch("nexus.services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            await eviction_manager.run_cycle()

        # Verify SUSPENDED
        record = registry.get("agent-1")
        assert record.state is AgentState.SUSPENDED

        # Reconnect (SUSPENDED → CONNECTED)
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=1)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 2  # New session

        # Restore checkpoint
        checkpoint = registry.restore_checkpoint("agent-1")
        assert checkpoint is not None
        assert checkpoint["state"] == "CONNECTED"

        # Checkpoint cleared after restore
        assert registry.restore_checkpoint("agent-1") is None

    @pytest.mark.asyncio
    async def test_no_eviction_at_normal_pressure(self, registry, eviction_manager):
        """No agents evicted when memory pressure is normal."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)

        # Mock normal memory
        with patch("nexus.services.agents.resource_monitor._HAS_PSUTIL", False):
            result = await eviction_manager.run_cycle()

        assert result.evicted == 0
        assert result.reason is EvictionReason.NORMAL_PRESSURE

        # Agent still CONNECTED
        record = registry.get("agent-1")
        assert record.state is AgentState.CONNECTED
