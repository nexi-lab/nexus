"""DT_PIPE-backed dispatch consumer for task lifecycle signals.

Produces task signals into a kernel ring buffer pipe and consumes them
in a background asyncio task, following the same pattern as
``WorkflowDispatchService`` and ``ZoektPipeConsumer``.

Flow::

    TaskWriteHook.on_post_write() [sync, kernel dispatch]
      → TaskDispatchPipeConsumer.on_task_signal(signal_type, payload)
        → JSON → sys_write("/nexus/pipes/task-dispatch")

    Background _consume() [asyncio.Task]
      → pipe_read() → JSON → _dispatch()
        → "task_created" → start worker via VFS syscalls
        → "task_updated" + status="in_review" → copilot review via VFS

All state mutations use TaskManagerService (VFS-backed) directly —
no HTTP loopback, no subprocess calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.task_manager.service import TaskManagerService
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

_TASK_DISPATCH_PIPE_PATH = "/nexus/pipes/task-dispatch"
_TASK_DISPATCH_PIPE_CAPACITY = 65_536  # 64KB

# Type alias for injectable LLM provider
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

    Lifecycle (deferred injection)::

        consumer = TaskDispatchPipeConsumer()
        task_write_hook.register_handler(consumer)
        consumer.bind_fs(nx)
        consumer.set_task_service(task_manager_service)
        await consumer.start()
        ...
        await consumer.stop()
    """

    def __init__(self, *, llm_fn: LLMCallable | None = None) -> None:
        self._nx: NexusFS | None = None
        self._task_svc: TaskManagerService | None = None
        self._llm_fn: LLMCallable = llm_fn or _no_llm
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None
        self._write_buffer: deque[bytes] = deque(maxlen=10_000)
        self._flush_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def bind_fs(self, nx: "NexusFS") -> None:
        """Bind NexusFS for sys_read/sys_write pipe access."""
        self._nx = nx

    def set_task_service(self, task_svc: TaskManagerService) -> None:
        """Inject TaskManagerService for direct VFS-backed operations."""
        self._task_svc = task_svc

    # ------------------------------------------------------------------
    # TaskSignalHandler (producer side)
    # ------------------------------------------------------------------

    def on_task_signal(self, signal_type: str, payload: dict[str, Any]) -> None:
        """Buffer signal for async flush via sys_write (non-blocking)."""
        if self._nx is None or not self._pipe_ready:
            return

        data = json.dumps({"type": signal_type, "payload": payload}).encode()
        self._write_buffer.append(data)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the dispatch pipe via sys_setattr and start background tasks."""
        if self._pipe_ready:
            return

        if self._nx is None:
            return

        from nexus.contracts.metadata import DT_PIPE

        self._nx.sys_setattr(
            _TASK_DISPATCH_PIPE_PATH,
            entry_type=DT_PIPE,
            capacity=_TASK_DISPATCH_PIPE_CAPACITY,
            owner_id="kernel",
        )

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("[TASK-DISPATCH] pipe consumer started")

    async def stop(self) -> None:
        """Graceful shutdown: stop flush, signal pipe closed, drain consumer."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

        if self._consumer_task is not None and not self._consumer_task.done():
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._nx.sys_unlink(_TASK_DISPATCH_PIPE_PATH)

            try:
                await asyncio.wait_for(asyncio.shield(self._consumer_task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer_task

            self._consumer_task = None

        self._pipe_ready = False
        logger.info("[TASK-DISPATCH] pipe consumer stopped")

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Background task: drain _write_buffer into pipe via sys_write."""
        assert self._nx is not None
        nx = self._nx

        while True:
            if self._write_buffer:
                while self._write_buffer:
                    data = self._write_buffer.popleft()
                    try:
                        nx.sys_write(_TASK_DISPATCH_PIPE_PATH, data)
                    except Exception:
                        logger.warning("[TASK-DISPATCH] pipe write failed, dropping signal")
            await asyncio.sleep(0.01)  # 10ms poll interval

    async def _consume(self) -> None:
        """Background loop: read from pipe via sys_read, deserialize, dispatch."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert self._nx is not None
        nx = self._nx

        while True:
            try:
                data = nx.sys_read(_TASK_DISPATCH_PIPE_PATH)
            except NexusFileNotFoundError:
                logger.debug("[TASK-DISPATCH] pipe closed, consumer exiting")
                break

            if not data:
                await asyncio.sleep(0.01)
                continue

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
            await self._task_svc.create_audit_entry(task_id, "task_created", detail="task created")

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
                await self._dispatch_unblocked_tasks()
            else:
                logger.debug("[TASK-DISPATCH] task_id=%s status=%s (no action)", task_id, status)
        else:
            logger.warning("[TASK-DISPATCH] unknown signal type: %s", signal_type)

    async def _start_worker(self, task_id: str) -> None:
        """Run the worker phase: fetch instruction → LLM → comment → in_review."""
        assert self._task_svc is not None
        svc = self._task_svc

        try:
            task = await svc.get_task(task_id)
            instruction = task.get("instruction", "do the task")

            await svc.update_task(task_id, status="running")
            await svc.create_audit_entry(
                task_id, "status_changed", actor="worker", detail="task started by worker"
            )

            worker_response = await self._llm_fn(instruction)

            await svc.create_comment(task_id, "worker", worker_response)
            await svc.update_task(task_id, status="in_review")
            await svc.create_audit_entry(
                task_id, "status_changed", actor="worker", detail="submitted for review"
            )
        except Exception as e:
            logger.error("[TASK-DISPATCH] worker failed for task %s: %s", task_id, e)
            try:
                await svc.create_comment(task_id, "worker", f"Worker failed: {e}")
                await svc.update_task(task_id, status="failed")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="worker", detail=f"task failed: {e}"
                )
            except Exception:
                logger.error(
                    "[TASK-DISPATCH] cleanup after worker failure also failed", exc_info=True
                )

    async def _copilot_review(self, task_id: str) -> None:
        """Copilot reviews the worker output and completes or fails the task."""
        assert self._task_svc is not None
        svc = self._task_svc

        try:
            comments = await svc.get_comments(task_id)
            last_content = comments[-1].get("content", "") if comments else "(no worker comment)"

            review = await self._llm_fn(
                f"Review this worker output and give brief feedback:\n\n{last_content}"
            )

            await svc.create_comment(task_id, "copilot", review)
            await svc.update_task(task_id, status="completed")
            await svc.create_audit_entry(
                task_id, "status_changed", actor="copilot", detail="task completed by copilot"
            )
        except Exception as e:
            logger.error("[TASK-DISPATCH] copilot review failed for task %s: %s", task_id, e)
            try:
                await svc.create_comment(task_id, "copilot", f"Review failed: {e}")
                await svc.update_task(task_id, status="failed")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="copilot", detail=f"review failed: {e}"
                )
            except Exception:
                logger.error(
                    "[TASK-DISPATCH] cleanup after copilot failure also failed", exc_info=True
                )

    async def _dispatch_unblocked_tasks(self) -> None:
        """Check for dispatchable tasks (created + unblocked) and start them."""
        assert self._task_svc is not None

        try:
            dispatchable = await self._task_svc.list_dispatchable_tasks()
        except Exception:
            logger.warning("[TASK-DISPATCH] failed to list dispatchable tasks", exc_info=True)
            return

        for task in dispatchable:
            tid = task.get("id", "?")
            logger.info("[TASK-DISPATCH] unblocked: dispatching task %s", tid)
            # Schedule worker start as a separate task to avoid blocking the consumer
            asyncio.create_task(self._start_worker(tid))
