"""Tests for CopilotOrchestrator (Issue #2761, Phase 2).

Tests the copilot/worker orchestration layer:
    - Delegation lifecycle (delegate → monitor → collect → cancel)
    - Permission inheritance (inherit-and-restrict model)
    - Budget enforcement (per-worker token caps)
    - Delivery policy configuration
    - Cancellation cascade
    - Fan-out delegation to multiple workers
"""

import asyncio
import fnmatch
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.a2a.models import TaskState
from nexus.bricks.a2a.task_manager import TaskManager
from nexus.contracts.agent_runtime_types import (
    AgentProcess,
    DelegationResult,
    DeliveryPolicy,
    ProcessAlreadyRunningError,
    ProcessState,
    WorkerConfig,
)
from nexus.system_services.agent_runtime.copilot_orchestrator import CopilotOrchestrator

# ======================================================================
# Stub ProcessManager — high-level ProcessManagerProtocol for orchestrator tests
# ======================================================================


class _StubProcessManager:
    """Stub PM implementing the high-level ProcessManagerProtocol.

    The real ProcessManager was refactored for kernel-level VFS protocol,
    but CopilotOrchestrator uses the high-level protocol from
    agent_runtime_types.py: spawn(agent_id, zone_id, *, parent_pid, metadata).
    """

    def __init__(self) -> None:
        self._processes: dict[str, AgentProcess] = {}
        self._agent_to_pid: dict[str, str] = {}
        self._counter = 0

    async def spawn(
        self,
        agent_id: str,
        zone_id: str,
        *,
        parent_pid: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentProcess:
        if agent_id in self._agent_to_pid:
            existing = self._processes.get(self._agent_to_pid[agent_id])
            if existing and existing.state == ProcessState.RUNNING:
                raise ProcessAlreadyRunningError(agent_id)

        self._counter += 1
        pid = f"pid-{self._counter}"
        proc = AgentProcess(
            pid=pid,
            agent_id=agent_id,
            zone_id=zone_id,
            state=ProcessState.RUNNING,
            parent_pid=parent_pid,
            metadata=metadata or {},
        )
        self._processes[pid] = proc
        self._agent_to_pid[agent_id] = pid
        return proc

    async def get_process(self, pid: str) -> AgentProcess | None:
        return self._processes.get(pid)

    async def terminate(self, pid: str, *, reason: str = "terminated") -> bool:  # noqa: ARG002
        proc = self._processes.get(pid)
        if proc is None:
            return False
        self._processes[pid] = AgentProcess(
            pid=proc.pid,
            agent_id=proc.agent_id,
            zone_id=proc.zone_id,
            state=ProcessState.ZOMBIE,
            parent_pid=proc.parent_pid,
            metadata=proc.metadata,
        )
        return True

    async def list_processes(
        self,
        *,
        zone_id: str | None = None,
        state: ProcessState | None = None,
    ) -> list[AgentProcess]:
        results = list(self._processes.values())
        if zone_id is not None:
            results = [p for p in results if p.zone_id == zone_id]
        if state is not None:
            results = [p for p in results if p.state == state]
        return results

    async def wait(self, pid: str) -> None:
        proc = self._processes.get(pid)
        if proc and proc.state == ProcessState.ZOMBIE:
            for agent_id, p in list(self._agent_to_pid.items()):
                if p == pid:
                    del self._agent_to_pid[agent_id]


def _make_mock_td() -> MagicMock:
    """Create a mock ToolDispatcher with manifest-based permission checking."""
    td = MagicMock()
    manifests: dict[str, Any] = {}
    td._manifests = manifests

    def _set_manifest(agent_id: str, manifest: Any) -> None:
        manifests[agent_id] = manifest

    async def _check_permission(
        tool_name: str,
        *,
        agent_id: str,
        zone_id: str,  # noqa: ARG001
    ) -> bool:
        manifest = manifests.get(agent_id)
        if manifest is None:
            return True
        for entry in manifest.entries:
            if fnmatch.fnmatch(tool_name, entry.tool_pattern):
                from nexus.contracts.access_manifest_types import ToolPermission

                return entry.permission == ToolPermission.ALLOW
        return True

    td.set_manifest = MagicMock(side_effect=_set_manifest)
    td.check_permission = AsyncMock(side_effect=_check_permission)
    td.register_handler = MagicMock()
    return td


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def pm() -> _StubProcessManager:
    return _StubProcessManager()


@pytest.fixture
def tm() -> TaskManager:
    return TaskManager()


@pytest.fixture
def td() -> MagicMock:
    return _make_mock_td()


@pytest.fixture
async def orchestrator(
    pm: _StubProcessManager, tm: TaskManager, td: MagicMock
) -> CopilotOrchestrator:
    return CopilotOrchestrator(
        process_manager=pm,
        task_manager=tm,
        tool_dispatcher=td,
    )


@pytest.fixture
async def copilot_pid(pm: _StubProcessManager) -> str:
    proc = await pm.spawn("copilot-1", "zone-1")
    return proc.pid


# ======================================================================
# Value type tests
# ======================================================================


class TestWorkerConfig:
    """Verify WorkerConfig frozen dataclass."""

    def test_defaults(self) -> None:
        config = WorkerConfig(agent_id="w-1", zone_id="z-1")
        assert config.tool_allowlist == ("*",)
        assert config.max_turns == 50
        assert config.budget_tokens is None
        assert config.delivery_policy == DeliveryPolicy.IMMEDIATE

    def test_custom(self) -> None:
        config = WorkerConfig(
            agent_id="w-1",
            zone_id="z-1",
            tool_allowlist=("vfs_read", "vfs_stat"),
            max_turns=10,
            budget_tokens=5000,
            delivery_policy=DeliveryPolicy.DEFERRED,
        )
        assert config.tool_allowlist == ("vfs_read", "vfs_stat")
        assert config.budget_tokens == 5000
        assert config.delivery_policy == DeliveryPolicy.DEFERRED

    def test_immutable(self) -> None:
        config = WorkerConfig(agent_id="w-1", zone_id="z-1")
        attr = "max_turns"
        with pytest.raises(AttributeError):
            setattr(config, attr, 10)


class TestDeliveryPolicy:
    """Verify DeliveryPolicy enum."""

    def test_values(self) -> None:
        assert DeliveryPolicy.IMMEDIATE == "immediate"
        assert DeliveryPolicy.DEFERRED == "deferred"
        assert DeliveryPolicy.ON_DEMAND == "on_demand"


class TestDelegationResult:
    """Verify DelegationResult frozen dataclass."""

    def test_creation(self) -> None:
        result = DelegationResult(
            task_id="task-1",
            worker_pid="proc-abc",
            worker_agent_id="worker-1",
        )
        assert result.task_id == "task-1"
        assert result.worker_pid == "proc-abc"
        assert result.worker_agent_id == "worker-1"

    def test_immutable(self) -> None:
        result = DelegationResult(task_id="t", worker_pid="p", worker_agent_id="w")
        attr = "task_id"
        with pytest.raises(AttributeError):
            setattr(result, attr, "new")


# ======================================================================
# Delegation lifecycle
# ======================================================================


class TestDelegation:
    """Test the delegate() → collect() lifecycle."""

    async def test_delegate_spawns_worker_and_creates_task(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        pm: _StubProcessManager,
        tm: TaskManager,
    ) -> None:
        """delegate() spawns a worker process and creates an A2A task."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Summarize /docs/readme.md",
            WorkerConfig(agent_id="worker-1", zone_id="zone-1"),
        )

        assert isinstance(result, DelegationResult)
        assert result.worker_agent_id == "worker-1"

        # Verify worker process was spawned
        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.state == ProcessState.RUNNING
        assert worker.parent_pid == copilot_pid

        # Verify A2A task was created
        task = await tm.get_task(result.task_id, zone_id="zone-1")
        assert task.status.state == TaskState.SUBMITTED

    async def test_delegate_with_budget(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        pm: _StubProcessManager,
    ) -> None:
        """Worker metadata includes budget_tokens."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Work",
            WorkerConfig(
                agent_id="worker-budget",
                zone_id="zone-1",
                budget_tokens=10_000,
            ),
        )

        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.metadata["budget_tokens"] == 10_000

    async def test_delegate_with_delivery_policy(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        tm: TaskManager,
    ) -> None:
        """Task metadata includes delivery policy."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Work",
            WorkerConfig(
                agent_id="worker-deferred",
                zone_id="zone-1",
                delivery_policy=DeliveryPolicy.DEFERRED,
            ),
        )

        task = await tm.get_task(result.task_id, zone_id="zone-1")
        assert task.metadata is not None
        assert task.metadata["delivery_policy"] == "deferred"

    async def test_collect_returns_task(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
    ) -> None:
        """collect() returns the A2A task after completion."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Work",
            WorkerConfig(agent_id="worker-collect", zone_id="zone-1"),
        )

        # Transition through FSM states so collect() unblocks
        await asyncio.sleep(0)  # let _run_worker transition SUBMITTED→WORKING
        await orchestrator.complete_task(result.task_id, zone_id="zone-1")

        task = await orchestrator.collect(result.task_id, zone_id="zone-1")
        assert task.id == result.task_id
        assert task.status.state == TaskState.COMPLETED


