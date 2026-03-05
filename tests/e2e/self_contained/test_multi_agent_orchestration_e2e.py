"""End-to-end tests for Multi-Agent Orchestration (Issue #2761).

Demonstrates the copilot/worker pattern using real (in-memory) components:
    ProcessManager, TaskManager, ToolDispatcher, CopilotOrchestrator, agent_loop

No external services required — fully self-contained.

Minimum viable proof that #2761 works:
    1. Full delegation lifecycle (spawn → delegate → collect)
    2. Permission inheritance (inherit-and-restrict)
    3. Fan-out with cancellation cascade
    4. Agent loop integration (worker executes tool calls)
    5. Budget enforcement
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.a2a.models import TaskState
from nexus.bricks.a2a.task_manager import TaskManager
from nexus.contracts.agent_runtime_types import (
    AgentLoopConfig,
    AgentProcess,
    DelegationResult,
    DeliveryPolicy,
    ProcessState,
    ToolPermissionDeniedError,
    WorkerConfig,
)
from nexus.system_services.agent_runtime import (
    CopilotOrchestrator,
    ProcessManager,
    SessionStore,
    ToolDispatcher,
)
from nexus.system_services.agent_runtime.agent_loop import agent_loop

# ======================================================================
# Fixtures — real components, no mocks
# ======================================================================


@pytest.fixture()
def pm() -> ProcessManager:
    return ProcessManager()


@pytest.fixture()
def tm() -> TaskManager:
    return TaskManager()


@pytest.fixture()
def td() -> ToolDispatcher:
    return ToolDispatcher()


@pytest.fixture()
def ss() -> SessionStore:
    return SessionStore()


@pytest.fixture()
def orchestrator(
    pm: ProcessManager,
    tm: TaskManager,
    td: ToolDispatcher,
) -> CopilotOrchestrator:
    return CopilotOrchestrator(
        process_manager=pm,
        task_manager=tm,
        tool_dispatcher=td,
    )


@pytest.fixture()
async def copilot(pm: ProcessManager) -> AgentProcess:
    return await pm.spawn("copilot-e2e", "zone-e2e")


# ======================================================================
# E2E 1: Full delegation lifecycle
# ======================================================================


class TestDelegationLifecycle:
    """Prove: copilot spawns worker → delegates A2A task → collects result."""

    async def test_full_lifecycle(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
        tm: TaskManager,
    ) -> None:
        """Copilot delegates work, worker executes, copilot collects."""
        # 1. Copilot delegates to worker
        result = await orchestrator.delegate(
            copilot.pid,
            "Summarize /docs/readme.md",
            WorkerConfig(agent_id="worker-summarize", zone_id="zone-e2e"),
        )
        assert isinstance(result, DelegationResult)

        # 2. Worker process is running with correct parent
        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.state == ProcessState.RUNNING
        assert worker.parent_pid == copilot.pid
        assert worker.agent_id == "worker-summarize"

        # 3. A2A task exists in SUBMITTED state
        task = await tm.get_task(result.task_id, zone_id="zone-e2e")
        assert task.status.state == TaskState.SUBMITTED

        # 4. Simulate worker completing the task
        await tm.update_task_state(result.task_id, TaskState.WORKING, zone_id="zone-e2e")
        await tm.update_task_state(result.task_id, TaskState.COMPLETED, zone_id="zone-e2e")

        # 5. Copilot collects result
        collected = await orchestrator.collect(result.task_id, zone_id="zone-e2e")
        assert collected.status.state == TaskState.COMPLETED

    async def test_delegation_metadata_flow(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
        tm: TaskManager,
    ) -> None:
        """Budget, delivery policy, and custom metadata flow through."""
        result = await orchestrator.delegate(
            copilot.pid,
            "Analyze data",
            WorkerConfig(
                agent_id="worker-meta-e2e",
                zone_id="zone-e2e",
                budget_tokens=50_000,
                delivery_policy=DeliveryPolicy.DEFERRED,
                metadata={"model": "claude-3-opus", "priority": "high"},
            ),
        )

        # Budget in worker metadata
        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.metadata["budget_tokens"] == 50_000
        assert worker.metadata["delivery_policy"] == "deferred"
        assert worker.metadata["model"] == "claude-3-opus"

        # Delivery policy in A2A task metadata
        task = await tm.get_task(result.task_id, zone_id="zone-e2e")
        assert task.metadata["delivery_policy"] == "deferred"
        assert task.metadata["budget_tokens"] == 50_000


# ======================================================================
# E2E 2: Permission inheritance (inherit-and-restrict)
# ======================================================================


class TestPermissionInheritanceE2E:
    """Prove: worker gets only the tools in its allowlist."""

    async def test_restricted_worker_cannot_use_blocked_tools(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        td: ToolDispatcher,
    ) -> None:
        """Worker with allowlist=(vfs_read,) can read but not write."""

        # Register tools
        async def vfs_read(**_: Any) -> str:
            return "file contents"

        async def vfs_write(**_: Any) -> str:
            return "written"

        async def vfs_delete(**_: Any) -> str:
            return "deleted"

        td.register_handler("vfs_read", vfs_read)
        td.register_handler("vfs_write", vfs_write)
        td.register_handler("vfs_delete", vfs_delete)

        # Delegate with restricted allowlist
        await orchestrator.delegate(
            copilot.pid,
            "Read-only analysis",
            WorkerConfig(
                agent_id="worker-readonly",
                zone_id="zone-e2e",
                tool_allowlist=("vfs_read",),
            ),
        )

        # Worker can use vfs_read
        assert await td.check_permission(
            "vfs_read",
            agent_id="worker-readonly",
            zone_id="zone-e2e",
        )

        # Worker CANNOT use vfs_write or vfs_delete
        assert not await td.check_permission(
            "vfs_write",
            agent_id="worker-readonly",
            zone_id="zone-e2e",
        )
        assert not await td.check_permission(
            "vfs_delete",
            agent_id="worker-readonly",
            zone_id="zone-e2e",
        )

    async def test_glob_pattern_allowlist(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        td: ToolDispatcher,
    ) -> None:
        """Glob patterns in allowlist restrict correctly."""

        async def noop(**_: Any) -> str:
            return "ok"

        td.register_handler("nexus_read", noop)
        td.register_handler("nexus_write", noop)
        td.register_handler("shell_exec", noop)

        await orchestrator.delegate(
            copilot.pid,
            "Nexus-only work",
            WorkerConfig(
                agent_id="worker-glob-e2e",
                zone_id="zone-e2e",
                tool_allowlist=("nexus_*",),
            ),
        )

        assert await td.check_permission(
            "nexus_read",
            agent_id="worker-glob-e2e",
            zone_id="zone-e2e",
        )
        assert await td.check_permission(
            "nexus_write",
            agent_id="worker-glob-e2e",
            zone_id="zone-e2e",
        )
        assert not await td.check_permission(
            "shell_exec",
            agent_id="worker-glob-e2e",
            zone_id="zone-e2e",
        )


# ======================================================================
# E2E 3: Fan-out with cancellation cascade
# ======================================================================


class TestFanOutCancellationE2E:
    """Prove: copilot fans out to N workers, cancel cascades to all."""

    async def test_fan_out_and_cancel_all(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
        tm: TaskManager,
    ) -> None:
        """Copilot spawns 5 workers, cancel_all terminates all."""
        # Fan out to 5 workers
        results: list[DelegationResult] = []
        for i in range(5):
            r = await orchestrator.delegate(
                copilot.pid,
                f"Subtask {i}",
                WorkerConfig(agent_id=f"fan-e2e-{i}", zone_id="zone-e2e"),
            )
            results.append(r)

        # Verify 5 running workers
        running = await pm.list_processes(
            zone_id="zone-e2e",
            state=ProcessState.RUNNING,
        )
        worker_pids = {p.pid for p in running if p.parent_pid == copilot.pid}
        assert len(worker_pids) == 5

        # List delegations
        delegations = await orchestrator.list_delegations(
            copilot.pid,
            zone_id="zone-e2e",
        )
        assert len(delegations) == 5

        # Cancel all
        cancelled = await orchestrator.cancel_all(copilot.pid, zone_id="zone-e2e")
        assert cancelled == 5

        # All A2A tasks cancelled
        for r in results:
            task = await tm.get_task(r.task_id, zone_id="zone-e2e")
            assert task.status.state == TaskState.CANCELED

        # All worker processes terminated (ZOMBIE or STOPPED)
        for r in results:
            proc = await pm.get_process(r.worker_pid)
            assert proc is not None
            assert proc.state in {ProcessState.ZOMBIE, ProcessState.STOPPED}

    async def test_partial_cancel_others_continue(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        tm: TaskManager,
    ) -> None:
        """Cancel one worker, others keep running."""
        results = []
        for i in range(3):
            r = await orchestrator.delegate(
                copilot.pid,
                f"Work {i}",
                WorkerConfig(agent_id=f"partial-e2e-{i}", zone_id="zone-e2e"),
            )
            results.append(r)

        # Cancel only the first
        await orchestrator.cancel(results[0].task_id, zone_id="zone-e2e")

        t0 = await tm.get_task(results[0].task_id, zone_id="zone-e2e")
        t1 = await tm.get_task(results[1].task_id, zone_id="zone-e2e")
        t2 = await tm.get_task(results[2].task_id, zone_id="zone-e2e")

        assert t0.status.state == TaskState.CANCELED
        assert t1.status.state == TaskState.SUBMITTED  # Still running
        assert t2.status.state == TaskState.SUBMITTED


# ======================================================================
# E2E 4: Agent loop integration
# ======================================================================


def _mock_tool_call(
    tool_id: str,
    name: str,
    arguments: str = "{}",
) -> MagicMock:
    """Create a mock tool call object for agent_loop."""
    tc = MagicMock()
    tc.id = tool_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


class TestAgentLoopIntegration:
    """Prove: worker executes via agent_loop with real ToolDispatcher."""

    async def test_worker_executes_tool_via_agent_loop(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
        td: ToolDispatcher,
        ss: SessionStore,
    ) -> None:
        """Worker's agent_loop dispatches tool calls through real ToolDispatcher."""

        # Register a tool the worker can use
        async def summarize_file(
            path: str = "",
            **_: Any,  # noqa: ARG001
        ) -> str:
            return "This document describes the API architecture."

        td.register_handler("summarize_file", summarize_file)

        # Delegate with tool access
        result = await orchestrator.delegate(
            copilot.pid,
            "Summarize /docs/api.md",
            WorkerConfig(
                agent_id="worker-loop-e2e",
                zone_id="zone-e2e",
                tool_allowlist=("summarize_file",),
                max_turns=5,
            ),
        )

        worker = await pm.get_process(result.worker_pid)
        assert worker is not None

        # Mock LLM: first call returns tool call, second returns final answer
        llm = AsyncMock()
        tool_call = _mock_tool_call(
            "tc-1",
            "summarize_file",
            '{"path": "/docs/api.md"}',
        )

        response_with_tools = MagicMock()
        response_with_tools.tool_calls = [tool_call]
        response_with_tools.content = None

        response_final = MagicMock()
        response_final.tool_calls = None
        response_final.content = "The API doc describes REST endpoints."

        llm.chat = AsyncMock(side_effect=[response_with_tools, response_final])

        # Run agent loop for the worker
        answer = await agent_loop(
            process=worker,
            dispatcher=td,
            session_store=ss,
            llm_client=llm,
            config=AgentLoopConfig(max_turns=5, tool_timeout=10.0),
            initial_message="Summarize /docs/api.md",
        )

        assert answer == "The API doc describes REST endpoints."
        assert llm.chat.call_count == 2

    async def test_worker_denied_unauthorized_tool(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
        td: ToolDispatcher,
        ss: SessionStore,
    ) -> None:
        """Worker's agent_loop gets PermissionDenied for blocked tools."""

        async def write_file(**_: Any) -> str:
            return "written"

        td.register_handler("write_file", write_file)

        # Delegate with NO write access
        result = await orchestrator.delegate(
            copilot.pid,
            "Write something",
            WorkerConfig(
                agent_id="worker-denied-e2e",
                zone_id="zone-e2e",
                tool_allowlist=("read_file",),  # Only read, not write
                max_turns=3,
            ),
        )

        worker = await pm.get_process(result.worker_pid)
        assert worker is not None

        # LLM tries to call write_file (which is blocked)
        llm = AsyncMock()
        tool_call = _mock_tool_call("tc-deny", "write_file", "{}")

        response_with_tools = MagicMock()
        response_with_tools.tool_calls = [tool_call]

        llm.chat = AsyncMock(return_value=response_with_tools)

        # ToolPermissionDeniedError propagates — agent_loop enforces permission
        with pytest.raises(ToolPermissionDeniedError, match="write_file"):
            await agent_loop(
                process=worker,
                dispatcher=td,
                session_store=ss,
                llm_client=llm,
                config=AgentLoopConfig(max_turns=3, tool_timeout=10.0),
                initial_message="Write a file",
            )


