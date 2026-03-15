"""VFS write hook that emits task lifecycle events."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from nexus.bricks.task_manager.events import (
    TaskCreatedEvent,
    TaskEventHandler,
    TaskUpdatedEvent,
)

if TYPE_CHECKING:
    from nexus.contracts.vfs_hooks import WriteHookContext

logger = logging.getLogger(__name__)

_TASK_PATTERN = "/.tasks/tasks/*.json"


class TaskWriteHook:
    """Post-write hook that emits task lifecycle events.

    Intercepts writes to ``/.tasks/tasks/*.json`` and fires
    :class:`TaskCreatedEvent` or :class:`TaskUpdatedEvent` to registered
    handlers.  No in-memory cache — all state comes from the written content
    and ``ctx.is_new_file``.
    """

    def __init__(self) -> None:
        self._handlers: list[TaskEventHandler] = []

    @property
    def name(self) -> str:
        return "task_manager_write"

    def register_handler(self, handler: TaskEventHandler) -> None:
        """Subscribe a handler to task lifecycle events."""
        self._handlers.append(handler)

    # ── VFSWriteHook protocol ──────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        if not fnmatch(ctx.path, _TASK_PATTERN):
            return

        try:
            doc = json.loads(ctx.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("[TASK-HOOK] failed to parse task JSON at %s", ctx.path)
            return

        if ctx.is_new_file:
            event = TaskCreatedEvent(
                task_id=doc.get("id", ""),
                mission_id=doc.get("mission_id", ""),
                instruction=doc.get("instruction", ""),
                worker_type=doc.get("worker_type"),
                blocked_by=doc.get("blocked_by", []),
                input_refs=doc.get("input_refs", []),
                label=doc.get("label"),
                created_at=doc.get("created_at", ""),
            )
            logger.info(
                "[TASK-HOOK] task_created task_id=%s mission_id=%s",
                event.task_id,
                event.mission_id,
            )
            for handler in self._handlers:
                try:
                    handler.on_task_created(event)
                except Exception:
                    logger.warning(
                        "[TASK-HOOK] handler %r failed on task_created",
                        handler,
                        exc_info=True,
                    )
        else:
            event = TaskUpdatedEvent(
                task_id=doc.get("id", ""),
                mission_id=doc.get("mission_id", ""),
                status=doc.get("status", ""),
                worker_type=doc.get("worker_type"),
                label=doc.get("label"),
                started_at=doc.get("started_at"),
                completed_at=doc.get("completed_at"),
                timestamp=datetime.now(UTC).isoformat(),
            )
            logger.info(
                "[TASK-HOOK] task_updated task_id=%s status=%s",
                event.task_id,
                event.status,
            )
            for handler in self._handlers:
                try:
                    handler.on_task_updated(event)
                except Exception:
                    logger.warning(
                        "[TASK-HOOK] handler %r failed on task_updated",
                        handler,
                        exc_info=True,
                    )
