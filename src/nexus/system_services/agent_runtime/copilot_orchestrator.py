"""CopilotOrchestrator — copilot/worker delegation layer (Issue #2761).

Manages the lifecycle of delegated tasks: spawn workers, assign work via
A2A TaskManager, enforce permission restrictions, monitor progress, and
handle cancellation cascades.

Architecture:
    CopilotOrchestrator
        ├── ProcessManager (spawn/terminate workers)
        ├── TaskManager    (A2A task tracking)
        ├── ToolDispatcher (permission enforcement)
        └── SessionStore   (checkpoint/restore)

Security model (inherit-and-restrict):
    - Workers inherit a SUBSET of copilot permissions
    - Permission scope is defined by WorkerConfig.tool_allowlist
    - Per-worker budget cap via WorkerConfig.budget_tokens
    - AccessManifest is created with created_by=copilot_agent_id
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.access_manifest_types import (
    AccessManifest,
    ManifestEntry,
    ToolPermission,
)
from nexus.contracts.agent_runtime_types import (
    DelegationResult,
    DeliveryPolicy,
    ProcessManagerProtocol,
    ProcessState,
    TaskManagerProtocol,
    ToolDispatcherProtocol,
    WorkerConfig,
)

logger = logging.getLogger(__name__)


class CopilotOrchestrator:
    """Orchestrates copilot/worker delegation.

    Delegates to A2A TaskManager for worker tracking, ProcessManager
    for process lifecycle, and ToolDispatcher for permission enforcement.
    """

    def __init__(
        self,
        *,
        process_manager: ProcessManagerProtocol,
        task_manager: TaskManagerProtocol,
        tool_dispatcher: ToolDispatcherProtocol,
    ) -> None:
        self._pm = process_manager
        self._tm = task_manager
        self._td = tool_dispatcher
        # copilot_pid -> list of (task_id, worker_pid, worker_agent_id)
        self._delegations: dict[str, list[DelegationResult]] = {}
        # Delivery infrastructure (keyed by task_id)
        self._completion_events: dict[str, asyncio.Event] = {}
        self._result_queues: dict[str, asyncio.Queue[Any]] = {}
        self._delivery_policies: dict[str, DeliveryPolicy] = {}

    async def delegate(
        self,
        copilot_pid: str,
        message: str,
        worker_config: WorkerConfig,
    ) -> DelegationResult:
        """Delegate work to a new worker.

        1. Spawns worker process (child of copilot)
        2. Creates AccessManifest restricting worker permissions
        3. Creates A2A task for tracking
        4. Returns DelegationResult
        """
        # Spawn worker process
        worker_proc = await self._pm.spawn(
            worker_config.agent_id,
            worker_config.zone_id,
            parent_pid=copilot_pid,
            metadata={
                "budget_tokens": worker_config.budget_tokens,
                "delivery_policy": worker_config.delivery_policy.value,
                **worker_config.metadata,
            },
        )

        # Set up permission restrictions
        copilot_proc = await self._pm.get_process(copilot_pid)
        copilot_agent_id = copilot_proc.agent_id if copilot_proc else "unknown"

        manifest = _build_worker_manifest(
            worker_config=worker_config,
            created_by=copilot_agent_id,
        )
        self._td.set_manifest(worker_config.agent_id, manifest)

        # Create A2A task (importlib to avoid tier violation)
        import importlib as _il

        _a2a = _il.import_module("nexus.bricks.a2a.models")
        task = await self._tm.create_task(
            _a2a.Message(role="user", parts=[_a2a.TextPart(text=message)]),
            zone_id=worker_config.zone_id,
            agent_id=worker_config.agent_id,
            metadata={
                "copilot_pid": copilot_pid,
                "worker_pid": worker_proc.pid,
                "budget_tokens": worker_config.budget_tokens,
                "delivery_policy": worker_config.delivery_policy.value,
            },
        )

        result = DelegationResult(
            task_id=task.id,
            worker_pid=worker_proc.pid,
            worker_agent_id=worker_config.agent_id,
            delivery_policy=worker_config.delivery_policy,
        )

        # Set up delivery infrastructure
        policy = worker_config.delivery_policy
        self._completion_events[task.id] = asyncio.Event()
        self._delivery_policies[task.id] = policy
        if policy == DeliveryPolicy.IMMEDIATE:
            self._result_queues[task.id] = asyncio.Queue(maxsize=256)

        # Launch background worker coordinator
        asyncio.create_task(
            self._run_worker(
                task_id=task.id,
                worker_pid=worker_proc.pid,
                worker_config=worker_config,
                zone_id=worker_config.zone_id,
            )
        )

        # Track delegation
        self._delegations.setdefault(copilot_pid, []).append(result)

        logger.debug(
            "Delegated task %s to worker %s (pid=%s) from copilot %s [%s]",
            task.id,
            worker_config.agent_id,
            worker_proc.pid,
            copilot_pid,
            policy.value,
        )
        return result

    async def _run_worker(
        self,
        task_id: str,
        worker_pid: str,  # noqa: ARG002
        worker_config: WorkerConfig,  # noqa: ARG002
        zone_id: str,
    ) -> None:
        """Background coordinator for a delegated worker.

        Updates A2A task state and signals completion via Event/Queue.
        Actual worker execution happens via a separate ProcessManager.resume()
        call — this method coordinates the lifecycle signals.

        Current implementation: placeholder that transitions SUBMITTED →
        WORKING immediately. Real execution (and WORKING → COMPLETED
        transition) will be driven by the agent loop integration.
        """
        import importlib as _il

        _TaskState = _il.import_module("nexus.bricks.a2a.models").TaskState

        try:
            # Transition to WORKING (worker spawned, ready for execution)
            await self._tm.update_task_state(task_id, _TaskState.WORKING, zone_id=zone_id)
        except Exception:
            logger.warning(
                "Failed to transition task %s to WORKING",
                task_id,
                exc_info=True,
            )

    async def complete_task(self, task_id: str, *, zone_id: str) -> None:
        """Signal that a delegated worker has finished.

        Called externally (e.g. by the agent loop) when the worker completes.
        Transitions task to COMPLETED and unblocks collect()/stream() waiters.
        """
        import importlib as _il

        _TaskState = _il.import_module("nexus.bricks.a2a.models").TaskState

        event = self._completion_events.get(task_id)
        queue = self._result_queues.get(task_id)

        try:
            await self._tm.update_task_state(task_id, _TaskState.COMPLETED, zone_id=zone_id)
        except Exception:
            logger.warning(
                "Failed to transition task %s to COMPLETED",
                task_id,
                exc_info=True,
            )
        finally:
            if queue is not None:
                await queue.put(None)  # sentinel
            if event is not None:
                event.set()

    async def fail_task(self, task_id: str, *, zone_id: str) -> None:
        """Signal that a delegated worker has failed.

        Called externally when the worker errors out.
        """
        import importlib as _il

        _TaskState = _il.import_module("nexus.bricks.a2a.models").TaskState

        event = self._completion_events.get(task_id)
        queue = self._result_queues.get(task_id)

        try:
            await self._tm.update_task_state(task_id, _TaskState.FAILED, zone_id=zone_id)
        except Exception:
            logger.warning(
                "Failed to transition task %s to FAILED",
                task_id,
                exc_info=True,
            )
        finally:
            if queue is not None:
                await queue.put(None)  # sentinel
            if event is not None:
                event.set()

    async def push_event(self, task_id: str, event: Any) -> None:
        """Push a progress event to the task's result queue (IMMEDIATE policy).

        Called externally to deliver streaming results.
        """
        queue = self._result_queues.get(task_id)
        if queue is not None:
            await queue.put(event)

    async def stream(self, task_id: str, *, zone_id: str) -> AsyncIterator[Any]:  # noqa: ARG002
        """Stream results from a delegated task (IMMEDIATE policy).

        Yields progress events as they arrive via asyncio.Queue.
        Uses the same queue+sentinel pattern as ProcessManager.resume().

        Raises:
            ValueError: If the task uses ON_DEMAND delivery policy.
            KeyError: If the task_id is not tracked.
        """
        policy = self._delivery_policies.get(task_id)
        if policy == DeliveryPolicy.ON_DEMAND:
            msg = f"Use collect() for ON_DEMAND policy (task {task_id})"
            raise ValueError(msg)

        queue = self._result_queues.get(task_id)
        if queue is None:
            msg = f"No result queue for task {task_id} (policy={policy})"
            raise KeyError(msg)

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            # Cleanup queue on exit
            self._result_queues.pop(task_id, None)

    async def collect(self, task_id: str, *, zone_id: str) -> Any:
        """Await task completion (non-blocking), then return the task.

        Uses asyncio.Event to wait for completion instead of polling.
        Falls back to direct get_task() for backward compatibility.
        """
        event = self._completion_events.get(task_id)
        if event is not None:
            await event.wait()
        return await self._tm.get_task(task_id, zone_id=zone_id)

    async def cancel(self, task_id: str, *, zone_id: str) -> None:
        """Cancel a delegated task and terminate the worker.

        Cancels the A2A task, terminates the worker process, and
        unblocks any collect()/stream() waiters.
        """
        task = await self._tm.get_task(task_id, zone_id=zone_id)

        # Cancel A2A task (if not already terminal, importlib to avoid tier violation)
        import importlib as _il

        _TaskState = _il.import_module("nexus.bricks.a2a.models").TaskState

        if task.status.state not in {
            _TaskState.COMPLETED,
            _TaskState.FAILED,
            _TaskState.CANCELED,
            _TaskState.REJECTED,
        }:
            await self._tm.cancel_task(task_id, zone_id=zone_id)

        # Terminate worker process (if still running)
        worker_pid = (task.metadata or {}).get("worker_pid")
        if worker_pid:
            proc = await self._pm.get_process(worker_pid)
            if proc and proc.state == ProcessState.RUNNING:
                await self._pm.terminate(worker_pid, reason="delegation cancelled")

        # Unblock any stream() consumers
        queue = self._result_queues.pop(task_id, None)
        if queue is not None:
            await queue.put(None)  # sentinel

        # Unblock any collect() waiters
        event = self._completion_events.pop(task_id, None)
        if event is not None:
            event.set()

        # Cleanup delivery policy tracking
        self._delivery_policies.pop(task_id, None)

    async def cancel_all(self, copilot_pid: str, *, zone_id: str) -> int:
        """Cancel all active delegations for a copilot.

        Returns the number of tasks cancelled.
        """
        delegations = self._delegations.get(copilot_pid, [])
        cancelled = 0

        for delegation in delegations:
            try:
                await self.cancel(delegation.task_id, zone_id=zone_id)
                cancelled += 1
            except Exception:
                logger.warning("Failed to cancel delegation %s", delegation.task_id, exc_info=True)

        return cancelled

    async def list_delegations(
        self,
        copilot_pid: str,
        *,
        zone_id: str,  # noqa: ARG002
    ) -> list[DelegationResult]:
        """List all delegations for a copilot."""
        return list(self._delegations.get(copilot_pid, []))


def _build_worker_manifest(
    *,
    worker_config: WorkerConfig,
    created_by: str,
) -> AccessManifest:
    """Build an AccessManifest that restricts worker to allowed tools.

    The inherit-and-restrict model:
    - ALLOW entries for each tool in the allowlist (glob patterns)
    - Final DENY wildcard to block everything else
    """
    entries: list[ManifestEntry] = []

    for pattern in worker_config.tool_allowlist:
        entries.append(ManifestEntry(tool_pattern=pattern, permission=ToolPermission.ALLOW))

    # Only add DENY wildcard if the allowlist isn't already "*"
    if "*" not in worker_config.tool_allowlist:
        entries.append(ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY))

    return AccessManifest(
        id=f"manifest-{worker_config.agent_id}",
        agent_id=worker_config.agent_id,
        zone_id=worker_config.zone_id,
        name=f"{worker_config.agent_id}-restricted",
        entries=tuple(entries),
        status="active",
        valid_from=datetime.now(tz=UTC).isoformat(),
        valid_until=None,
        created_by=created_by,
    )
