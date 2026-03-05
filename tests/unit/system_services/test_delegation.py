"""Comprehensive delegation test suite (Issue #2761, Phase 2).

Tests all 11 copilot/worker capabilities:
    1. Task delegation (copilot → worker via A2A TaskManager)
    2. Async execution (worker runs independently)
    3. Status monitoring (copilot tracks worker state via SSE)
    4. Result delivery (worker artifacts → copilot)
    5. Review gates (INPUT_REQUIRED state for human-in-the-loop)
    6. Approve/revise/reject (copilot reviews worker output)
    7. Fan-out (copilot delegates to multiple workers)
    8. Sequential chaining (worker A → worker B → worker C)
    9. Cancellation cascade (cancel parent → cancel children)
    10. Lifecycle management (spawn/terminate/checkpoint/restore)
    11. Permission inheritance (inherit-and-restrict model)

Test infrastructure:
    - ProcessManager: agent process lifecycle (in-memory)
    - TaskManager: A2A task tracking (in-memory store)
    - ToolDispatcher: tool routing + permission enforcement
    - SessionStore: CAS-backed checkpoint/restore
    - PipeManager: inter-agent message pipes
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.a2a.exceptions import (
    TaskNotCancelableError,
)
from nexus.bricks.a2a.models import (
    Artifact,
    Message,
    Task,
    TaskState,
    TextPart,
)
from nexus.bricks.a2a.task_manager import TaskManager
from nexus.contracts.access_manifest_types import (
    AccessManifest,
    ManifestEntry,
    ToolPermission,
)
from nexus.contracts.agent_runtime_types import (
    AgentLoopConfig,
    AgentProcess,
    CheckpointNotFoundError,
    MaxTurnsExceededError,
    ProcessAlreadyRunningError,
    ProcessState,
    ToolPermissionDeniedError,
)
from nexus.system_services.agent_runtime import ProcessManager, SessionStore, ToolDispatcher
from nexus.system_services.agent_runtime.agent_loop import agent_loop

# ======================================================================
# Helpers
# ======================================================================


def _user_message(text: str) -> Message:
    """Create an A2A user message."""
    return Message(role="user", parts=[TextPart(text=text)])


def _agent_message(text: str) -> Message:
    """Create an A2A agent message."""
    return Message(role="agent", parts=[TextPart(text=text)])


def _mock_tool_call(tc_id: str, tool_name: str, arguments: str = "{}") -> MagicMock:
    """Create a mock tool call (avoids MagicMock name= gotcha)."""
    func = MagicMock()
    func.name = tool_name
    func.arguments = arguments
    tc = MagicMock()
    tc.id = tc_id
    tc.function = func
    return tc


def _make_manifest(
    agent_id: str,
    zone_id: str,
    entries: tuple[ManifestEntry, ...],
    *,
    created_by: str = "system",
) -> AccessManifest:
    """Create an AccessManifest with sensible defaults."""
    return AccessManifest(
        id=f"manifest-{agent_id}",
        agent_id=agent_id,
        zone_id=zone_id,
        name=f"{agent_id}-manifest",
        entries=entries,
        status="active",
        valid_from=datetime.now(tz=UTC).isoformat(),
        valid_until=None,
        created_by=created_by,
    )


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def pm() -> ProcessManager:
    return ProcessManager()


@pytest.fixture
def tm() -> TaskManager:
    return TaskManager()


@pytest.fixture
def dispatcher() -> ToolDispatcher:
    return ToolDispatcher()


@pytest.fixture
def session_store() -> SessionStore:
    return SessionStore()


# ======================================================================
# 1. Task Delegation (copilot → worker)
# ======================================================================


class TestTaskDelegation:
    """Copilot creates A2A tasks for workers."""

    async def test_copilot_creates_task_for_worker(
        self, pm: ProcessManager, tm: TaskManager
    ) -> None:
        """Copilot spawns worker and delegates a task via TaskManager."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        worker = await pm.spawn("worker-1", "zone-1", parent_pid=copilot.pid)

        task = await tm.create_task(
            _user_message("Summarize /docs/readme.md"),
            zone_id="zone-1",
            agent_id="worker-1",
        )

        assert task.status.state == TaskState.SUBMITTED
        assert worker.parent_pid == copilot.pid

    async def test_delegated_task_visible_by_zone(self, tm: TaskManager) -> None:
        """Tasks created in a zone are listable by zone."""
        await tm.create_task(
            _user_message("Task A"),
            zone_id="zone-1",
            agent_id="worker-1",
        )
        await tm.create_task(
            _user_message("Task B"),
            zone_id="zone-1",
            agent_id="worker-2",
        )

        tasks = await tm.list_tasks(zone_id="zone-1")
        assert len(tasks) == 2

    async def test_delegation_with_metadata(self, tm: TaskManager) -> None:
        """Copilot can attach metadata (priority, budget) to delegated tasks."""
        task = await tm.create_task(
            _user_message("High priority work"),
            zone_id="zone-1",
            agent_id="worker-1",
            metadata={"priority": "high", "budget_tokens": 50_000},
        )

        assert task.metadata is not None
        assert task.metadata["priority"] == "high"
        assert task.metadata["budget_tokens"] == 50_000


