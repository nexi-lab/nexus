"""Workflow dispatch service (#625).

Dispatches workflow trigger events via PipeManager (userspace API
over DT_PIPE kernel IPC) and broadcasts to webhook subscriptions.

Implements ``VFSObserver`` — registered in KernelDispatch's OBSERVE phase so
the kernel fires a single ``FileEvent`` without knowing about workflows.

DI dependencies (no god-object access):
    - pipe_manager: PipeManager for kernel IPC ring buffers
    - workflow_engine: WorkflowProtocol for firing workflow events
    - subscription_manager: Optional webhook broadcast (injected late by server)
    - enable_workflows: Feature flag from DistributedConfig
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

from nexus.bricks.workflows.protocol import WorkflowProtocol
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.file_events import FileEvent
from nexus.system_services.pipe_manager import PipeManager

logger = logging.getLogger(__name__)

# PipeManager VFS path and capacity for workflow events (Task #808)
_WORKFLOW_PIPE_PATH = "/nexus/pipes/workflow-events"
_WORKFLOW_PIPE_CAPACITY = 65_536  # 64KB


class WorkflowDispatchService:
    """Dispatches workflow trigger events via PipeManager and webhook subscriptions.

    Implements ``WorkflowDispatchProtocol`` and ``VFSObserver``.
    """

    def __init__(
        self,
        *,
        pipe_manager: PipeManager | None,
        workflow_engine: WorkflowProtocol | None,
        subscription_manager: Any = None,
        enable_workflows: bool = True,
    ) -> None:
        self._pipe_manager = pipe_manager
        self._workflow_engine = workflow_engine
        self._subscription_manager = subscription_manager
        self._enable_workflows = enable_workflows
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Late injection (subscription_manager set after server lifespan init)
    # ------------------------------------------------------------------

    def set_subscription_manager(self, manager: Any) -> None:
        """Inject subscription_manager after server lifespan initialization."""
        self._subscription_manager = manager

    # ------------------------------------------------------------------
    # VFSObserver — called by KernelDispatch OBSERVE phase
    # ------------------------------------------------------------------

    def on_mutation(self, event: FileEvent) -> None:
        """Translate kernel FileEvent into workflow fire + webhook broadcast."""
        from nexus.core.file_events import FileEventType

        trigger_type = (
            event.type.value if isinstance(event.type, FileEventType) else str(event.type)
        )

        # Build event context from FileEvent fields
        ctx: dict[str, Any] = {
            "zone_id": event.zone_id,
            "agent_id": event.agent_id,
            "user_id": event.user_id,
            "timestamp": event.timestamp,
        }
        if event.type is FileEventType.FILE_RENAME:
            ctx["old_path"] = event.path
            ctx["new_path"] = event.new_path
        else:
            ctx["file_path"] = event.path

        if event.size is not None:
            ctx["size"] = event.size
        if event.etag is not None:
            ctx["etag"] = event.etag
        if event.version is not None:
            ctx["version"] = event.version
        if event.type is FileEventType.FILE_WRITE:
            ctx["created"] = event.is_new

        label = f"{trigger_type}:{event.path}"
        self.fire(trigger_type, ctx, label)

    # ------------------------------------------------------------------
    # fire() — sync, called from on_mutation or directly
    # ------------------------------------------------------------------

    def fire(self, trigger_type: str, event_context: dict[str, Any], label: str) -> None:
        """Fire a workflow event and broadcast to webhook subscriptions.

        Uses PipeManager userspace API — never touches RingBuffer directly.
        """
        if not (self._enable_workflows and self._workflow_engine):
            return

        from nexus.core.pipe import PipeClosedError, PipeFullError

        if self._pipe_manager is not None and self._pipe_ready:
            try:
                data = json.dumps({"type": trigger_type, "ctx": event_context}).encode()
                self._pipe_manager.pipe_write_nowait(_WORKFLOW_PIPE_PATH, data)
            except (PipeClosedError, PipeFullError):
                logger.warning("Workflow pipe full/closed, dropping event: %s", label)
        else:
            # Fallback: fire-and-forget (CLI mode or pre-startup, no pipe yet)
            from nexus.lib.sync_bridge import fire_and_forget

            fire_and_forget(self._workflow_engine.fire_event(trigger_type, event_context))

        if self._subscription_manager:
            from nexus.lib.sync_bridge import fire_and_forget

            event_type = label.split(":")[0] if ":" in label else label
            fire_and_forget(
                self._subscription_manager.broadcast(
                    event_type,
                    event_context,
                    event_context.get("zone_id", ROOT_ZONE_ID),
                )
            )

    # ------------------------------------------------------------------
    # start() / stop() — async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create workflow pipe via PipeManager and start background consumer (idempotent)."""
        if self._pipe_ready:
            return

        if self._pipe_manager is None:
            return  # CLI mode — no pipe manager

        from nexus.core.pipe import PipeError

        try:
            self._pipe_manager.create(
                _WORKFLOW_PIPE_PATH,
                capacity=_WORKFLOW_PIPE_CAPACITY,
                owner_id="kernel",
            )
        except PipeError:
            # Pipe already exists (e.g., restart recovery) — open it
            self._pipe_manager.open(_WORKFLOW_PIPE_PATH, capacity=_WORKFLOW_PIPE_CAPACITY)

        self._pipe_ready = True

        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """Graceful shutdown: signal pipe closed, drain remaining events, then stop."""
        if self._consumer_task is not None and not self._consumer_task.done():
            # Signal close — wakes blocked consumer, allows drain of remaining messages
            if self._pipe_manager is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._pipe_manager.signal_close(_WORKFLOW_PIPE_PATH)

            # Let consumer drain naturally, with timeout
            try:
                await asyncio.wait_for(asyncio.shield(self._consumer_task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer_task

            self._consumer_task = None
        self._pipe_ready = False

    # ------------------------------------------------------------------
    # _consume() — background consumer loop
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Background consumer for workflow events via PipeManager (#808).

        Reads from PipeManager by VFS path (userspace API).
        Deserializes JSON messages and fires workflow engine events.
        """
        from nexus.core.pipe import PipeClosedError, PipeNotFoundError

        assert self._pipe_manager is not None  # guaranteed by start()
        assert self._workflow_engine is not None  # guaranteed by constructor guard
        pipe_mgr = self._pipe_manager
        engine = self._workflow_engine
        while True:
            try:
                data = await pipe_mgr.pipe_read(_WORKFLOW_PIPE_PATH)
            except (PipeClosedError, PipeNotFoundError):
                logger.debug("Workflow pipe closed, consumer exiting")
                break
            try:
                msg = json.loads(data)
                await engine.fire_event(msg["type"], msg["ctx"])
            except Exception as e:
                logger.error("Workflow event processing failed: %s", e)
