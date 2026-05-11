"""DT_PIPE-backed dispatch consumer for task lifecycle signals.

Produces task signals into a kernel ring buffer pipe and consumes them
in a background asyncio task, following the same pattern as
``WorkflowDispatchService`` and ``ZoektWriteObserver``.

Flow::

    TaskWriteHook.on_post_write() [sync, kernel dispatch]
      → TaskDispatchPipeConsumer.on_task_signal(signal_type, payload)
        → JSON → sys_write("/nexus/pipes/task-dispatch")

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
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.task_manager.service import TaskManagerService
    from nexus.core.nexus_fs import NexusFS

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
        consumer.set_nx(nx)
        consumer.set_task_service(task_manager_service)
        await consumer.start()
        ...
        await consumer.stop()
    """

    def __init__(
        self,
        *,
        acp_service: Any | None = None,
        agent_registry: Any | None = None,
        llm_fn: LLMCallable | None = None,
        max_worker_starts: int = 16,
    ) -> None:
        self._nx: NexusFS | None = None
        self._task_svc: TaskManagerService | None = None
        self._acp_service = acp_service
        self._agent_registry = agent_registry
        self._llm_fn: LLMCallable = llm_fn or _no_llm  # used by copilot review
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None
        self._max_worker_starts = max(1, max_worker_starts)
        self._worker_start_semaphore: asyncio.Semaphore | None = None
        self._worker_tasks: set[asyncio.Task[None]] = set()
        self._worker_task_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def set_nx(self, nx: "NexusFS") -> None:
        """Inject NexusFS for Rust-kernel pipe I/O."""
        self._nx = nx

    def set_task_service(self, task_svc: TaskManagerService) -> None:
        """Inject TaskManagerService for direct VFS-backed operations."""
        self._task_svc = task_svc

    def set_acp_service(self, acp_service: Any) -> None:
        self._acp_service = acp_service

    def set_agent_registry(self, agent_registry: Any) -> None:
        self._agent_registry = agent_registry

    # ------------------------------------------------------------------
    # TaskSignalHandler (producer side)
    # ------------------------------------------------------------------

    def on_task_signal(self, signal_type: str, payload: dict[str, Any]) -> None:
        """Serialize signal and write to pipe via sys_write (non-blocking for DT_PIPE)."""
        if self._nx is None or not self._pipe_ready:
            return

        try:
            data = json.dumps({"type": signal_type, "payload": payload}).encode()
            self._nx.sys_write(_TASK_DISPATCH_PIPE_PATH, data)
        except Exception:
            logger.warning("[TASK-DISPATCH] pipe full/closed, dropping signal: %s", signal_type)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the dispatch pipe and start the background consumer."""
        if self._pipe_ready:
            return

        if self._nx is None:
            return

        from nexus.contracts.metadata import DT_PIPE

        try:
            self._nx.sys_setattr(
                _TASK_DISPATCH_PIPE_PATH,
                entry_type=DT_PIPE,
                capacity=_TASK_DISPATCH_PIPE_CAPACITY,
                owner_id="kernel",
            )
        except Exception as exc:
            logger.warning("[TASK-DISPATCH] pipe unavailable, dispatch disabled: %s", exc)
            return

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())
        logger.info("[TASK-DISPATCH] pipe consumer started")

    async def stop(self) -> None:
        """Graceful shutdown: close pipe, cancel consumer task."""
        if self._consumer_task is not None and not self._consumer_task.done():
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._nx.pipe_close(_TASK_DISPATCH_PIPE_PATH)

            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task

            self._consumer_task = None

        if self._worker_tasks:
            for task in list(self._worker_tasks):
                task.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
            self._worker_task_ids.clear()

        self._pipe_ready = False
        logger.info("[TASK-DISPATCH] pipe consumer stopped")

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Background loop: read from pipe via sys_read, deserialize, dispatch."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert self._nx is not None

        while True:
            try:
                data = await asyncio.to_thread(
                    self._nx.sys_read,
                    _TASK_DISPATCH_PIPE_PATH,
                    timeout_ms=0,
                )
            except NexusFileNotFoundError:
                logger.debug("[TASK-DISPATCH] pipe closed, consumer exiting")
                break

            if not data:
                await asyncio.sleep(0.01)
                continue

            # _TASK_DISPATCH_PIPE_PATH is a DT_PIPE — sys_read returns
            # raw bytes for DT_PIPE (only DT_STREAM returns the dict shape).
            assert isinstance(data, bytes)

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

    def _build_worker_prompt(self, task_id: str, instruction: str) -> str:
        """Build an enriched prompt that keeps task reporting in-process."""
        return f"""\
