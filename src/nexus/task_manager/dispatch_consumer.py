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
    ) -> None:
        self._nx: NexusFS | None = None
        self._task_svc: TaskManagerService | None = None
        self._acp_service = acp_service
        self._agent_registry = agent_registry
        self._llm_fn: LLMCallable = llm_fn or _no_llm  # used by copilot review
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None
        self._server_base_url: str = "http://127.0.0.1:2026"
        self._api_key: str = ""

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

    def set_server_info(self, base_url: str, api_key: str) -> None:
        """Inject server base URL and API key for enriched worker prompts."""
        self._server_base_url = base_url
        self._api_key = api_key

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
                data = self._nx.sys_read(_TASK_DISPATCH_PIPE_PATH)
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

    def _build_worker_prompt(self, task_id: str, instruction: str) -> str:
        """Build an enriched prompt that gives the worker agent API context."""
        base = self._server_base_url
        return f"""\
You are a worker agent assigned to task {task_id}.

## Your Task
{instruction}

## When Done
After completing your work, use curl to report results:

1. Add a comment with your work output:
   curl -X POST {base}/api/v2/comments \\
     -H "Content-Type: application/json" \\
     -d '{{"task_id":"{task_id}","author":"worker","content":"YOUR_RESULT"}}'

2. Update the task status to "in_review":
   curl -X PATCH {base}/api/v2/tasks/{task_id} \\
     -H "Content-Type: application/json" \\
     -d '{{"status":"in_review"}}'

## API Reference
Base URL: {base}

| Method | Endpoint                              | Body                                          |
|--------|---------------------------------------|-----------------------------------------------|
| GET    | /api/v2/tasks/{task_id}               | —                                             |
| PATCH  | /api/v2/tasks/{task_id}               | {{status, output_refs}}                         |
| POST   | /api/v2/comments                      | {{task_id, author, content, artifact_refs?}}    |
| GET    | /api/v2/comments?task_id={task_id}    | —                                             |
| POST   | /api/v2/tasks/{task_id}/audit         | {{action, actor, detail}}                       |
| GET    | /api/v2/tasks/{task_id}/history       | —                                             |

Status flow: created → running → in_review → completed
"""

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

            # Ensure status advances — agent may not have called the PATCH curl
            current = await svc.get_task(task_id)
            if current.get("status") not in ("in_review", "completed", "failed"):
                await svc.update_task(task_id, status="in_review")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="system", detail="worker done → in_review"
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

            base = self._server_base_url
            review_prompt = f"""\
You are a copilot reviewer for task {task_id}.

## Original Task Instruction
{instruction}

## Worker Output
{last_content}

## Your Job
Review the worker's output above. Check if it adequately addresses the task instruction.
Give brief feedback (2-3 sentences). Then decide: approve or reject.

## When Done
1. Add your review comment:
   curl -X POST {base}/api/v2/comments \\
     -H "Content-Type: application/json" \\
     -d '{{"task_id":"{task_id}","author":"copilot","content":"YOUR_REVIEW"}}'

2. If approved, mark completed:
   curl -X PATCH {base}/api/v2/tasks/{task_id} \\
     -H "Content-Type: application/json" \\
     -d '{{"status":"completed"}}'

   If rejected, mark failed:
   curl -X PATCH {base}/api/v2/tasks/{task_id} \\
     -H "Content-Type: application/json" \\
     -d '{{"status":"failed"}}'
"""
            result = await self._acp_service.call_agent(
                agent_id="gemini",
                prompt=review_prompt,
                owner_id="task_manager",
                zone_id=ROOT_ZONE_ID,
                labels={"task_id": task_id, "role": "copilot"},
            )

            copilot_label = f"{result.agent_id}:{result.pid}"

            # Ensure status advances — agent may not have called the PATCH curl
            current = await svc.get_task(task_id)
            if current.get("status") not in ("completed", "failed"):
                await svc.update_task(task_id, status="completed")
                await svc.create_audit_entry(
                    task_id, "status_changed", actor="copilot", detail="review done → completed"
                )
            final_status = (await svc.get_task(task_id)).get("status")
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
            # Schedule worker start as a separate task to avoid blocking the consumer
            asyncio.create_task(self._start_worker(tid))
