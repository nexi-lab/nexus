"""VFS write hook that emits task lifecycle signals to DT_PIPE.

Intercepts writes to ``/.tasks/tasks/*.json``, parses the JSON content,
and pushes a signal dict into the task-dispatch pipe.  No intermediate
event dataclasses — the kernel FileEvent already provides the audit
trail; this hook only produces dispatch hints for the consumer.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.task_manager.events import TaskSignalHandler
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import WriteHookContext

logger = logging.getLogger(__name__)

_TASK_PATTERN = "/.tasks/tasks/*.json"


class TaskWriteHook:
    """Post-write hook that pushes task signals to registered handlers.

    Intercepts writes to ``/.tasks/tasks/*.json`` and fires signal dicts
    to registered :class:`TaskSignalHandler` implementations.  No in-memory
    cache — all state comes from the written content and ``ctx.is_new_file``.
    """

    # ── Hook spec (duck-typed) ────────────────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,))

    def __init__(self) -> None:
        self._handlers: list[TaskSignalHandler] = []

    @property
    def name(self) -> str:
        return "task_manager_write"

    def register_handler(self, handler: TaskSignalHandler) -> None:
        """Subscribe a handler to task lifecycle signals."""
        self._handlers.append(handler)

    # ── VFSWriteHook protocol ──────────────────────────────────────────

    def on_post_write(self, ctx: "WriteHookContext") -> None:
        if not fnmatch(ctx.path, _TASK_PATTERN):
            return

        try:
            doc = json.loads(ctx.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("[TASK-HOOK] failed to parse task JSON at %s", ctx.path)
            return

        signal_type = "task_created" if ctx.is_new_file else "task_updated"
        payload: dict[str, Any] = {
            "task_id": doc.get("id", ""),
            "mission_id": doc.get("mission_id", ""),
            "status": doc.get("status", ""),
            "instruction": doc.get("instruction", ""),
            "worker_type": doc.get("worker_type"),
            "blocked_by": doc.get("blocked_by", []),
            "input_refs": doc.get("input_refs", []),
            "label": doc.get("label"),
            "started_at": doc.get("started_at"),
            "completed_at": doc.get("completed_at"),
            "timestamp": datetime.now(UTC).isoformat(),
        }

        logger.info(
            "[TASK-HOOK] %s task_id=%s status=%s",
            signal_type,
            payload["task_id"],
            payload["status"],
        )

        for handler in self._handlers:
            try:
                handler.on_task_signal(signal_type, payload)
            except Exception:
                logger.warning(
                    "[TASK-HOOK] handler %r failed on %s",
                    handler,
                    signal_type,
                    exc_info=True,
                )
