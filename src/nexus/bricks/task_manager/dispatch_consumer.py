"""DT_PIPE-backed dispatch consumer for task lifecycle events.

Produces task signals into a kernel ring buffer pipe and consumes them
in a background asyncio task, following the same pattern as
``WorkflowDispatchService`` and ``ZoektPipeConsumer``.

Flow::

    TaskWriteHook.on_post_write() [sync, kernel dispatch]
      → TaskDispatchPipeConsumer.on_task_created/updated() [TaskEventHandler]
        → JSON → pipe_write_nowait("/nexus/pipes/task-dispatch")

    Background _consume() [asyncio.Task]
      → pipe_read() → JSON → _dispatch()
        → "task_created" → log (placeholder: worker starts)
        → "task_updated" + status="in_review" → log (placeholder: copilot reviews)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.bricks.task_manager.events import (
    TaskCreatedEvent,
    TaskUpdatedEvent,
)

if TYPE_CHECKING:
    from nexus.core.pipe_manager import PipeManager

logger = logging.getLogger(__name__)

_TASK_DISPATCH_PIPE_PATH = "/nexus/pipes/task-dispatch"
_TASK_DISPATCH_PIPE_CAPACITY = 65_536  # 64KB


class TaskDispatchPipeConsumer:
    """Produces and consumes task dispatch signals via DT_PIPE.

    Implements ``TaskEventHandler`` so it can be registered with
    :class:`TaskWriteHook`.  The producer side serialises events to JSON
    and writes them into the kernel pipe.  The consumer side runs a
    background ``asyncio.Task`` that reads from the pipe and routes
    signals via ``_dispatch()``.

    Lifecycle (deferred injection)::

        consumer = TaskDispatchPipeConsumer()
        task_write_hook.register_handler(consumer)
        consumer.set_pipe_manager(pipe_manager)
        await consumer.start()
        ...
        await consumer.stop()
    """

    def __init__(self) -> None:
        self._pipe_manager: PipeManager | None = None
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def set_pipe_manager(self, pipe_manager: PipeManager) -> None:
        """Inject PipeManager after server lifespan initialization."""
        self._pipe_manager = pipe_manager

    # ------------------------------------------------------------------
    # TaskEventHandler (producer side)
    # ------------------------------------------------------------------

    def on_task_created(self, event: TaskCreatedEvent) -> None:
        """Serialize task_created event and write to pipe."""
        self._fire("task_created", asdict(event))

    def on_task_updated(self, event: TaskUpdatedEvent) -> None:
        """Serialize task_updated event and write to pipe."""
        self._fire("task_updated", asdict(event))

    def _fire(self, signal_type: str, payload: dict[str, Any]) -> None:
        """Encode signal as JSON and push into the pipe (non-blocking)."""
        if self._pipe_manager is None or not self._pipe_ready:
            # Pipe not ready yet — drop the signal (dispatch hints, not audit)
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
        """Graceful shutdown: signal pipe closed, drain, then cancel."""
        if self._consumer_task is not None and not self._consumer_task.done():
            if self._pipe_manager is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._pipe_manager.signal_close(_TASK_DISPATCH_PIPE_PATH)

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
    # Dispatch routing
    # ------------------------------------------------------------------

    _LOG_FILE = "/tmp/nexus-log"
    _API_BASE = "http://localhost:2026/api/v2"

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route a deserialized pipe message by signal type."""
        signal_type = msg.get("type")
        payload = msg.get("payload", {})
        ts = datetime.now(UTC).isoformat()

        if signal_type == "task_created":
            task_id = payload.get("task_id", "?")
            mission_id = payload.get("mission_id", "?")
            await self._api_audit(task_id, "task_created", detail="task created")

            # Check if task is blocked
            blocked_by = payload.get("blocked_by") or []
            if blocked_by:
                self._shell_log(
                    f"[{ts}] task_created task_id={task_id} "
                    f"mission_id={mission_id} → blocked by {blocked_by}"
                )
                return

            self._shell_log(
                f"[{ts}] task_created task_id={task_id} "
                f"mission_id={mission_id} → worker starts work"
            )
            await self._start_worker(task_id)

        elif signal_type == "task_updated":
            task_id = payload.get("task_id", "?")
            status = payload.get("status", "?")
            if status == "in_review":
                self._shell_log(
                    f"[{ts}] task_updated task_id={task_id} status={status} → copilot reviews"
                )
                try:
                    worker_comment = await self._api_get_last_comment(task_id)
                    review = await self._llm_prompt(
                        f"Review this worker output and give brief feedback:\n\n{worker_comment}"
                    )
                    await self._api_comment(task_id, "copilot", review)
                    await self._api_patch_status(task_id, "completed")
                    await self._api_audit(
                        task_id,
                        "status_changed",
                        actor="copilot",
                        detail="task completed by copilot",
                    )
                except Exception as e:
                    self._shell_log(f"  copilot failed: {e}")
                    await self._api_comment(task_id, "copilot", f"Review failed: {e}")
                    await self._api_patch_status(task_id, "failed")
                    await self._api_audit(
                        task_id, "status_changed", actor="copilot", detail=f"review failed: {e}"
                    )
            elif status in ("completed", "failed", "cancelled"):
                self._shell_log(
                    f"[{ts}] task_updated task_id={task_id} status={status} "
                    f"→ checking for unblocked tasks"
                )
                await self._dispatch_unblocked_tasks()
            else:
                self._shell_log(f"[{ts}] task_updated task_id={task_id} status={status}")
        else:
            self._shell_log(f"[{ts}] unknown signal type: {signal_type}")

    async def _start_worker(self, task_id: str) -> None:
        """Run the worker phase: fetch instruction → LLM → comment → in_review."""
        try:
            instruction = await self._api_get_instruction(task_id)
            await self._api_patch_status(task_id, "running")
            await self._api_audit(
                task_id, "status_changed", actor="worker", detail="task started by worker"
            )
            worker_response = await self._llm_prompt(instruction)
            await self._api_comment(task_id, "worker", worker_response)
            await self._api_patch_status(task_id, "in_review")
            await self._api_audit(
                task_id, "status_changed", actor="worker", detail="status changed to in_review"
            )
        except Exception as e:
            self._shell_log(f"  worker failed: {e}")
            await self._api_comment(task_id, "worker", f"Worker failed: {e}")
            await self._api_patch_status(task_id, "failed")
            await self._api_audit(
                task_id, "status_changed", actor="worker", detail=f"task failed: {e}"
            )

    async def _dispatch_unblocked_tasks(self) -> None:
        """Check for dispatchable tasks (created + unblocked) and start them."""
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            f"{self._API_BASE}/tasks",
            "-H",
            "Content-Type: application/json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        try:
            tasks = json.loads(stdout.decode())
        except Exception:
            return
        for t in tasks:
            tid = t.get("id", "?")
            self._shell_log(f"  unblocked: dispatching task {tid}")
            await self._start_worker(tid)

    async def _api_comment(self, task_id: str, author: str, content: str) -> None:
        """Add a comment to a task via REST API."""
        data = json.dumps({"task_id": task_id, "author": author, "content": content})
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            "-X",
            "POST",
            f"{self._API_BASE}/comments",
            "-H",
            "Content-Type: application/json",
            "-d",
            data,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        self._shell_log(f"  comment({author}): {stdout.decode().strip()}")

    async def _api_patch_status(self, task_id: str, status: str) -> None:
        """Transition a task's status via REST API."""
        data = json.dumps({"status": status})
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            "-X",
            "PATCH",
            f"{self._API_BASE}/tasks/{task_id}",
            "-H",
            "Content-Type: application/json",
            "-d",
            data,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        self._shell_log(f"  patch({status}): {stdout.decode().strip()}")

    async def _llm_prompt(self, prompt: str) -> str:
        """Run `gemini -p <prompt>` and return stdout."""
        proc = await asyncio.create_subprocess_exec(
            "gemini",
            "-p",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        result = stdout.decode().strip()
        # gemini may prefix MCP/tool warnings — strip lines before actual content
        clean_lines = []
        for line in result.splitlines():
            stripped = line.strip()
            if stripped.startswith(("MCP ", "Run /mcp", "[MCP")):
                continue
            clean_lines.append(line)
        result = "\n".join(clean_lines).strip()
        if not result:
            result = "(no response)"
        self._shell_log(f"  gemini: {result[:120]}...")
        return result

    async def _api_get_instruction(self, task_id: str) -> str:
        """Fetch task instruction via REST API."""
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            f"{self._API_BASE}/tasks/{task_id}",
            "-H",
            "Content-Type: application/json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        try:
            return json.loads(stdout.decode()).get("instruction", "do the task")
        except Exception:
            return "do the task"

    async def _api_get_last_comment(self, task_id: str) -> str:
        """Fetch the last comment content for a task."""
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            f"{self._API_BASE}/comments?task_id={task_id}",
            "-H",
            "Content-Type: application/json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        try:
            comments = json.loads(stdout.decode())
            if comments:
                return comments[-1].get("content", "")
        except Exception:
            pass
        return "(no worker comment found)"

    async def _api_audit(
        self,
        task_id: str,
        action: str,
        *,
        actor: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Create an audit trail entry via REST API."""
        payload: dict[str, Any] = {"action": action}
        if actor is not None:
            payload["actor"] = actor
        if detail is not None:
            payload["detail"] = detail
        data = json.dumps(payload)
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            "-X",
            "POST",
            f"{self._API_BASE}/tasks/{task_id}/audit",
            "-H",
            "Content-Type: application/json",
            "-d",
            data,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        self._shell_log(f"  audit({action}): {stdout.decode().strip()}")

    def _shell_log(self, line: str) -> None:
        """Append a line to /tmp/nexus-log via bash."""
        try:
            subprocess.run(
                ["bash", "-c", f"echo {line!r} >> {self._LOG_FILE}"],
                timeout=2,
            )
        except Exception as e:
            logger.warning("[TASK-DISPATCH] shell_log failed: %s", e)
