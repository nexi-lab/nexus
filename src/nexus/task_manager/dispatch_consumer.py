"""DT_PIPE-backed dispatch consumer for task lifecycle signals.

Produces task signals into a kernel ring buffer pipe and consumes them
in a background asyncio task, following the same pattern as
``WorkflowDispatchService`` and ``ZoektPipeConsumer``.

Flow::

    TaskWriteHook.on_post_write() [sync, kernel dispatch]
      → TaskDispatchPipeConsumer.on_task_signal(signal_type, payload)
        → JSON → pipe_write_nowait("/nexus/pipes/task-dispatch")

    Background _consume() [asyncio.Task]
      → pipe_read() → JSON → _dispatch()
        → "task_created" → spawn worker via AcpService
        → "task_updated" + status="in_review" → copilot review via VFS

All state mutations use TaskManagerService (VFS-backed) directly —
no HTTP loopback, no subprocess calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.task_manager.service import TaskManagerService
    from nexus.system_services.pipe_manager import PipeManager

logger = logging.getLogger(__name__)

_TASK_DISPATCH_PIPE_PATH = "/nexus/pipes/task-dispatch"
_TASK_DISPATCH_PIPE_CAPACITY = 65_536  # 64KB

# Type alias for injectable LLM provider (used by copilot review path)
LLMCallable = Callable[[str], Awaitable[str]]


async def _no_llm(_prompt: str) -> str:
    """Placeholder LLM provider — returns a stub until VFS-routed LLM is wired."""
    return "(LLM provider not configured — VFS-routed LLM pending)"


class TaskDispatchPipeConsumer:
    """Produces and consumes task dispatch signals via DT_PIPE.

    Implements ``TaskSignalHandler`` so it can be registered with
    :class:`TaskWriteHook`.  The producer side serialises signals to JSON
    and writes them into the kernel pipe.  The consumer side runs a
    background ``asyncio.Task`` that reads from the pipe and routes
    signals via ``_dispatch()``.

    All state mutations go through ``TaskManagerService`` which uses
    NexusFS ``sys_read``/``sys_write`` — getting file events, permissions,
    and audit trail for free.

    Worker completion is environment-driven: ``AcpService.call_agent()``
    awaits the subprocess to exit (ZOMBIE → reap), so no polling is needed.

    Lifecycle (deferred injection)::

        consumer = TaskDispatchPipeConsumer()
        task_write_hook.register_handler(consumer)
        consumer.set_pipe_manager(pipe_manager)
        consumer.set_task_service(task_manager_service)
        await consumer.start()
        ...
        await consumer.stop()
    """

    def __init__(
        self,
        *,
        acp_service: Any | None = None,
        process_table: Any | None = None,
        llm_fn: LLMCallable | None = None,
    ) -> None:
        self._pipe_manager: PipeManager | None = None
        self._task_svc: TaskManagerService | None = None
        self._acp_service = acp_service
        self._process_table = process_table
        self._llm_fn: LLMCallable = llm_fn or _no_llm  # used by copilot review
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def set_pipe_manager(self, pipe_manager: PipeManager) -> None:
        """Inject PipeManager after server lifespan initialization."""
        self._pipe_manager = pipe_manager

    def set_task_service(self, task_svc: TaskManagerService) -> None:
        """Inject TaskManagerService for direct VFS-backed operations."""
        self._task_svc = task_svc

    def set_acp_service(self, acp_service: Any) -> None:
        self._acp_service = acp_service

    def set_process_table(self, process_table: Any) -> None:
        self._process_table = process_table

    # ------------------------------------------------------------------
    # TaskSignalHandler (producer side)
    # ------------------------------------------------------------------

    def on_task_signal(self, signal_type: str, payload: dict[str, Any]) -> None:
        """Serialize signal and write to pipe (non-blocking)."""
        if self._pipe_manager is None or not self._pipe_ready:
            return

        from nexus.core.pipe import PipeClosedError, PipeFullError

        try:
            data = json.dumps({"type": signal_type, "payload": payload}).encode()
            self._pipe_manager.pipe_write_nowait(_TASK_DISPATCH_PIPE_PATH, data)
        except (PipeClosedError, PipeFullError):
            logger.warning("[TASK-DISPATCH] pipe full/closed, dropping signal: %s", signal_type)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the dispatch pipe and start the background consumer."""
        if self._pipe_ready:
            return

        if self._pipe_manager is None:
            return

        from nexus.core.pipe import PipeError

        try:
            self._pipe_manager.create(
                _TASK_DISPATCH_PIPE_PATH,
                capacity=_TASK_DISPATCH_PIPE_CAPACITY,
                owner_id="kernel",
            )
        except PipeError:
            self._pipe_manager.open(_TASK_DISPATCH_PIPE_PATH, capacity=_TASK_DISPATCH_PIPE_CAPACITY)

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())
        logger.info("[TASK-DISPATCH] pipe consumer started")

    async def stop(self) -> None:
        """Graceful shutdown: close pipe, cancel consumer task."""
        if self._consumer_task is not None and not self._consumer_task.done():
            if self._pipe_manager is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._pipe_manager.close(_TASK_DISPATCH_PIPE_PATH)

            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task

            self._consumer_task = None

        self._pipe_ready = False
        logger.info("[TASK-DISPATCH] pipe consumer stopped")

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Background loop: read from pipe, deserialize, dispatch."""
        from nexus.core.pipe import PipeClosedError, PipeNotFoundError

        assert self._pipe_manager is not None
        pipe_mgr = self._pipe_manager

        while True:
            try:
                data = await pipe_mgr.pipe_read(_TASK_DISPATCH_PIPE_PATH)
            except (PipeClosedError, PipeNotFoundError):
                logger.debug("[TASK-DISPATCH] pipe closed, consumer exiting")
                break

            try:
                msg = json.loads(data)
                await self._dispatch(msg)
            except Exception as e:
                logger.error("[TASK-DISPATCH] event processing failed: %s", e)

    # ------------------------------------------------------------------
    # Dispatch routing — all ops via VFS-backed TaskManagerService
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route a deserialized pipe message by signal type."""
        if self._task_svc is None:
            logger.warning("[TASK-DISPATCH] no TaskManagerService — dropping signal")
            return

        signal_type = msg.get("type")
        payload = msg.get("payload", {})

        if signal_type == "task_created":
            task_id = payload.get("task_id", "?")
            mission_id = payload.get("mission_id", "?")

            # Record audit via VFS
            self._task_svc.create_audit_entry(task_id, "task_created", detail="task created")

            # Check if task is blocked
            blocked_by = payload.get("blocked_by") or []
            if blocked_by:
                logger.info(
                    "[TASK-DISPATCH] task_created task_id=%s mission_id=%s blocked_by=%s",
                    task_id,
                    mission_id,
                    blocked_by,
                )
                return

            logger.info(
                "[TASK-DISPATCH] task_created task_id=%s mission_id=%s → starting worker",
                task_id,
                mission_id,
            )
            await self._start_worker(task_id)

        elif signal_type == "task_updated":
            task_id = payload.get("task_id", "?")
            status = payload.get("status", "?")

            if status == "in_review":
                logger.info("[TASK-DISPATCH] task_id=%s in_review → copilot review", task_id)
                await self._copilot_review(task_id)
            elif status in ("completed", "failed", "cancelled"):
                logger.info(
                    "[TASK-DISPATCH] task_id=%s %s → checking unblocked tasks",
                    task_id,
                    status,
                )
                self._dispatch_unblocked_tasks()
            else:
                logger.debug("[TASK-DISPATCH] task_id=%s status=%s (no action)", task_id, status)
        else:
            logger.warning("[TASK-DISPATCH] unknown signal type: %s", signal_type)

    async def _start_worker(self, task_id: str) -> None:
        """Spawn worker agent via AcpService — environment-driven completion."""
        assert self._task_svc is not None
        svc = self._task_svc

        try:
            task = svc.get_task(task_id)
            instruction = task.get("instruction", "do the task")
            agent_id = task.get("worker_type") or "claude-code"

            svc.update_task(task_id, status="running")
            svc.create_audit_entry(task_id, "status_changed", actor="system", detail="task started")

            if self._acp_service is None:
                logger.error("[TASK-DISPATCH] AcpService not wired for task %s", task_id)
                svc.update_task(task_id, status="failed")
                svc.create_audit_entry(
                    task_id, "status_changed", actor="system", detail="AcpService unavailable"
                )
                return

            from nexus.contracts.constants import ROOT_ZONE_ID

            result = await self._acp_service.call_agent(
                agent_id=agent_id,
                prompt=instruction,
                owner_id="task_manager",
                zone_id=ROOT_ZONE_ID,
                labels={"task_id": task_id},
            )

            agent_label = f"{result.agent_id}:{result.pid}"
            svc.update_task(task_id, worker_pid=result.pid, agent_name=result.agent_id)
            svc.create_comment(task_id, agent_label, result.response)
            svc.create_audit_entry(
                task_id,
                "status_changed",
                actor=agent_label,
                detail="worker completed, pending review",
            )
            svc.update_task(task_id, status="in_review")

        except Exception as e:
            logger.error("[TASK-DISPATCH] worker failed for task %s: %s", task_id, e)
            try:
                svc.create_comment(task_id, "system", f"Worker failed: {e}")
                svc.update_task(task_id, status="failed")
                svc.create_audit_entry(
                    task_id, "status_changed", actor="system", detail=f"task failed: {e}"
                )
            except Exception:
                logger.error("[TASK-DISPATCH] cleanup after failure also failed", exc_info=True)

    async def _copilot_review(self, task_id: str) -> None:
        """Copilot reviews the worker output and completes or fails the task.

        TODO: migrate to AcpService.call_agent() — spawn copilot agent subprocess
        instead of calling LLM directly.
        """
        assert self._task_svc is not None
        svc = self._task_svc

        try:
            comments = svc.get_comments(task_id)
            last_content = comments[-1].get("content", "") if comments else "(no worker comment)"

            review = await self._llm_fn(
                f"Review this worker output and give brief feedback:\n\n{last_content}"
            )

            svc.create_comment(task_id, "copilot", review)
            svc.update_task(task_id, status="completed")
            svc.create_audit_entry(
                task_id, "status_changed", actor="copilot", detail="task completed by copilot"
            )
        except Exception as e:
            logger.error("[TASK-DISPATCH] copilot review failed for task %s: %s", task_id, e)
            try:
                svc.create_comment(task_id, "copilot", f"Review failed: {e}")
                svc.update_task(task_id, status="failed")
                svc.create_audit_entry(
                    task_id, "status_changed", actor="copilot", detail=f"review failed: {e}"
                )
            except Exception:
                logger.error(
                    "[TASK-DISPATCH] cleanup after copilot failure also failed", exc_info=True
                )

    def _dispatch_unblocked_tasks(self) -> None:
        """Check for dispatchable tasks (created + unblocked) and start them."""
        assert self._task_svc is not None

        try:
            dispatchable = self._task_svc.list_dispatchable_tasks()
        except Exception:
            logger.warning("[TASK-DISPATCH] failed to list dispatchable tasks", exc_info=True)
            return

        for task in dispatchable:
            tid = task.get("id", "?")
            logger.info("[TASK-DISPATCH] unblocked: dispatching task %s", tid)
            # Schedule worker start as a separate task to avoid blocking the consumer
            asyncio.create_task(self._start_worker(tid))