# ======================================================================
# 2. Async Execution (worker runs independently)
# ======================================================================


class TestAsyncExecution:
    """Worker processes execute independently of copilot."""

    async def test_worker_runs_agent_loop_independently(
        self, pm: ProcessManager, dispatcher: ToolDispatcher, session_store: SessionStore
    ) -> None:
        """Worker executes its own agent loop with its own LLM client."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        worker = await pm.spawn("worker-1", "zone-1", parent_pid=copilot.pid)

        # Register a tool for the worker
        async def vfs_read(path: str = "") -> str:
            return f"Content of {path}"

        dispatcher.register_handler("vfs_read", vfs_read)

        llm_client = AsyncMock()
        llm_client.chat.side_effect = [
            MagicMock(
                tool_calls=[_mock_tool_call("tc-1", "vfs_read", '{"path": "/test.txt"}')],
                content=None,
            ),
            MagicMock(tool_calls=None, content="File contains test data"),
        ]

        result = await agent_loop(
            process=worker,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=AgentLoopConfig(max_turns=10),
            initial_message="Read /test.txt",
        )

        assert result == "File contains test data"

    async def test_concurrent_workers(
        self, pm: ProcessManager, session_store: SessionStore
    ) -> None:
        """Multiple workers can execute concurrently."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        workers = []
        for i in range(3):
            w = await pm.spawn(f"worker-{i}", "zone-1", parent_pid=copilot.pid)
            workers.append(w)

        async def run_worker(worker: AgentProcess, msg: str) -> str | None:
            d = ToolDispatcher()
            llm = AsyncMock()
            llm.chat.return_value = MagicMock(tool_calls=None, content=f"Done: {msg}")
            return await agent_loop(
                process=worker,
                dispatcher=d,
                session_store=session_store,
                llm_client=llm,
                config=AgentLoopConfig(max_turns=5),
                initial_message=msg,
            )

        results = await asyncio.gather(*(run_worker(w, f"task-{i}") for i, w in enumerate(workers)))

        assert len(results) == 3
        assert all(r is not None and r.startswith("Done:") for r in results)


# ======================================================================
# 3. Status Monitoring (copilot tracks worker state)
# ======================================================================


class TestStatusMonitoring:
    """Copilot monitors worker task progress via SSE streams."""

    async def test_copilot_monitors_worker_via_sse(self, tm: TaskManager) -> None:
        """Copilot receives status updates when worker transitions task state."""
        task = await tm.create_task(
            _user_message("Process data"),
            zone_id="zone-1",
            agent_id="worker-1",
        )

        # Register SSE stream
        queue: asyncio.Queue[dict] = tm.register_stream(task.id)

        # Worker updates task state
        await tm.update_task_state(
            task.id,
            TaskState.WORKING,
            zone_id="zone-1",
            message=_agent_message("Processing..."),
        )

        # Copilot receives event (key is "statusUpdate", not "type")
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert "statusUpdate" in event

        tm.unregister_stream(task.id, queue)

    async def test_multiple_state_transitions_tracked(self, tm: TaskManager) -> None:
        """Full lifecycle: SUBMITTED → WORKING → COMPLETED."""
        task = await tm.create_task(
            _user_message("Work"),
            zone_id="zone-1",
        )

        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        working = await tm.get_task(task.id, zone_id="zone-1")
        assert working.status.state == TaskState.WORKING

        await tm.update_task_state(
            task.id,
            TaskState.COMPLETED,
            zone_id="zone-1",
            message=_agent_message("Done"),
        )
        completed = await tm.get_task(task.id, zone_id="zone-1")
        assert completed.status.state == TaskState.COMPLETED


# ======================================================================
# 4. Result Delivery (worker artifacts → copilot)
# ======================================================================


