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

Issue #1812: async on_mutation + event_mask filtering.
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.bricks.workflows.protocol import WorkflowProtocol
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.file_events import ALL_FILE_EVENTS, FileEvent

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# VFS path and capacity for workflow events (Task #808)
_WORKFLOW_PIPE_PATH = "/nexus/pipes/workflow-events"
_WORKFLOW_PIPE_CAPACITY = 65_536  # 64KB


class WorkflowDispatchService:
    """Dispatches workflow trigger events via VFS syscalls and webhook subscriptions.

    Implements ``WorkflowDispatchProtocol`` and ``VFSObserver``.
    """

    event_mask: int = ALL_FILE_EVENTS

    def __init__(
        self,
        *,
        workflow_engine: WorkflowProtocol | None,
        subscription_manager: Any = None,
        enable_workflows: bool = True,
    ) -> None:
        self._nx: NexusFS | None = None
        self._workflow_engine = workflow_engine
        self._subscription_manager = subscription_manager
        self._enable_workflows = enable_workflows
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Late injection
    # ------------------------------------------------------------------

    def bind_fs(self, nx: "NexusFS") -> None:
        """Bind NexusFS for sys_read/sys_write pipe access."""
        self._nx = nx

    # ------------------------------------------------------------------
    # Late injection (subscription_manager set after server lifespan init)
    # ------------------------------------------------------------------

    def set_subscription_manager(self, manager: Any) -> None:
        """Inject subscription_manager after server lifespan initialization."""
        self._subscription_manager = manager

    # ------------------------------------------------------------------
    # VFSObserver — called by KernelDispatch OBSERVE phase
    # ------------------------------------------------------------------

    async def on_mutation(self, event: FileEvent) -> None:
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
        await self.fire(trigger_type, ctx, label)

    # ------------------------------------------------------------------
    # fire() — async, called from on_mutation or directly
    # ------------------------------------------------------------------

    async def fire(self, trigger_type: str, event_context: dict[str, Any], label: str) -> None:
        """Fire a workflow event and broadcast to webhook subscriptions.

        Uses VFS sys_write to push into DT_PIPE.
        """
        if not (self._enable_workflows and self._workflow_engine):
            return

        if self._nx is not None and self._pipe_ready:
            try:
                data = json.dumps({"type": trigger_type, "ctx": event_context}).encode()
                await self._nx.sys_write(_WORKFLOW_PIPE_PATH, data)
            except Exception:
                logger.warning("Workflow pipe write failed, dropping event: %s", label)
        else:
            # Fallback: direct async call (CLI mode or pre-startup, no pipe yet)
            await self._workflow_engine.fire_event(trigger_type, event_context)

        if self._subscription_manager:
            event_type = label.split(":")[0] if ":" in label else label
            await self._subscription_manager.broadcast(
                event_type,
                event_context,
                event_context.get("zone_id", ROOT_ZONE_ID),
            )

    # ------------------------------------------------------------------
    # start() / stop() — async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create workflow pipe via VFS syscall and start background consumer (idempotent)."""
        if self._pipe_ready:
            return

        if self._nx is None:
            return  # CLI mode — no NexusFS

        pipe_manager = getattr(self._nx, "_pipe_manager", None)
        if pipe_manager is None:
            return

        pipe_manager.ensure(
            _WORKFLOW_PIPE_PATH,
            capacity=_WORKFLOW_PIPE_CAPACITY,
            owner_id="kernel",
        )

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """Graceful shutdown: unlink pipe, drain remaining events, then stop."""
        if self._consumer_task is not None and not self._consumer_task.done():
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    await self._nx.sys_unlink(_WORKFLOW_PIPE_PATH)

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
        """Background consumer for workflow events via VFS sys_read (#808)."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert self._nx is not None  # guaranteed by start()
        assert self._workflow_engine is not None  # guaranteed by constructor guard
        nx = self._nx
        engine = self._workflow_engine
        while True:
            try:
                data = await nx.sys_read(_WORKFLOW_PIPE_PATH)
            except NexusFileNotFoundError:
                logger.debug("Workflow pipe closed, consumer exiting")
                break
            try:
                msg = json.loads(data)
                await engine.fire_event(msg["type"], msg["ctx"])
            except Exception as e:
                logger.error("Workflow event processing failed: %s", e)