You are a worker agent assigned to task {task_id}.

## Your Task
{instruction}

## When Done
Return your work output directly in your final response.
Do not call external tools or APIs to report task status.
The task manager will record your response and move the task into review.
"""

    @staticmethod
    def _result_text(result: Any) -> str:
        """Extract a human-facing response from an ACP result."""
        response = str(getattr(result, "response", "") or "").strip()
        if response:
            return response
        stdout = str(getattr(result, "raw_stdout", "") or "").strip()
        if stdout:
            return stdout
        return "(agent returned no output)"

    @staticmethod
    def _parse_review_response(text: str) -> tuple[str, str]:
        """Parse copilot review output into a decision and comment body."""
        stripped = text.strip()
        decision_match = re.search(r"^\s*DECISION:\s*(approve|reject)\s*$", stripped, re.I | re.M)
        decision = decision_match.group(1).lower() if decision_match else "approve"

        review_match = re.search(r"^\s*REVIEW:\s*(.*)$", stripped, re.I | re.M | re.S)
        if review_match:
            review = review_match.group(1).strip()
        else:
            review = re.sub(
                r"^\s*DECISION:\s*(approve|reject)\s*$", "", stripped, flags=re.I | re.M
            )
            review = review.strip()

        if not review:
            review = "(copilot returned no review text)"

        return decision, review

    async def _start_worker(self, task_id: str) -> None:
        """Spawn worker agent via AcpService — environment-driven completion."""
        assert self._task_svc is not None
        svc = self._task_svc

        try:
            task = await svc.get_task(task_id)
            instruction = task.get("instruction", "do the task")
            agent_id = task.get("worker_type") or "claude"

            await svc.update_task(task_id, status="running")
            await svc.create_audit_entry(
                task_id, "status_changed", actor="system", detail="task started"
            )

            if self._acp_service is None:
                logger.error("[TASK-DISPATCH] AcpService not wired for task %s", task_id)
                await svc.update_task(task_id, status="failed")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="system", detail="AcpService unavailable"
                )
                return

            from nexus.contracts.constants import ROOT_ZONE_ID

            enriched_prompt = self._build_worker_prompt(task_id, instruction)

            result = await self._acp_service.call_agent(
                agent_id=agent_id,
                prompt=enriched_prompt,
                owner_id="task_manager",
                zone_id=ROOT_ZONE_ID,
                labels={"task_id": task_id},
            )

            agent_label = f"{result.agent_id}:{result.pid}"
            await svc.update_task(task_id, worker_pid=result.pid, agent_name=result.agent_id)
            await svc.create_comment(task_id, "worker", self._result_text(result))

            # Advance review in-process after recording the worker output.
            current = await svc.get_task(task_id)
            if current.get("status") not in ("in_review", "completed", "failed"):
                await svc.update_task(task_id, status="in_review")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="system", detail="worker output recorded"
                )
            final_status = (await svc.get_task(task_id)).get("status")
            logger.info(
                "[TASK-DISPATCH] worker done for task %s (agent=%s, status=%s)",
                task_id,
                agent_label,
                final_status,
            )

        except Exception as e:
            logger.error("[TASK-DISPATCH] worker failed for task %s: %s", task_id, e)
            try:
                await svc.create_comment(task_id, "system", f"Worker failed: {e}")
                await svc.update_task(task_id, status="failed")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="system", detail=f"task failed: {e}"
                )
            except Exception:
                logger.error("[TASK-DISPATCH] cleanup after failure also failed", exc_info=True)

    async def _copilot_review(self, task_id: str) -> None:
        """Copilot reviews the worker output via ACP (gemini) and completes or fails the task."""
        assert self._task_svc is not None
        svc = self._task_svc

        try:
            task = await svc.get_task(task_id)
            instruction = task.get("instruction", "")
            comments = await svc.get_comments(task_id)
            last_content = comments[-1].get("content", "") if comments else "(no worker comment)"

            if self._acp_service is None:
                logger.warning(
                    "[TASK-DISPATCH] AcpService not wired for copilot review on task %s", task_id
                )
                await svc.create_comment(
                    task_id, "copilot", "(copilot review skipped — AcpService unavailable)"
                )
                await svc.update_task(task_id, status="completed")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="copilot", detail="auto-completed (no ACP)"
                )
                return

            from nexus.contracts.constants import ROOT_ZONE_ID

            review_prompt = f"""\
