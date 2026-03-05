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

import logging
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.access_manifest_types import (
    AccessManifest,
    ManifestEntry,
    ToolPermission,
)
from nexus.contracts.agent_runtime_types import (
    DelegationResult,
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

        # Create A2A task (lazy import to avoid LEGO violation)
        from nexus.bricks.a2a.models import Message, TextPart

        task = await self._tm.create_task(
            Message(role="user", parts=[TextPart(text=message)]),
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
        )

        # Track delegation
        self._delegations.setdefault(copilot_pid, []).append(result)

        logger.debug(
            "Delegated task %s to worker %s (pid=%s) from copilot %s",
            task.id,
            worker_config.agent_id,
            worker_proc.pid,
            copilot_pid,
        )
        return result

    async def collect(self, task_id: str, *, zone_id: str) -> Any:
        """Collect results from a delegated task.

        Returns the A2A Task object with artifacts and status.
        """
        return await self._tm.get_task(task_id, zone_id=zone_id)

    async def cancel(self, task_id: str, *, zone_id: str) -> None:
        """Cancel a delegated task and terminate the worker.

        Cancels the A2A task and terminates the worker process.
        """
        task = await self._tm.get_task(task_id, zone_id=zone_id)

        # Cancel A2A task (if not already terminal, lazy import)
        from nexus.bricks.a2a.models import TaskState

        if task.status.state not in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.REJECTED,
        }:
            await self._tm.cancel_task(task_id, zone_id=zone_id)

        # Terminate worker process (if still running)
        worker_pid = (task.metadata or {}).get("worker_pid")
        if worker_pid:
            proc = await self._pm.get_process(worker_pid)
            if proc and proc.state == ProcessState.RUNNING:
                await self._pm.terminate(worker_pid, reason="delegation cancelled")

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
