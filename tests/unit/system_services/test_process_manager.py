"""TDD test scaffolding for ProcessManager (Issue #2761, Phase 1).

Tests define the expected behavior of ProcessManagerProtocol implementations.
Written RED-first: these tests will fail until the concrete implementation
is built in system_services/agent_runtime/.

Contract under test:
    ProcessManager.spawn()      — create + start agent process
    ProcessManager.terminate()  — stop a running process
    ProcessManager.wait()       — block until process exits
    ProcessManager.get_process() — lookup by PID
    ProcessManager.list_processes() — query with filters
    ProcessManager.checkpoint() — serialize to CAS
    ProcessManager.restore()    — deserialize from CAS

See: src/nexus/contracts/agent_runtime_types.py
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from nexus.contracts.agent_runtime_types import (
    PROCESS_TRANSITIONS,
    AgentProcess,
    CheckpointNotFoundError,
    ExitStatus,
    ProcessAlreadyRunningError,
    ProcessManagerProtocol,
    ProcessNotFoundError,
    ProcessState,
    validate_process_transition,
)

# ======================================================================
# Value type tests (these pass immediately — validate the contracts)
# ======================================================================


class TestProcessState:
    """Verify ProcessState enum and transition matrix."""

    def test_all_states_defined(self) -> None:
        assert set(ProcessState) == {
            ProcessState.CREATED,
            ProcessState.RUNNING,
            ProcessState.PAUSED,
            ProcessState.STOPPED,
            ProcessState.ZOMBIE,
        }

    def test_terminal_states_have_no_transitions(self) -> None:
        assert PROCESS_TRANSITIONS[ProcessState.STOPPED] == frozenset()
        assert PROCESS_TRANSITIONS[ProcessState.ZOMBIE] == frozenset()

    def test_every_state_has_transition_entry(self) -> None:
        for state in ProcessState:
            assert state in PROCESS_TRANSITIONS, f"Missing transition entry for {state}"

    @pytest.mark.parametrize(
        ("current", "target", "expected"),
        [
            # CREATED transitions
            (ProcessState.CREATED, ProcessState.RUNNING, True),
            (ProcessState.CREATED, ProcessState.ZOMBIE, True),
            (ProcessState.CREATED, ProcessState.PAUSED, False),
            (ProcessState.CREATED, ProcessState.STOPPED, False),
            # RUNNING transitions
            (ProcessState.RUNNING, ProcessState.PAUSED, True),
            (ProcessState.RUNNING, ProcessState.STOPPED, True),
            (ProcessState.RUNNING, ProcessState.ZOMBIE, True),
            (ProcessState.RUNNING, ProcessState.CREATED, False),
            # PAUSED transitions
            (ProcessState.PAUSED, ProcessState.RUNNING, True),
            (ProcessState.PAUSED, ProcessState.STOPPED, True),
            (ProcessState.PAUSED, ProcessState.ZOMBIE, True),
            (ProcessState.PAUSED, ProcessState.CREATED, False),
            # Terminal states
            (ProcessState.STOPPED, ProcessState.RUNNING, False),
            (ProcessState.ZOMBIE, ProcessState.RUNNING, False),
        ],
    )
    def test_validate_transition(
        self, current: ProcessState, target: ProcessState, expected: bool
    ) -> None:
        assert validate_process_transition(current, target) is expected

    def test_no_self_transitions(self) -> None:
        for state in ProcessState:
            assert validate_process_transition(state, state) is False


class TestAgentProcess:
    """Verify AgentProcess frozen dataclass."""

    def test_immutable(self) -> None:
        proc = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.CREATED,
        )
        attr = "state"
        with pytest.raises(AttributeError):
            setattr(proc, attr, ProcessState.RUNNING)

    def test_defaults(self) -> None:
        proc = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.CREATED,
        )
        assert proc.parent_pid is None
        assert proc.started_at is None
        assert proc.exit_status is None
        assert proc.turn_count == 0
        assert proc.metadata == {}

    def test_with_parent_pid(self) -> None:
        """Copilot/worker hierarchy: worker has parent_pid of copilot."""
        copilot = AgentProcess(
            pid="p-copilot",
            agent_id="copilot-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        worker = AgentProcess(
            pid="p-worker",
            agent_id="worker-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
            parent_pid=copilot.pid,
        )
        assert worker.parent_pid == "p-copilot"


class TestExitStatus:
    """Verify ExitStatus frozen dataclass."""

    def test_success_exit(self) -> None:
        status = ExitStatus(
            pid="p-1",
            exit_code=0,
            reason="completed",
            terminated_at=datetime.now(tz=UTC),
        )
        assert status.exit_code == 0

    def test_error_exit(self) -> None:
        status = ExitStatus(
            pid="p-1",
            exit_code=1,
            reason="max turns exceeded",
            terminated_at=datetime.now(tz=UTC),
        )
        assert status.exit_code == 1

    def test_signal_exit(self) -> None:
        """Negative exit code = killed by signal (Linux convention)."""
        status = ExitStatus(
            pid="p-1",
            exit_code=-9,
            reason="SIGKILL",
            terminated_at=datetime.now(tz=UTC),
        )
        assert status.exit_code < 0


class TestExceptions:
    """Verify exception types and attributes."""

    def test_process_not_found(self) -> None:
        err = ProcessNotFoundError("p-999")
        assert err.pid == "p-999"
        assert err.is_expected is True
        assert err.status_code == 404

    def test_process_already_running(self) -> None:
        err = ProcessAlreadyRunningError("agent-1")
        assert err.agent_id == "agent-1"
        assert err.is_expected is True
        assert err.status_code == 409

    def test_checkpoint_not_found(self) -> None:
        err = CheckpointNotFoundError("abc123deadbeef")
        assert err.checkpoint_hash == "abc123deadbeef"
        assert err.is_expected is True


# ======================================================================
# Protocol conformance (structural typing check)
# ======================================================================


class TestProtocolConformance:
    """Verify that a mock can satisfy ProcessManagerProtocol."""

    def test_mock_satisfies_protocol(self) -> None:
        """A properly configured mock is a structural match."""
        mock = AsyncMock(spec=ProcessManagerProtocol)
        assert isinstance(mock, ProcessManagerProtocol)


# ======================================================================
# Behavioral tests (RED — need real implementation)
# ======================================================================


class TestProcessManagerSpawn:
    """Tests for ProcessManager.spawn() — creating agent processes."""

    async def test_spawn_returns_process_in_running_state(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")

        assert isinstance(proc, AgentProcess)
        assert proc.agent_id == "agent-1"
        assert proc.zone_id == "zone-1"
        assert proc.state == ProcessState.RUNNING
        assert proc.started_at is not None
        assert proc.pid  # non-empty PID

    async def test_spawn_generates_unique_pids(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        p1 = await mgr.spawn("agent-1", "zone-1")
        p2 = await mgr.spawn("agent-2", "zone-1")
        assert p1.pid != p2.pid

    async def test_spawn_duplicate_raises_already_running(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        await mgr.spawn("agent-1", "zone-1")
        with pytest.raises(ProcessAlreadyRunningError):
            await mgr.spawn("agent-1", "zone-1")

    async def test_spawn_with_parent_pid(self) -> None:
        """Worker process tracks copilot as parent."""
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        copilot = await mgr.spawn("copilot-1", "zone-1")
        worker = await mgr.spawn("worker-1", "zone-1", parent_pid=copilot.pid)
        assert worker.parent_pid == copilot.pid


class TestProcessManagerTerminate:
    """Tests for ProcessManager.terminate() — stopping processes."""

    async def test_terminate_running_process(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        result = await mgr.terminate(proc.pid)

        assert result is True
        updated = await mgr.get_process(proc.pid)
        assert updated is not None
        assert updated.state in {ProcessState.STOPPED, ProcessState.ZOMBIE}

    async def test_terminate_nonexistent_raises(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        with pytest.raises(ProcessNotFoundError):
            await mgr.terminate("p-nonexistent")

    async def test_terminate_already_stopped_returns_false(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        await mgr.terminate(proc.pid)
        result = await mgr.terminate(proc.pid)
        assert result is False


class TestProcessManagerWait:
    """Tests for ProcessManager.wait() — blocking until exit."""

    async def test_wait_returns_exit_status(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        await mgr.terminate(proc.pid)
        status = await mgr.wait(proc.pid)

        assert isinstance(status, ExitStatus)
        assert status.pid == proc.pid
        assert status.terminated_at is not None

    async def test_wait_timeout_raises(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        with pytest.raises(TimeoutError):
            await mgr.wait(proc.pid, timeout=0.01)

    async def test_wait_reaps_zombie(self) -> None:
        """After wait(), zombie process is cleaned up."""
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        await mgr.terminate(proc.pid)
        await mgr.wait(proc.pid)

        # Zombie should be reaped
        result = await mgr.get_process(proc.pid)
        assert result is None or result.state == ProcessState.STOPPED


class TestProcessManagerQuery:
    """Tests for get_process() and list_processes()."""

    async def test_get_process_returns_none_for_unknown(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        assert await mgr.get_process("p-unknown") is None

    async def test_list_processes_empty(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        assert await mgr.list_processes() == []

    async def test_list_processes_filters_by_zone(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        await mgr.spawn("agent-1", "zone-a")
        await mgr.spawn("agent-2", "zone-b")

        zone_a = await mgr.list_processes(zone_id="zone-a")
        assert len(zone_a) == 1
        assert zone_a[0].zone_id == "zone-a"

    async def test_list_processes_filters_by_state(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        await mgr.spawn("agent-1", "zone-1")
        p2 = await mgr.spawn("agent-2", "zone-1")
        await mgr.terminate(p2.pid)

        running = await mgr.list_processes(state=ProcessState.RUNNING)
        assert len(running) == 1
        assert running[0].agent_id == "agent-1"


class TestProcessManagerCheckpoint:
    """Tests for checkpoint/restore — CAS-backed state persistence."""

    async def test_checkpoint_returns_hash(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        hash_ = await mgr.checkpoint(proc.pid)

        assert isinstance(hash_, str)
        assert len(hash_) > 0

    async def test_checkpoint_pauses_process(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        await mgr.checkpoint(proc.pid)

        updated = await mgr.get_process(proc.pid)
        assert updated is not None
        assert updated.state == ProcessState.PAUSED

    async def test_restore_creates_new_process(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        hash_ = await mgr.checkpoint(proc.pid)

        restored = await mgr.restore(hash_, zone_id="zone-1")
        assert isinstance(restored, AgentProcess)
        assert restored.agent_id == "agent-1"
        assert restored.state == ProcessState.RUNNING

    async def test_restore_nonexistent_raises(self) -> None:
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        with pytest.raises(CheckpointNotFoundError):
            await mgr.restore("nonexistent-hash", zone_id="zone-1")

    async def test_checkpoint_preserves_turn_count(self) -> None:
        """Restored process should retain the turn count from checkpoint."""
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        mgr = ProcessManager()
        proc = await mgr.spawn("agent-1", "zone-1")
        # Simulate turns by updating metadata (implementation detail)
        hash_ = await mgr.checkpoint(proc.pid)
        restored = await mgr.restore(hash_, zone_id="zone-1")
        assert restored.turn_count == proc.turn_count