# ======================================================================
# Permission inheritance
# ======================================================================


class TestPermissionInheritance:
    """Test inherit-and-restrict security model."""

    async def test_worker_restricted_by_allowlist(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        td: MagicMock,
    ) -> None:
        """Worker with specific allowlist can only use those tools."""

        async def vfs_read() -> str:
            return "ok"

        async def vfs_write() -> str:
            return "ok"

        td.register_handler("vfs_read", vfs_read)
        td.register_handler("vfs_write", vfs_write)

        await orchestrator.delegate(
            copilot_pid,
            "Read-only work",
            WorkerConfig(
                agent_id="worker-ro",
                zone_id="zone-1",
                tool_allowlist=("vfs_read",),
            ),
        )

        # Worker can read but not write
        assert await td.check_permission("vfs_read", agent_id="worker-ro", zone_id="zone-1")
        assert not await td.check_permission("vfs_write", agent_id="worker-ro", zone_id="zone-1")

    async def test_worker_wildcard_allowlist_is_permissive(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        td: MagicMock,
    ) -> None:
        """Worker with wildcard allowlist can use all tools."""

        async def any_tool() -> str:
            return "ok"

        td.register_handler("any_tool", any_tool)

        await orchestrator.delegate(
            copilot_pid,
            "Unrestricted work",
            WorkerConfig(agent_id="worker-all", zone_id="zone-1"),
        )

        assert await td.check_permission("any_tool", agent_id="worker-all", zone_id="zone-1")

    async def test_manifest_created_by_copilot(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        td: MagicMock,
    ) -> None:
        """AccessManifest tracks the copilot that created the restriction."""
        await orchestrator.delegate(
            copilot_pid,
            "Work",
            WorkerConfig(
                agent_id="worker-audit",
                zone_id="zone-1",
                tool_allowlist=("vfs_read",),
            ),
        )

        # The manifest should exist on the dispatcher
        manifest = td._manifests.get("worker-audit")
        assert manifest is not None
        assert manifest.created_by == "copilot-1"

    async def test_glob_pattern_in_allowlist(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        td: MagicMock,
    ) -> None:
        """Glob patterns in allowlist work correctly."""

        async def nexus_read() -> str:
            return "ok"

        async def nexus_write() -> str:
            return "ok"

        async def vfs_stat() -> str:
            return "ok"

        td.register_handler("nexus_read", nexus_read)
        td.register_handler("nexus_write", nexus_write)
        td.register_handler("vfs_stat", vfs_stat)

        await orchestrator.delegate(
            copilot_pid,
            "Nexus-only work",
            WorkerConfig(
                agent_id="worker-glob",
                zone_id="zone-1",
                tool_allowlist=("nexus_*",),
            ),
        )

        assert await td.check_permission("nexus_read", agent_id="worker-glob", zone_id="zone-1")
        assert await td.check_permission("nexus_write", agent_id="worker-glob", zone_id="zone-1")
        assert not await td.check_permission("vfs_stat", agent_id="worker-glob", zone_id="zone-1")


