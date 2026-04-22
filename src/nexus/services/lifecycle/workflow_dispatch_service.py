"""Workflow dispatch service (#625).

Dispatches workflow trigger events via Rust kernel DT_PIPE IPC
and broadcasts to webhook subscriptions.

Registered in KernelDispatch's OBSERVE phase so the Rust kernel fires
a single ``FileEvent`` without knowing about workflows.

DI dependencies (no god-object access):
    - nx: NexusFS for Rust-kernel pipe I/O
    - workflow_engine: WorkflowProtocol for firing workflow events
    - subscription_manager: Optional webhook broadcast (injected late by server)
    - enable_workflows: Feature flag from DistributedConfig

Issue #1812: event_mask filtering.
Issue #3646: observer dispatch is now fully Rust-native.
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.bricks.workflows.protocol import WorkflowProtocol
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# VFS path and capacity for workflow events (Task #808)
_WORKFLOW_PIPE_PATH = "/nexus/pipes/workflow-events"
_WORKFLOW_PIPE_CAPACITY = 65_536  # 64KB


class WorkflowDispatchService:
    """Dispatches workflow trigger events via Rust kernel DT_PIPE and webhook subscriptions.

    Implements ``WorkflowDispatchProtocol``.
    """

    def __init__(
        self,
        *,
        nx: "NexusFS | None" = None,
        workflow_engine: WorkflowProtocol | None,
        subscription_manager: Any = None,
        enable_workflows: bool = True,
        **_kwargs: Any,
    ) -> None:
        self._nx = nx
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
    # _fire_sync() — sync fast path
    # ------------------------------------------------------------------

    def _fire_sync(self, trigger_type: str, event_context: dict[str, Any], label: str) -> None:
        """Sync dispatch: sys_write to DT_PIPE + fire-and-forget async work."""
        if not (self._enable_workflows and self._workflow_engine):
            return

        from nexus.core.pipe import PipeClosedError, PipeFullError

        if self._nx is not None and self._pipe_ready:
            try:
                data = json.dumps({"type": trigger_type, "ctx": event_context}).encode()
                self._nx.sys_write(_WORKFLOW_PIPE_PATH, data)
            except (PipeClosedError, PipeFullError):
                logger.warning("Workflow pipe full/closed, dropping event: %s", label)
        else:
            # Fallback: fire-and-forget async call (CLI mode or pre-startup)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._workflow_engine.fire_event(trigger_type, event_context))
            except RuntimeError:
                pass  # no event loop

        if self._subscription_manager:
            event_type = label.split(":")[0] if ":" in label else label
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._subscription_manager.broadcast(
                        event_type,
                        event_context,
                        event_context.get("zone_id", ROOT_ZONE_ID),
                    )
                )
            except RuntimeError:
                pass  # no event loop

    # ------------------------------------------------------------------
    # fire() — async, called directly by external callers (not from OBSERVE)
    # ------------------------------------------------------------------

    async def fire(self, trigger_type: str, event_context: dict[str, Any], label: str) -> None:
        """Async fire for direct callers (not from OBSERVE path).

        Uses sys_write to DT_PIPE — routes through Rust kernel ring buffer.
        """
        if not (self._enable_workflows and self._workflow_engine):
            return

        if self._nx is not None and self._pipe_ready:
            try:
                data = json.dumps({"type": trigger_type, "ctx": event_context}).encode()
                self._nx.sys_write(_WORKFLOW_PIPE_PATH, data)
            except Exception:
                logger.warning("Workflow pipe full/closed, dropping event: %s", label)
        else:
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
        """Create workflow pipe via sys_setattr and start background consumer (idempotent)."""
        if self._pipe_ready:
            return

        if self._nx is None:
            return  # CLI mode — no NexusFS

        from nexus.contracts.metadata import DT_PIPE

        try:
            self._nx.sys_setattr(
                _WORKFLOW_PIPE_PATH,
                entry_type=DT_PIPE,
                capacity=_WORKFLOW_PIPE_CAPACITY,
                owner_id="kernel",
            )
        except Exception as exc:
            logger.warning("[WORKFLOW-DISPATCH] pipe unavailable, dispatch disabled: %s", exc)
            return

        self._pipe_ready = True

        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """Graceful shutdown: signal pipe closed, drain remaining events, then stop."""
        if self._consumer_task is not None and not self._consumer_task.done():
            # Signal close — wakes blocked consumer, allows drain of remaining messages
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._nx.pipe_close(_WORKFLOW_PIPE_PATH)

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
        """Background consumer for workflow events via sys_read (#808).

        Reads from Rust kernel pipe by VFS path.
        Deserializes JSON messages and fires workflow engine events.
        """
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert self._nx is not None  # guaranteed by start()
        assert self._workflow_engine is not None  # guaranteed by constructor guard
        engine = self._workflow_engine
        while True:
            try:
                data = self._nx.sys_read(_WORKFLOW_PIPE_PATH)
            except NexusFileNotFoundError:
                logger.debug("Workflow pipe closed, consumer exiting")
                break
            try:
                msg = json.loads(data)
                await engine.fire_event(msg["type"], msg["ctx"])
            except Exception as e:
                logger.error("Workflow event processing failed: %s", e)