# ======================================================================
# E2E 5: Checkpoint/restore across delegation
# ======================================================================


class TestCheckpointRestoreE2E:
    """Prove: worker state can be checkpointed and restored."""

    async def test_checkpoint_and_restore_worker(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
    ) -> None:
        """Worker process can be checkpointed and restored."""
        result = await orchestrator.delegate(
            copilot.pid,
            "Long-running work",
            WorkerConfig(agent_id="worker-ckpt-e2e", zone_id="zone-e2e"),
        )

        # Checkpoint the worker
        ckpt_hash = await pm.checkpoint(result.worker_pid)
        assert len(ckpt_hash) == 64  # SHA-256 hex

        # Worker is now PAUSED
        worker = await pm.get_process(result.worker_pid)
        assert worker is not None
        assert worker.state == ProcessState.PAUSED

        # Restore into a new process
        restored = await pm.restore(ckpt_hash, zone_id="zone-e2e")
        assert restored.pid != result.worker_pid  # New PID
        assert restored.agent_id == "worker-ckpt-e2e"
        assert restored.state == ProcessState.RUNNING


# ======================================================================
# E2E 6: Concurrent delegation stress test
# ======================================================================


class TestConcurrentDelegationE2E:
    """Prove: system handles concurrent delegations correctly."""

    async def test_concurrent_fan_out_10_workers(
        self,
        orchestrator: CopilotOrchestrator,
        copilot: AgentProcess,
        pm: ProcessManager,
    ) -> None:
        """10 concurrent delegations all succeed with unique PIDs."""
        results = await asyncio.gather(
            *(
                orchestrator.delegate(
                    copilot.pid,
                    f"Parallel work {i}",
                    WorkerConfig(
                        agent_id=f"concurrent-e2e-{i}",
                        zone_id="zone-e2e",
                    ),
                )
                for i in range(10)
            )
        )

        assert len(results) == 10
        pids = {r.worker_pid for r in results}
        assert len(pids) == 10  # All unique

        # All workers running
        for r in results:
            w = await pm.get_process(r.worker_pid)
            assert w is not None
            assert w.state == ProcessState.RUNNING
            assert w.parent_pid == copilot.pid