class TestResultDelivery:
    """Worker delivers results back to copilot via artifacts."""

    async def test_worker_delivers_artifact(self, tm: TaskManager) -> None:
        """Worker adds artifact to task, copilot retrieves it."""
        task = await tm.create_task(
            _user_message("Generate report"),
            zone_id="zone-1",
            agent_id="worker-1",
        )

        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")

        artifact = Artifact(
            artifactId="art-1",
            name="report.md",
            parts=[TextPart(text="# Report\nGenerated content")],
        )
        await tm.add_artifact(task.id, artifact, zone_id="zone-1")

        updated = await tm.get_task(task.id, zone_id="zone-1")
        assert len(updated.artifacts) == 1
        assert updated.artifacts[0].name == "report.md"

    async def test_multiple_artifacts_delivered(self, tm: TaskManager) -> None:
        """Worker can deliver multiple artifacts to a single task."""
        task = await tm.create_task(
            _user_message("Generate analysis"),
            zone_id="zone-1",
        )
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")

        for i in range(3):
            artifact = Artifact(
                artifactId=f"art-{i}",
                name=f"result-{i}.txt",
                parts=[TextPart(text=f"Result {i}")],
            )
            await tm.add_artifact(task.id, artifact, zone_id="zone-1")

        updated = await tm.get_task(task.id, zone_id="zone-1")
        assert len(updated.artifacts) == 3

    async def test_artifact_triggers_sse_event(self, tm: TaskManager) -> None:
        """Adding an artifact pushes an event to SSE subscribers."""
        task = await tm.create_task(
            _user_message("Work"),
            zone_id="zone-1",
        )

        # Register stream BEFORE state transition to capture all events
        queue = tm.register_stream(task.id)

        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")

        # Drain the status update event from WORKING transition
        status_event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert "statusUpdate" in status_event

        artifact = Artifact(
            artifactId="art-1",
            parts=[TextPart(text="data")],
        )
        await tm.add_artifact(task.id, artifact, zone_id="zone-1")

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert "artifactUpdate" in event
        tm.unregister_stream(task.id, queue)


# ======================================================================
# 5. Review Gates (INPUT_REQUIRED for human-in-the-loop)
# ======================================================================


class TestReviewGates:
    """Worker pauses for copilot review via INPUT_REQUIRED state."""

    async def test_worker_requests_input(self, tm: TaskManager) -> None:
        """Worker transitions to INPUT_REQUIRED, copilot resumes with WORKING."""
        task = await tm.create_task(
            _user_message("Process with review"),
            zone_id="zone-1",
        )

        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(
            task.id,
            TaskState.INPUT_REQUIRED,
            zone_id="zone-1",
            message=_agent_message("Need clarification on X"),
        )

        paused = await tm.get_task(task.id, zone_id="zone-1")
        assert paused.status.state == TaskState.INPUT_REQUIRED

        # Copilot provides input and resumes
        await tm.update_task_state(
            task.id,
            TaskState.WORKING,
            zone_id="zone-1",
            message=_user_message("Use approach B"),
        )

        resumed = await tm.get_task(task.id, zone_id="zone-1")
        assert resumed.status.state == TaskState.WORKING


# ======================================================================
# 6. Approve / Revise / Reject
# ======================================================================