You are a copilot reviewer for task {task_id}.

## Original Task Instruction
{instruction}

## Worker Output
{last_content}

## Your Job
Review the worker's output above. Check if it adequately addresses the task instruction.
Return a concise review in this exact format:

DECISION: approve or reject
REVIEW: 2-3 sentences of feedback

## When Done
Do not call external tools or APIs. The task manager will record your review
and apply the final task status from your DECISION line.
"""
            result = await self._acp_service.call_agent(
                agent_id="gemini",
                prompt=review_prompt,
                owner_id="task_manager",
                zone_id=ROOT_ZONE_ID,
                labels={"task_id": task_id, "role": "copilot"},
            )

            copilot_label = f"{result.agent_id}:{result.pid}"
            decision, review = self._parse_review_response(self._result_text(result))
            await svc.create_comment(task_id, "copilot", review)

            # Advance completion in-process from the copilot decision.
            current = await svc.get_task(task_id)
            if current.get("status") not in ("completed", "failed"):
                final_status = "failed" if decision == "reject" else "completed"
                await svc.update_task(task_id, status=final_status)
                await svc.create_audit_entry(
                    task_id,
                    "status_changed",
                    actor="copilot",
                    detail=f"review recorded → {final_status}",
                )
            final_status = str((await svc.get_task(task_id)).get("status"))
            logger.info(
                "[TASK-DISPATCH] copilot done for task %s (agent=%s, status=%s)",
                task_id,
                copilot_label,
                final_status,
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
            self._schedule_worker_start(tid)

    def _get_worker_start_semaphore(self) -> asyncio.Semaphore:
        if self._worker_start_semaphore is None:
            self._worker_start_semaphore = asyncio.Semaphore(self._max_worker_starts)
        return self._worker_start_semaphore

    def _schedule_worker_start(self, task_id: str) -> None:
        if task_id in self._worker_task_ids:
            logger.debug("[TASK-DISPATCH] worker start already scheduled for task %s", task_id)
            return

        self._worker_task_ids.add(task_id)
        task = asyncio.create_task(self._run_scheduled_worker(task_id))
        self._worker_tasks.add(task)

        def _discard_done(done: asyncio.Task[None]) -> None:
            self._on_worker_task_done(task_id, done)

        task.add_done_callback(_discard_done)

    async def _run_scheduled_worker(self, task_id: str) -> None:
        try:
            async with self._get_worker_start_semaphore():
                await self._start_worker(task_id)
        finally:
            current = asyncio.current_task()
            if current is not None:
                self._worker_tasks.discard(current)
            self._worker_task_ids.discard(task_id)

    def _on_worker_task_done(self, task_id: str, task: asyncio.Task[None]) -> None:
        self._worker_tasks.discard(task)
        self._worker_task_ids.discard(task_id)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "[TASK-DISPATCH] scheduled worker task failed for %s: %s",
                task_id,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
