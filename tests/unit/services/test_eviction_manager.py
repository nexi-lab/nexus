"""Unit tests for EvictionManager (Issue #2170).

Tests cover:
- No eviction at normal pressure
- Eviction at critical pressure sends SIGSTOP to processes
- Cooldown prevents rapid eviction cycles
- Stale generation detected via CAS check (skipped)
- InvalidTransitionError caught gracefully (agent reconnected)
- Watermark band stops eviction at low pressure
- Manual eviction via evict_agent()
- _build_checkpoint produces correct data
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nexus.contracts.agent_types import EvictionReason
from nexus.contracts.process_types import (
    ExternalProcessInfo,
    InvalidTransitionError,
    ProcessDescriptor,
    ProcessKind,
    ProcessState,
)
from nexus.contracts.qos import QoSClass
from nexus.lib.performance_tuning import EvictionTuning
from nexus.system_services.agents.eviction_manager import EvictionManager
from nexus.system_services.agents.eviction_policy import LRUEvictionPolicy, QoSEvictionPolicy
from nexus.system_services.agents.resource_monitor import PressureLevel


def _make_process(
    pid: str,
    generation: int = 1,
    last_heartbeat: datetime | None = None,
) -> ProcessDescriptor:
    """Create a minimal ProcessDescriptor for testing."""
    now = datetime.now(UTC)
    return ProcessDescriptor(
        pid=pid,
        ppid=None,
        name=pid,
        kind=ProcessKind.UNMANAGED,
        state=ProcessState.RUNNING,
        owner_id="test-owner",
        zone_id="test-zone",
        generation=generation,
        created_at=now,
        updated_at=now,
        labels={},
        external_info=ExternalProcessInfo(
            connection_id=pid,
            last_heartbeat=last_heartbeat,
        )
        if last_heartbeat
        else None,
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
def mock_process_table():
    """Create a mock ProcessTable."""
    pt = MagicMock()
    pt.list_by_priority.return_value = []
    pt.count_by_state.return_value = 0
    pt.get.return_value = None
    pt.signal.return_value = None
    return pt


@pytest.fixture
def mock_monitor():
    """Create a mock ResourceMonitor."""
    from unittest.mock import AsyncMock

    monitor = AsyncMock()
    monitor.check_pressure.return_value = PressureLevel.NORMAL
    return monitor


@pytest.fixture
def policy():
    """Create an LRUEvictionPolicy."""
    return LRUEvictionPolicy()


@pytest.fixture
def manager(mock_process_table, mock_monitor, policy, tuning):
    """Create an EvictionManager with mocked dependencies."""
    return EvictionManager(
        process_table=mock_process_table,
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
    async def test_eviction_at_critical_pressure(self, manager, mock_process_table, mock_monitor):
        """Processes receive SIGSTOP at critical pressure."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        processes = [_make_process(f"agent-{i}") for i in range(3)]
        mock_process_table.list_by_priority.return_value = processes
        # CAS check: get() returns the same descriptor for each pid
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        result = await manager.run_cycle()

        assert result.evicted == 3
        assert result.reason is EvictionReason.PRESSURE_CRITICAL
        assert mock_process_table.signal.call_count == 3
        assert mock_process_table.get.call_count == 3

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_eviction(
        self, manager, mock_process_table, mock_monitor
    ):
        """Second cycle within cooldown returns cooldown reason."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        processes = [_make_process("agent-1")]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        # First cycle succeeds
        result1 = await manager.run_cycle()
        assert result1.evicted == 1

        # Second cycle hits cooldown
        result2 = await manager.run_cycle()
        assert result2.evicted == 0
        assert result2.reason is EvictionReason.COOLDOWN

    @pytest.mark.asyncio
    async def test_stale_generation_skipped(self, manager, mock_process_table, mock_monitor):
        """Stale generation detected via CAS check — agent is skipped."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        processes = [_make_process("agent-1"), _make_process("agent-2")]
        mock_process_table.list_by_priority.return_value = processes

        # First agent has stale generation (get returns different gen),
        # second agent succeeds
        stale_agent = _make_process("agent-1", generation=99)

        def get_side_effect(pid):
            if pid == "agent-1":
                return stale_agent  # different generation → CAS failure
            return next((p for p in processes if p.pid == pid), None)

        mock_process_table.get.side_effect = get_side_effect

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_agent_reconnected_between_detect_and_evict(
        self, manager, mock_process_table, mock_monitor
    ):
        """InvalidTransitionError caught when agent reconnected."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        processes = [_make_process("agent-1")]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        mock_process_table.signal.side_effect = InvalidTransitionError(
            "agent-1 already reconnected"
        )

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_unexpected_exception_caught(self, manager, mock_process_table, mock_monitor):
        """Unexpected Exception in transition is caught and agent is skipped."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        processes = [_make_process("agent-1"), _make_process("agent-2")]
        mock_process_table.list_by_priority.return_value = processes

        call_count = {"n": 0}

        def get_side_effect(pid):
            return next((p for p in processes if p.pid == pid), None)

        mock_process_table.get.side_effect = get_side_effect

        # First agent raises unexpected error, second succeeds
        def signal_side_effect(pid, sig):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("DB connection lost")
            return None

        mock_process_table.signal.side_effect = signal_side_effect

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_no_candidates_returns_early(self, manager, mock_process_table, mock_monitor):
        """When no eviction candidates are found, returns early."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        mock_process_table.list_by_priority.return_value = []

        result = await manager.run_cycle()

        assert result.evicted == 0
        assert result.reason is EvictionReason.NO_CANDIDATES

    @pytest.mark.asyncio
    async def test_warning_pressure_triggers_eviction(
        self, manager, mock_process_table, mock_monitor
    ):
        """Warning pressure (not just critical) triggers eviction."""
        mock_monitor.check_pressure.return_value = PressureLevel.WARNING
        processes = [_make_process("agent-1")]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.reason is EvictionReason.PRESSURE_WARNING

    @pytest.mark.asyncio
    async def test_over_cap_triggers_eviction(self, manager, mock_process_table, mock_monitor):
        """Agent cap exceeded at normal pressure triggers eviction."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        # Cap is 100 in fixture, report 101
        mock_process_table.count_by_state.return_value = 101
        processes = [_make_process("agent-1")]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        result = await manager.run_cycle()

        assert result.evicted == 1
        assert result.reason is EvictionReason.OVER_AGENT_CAP

    @pytest.mark.asyncio
    async def test_over_cap_skips_pressure_recheck(self, manager, mock_process_table, mock_monitor):
        """Over-cap eviction skips post-eviction pressure re-check."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        mock_process_table.count_by_state.return_value = 101
        processes = [_make_process("agent-1")]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        result = await manager.run_cycle()

        assert result.post_pressure == "normal"
        # check_pressure called once (initial), not twice (no re-check)
        assert mock_monitor.check_pressure.call_count == 1


class TestBuildCheckpoint:
    """Tests for EvictionManager._build_checkpoint() (Issue #12A)."""

    def test_build_checkpoint_all_fields(self):
        """_build_checkpoint produces all 4 required fields."""
        now = datetime.now(UTC)
        process = _make_process("agent-1", last_heartbeat=now, generation=5)

        checkpoint = EvictionManager._build_checkpoint(process)

        assert checkpoint["state"] == str(ProcessState.RUNNING)
        assert checkpoint["generation"] == 5
        assert checkpoint["last_heartbeat"] == now.isoformat()
        assert isinstance(checkpoint["evicted_at"], float)
        assert len(checkpoint) == 4

    def test_build_checkpoint_none_heartbeat(self):
        """_build_checkpoint handles None last_heartbeat (no external_info)."""
        process = _make_process("agent-1")

        checkpoint = EvictionManager._build_checkpoint(process)

        assert checkpoint["last_heartbeat"] is None
        assert checkpoint["state"] == str(ProcessState.RUNNING)
        assert checkpoint["generation"] == 1


class TestManualEviction:
    """Tests for EvictionManager.evict_agent()."""

    @pytest.mark.asyncio
    async def test_evict_connected_agent(self, manager, mock_process_table):
        """evict_agent() sends SIGSTOP to a RUNNING process."""
        process = _make_process("agent-1")
        mock_process_table.get.return_value = process

        result = await manager.evict_agent("agent-1")

        assert result.evicted == 1
        assert result.reason is EvictionReason.MANUAL
        mock_process_table.signal.assert_called_once()
        # get() called twice: once for initial lookup, once for CAS check
        assert mock_process_table.get.call_count == 2

    @pytest.mark.asyncio
    async def test_evict_nonexistent_agent_raises(self, manager, mock_process_table):
        """evict_agent() raises ValueError for nonexistent agent."""
        mock_process_table.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await manager.evict_agent("no-such")

    @pytest.mark.asyncio
    async def test_evict_non_running_agent_raises(self, manager, mock_process_table):
        """evict_agent() raises ValueError for non-RUNNING process."""
        now = datetime.now(UTC)
        process = ProcessDescriptor(
            pid="agent-1",
            ppid=None,
            name="agent-1",
            owner_id="test-owner",
            zone_id="test-zone",
            kind=ProcessKind.UNMANAGED,
            state=ProcessState.SLEEPING,
            generation=1,
            created_at=now,
            updated_at=now,
            labels={},
        )
        mock_process_table.get.return_value = process

        with pytest.raises(ValueError, match="not RUNNING"):
            await manager.evict_agent("agent-1")


# ---------------------------------------------------------------------------
# QoS-specific tests (Issue #2171)
# ---------------------------------------------------------------------------


def _make_qos_process(
    pid: str,
    last_heartbeat: datetime | None = None,
    generation: int = 1,
    eviction_class: QoSClass = QoSClass.STANDARD,
) -> ProcessDescriptor:
    """Create a ProcessDescriptor with eviction_class label for QoS testing."""
    now = datetime.now(UTC)
    hb = last_heartbeat or (now - timedelta(hours=1))
    return ProcessDescriptor(
        pid=pid,
        ppid=None,
        name=pid,
        kind=ProcessKind.UNMANAGED,
        state=ProcessState.RUNNING,
        owner_id="test-owner",
        zone_id="test-zone",
        generation=generation,
        created_at=now,
        updated_at=now,
        labels={"eviction_class": eviction_class.value},
        external_info=ExternalProcessInfo(
            connection_id=pid,
            last_heartbeat=hb,
        ),
    )


class TestTriggerImmediateCycle:
    """Tests for EvictionManager.trigger_immediate_cycle()."""

    def test_sets_urgent_event(self, mock_process_table, mock_monitor, tuning):
        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            process_table=mock_process_table,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        assert not mgr.urgent_event.is_set()
        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        assert mgr.urgent_event.is_set()

    @pytest.mark.asyncio
    async def test_preemption_evicts_spot_for_premium(
        self, mock_process_table, mock_monitor, tuning
    ):
        """When premium triggers preemption, only spot agents are evicted."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        mock_process_table.count_by_state.return_value = 100  # at cap

        spot_process = _make_qos_process("spot-1", eviction_class=QoSClass.SPOT)
        premium_process = _make_qos_process("premium-1", eviction_class=QoSClass.PREMIUM)
        mock_process_table.list_by_priority.return_value = [spot_process, premium_process]

        # CAS check: get() returns the process for signal
        def get_side_effect(pid):
            for p in [spot_process, premium_process]:
                if p.pid == pid:
                    return p
            return None

        mock_process_table.get.side_effect = get_side_effect

        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            process_table=mock_process_table,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        # Trigger preemption
        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        result = await mgr.run_cycle()

        assert result.evicted == 1
        # The signal should only be called for spot, not premium
        call_args = mock_process_table.signal.call_args_list
        signalled_pids = [c[0][0] for c in call_args]
        assert "spot-1" in signalled_pids
        assert "premium-1" not in signalled_pids

    @pytest.mark.asyncio
    async def test_preemption_skips_cooldown(self, mock_process_table, mock_monitor, tuning):
        """Preemption trigger bypasses cooldown."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL
        processes = [_make_qos_process("agent-1", eviction_class=QoSClass.SPOT)]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            process_table=mock_process_table,
            monitor=mock_monitor,
            policy=policy,
            tuning=tuning,
        )

        # First cycle puts us in cooldown
        result1 = await mgr.run_cycle()
        assert result1.evicted == 1

        # Now trigger preemption -- should bypass cooldown
        new_processes = [_make_qos_process("agent-2", eviction_class=QoSClass.SPOT)]
        mock_process_table.list_by_priority.return_value = new_processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in new_processes if p.pid == pid), None
        )
        mgr.trigger_immediate_cycle(QoSClass.PREMIUM)
        result2 = await mgr.run_cycle()
        assert result2.evicted == 1  # Not blocked by cooldown

    @pytest.mark.asyncio
    async def test_urgent_event_cleared_after_cycle(self, mock_process_table, mock_monitor, tuning):
        """Urgent event is cleared after run_cycle consumes it."""
        mock_monitor.check_pressure.return_value = PressureLevel.NORMAL
        mock_process_table.count_by_state.return_value = 100

        processes = [_make_qos_process("spot-1", eviction_class=QoSClass.SPOT)]
        mock_process_table.list_by_priority.return_value = processes
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        policy = QoSEvictionPolicy()
        mgr = EvictionManager(
            process_table=mock_process_table,
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
    async def test_context_passed_to_policy(self, mock_process_table, mock_monitor, tuning):
        """EvictionContext is built and passed to policy.select_candidates."""
        mock_monitor.check_pressure.return_value = PressureLevel.CRITICAL

        processes = [_make_qos_process("spot-1", eviction_class=QoSClass.SPOT)]
        mock_process_table.list_by_priority.return_value = processes

        # Use a tracking mock policy
        tracking_policy = MagicMock()
        tracking_policy.select_candidates.return_value = processes

        # CAS check
        mock_process_table.get.side_effect = lambda pid: next(
            (p for p in processes if p.pid == pid), None
        )

        mgr = EvictionManager(
            process_table=mock_process_table,
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