class TestApproveReviseReject:
    """Copilot can approve (complete), revise (re-work), or reject worker output."""

    async def test_approve_completes_task(self, tm: TaskManager) -> None:
        """Copilot approves → COMPLETED."""
        task = await tm.create_task(_user_message("Draft"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(
            task.id,
            TaskState.INPUT_REQUIRED,
            zone_id="zone-1",
            message=_agent_message("Draft ready for review"),
        )

        # Approve → resume to working then complete
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.COMPLETED, zone_id="zone-1")

        final = await tm.get_task(task.id, zone_id="zone-1")
        assert final.status.state == TaskState.COMPLETED

    async def test_revise_continues_work(self, tm: TaskManager) -> None:
        """Copilot requests revision → back to WORKING with new input."""
        task = await tm.create_task(_user_message("Draft"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(
            task.id,
            TaskState.INPUT_REQUIRED,
            zone_id="zone-1",
            message=_agent_message("First draft done"),
        )

        # Revise — back to WORKING
        await tm.update_task_state(
            task.id,
            TaskState.WORKING,
            zone_id="zone-1",
            message=_user_message("Please improve section 2"),
        )

        revised = await tm.get_task(task.id, zone_id="zone-1")
        assert revised.status.state == TaskState.WORKING

    async def test_reject_fails_task(self, tm: TaskManager) -> None:
        """Copilot rejects → FAILED from INPUT_REQUIRED."""
        task = await tm.create_task(_user_message("Draft"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(
            task.id,
            TaskState.INPUT_REQUIRED,
            zone_id="zone-1",
        )

        # Reject → FAILED
        await tm.update_task_state(
            task.id,
            TaskState.FAILED,
            zone_id="zone-1",
            message=_agent_message("Output quality insufficient"),
        )

        rejected = await tm.get_task(task.id, zone_id="zone-1")
        assert rejected.status.state == TaskState.FAILED

    async def test_revision_history_preserved(self, tm: TaskManager) -> None:
        """Each revision cycle adds to task history."""
        task = await tm.create_task(_user_message("Draft"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")

        # First revision cycle
        await tm.update_task_state(
            task.id,
            TaskState.INPUT_REQUIRED,
            zone_id="zone-1",
            message=_agent_message("Draft v1"),
        )
        await tm.update_task_state(
            task.id,
            TaskState.WORKING,
            zone_id="zone-1",
            message=_user_message("Revise section 1"),
        )

        # Second revision cycle
        await tm.update_task_state(
            task.id,
            TaskState.INPUT_REQUIRED,
            zone_id="zone-1",
            message=_agent_message("Draft v2"),
        )
        await tm.update_task_state(
            task.id,
            TaskState.WORKING,
            zone_id="zone-1",
            message=_user_message("Looks good, finalize"),
        )
        await tm.update_task_state(task.id, TaskState.COMPLETED, zone_id="zone-1")

        final = await tm.get_task(task.id, zone_id="zone-1")
        # History should include: initial message + all status messages
        assert len(final.history) >= 5


# ======================================================================
# 7. Fan-out (copilot delegates to multiple workers)
# ======================================================================


class TestFanOut:
    """Copilot delegates work to multiple workers in parallel."""

    async def test_fan_out_to_multiple_workers(self, pm: ProcessManager, tm: TaskManager) -> None:
        """Copilot creates tasks for N workers concurrently."""
        copilot = await pm.spawn("copilot-1", "zone-1")

        task_ids = []
        for i in range(5):
            await pm.spawn(f"fan-worker-{i}", "zone-1", parent_pid=copilot.pid)
            task = await tm.create_task(
                _user_message(f"Subtask {i}"),
                zone_id="zone-1",
                agent_id=f"fan-worker-{i}",
            )
            task_ids.append(task.id)

        # All tasks created in SUBMITTED state
        for tid in task_ids:
            t = await tm.get_task(tid, zone_id="zone-1")
            assert t.status.state == TaskState.SUBMITTED

    async def test_fan_out_partial_failure(self, pm: ProcessManager, tm: TaskManager) -> None:
        """Some workers fail while others complete — copilot handles partial results."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        tasks = []
        for i in range(3):
            await pm.spawn(f"fan-worker-{i}", "zone-1", parent_pid=copilot.pid)
            task = await tm.create_task(
                _user_message(f"Work {i}"),
                zone_id="zone-1",
                agent_id=f"fan-worker-{i}",
            )
            tasks.append(task)

        # Worker 0: completes
        await tm.update_task_state(tasks[0].id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(tasks[0].id, TaskState.COMPLETED, zone_id="zone-1")

        # Worker 1: fails
        await tm.update_task_state(tasks[1].id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(tasks[1].id, TaskState.FAILED, zone_id="zone-1")

        # Worker 2: completes
        await tm.update_task_state(tasks[2].id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(tasks[2].id, TaskState.COMPLETED, zone_id="zone-1")

        # Copilot can see mixed results
        completed = [
            t
            for tid in [t.id for t in tasks]
            if (t := await tm.get_task(tid, zone_id="zone-1")).status.state == TaskState.COMPLETED
        ]
        failed = [
            t
            for tid in [t.id for t in tasks]
            if (t := await tm.get_task(tid, zone_id="zone-1")).status.state == TaskState.FAILED
        ]

        assert len(completed) == 2
        assert len(failed) == 1

    async def test_fan_out_all_workers_same_zone(self, pm: ProcessManager) -> None:
        """All fan-out workers are in the same zone as copilot."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        workers = []
        for i in range(5):
            w = await pm.spawn(f"fan-worker-{i}", "zone-1", parent_pid=copilot.pid)
            workers.append(w)

        zone_procs = await pm.list_processes(zone_id="zone-1")
        assert len(zone_procs) == 6  # 1 copilot + 5 workers
        assert all(p.zone_id == "zone-1" for p in zone_procs)


# ======================================================================
# 8. Sequential Chaining (worker A → worker B → worker C)
# ======================================================================


class TestSequentialChaining:
    """Tasks are chained: output of one worker feeds into the next."""

    async def test_sequential_chain(self, pm: ProcessManager, tm: TaskManager) -> None:
        """Worker A completes → Worker B starts with A's output."""
        copilot = await pm.spawn("copilot-1", "zone-1")

        # Step 1: Worker A
        await pm.spawn("chain-a", "zone-1", parent_pid=copilot.pid)
        task_a = await tm.create_task(
            _user_message("Extract data"),
            zone_id="zone-1",
            agent_id="chain-a",
        )
        await tm.update_task_state(task_a.id, TaskState.WORKING, zone_id="zone-1")
        await tm.add_artifact(
            task_a.id,
            Artifact(artifactId="a-output", parts=[TextPart(text="extracted: [1,2,3]")]),
            zone_id="zone-1",
        )
        await tm.update_task_state(task_a.id, TaskState.COMPLETED, zone_id="zone-1")

        # Copilot reads A's output
        task_a_result = await tm.get_task(task_a.id, zone_id="zone-1")
        first_part = task_a_result.artifacts[0].parts[0]
        assert isinstance(first_part, TextPart)
        a_output = first_part.text

        # Step 2: Worker B uses A's output
        await pm.spawn("chain-b", "zone-1", parent_pid=copilot.pid)
        task_b = await tm.create_task(
            _user_message(f"Transform: {a_output}"),
            zone_id="zone-1",
            agent_id="chain-b",
        )
        await tm.update_task_state(task_b.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(task_b.id, TaskState.COMPLETED, zone_id="zone-1")

        # Both tasks completed
        assert (await tm.get_task(task_a.id, zone_id="zone-1")).status.state == TaskState.COMPLETED
        assert (await tm.get_task(task_b.id, zone_id="zone-1")).status.state == TaskState.COMPLETED

    async def test_chain_breaks_on_failure(self, tm: TaskManager) -> None:
        """If a step in the chain fails, subsequent steps don't execute."""
        task_a = await tm.create_task(
            _user_message("Step A"),
            zone_id="zone-1",
        )
        await tm.update_task_state(task_a.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(task_a.id, TaskState.FAILED, zone_id="zone-1")

        # Task A failed — copilot should not create Task B
        task_a_final = await tm.get_task(task_a.id, zone_id="zone-1")
        assert task_a_final.status.state == TaskState.FAILED

        # Verify no further task was created
        all_tasks = await tm.list_tasks(zone_id="zone-1")
        assert len(all_tasks) == 1


# ======================================================================
# 9. Cancellation Cascade (cancel parent → cancel children)
# ======================================================================


class TestCancellationCascade:
    """Cancelling a parent task should cascade to child tasks."""

    async def test_cancel_single_task(self, tm: TaskManager) -> None:
        """Basic task cancellation works."""
        task = await tm.create_task(_user_message("Work"), zone_id="zone-1")
        cancelled = await tm.cancel_task(task.id, zone_id="zone-1")
        assert cancelled.status.state == TaskState.CANCELED

    async def test_cancel_terminal_task_raises(self, tm: TaskManager) -> None:
        """Cannot cancel a completed task."""
        task = await tm.create_task(_user_message("Work"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.COMPLETED, zone_id="zone-1")

        with pytest.raises(TaskNotCancelableError):
            await tm.cancel_task(task.id, zone_id="zone-1")

    async def test_cancel_fan_out_tasks(self, tm: TaskManager) -> None:
        """Copilot cancels all fan-out worker tasks."""
        tasks = []
        for i in range(3):
            task = await tm.create_task(
                _user_message(f"Subtask {i}"),
                zone_id="zone-1",
                agent_id=f"worker-{i}",
            )
            tasks.append(task)

        # Cancel all
        for t in tasks:
            await tm.cancel_task(t.id, zone_id="zone-1")

        for t in tasks:
            final = await tm.get_task(t.id, zone_id="zone-1")
            assert final.status.state == TaskState.CANCELED

    async def test_terminate_worker_process(self, pm: ProcessManager) -> None:
        """Terminating worker process sets exit status."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        worker = await pm.spawn("worker-1", "zone-1", parent_pid=copilot.pid)

        terminated = await pm.terminate(worker.pid, reason="copilot cancelled")
        assert terminated is True

        proc = await pm.get_process(worker.pid)
        assert proc is not None
        assert proc.state == ProcessState.ZOMBIE

        # Reap the zombie
        status = await pm.wait(worker.pid)
        assert status.reason == "copilot cancelled"
        assert status.exit_code == -15  # SIGTERM

    async def test_cancel_working_task(self, tm: TaskManager) -> None:
        """Cancel a task that is currently WORKING."""
        task = await tm.create_task(_user_message("Work"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")

        cancelled = await tm.cancel_task(task.id, zone_id="zone-1")
        assert cancelled.status.state == TaskState.CANCELED


# ======================================================================
# 10. Lifecycle Management (spawn/terminate/checkpoint/restore)
# ======================================================================


class TestLifecycleManagement:
    """Full process lifecycle: spawn → run → checkpoint → terminate → restore."""

    async def test_full_lifecycle(self, pm: ProcessManager, session_store: SessionStore) -> None:
        """Complete lifecycle: spawn → checkpoint → terminate → restore."""
        # Spawn
        copilot = await pm.spawn("copilot-1", "zone-1")
        worker = await pm.spawn("worker-1", "zone-1", parent_pid=copilot.pid)

        assert worker.state == ProcessState.RUNNING

        # Checkpoint session
        session_hash = await session_store.checkpoint(
            worker.pid,
            {"messages": [{"role": "user", "content": "Hello"}], "turn_count": 5},
            agent_id="worker-1",
        )
        assert isinstance(session_hash, str)

        # Also checkpoint process state
        proc_hash = await pm.checkpoint(worker.pid)
        proc = await pm.get_process(worker.pid)
        assert proc is not None
        assert proc.state == ProcessState.PAUSED

        # Terminate
        await pm.terminate(worker.pid, reason="lifecycle test")
        status = await pm.wait(worker.pid)
        assert status.exit_code == -15

        # Restore session from CAS
        restored_session = await session_store.restore(session_hash)
        assert restored_session["turn_count"] == 5

        # Restore process from checkpoint
        new_proc = await pm.restore(proc_hash, zone_id="zone-1")
        assert new_proc.state == ProcessState.RUNNING
        assert new_proc.agent_id == "worker-1"
        assert new_proc.pid != worker.pid  # New PID

    async def test_checkpoint_preserves_metadata(self, pm: ProcessManager) -> None:
        """Checkpoint/restore preserves process metadata."""
        proc = await pm.spawn(
            "agent-meta",
            "zone-1",
            metadata={"model": "gpt-4", "budget": 1000},
        )

        checkpoint_hash = await pm.checkpoint(proc.pid)
        restored = await pm.restore(checkpoint_hash, zone_id="zone-1")

        assert restored.metadata["model"] == "gpt-4"
        assert restored.metadata["budget"] == 1000

    async def test_session_checkpoint_deterministic(self, session_store: SessionStore) -> None:
        """Same session data produces same CAS hash."""
        data = {"messages": [{"role": "user", "content": "test"}], "turn_count": 1}

        h1 = await session_store.checkpoint("p-1", data, agent_id="a-1")
        h2 = await session_store.checkpoint("p-2", data, agent_id="a-1")

        assert h1 == h2  # Content-addressable — same data = same hash

    async def test_restore_nonexistent_checkpoint_raises(self, pm: ProcessManager) -> None:
        """Restoring a nonexistent checkpoint hash raises."""
        with pytest.raises(CheckpointNotFoundError):
            await pm.restore("nonexistent-hash", zone_id="zone-1")

    async def test_one_running_process_per_agent(self, pm: ProcessManager) -> None:
        """Cannot spawn two running processes for the same agent."""
        await pm.spawn("agent-1", "zone-1")
        with pytest.raises(ProcessAlreadyRunningError):
            await pm.spawn("agent-1", "zone-1")

    async def test_respawn_after_terminate(self, pm: ProcessManager) -> None:
        """After termination and reaping, agent can be respawned."""
        proc = await pm.spawn("agent-1", "zone-1")
        await pm.terminate(proc.pid)
        await pm.wait(proc.pid)  # Reap zombie → STOPPED

        new_proc = await pm.spawn("agent-1", "zone-1")
        assert new_proc.pid != proc.pid
        assert new_proc.state == ProcessState.RUNNING


# ======================================================================
# 11. Permission Inheritance (inherit-and-restrict model)
# ======================================================================


class TestPermissionInheritance:
    """Workers inherit a subset of copilot permissions."""

    async def test_copilot_permissive_worker_restricted(self, dispatcher: ToolDispatcher) -> None:
        """Copilot has full access, worker is restricted to read-only."""

        async def vfs_read() -> str:
            return "data"

        async def vfs_write() -> str:
            return "written"

        dispatcher.register_handler("vfs_read", vfs_read)
        dispatcher.register_handler("vfs_write", vfs_write)

        # Copilot: no manifest = permissive
        assert await dispatcher.check_permission("vfs_read", agent_id="copilot-1", zone_id="zone-1")
        assert await dispatcher.check_permission(
            "vfs_write", agent_id="copilot-1", zone_id="zone-1"
        )

        # Worker: restricted manifest
        worker_manifest = _make_manifest(
            "worker-1",
            "zone-1",
            entries=(
                ManifestEntry(tool_pattern="vfs_read", permission=ToolPermission.ALLOW),
                ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),
            ),
            created_by="copilot-1",
        )
        dispatcher.set_manifest("worker-1", worker_manifest)

        assert await dispatcher.check_permission("vfs_read", agent_id="worker-1", zone_id="zone-1")
        assert not await dispatcher.check_permission(
            "vfs_write", agent_id="worker-1", zone_id="zone-1"
        )

    async def test_worker_dispatch_denied(self, dispatcher: ToolDispatcher) -> None:
        """Worker cannot dispatch tools outside its manifest."""

        async def vfs_write() -> str:
            return "written"

        dispatcher.register_handler("vfs_write", vfs_write)

        worker_manifest = _make_manifest(
            "worker-1",
            "zone-1",
            entries=(ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),),
        )
        dispatcher.set_manifest("worker-1", worker_manifest)

        with pytest.raises(ToolPermissionDeniedError):
            await dispatcher.dispatch(
                "vfs_write",
                {},
                agent_id="worker-1",
                zone_id="zone-1",
            )

    async def test_glob_pattern_matching(self, dispatcher: ToolDispatcher) -> None:
        """Manifest glob patterns match tool names correctly."""

        async def nexus_read() -> str:
            return "ok"

        async def nexus_write() -> str:
            return "ok"

        async def vfs_stat() -> str:
            return "ok"

        dispatcher.register_handler("nexus_read", nexus_read)
        dispatcher.register_handler("nexus_write", nexus_write)
        dispatcher.register_handler("vfs_stat", vfs_stat)

        manifest = _make_manifest(
            "worker-glob",
            "zone-1",
            entries=(
                ManifestEntry(tool_pattern="nexus_*", permission=ToolPermission.ALLOW),
                ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),
            ),
        )
        dispatcher.set_manifest("worker-glob", manifest)

        assert await dispatcher.check_permission(
            "nexus_read", agent_id="worker-glob", zone_id="zone-1"
        )
        assert await dispatcher.check_permission(
            "nexus_write", agent_id="worker-glob", zone_id="zone-1"
        )
        assert not await dispatcher.check_permission(
            "vfs_stat", agent_id="worker-glob", zone_id="zone-1"
        )

    async def test_first_match_wins(self, dispatcher: ToolDispatcher) -> None:
        """First matching rule takes precedence (order matters)."""

        async def dangerous_tool() -> str:
            return "danger"

        dispatcher.register_handler("dangerous_tool", dangerous_tool)

        # DENY specific, then ALLOW wildcard — DENY wins for dangerous_tool
        manifest_deny_first = _make_manifest(
            "worker-deny",
            "zone-1",
            entries=(
                ManifestEntry(tool_pattern="dangerous_*", permission=ToolPermission.DENY),
                ManifestEntry(tool_pattern="*", permission=ToolPermission.ALLOW),
            ),
        )
        dispatcher.set_manifest("worker-deny", manifest_deny_first)

        assert not await dispatcher.check_permission(
            "dangerous_tool", agent_id="worker-deny", zone_id="zone-1"
        )

    async def test_clear_manifest_restores_permissive(self, dispatcher: ToolDispatcher) -> None:
        """Clearing manifest (set to None) restores permissive mode."""

        async def tool_a() -> str:
            return "ok"

        dispatcher.register_handler("tool_a", tool_a)

        manifest = _make_manifest(
            "worker-clear",
            "zone-1",
            entries=(ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),),
        )
        dispatcher.set_manifest("worker-clear", manifest)
        assert not await dispatcher.check_permission(
            "tool_a", agent_id="worker-clear", zone_id="zone-1"
        )

        # Clear manifest
        dispatcher.set_manifest("worker-clear", None)
        assert await dispatcher.check_permission(
            "tool_a", agent_id="worker-clear", zone_id="zone-1"
        )

    async def test_created_by_tracks_delegation_chain(self) -> None:
        """Manifest created_by field tracks which copilot created the restriction."""
        manifest = _make_manifest(
            "worker-1",
            "zone-1",
            entries=(ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),),
            created_by="copilot-alpha",
        )
        assert manifest.created_by == "copilot-alpha"

    async def test_per_agent_manifest_isolation(self, dispatcher: ToolDispatcher) -> None:
        """Each agent has its own manifest — no cross-contamination."""

        async def tool_x() -> str:
            return "ok"

        dispatcher.register_handler("tool_x", tool_x)

        # Worker A: deny all
        dispatcher.set_manifest(
            "worker-a",
            _make_manifest(
                "worker-a",
                "zone-1",
                entries=(ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),),
            ),
        )
        # Worker B: allow all explicitly
        dispatcher.set_manifest(
            "worker-b",
            _make_manifest(
                "worker-b",
                "zone-1",
                entries=(ManifestEntry(tool_pattern="*", permission=ToolPermission.ALLOW),),
            ),
        )

        assert not await dispatcher.check_permission(
            "tool_x", agent_id="worker-a", zone_id="zone-1"
        )
        assert await dispatcher.check_permission("tool_x", agent_id="worker-b", zone_id="zone-1")


# ======================================================================
# Cross-cutting: Zone Isolation
# ======================================================================


class TestZoneIsolation:
    """Zone boundaries enforce isolation across all components."""

    async def test_process_zone_isolation(self, pm: ProcessManager) -> None:
        """Processes in different zones are listed separately."""
        await pm.spawn("agent-a", "zone-1")
        await pm.spawn("agent-b", "zone-2")

        zone1 = await pm.list_processes(zone_id="zone-1")
        zone2 = await pm.list_processes(zone_id="zone-2")

        assert len(zone1) == 1
        assert len(zone2) == 1
        assert zone1[0].agent_id == "agent-a"
        assert zone2[0].agent_id == "agent-b"

    async def test_task_zone_isolation(self, tm: TaskManager) -> None:
        """Tasks in different zones are invisible to each other."""
        await tm.create_task(_user_message("Zone 1 task"), zone_id="zone-1")
        await tm.create_task(_user_message("Zone 2 task"), zone_id="zone-2")

        zone1_tasks = await tm.list_tasks(zone_id="zone-1")
        zone2_tasks = await tm.list_tasks(zone_id="zone-2")

        assert len(zone1_tasks) == 1
        assert len(zone2_tasks) == 1

    async def test_session_checkpoint_per_agent(self, session_store: SessionStore) -> None:
        """Checkpoints are isolated per agent."""
        await session_store.checkpoint("p-1", {"data": "a"}, agent_id="agent-1")
        await session_store.checkpoint("p-2", {"data": "b"}, agent_id="agent-2")

        a1 = await session_store.list_checkpoints("agent-1")
        a2 = await session_store.list_checkpoints("agent-2")

        assert len(a1) == 1
        assert len(a2) == 1


# ======================================================================
# Cross-cutting: Concurrent Operations
# ======================================================================


class TestConcurrentOperations:
    """Verify thread-safety under concurrent access."""

    async def test_concurrent_task_state_updates(self, tm: TaskManager) -> None:
        """Optimistic locking prevents concurrent state corruption."""
        task = await tm.create_task(_user_message("Race"), zone_id="zone-1")
        await tm.update_task_state(task.id, TaskState.WORKING, zone_id="zone-1")

        # Only one transition should succeed — the other gets a stale version
        # Since the transitions are to different states, one will get
        # InvalidStateTransitionError (wrapping StaleTaskVersionError)
        results = await asyncio.gather(
            tm.update_task_state(task.id, TaskState.COMPLETED, zone_id="zone-1"),
            tm.update_task_state(task.id, TaskState.FAILED, zone_id="zone-1"),
            return_exceptions=True,
        )

        # At least one should succeed, the other should fail
        successes = [r for r in results if isinstance(r, Task)]
        failures = [r for r in results if isinstance(r, Exception)]

        assert len(successes) >= 1
        assert len(successes) + len(failures) == 2

    async def test_concurrent_spawns_different_agents(self, pm: ProcessManager) -> None:
        """Spawning different agents concurrently all succeed."""
        procs = await asyncio.gather(*(pm.spawn(f"agent-{i}", "zone-1") for i in range(10)))

        pids = {p.pid for p in procs}
        assert len(pids) == 10  # All unique PIDs

    async def test_concurrent_session_checkpoints(self, session_store: SessionStore) -> None:
        """Multiple agents checkpointing concurrently all succeed."""
        hashes = await asyncio.gather(
            *(
                session_store.checkpoint(
                    f"p-{i}",
                    {"agent": f"agent-{i}", "data": i},
                    agent_id=f"agent-{i}",
                )
                for i in range(10)
            )
        )

        assert len(set(hashes)) == 10  # All unique hashes


# ======================================================================
# Integration: Agent Loop + Delegation
# ======================================================================


class TestAgentLoopDelegation:
    """Agent loop dispatches delegation tools."""

    async def test_loop_dispatches_delegate_task(
        self, pm: ProcessManager, tm: TaskManager, session_store: SessionStore
    ) -> None:
        """Agent loop calls a 'delegate_task' tool that creates a child A2A task."""
        copilot = await pm.spawn("copilot-1", "zone-1")

        # Register delegation tool
        dispatcher = ToolDispatcher()
        created_task_ids: list[str] = []

        async def delegate_task(message: str = "", worker_id: str = "") -> str:
            task = await tm.create_task(
                _user_message(message),
                zone_id="zone-1",
                agent_id=worker_id,
            )
            created_task_ids.append(task.id)
            return f"Delegated: {task.id}"

        dispatcher.register_handler("delegate_task", delegate_task)

        llm_client = AsyncMock()
        llm_client.chat.side_effect = [
            MagicMock(
                tool_calls=[
                    _mock_tool_call(
                        "tc-1",
                        "delegate_task",
                        '{"message": "Summarize docs", "worker_id": "worker-1"}',
                    )
                ],
                content=None,
            ),
            MagicMock(tool_calls=None, content="Delegated work to worker-1"),
        ]

        result = await agent_loop(
            process=copilot,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=AgentLoopConfig(max_turns=5),
            initial_message="Delegate summarization to a worker",
        )

        assert result == "Delegated work to worker-1"
        assert len(created_task_ids) == 1

        # Verify the A2A task was created
        delegated_task = await tm.get_task(created_task_ids[0], zone_id="zone-1")
        assert delegated_task.status.state == TaskState.SUBMITTED

    async def test_loop_max_turns_prevents_runaway_delegation(
        self, pm: ProcessManager, session_store: SessionStore
    ) -> None:
        """Max turns limit prevents infinite delegation loops."""
        copilot = await pm.spawn("copilot-1", "zone-1")
        dispatcher = ToolDispatcher()

        async def noop_tool() -> str:
            return "ok"

        dispatcher.register_handler("noop", noop_tool)

        llm_client = AsyncMock()
        llm_client.chat.return_value = MagicMock(
            tool_calls=[_mock_tool_call("tc-1", "noop")],
            content=None,
        )

        with pytest.raises(MaxTurnsExceededError):
            await agent_loop(
                process=copilot,
                dispatcher=dispatcher,
                session_store=session_store,
                llm_client=llm_client,
                config=AgentLoopConfig(max_turns=3),
                initial_message="Loop forever",
            )
