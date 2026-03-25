"""MessageProcessor registry for lifecycle management (Issue #2037).

Maps agent_id → MessageProcessor instances, handles start/stop lifecycle.
Enables POST_WRITE hooks to trigger the correct processor for each agent.

Issue #3197: Optionally injects PipeWakeupListener into MessageProcessor
instances for receiver-side DT_PIPE wakeup when pipe_manager is available.

Architecture: per NEXUS-LEGO-ARCHITECTURE.md §2.4.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.ipc.delivery import MessageHandler, MessageProcessor

logger = logging.getLogger(__name__)


class MessageProcessorRegistry:
    """Registry for MessageProcessor instances.

    Maps agent_id → MessageProcessor and manages lifecycle (start/stop).
    Thread-safe for async concurrent access via asyncio.Lock.

    When ``pipe_manager`` is provided, ``create_processor()`` automatically
    attaches a ``PipeWakeupListener`` for receiver-side DT_PIPE wakeup
    (Issue #3197).

    Example:
        >>> registry = MessageProcessorRegistry(pipe_manager=pm)
        >>> processor = registry.create_processor(storage, "agent_a", handler, zone_id="root")
        >>> await registry.register("agent_a", processor)
        >>> await registry.start_all()
    """

    def __init__(self, pipe_manager: Any = None) -> None:
        self._processors: dict[str, MessageProcessor] = {}
        self._lock = asyncio.Lock()
        self._pipe_manager = pipe_manager

    def create_processor(
        self,
        storage: Any,
        agent_id: str,
        handler: "MessageHandler",
        *,
        zone_id: str,
        **kwargs: Any,
    ) -> "MessageProcessor":
        """Create a MessageProcessor with DT_PIPE wakeup auto-wired.

        If ``pipe_manager`` was provided to the registry, a
        ``PipeWakeupListener`` is created for this agent and attached
        to the processor.  Otherwise the processor works without
        DT_PIPE (poll/EventBus fallback).

        Args:
            storage: VFSOperations storage driver.
            agent_id: Agent whose inbox to process.
            handler: Async callback for each message.
            zone_id: Zone ID for multi-tenant isolation.
            **kwargs: Additional MessageProcessor keyword arguments
                (cache_store, verifier, signing_mode, etc.).

        Returns:
            A configured MessageProcessor instance.
        """
        from nexus.bricks.ipc.delivery import MessageProcessor

        wakeup_listener = None
        if self._pipe_manager is not None and "wakeup_listener" not in kwargs:
            try:
                from nexus.bricks.ipc.wakeup import PipeWakeupListener

                wakeup_listener = PipeWakeupListener(self._pipe_manager, agent_id)
            except Exception:
                logger.debug("Could not create PipeWakeupListener for agent %s", agent_id)

        return MessageProcessor(
            storage,
            agent_id,
            handler,
            zone_id=zone_id,
            wakeup_listener=wakeup_listener,
            **kwargs,
        )

    async def register(self, agent_id: str, processor: "MessageProcessor") -> None:
        """Register a MessageProcessor for an agent.

        If a processor already exists for this agent_id, it is stopped
        and replaced.

        Args:
            agent_id: The agent ID.
            processor: The MessageProcessor instance.
        """
        async with self._lock:
            # Stop existing processor if any
            if agent_id in self._processors:
                logger.info("Replacing existing processor for agent %s", agent_id)
                old_processor = self._processors[agent_id]
                try:
                    await old_processor.stop()
                except Exception:
                    logger.warning(
                        "Failed to stop old processor for agent %s",
                        agent_id,
                        exc_info=True,
                    )

            self._processors[agent_id] = processor
            logger.debug("Registered processor for agent %s", agent_id)

    async def unregister(self, agent_id: str) -> bool:
        """Unregister and stop a MessageProcessor.

        Args:
            agent_id: The agent ID.

        Returns:
            True if the processor was found and unregistered, False otherwise.
        """
        async with self._lock:
            processor = self._processors.pop(agent_id, None)
            if processor is None:
                return False

            try:
                await processor.stop()
            except Exception:
                logger.warning(
                    "Failed to stop processor for agent %s during unregister",
                    agent_id,
                    exc_info=True,
                )

            logger.debug("Unregistered processor for agent %s", agent_id)
            return True

    def get(self, agent_id: str) -> "MessageProcessor | None":
        """Get the MessageProcessor for an agent (no lock needed for read).

        Args:
            agent_id: The agent ID.

        Returns:
            The MessageProcessor instance, or None if not registered.
        """
        return self._processors.get(agent_id)

    async def start_all(self) -> None:
        """Start all registered processors (hot-path listeners).

        Called during application startup after all processors are registered.
        """
        tasks = []
        for agent_id, processor in self._processors.items():
            logger.debug("Starting processor for agent %s", agent_id)
            tasks.append(processor.start())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Started %d MessageProcessor(s)", len(tasks))

    async def stop_all(self) -> None:
        """Stop all registered processors (graceful shutdown).

        Called during application shutdown. Waits for all pending
        handler tasks to complete.
        """
        async with self._lock:
            for agent_id, processor in self._processors.items():
                try:
                    await processor.stop()
                    logger.info("Stopped MessageProcessor for agent %s", agent_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to stop MessageProcessor for agent %s: %s",
                        agent_id,
                        exc,
                        exc_info=True,
                    )
            self._processors.clear()

    def count(self) -> int:
        """Return the number of registered processors."""
        return len(self._processors)

    def list_agents(self) -> list[str]:
        """List all agent IDs with registered processors.

        Returns:
            List of agent IDs.
        """
        return list(self._processors.keys())