# ======================================================================
# Cancellation
# ======================================================================


class TestCancellation:
    """Test cancellation of delegated tasks."""

    async def test_cancel_delegation(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        tm: TaskManager,
        pm: _StubProcessManager,
    ) -> None:
        """cancel() cancels the A2A task and terminates the worker."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Cancellable work",
            WorkerConfig(agent_id="worker-cancel", zone_id="zone-1"),
        )

        await orchestrator.cancel(result.task_id, zone_id="zone-1")

        # Task is cancelled
        task = await tm.get_task(result.task_id, zone_id="zone-1")
        assert task.status.state == TaskState.CANCELED

        # Worker is terminated (ZOMBIE, awaiting reap)
        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.state in {ProcessState.ZOMBIE, ProcessState.STOPPED}

    async def test_cancel_all_delegations(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        tm: TaskManager,
    ) -> None:
        """cancel_all() cancels all active delegations for a copilot."""
        for i in range(3):
            await orchestrator.delegate(
                copilot_pid,
                f"Work {i}",
                WorkerConfig(agent_id=f"worker-all-{i}", zone_id="zone-1"),
            )

        cancelled = await orchestrator.cancel_all(copilot_pid, zone_id="zone-1")
        assert cancelled == 3

    async def test_cancel_already_completed_is_safe(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        tm: TaskManager,
    ) -> None:
        """Cancelling a completed task is handled gracefully (no raise)."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Quick work",
            WorkerConfig(agent_id="worker-done", zone_id="zone-1"),
        )

        # Complete the task manually
        await tm.update_task_state(result.task_id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(result.task_id, TaskState.COMPLETED, zone_id="zone-1")

        # Cancel should not raise
        await orchestrator.cancel(result.task_id, zone_id="zone-1")

        # Task remains completed (not cancelled)
        task = await tm.get_task(result.task_id, zone_id="zone-1")
        assert task.status.state == TaskState.COMPLETED


# ======================================================================
# List delegations
# ======================================================================


class TestListDelegations:
    """Test listing active delegations."""

    async def test_list_delegations(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
    ) -> None:
        """list_delegations() returns all delegations for a copilot."""
        for i in range(3):
            await orchestrator.delegate(
                copilot_pid,
                f"Work {i}",
                WorkerConfig(agent_id=f"worker-list-{i}", zone_id="zone-1"),
            )

        delegations = await orchestrator.list_delegations(copilot_pid, zone_id="zone-1")
        assert len(delegations) == 3
        assert all(isinstance(d, DelegationResult) for d in delegations)

    async def test_list_delegations_empty(
        self,
        orchestrator: CopilotOrchestrator,
    ) -> None:
        """list_delegations() returns empty list for unknown copilot."""
        delegations = await orchestrator.list_delegations("unknown-pid", zone_id="zone-1")
        assert delegations == []


# ======================================================================
# Fan-out
# ======================================================================


class TestFanOut:
    """Test delegating to multiple workers."""

    async def test_fan_out_delegation(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
    ) -> None:
        """Copilot can delegate to multiple workers in parallel."""
        results = []
        for i in range(5):
            r = await orchestrator.delegate(
                copilot_pid,
                f"Subtask {i}",
                WorkerConfig(agent_id=f"fan-{i}", zone_id="zone-1"),
            )
            results.append(r)

        assert len(results) == 5
        pids = {r.worker_pid for r in results}
        assert len(pids) == 5  # All unique PIDs

    async def test_fan_out_partial_cancel(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        tm: TaskManager,
    ) -> None:
        """Cancel specific workers while others continue."""
        results = []
        for i in range(3):
            r = await orchestrator.delegate(
                copilot_pid,
                f"Work {i}",
                WorkerConfig(agent_id=f"fan-cancel-{i}", zone_id="zone-1"),
            )
            results.append(r)

        # Cancel only first worker
        await orchestrator.cancel(results[0].task_id, zone_id="zone-1")

        # First cancelled, others still submitted
        t0 = await tm.get_task(results[0].task_id, zone_id="zone-1")
        t1 = await tm.get_task(results[1].task_id, zone_id="zone-1")
        t2 = await tm.get_task(results[2].task_id, zone_id="zone-1")

        assert t0.status.state == TaskState.CANCELED
        assert t1.status.state == TaskState.SUBMITTED
        assert t2.status.state == TaskState.SUBMITTED


# ======================================================================
# Edge cases
# ======================================================================


class TestEdgeCases:
    """Edge case and robustness tests."""

    async def test_delegate_same_agent_twice_raises(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
    ) -> None:
        """Cannot delegate to the same agent if it's already running."""
        await orchestrator.delegate(
            copilot_pid,
            "First task",
            WorkerConfig(agent_id="worker-dup", zone_id="zone-1"),
        )

        with pytest.raises(ProcessAlreadyRunningError):
            await orchestrator.delegate(
                copilot_pid,
                "Second task",
                WorkerConfig(agent_id="worker-dup", zone_id="zone-1"),
            )

    async def test_delegate_with_custom_metadata(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        pm: _StubProcessManager,
    ) -> None:
        """Custom metadata is passed through to the worker process."""
        result = await orchestrator.delegate(
            copilot_pid,
            "Work",
            WorkerConfig(
                agent_id="worker-meta",
                zone_id="zone-1",
                metadata={"model": "gpt-4", "priority": "high"},
            ),
        )

        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.metadata["model"] == "gpt-4"
        assert worker.metadata["priority"] == "high"


# ======================================================================
# Concurrent multi-agent operations
# ======================================================================


class TestConcurrentOperations:
    """Test concurrent multi-agent scenarios with asyncio.gather."""

    async def test_concurrent_spawns_same_zone(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        pm: _StubProcessManager,
    ) -> None:
        """Concurrent delegations to different agents in the same zone succeed."""
        results = await asyncio.gather(
            *(
                orchestrator.delegate(
                    copilot_pid,
                    f"Work {i}",
                    WorkerConfig(agent_id=f"concurrent-{i}", zone_id="zone-1"),
                )
                for i in range(10)
            )
        )

        assert len(results) == 10
        pids = {r.worker_pid for r in results}
        assert len(pids) == 10  # All unique

        # All processes running in same zone
        procs = await pm.list_processes(zone_id="zone-1", state=ProcessState.RUNNING)
        # At least 10 workers + the copilot
        worker_pids = {p.pid for p in procs if p.parent_pid == copilot_pid}
        assert len(worker_pids) == 10

    async def test_concurrent_spawns_duplicate_agent_one_wins(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
    ) -> None:
        """Concurrent delegations for the same agent_id: exactly one succeeds."""
        tasks = [
            orchestrator.delegate(
                copilot_pid,
                f"Attempt {i}",
                WorkerConfig(agent_id="same-agent", zone_id="zone-1"),
            )
            for i in range(5)
        ]

        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        successes = [r for r in outcomes if isinstance(r, DelegationResult)]
        errors = [r for r in outcomes if isinstance(r, ProcessAlreadyRunningError)]

        assert len(successes) == 1
        assert len(errors) == 4

    async def test_concurrent_cancel_and_delegate(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
        tm: TaskManager,
    ) -> None:
        """Cancel and re-delegate can run concurrently without corruption."""
        # Initial delegation
        result = await orchestrator.delegate(
            copilot_pid,
            "Initial work",
            WorkerConfig(agent_id="cancel-redelegate", zone_id="zone-1"),
        )

        # Cancel the task
        await orchestrator.cancel(result.task_id, zone_id="zone-1")

        # After cancel, the agent's process is terminated, so we can delegate again
        # (need to clean up the agent_to_pid mapping first by waiting)
        worker = await orchestrator._pm.get_process(result.worker_pid)
        if worker and worker.state == ProcessState.ZOMBIE:
            await orchestrator._pm.wait(result.worker_pid)

        # Re-delegate should succeed
        result2 = await orchestrator.delegate(
            copilot_pid,
            "Re-delegated work",
            WorkerConfig(agent_id="cancel-redelegate", zone_id="zone-1"),
        )
        assert result2.worker_pid != result.worker_pid
        assert result2.task_id != result.task_id

    async def test_parent_terminate_while_child_delegating(
        self,
        pm: _StubProcessManager,
        tm: TaskManager,
        td: MagicMock,
    ) -> None:
        """Terminating a copilot while workers are being delegated."""
        copilot = await pm.spawn("copilot-terminate", "zone-1")
        orch = CopilotOrchestrator(
            process_manager=pm,
            task_manager=tm,
            tool_dispatcher=td,
        )

        # Delegate some workers
        results = []
        for i in range(3):
            r = await orch.delegate(
                copilot.pid,
                f"Work {i}",
                WorkerConfig(agent_id=f"child-{i}", zone_id="zone-1"),
            )
            results.append(r)

        # Terminate the copilot
        await pm.terminate(copilot.pid, reason="parent killed")

        # Verify copilot is zombie
        parent = await pm.get_process(copilot.pid)
        assert parent is not None
        assert parent.state == ProcessState.ZOMBIE

        # Children are still running (no automatic cascade in ProcessManager)
        for r in results:
            child = await pm.get_process(r.worker_pid)
            assert child is not None
            assert child.state == ProcessState.RUNNING

        # But cancel_all still works (cascading through orchestrator)
        cancelled = await orch.cancel_all(copilot.pid, zone_id="zone-1")
        assert cancelled == 3

    async def test_concurrent_cancel_all_idempotent(
        self,
        orchestrator: CopilotOrchestrator,
        copilot_pid: str,
    ) -> None:
        """Multiple concurrent cancel_all calls don't corrupt state."""
        for i in range(5):
            await orchestrator.delegate(
                copilot_pid,
                f"Work {i}",
                WorkerConfig(agent_id=f"cancel-all-{i}", zone_id="zone-1"),
            )

        # Fire multiple cancel_all concurrently
        results = await asyncio.gather(
            orchestrator.cancel_all(copilot_pid, zone_id="zone-1"),
            orchestrator.cancel_all(copilot_pid, zone_id="zone-1"),
            orchestrator.cancel_all(copilot_pid, zone_id="zone-1"),
        )

        # Total cancelled across all calls should be 5 (first call cancels, others are idempotent)
        total = sum(results)
        # Each call iterates the same list and attempts cancellation.
        # Already-cancelled tasks are handled gracefully, so total >= 5
        assert total >= 5

    async def test_heartbeat_during_state_transition(
        self,
        pm: _StubProcessManager,
    ) -> None:
        """Process state can be queried concurrently during transitions."""
        proc = await pm.spawn("heartbeat-agent", "zone-1")

        async def check_heartbeat() -> ProcessState:
            p = await pm.get_process(proc.pid)
            assert p is not None
            return p.state

        async def terminate_after_delay() -> bool:
            await asyncio.sleep(0.001)
            return await pm.terminate(proc.pid, reason="shutdown")

        # Run heartbeat checks concurrently with termination
        results = await asyncio.gather(
            check_heartbeat(),
            check_heartbeat(),
            terminate_after_delay(),
            check_heartbeat(),
        )

        # First two checks see RUNNING, third is the termination, fourth could be either
        assert results[0] == ProcessState.RUNNING
        assert results[1] == ProcessState.RUNNING
        assert results[2] is True  # Termination succeeded
        # Fourth check: either RUNNING (before termination) or ZOMBIE (after)
        assert results[3] in {ProcessState.RUNNING, ProcessState.ZOMBIE}


# ======================================================================
# LEGO tier validation (Issue #2761, Phase 2)
# ======================================================================


def _make_mock_orchestrator() -> tuple[CopilotOrchestrator, AsyncMock, TaskManager, AsyncMock]:
    """Build a CopilotOrchestrator with mock PM/TD and real TaskManager."""
    mock_pm = AsyncMock()
    # spawn returns a mock AgentProcess
    mock_pm.spawn = AsyncMock(
        side_effect=lambda agent_id, zone_id, **kw: AgentProcess(
            pid=f"pid-{agent_id}",
            agent_id=agent_id,
            zone_id=zone_id,
            state=ProcessState.RUNNING,
            parent_pid=kw.get("parent_pid"),
            metadata=kw.get("metadata", {}),
        )
    )
    mock_pm.get_process = AsyncMock(
        return_value=AgentProcess(
            pid="pid-copilot",
            agent_id="copilot-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
    )
    mock_pm.terminate = AsyncMock()

    tm = TaskManager()
    mock_td = MagicMock()
    mock_td.set_manifest = MagicMock()

    orch = CopilotOrchestrator(
        process_manager=mock_pm,
        task_manager=tm,
        tool_dispatcher=mock_td,
    )
    return orch, mock_pm, tm, mock_td


class TestDeliveryMechanisms:
    """Test DeliveryPolicy wiring: IMMEDIATE stream, ON_DEMAND collect, cancel unblocking."""

    async def test_delegate_stores_delivery_policy_in_result(self) -> None:
        """DelegationResult includes the delivery_policy from WorkerConfig."""
        orch, _, _, _ = _make_mock_orchestrator()
        result = await orch.delegate(
            "pid-copilot",
            "Work",
            WorkerConfig(
                agent_id="worker-policy",
                zone_id="zone-1",
                delivery_policy=DeliveryPolicy.ON_DEMAND,
            ),
        )
        assert result.delivery_policy == DeliveryPolicy.ON_DEMAND

    async def test_stream_yields_events_for_immediate_policy(self) -> None:
        """stream() yields pushed events then terminates on sentinel."""
        orch, _, _, _ = _make_mock_orchestrator()
        result = await orch.delegate(
            "pid-copilot",
            "Stream work",
            WorkerConfig(
                agent_id="worker-stream",
                zone_id="zone-1",
                delivery_policy=DeliveryPolicy.IMMEDIATE,
            ),
        )

        # Push events then signal completion (which pushes sentinel)
        await orch.push_event(result.task_id, {"step": 1})
        await orch.push_event(result.task_id, {"step": 2})
        await orch.complete_task(result.task_id, zone_id="zone-1")

        # stream() should yield the 2 events then stop
        events = []
        async for event in orch.stream(result.task_id, zone_id="zone-1"):
            events.append(event)

        assert events == [{"step": 1}, {"step": 2}]

    async def test_collect_awaits_completion_not_polls(self) -> None:
        """collect() uses asyncio.Event to wait, not polling."""
        orch, _, _, _ = _make_mock_orchestrator()
        result = await orch.delegate(
            "pid-copilot",
            "Collect work",
            WorkerConfig(agent_id="worker-collect-event", zone_id="zone-1"),
        )

        # Signal completion after a short delay
        async def _complete_later() -> None:
            await asyncio.sleep(0.01)
            await orch.complete_task(result.task_id, zone_id="zone-1")

        asyncio.create_task(_complete_later())

        # collect() should wait for the event, not just return immediately
        task = await orch.collect(result.task_id, zone_id="zone-1")
        assert task.status.state == TaskState.COMPLETED

    async def test_stream_raises_for_on_demand_policy(self) -> None:
        """stream() raises ValueError for ON_DEMAND delivery policy."""
        orch, _, _, _ = _make_mock_orchestrator()
        result = await orch.delegate(
            "pid-copilot",
            "On-demand work",
            WorkerConfig(
                agent_id="worker-on-demand",
                zone_id="zone-1",
                delivery_policy=DeliveryPolicy.ON_DEMAND,
            ),
        )

        with pytest.raises(ValueError, match="ON_DEMAND"):
            async for _ in orch.stream(result.task_id, zone_id="zone-1"):
                pass

    async def test_cancel_unblocks_collect(self) -> None:
        """cancel() sets the completion event so collect() unblocks."""
        orch, _, _, _ = _make_mock_orchestrator()
        result = await orch.delegate(
            "pid-copilot",
            "Cancel collect work",
            WorkerConfig(agent_id="worker-cancel-collect", zone_id="zone-1"),
        )

        # Cancel after a short delay
        async def _cancel_later() -> None:
            await asyncio.sleep(0.01)
            await orch.cancel(result.task_id, zone_id="zone-1")

        asyncio.create_task(_cancel_later())

        # collect() should return once cancel sets the event
        task = await orch.collect(result.task_id, zone_id="zone-1")
        assert task.status.state == TaskState.CANCELED

    async def test_cancel_unblocks_stream(self) -> None:
        """cancel() pushes sentinel to queue so stream() terminates."""
        orch, _, _, _ = _make_mock_orchestrator()
        result = await orch.delegate(
            "pid-copilot",
            "Cancel stream work",
            WorkerConfig(
                agent_id="worker-cancel-stream",
                zone_id="zone-1",
                delivery_policy=DeliveryPolicy.IMMEDIATE,
            ),
        )

        # Push one event then cancel
        await orch.push_event(result.task_id, {"partial": True})

        async def _cancel_later() -> None:
            await asyncio.sleep(0.01)
            await orch.cancel(result.task_id, zone_id="zone-1")

        asyncio.create_task(_cancel_later())

        events = []
        async for event in orch.stream(result.task_id, zone_id="zone-1"):
            events.append(event)

        assert events == [{"partial": True}]


# ======================================================================
# LEGO tier validation (Issue #2761, Phase 2)
# ======================================================================


class TestLEGOTierCompliance:
    """Verify CopilotOrchestrator has no module-level brick imports."""

    def test_no_bricks_module_level_import(self) -> None:
        """CopilotOrchestrator should not import nexus.bricks at module level.

        The LEGO architecture requires system_services (Tier 1) to use
        Protocol types from contracts, not concrete classes from bricks (Tier 2).
        Brick imports should only happen lazily inside method bodies.
        """
        import ast
        import importlib
        import inspect

        mod = importlib.import_module("nexus.system_services.agent_runtime.copilot_orchestrator")
        source = inspect.getsource(mod)
        tree = ast.parse(source)

        # Check top-level imports (not inside functions/methods)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("nexus.bricks"), (
                        f"Module-level import of brick: {alias.name}"
                    )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("nexus.bricks")
            ):
                pytest.fail(
                    f"Module-level 'from {node.module} import ...' violates LEGO tier boundary"
                )
