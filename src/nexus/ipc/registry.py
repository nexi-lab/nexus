"""MessageProcessor registry for IPC hook integration (Issue #2037).

Provides a registry to look up MessageProcessor instances by agent_id,
required for POST_WRITE hooks to trigger inbox processing.

This is a Tier 3 System Service that manages the lifecycle of MessageProcessor
instances and provides O(1) lookup for the POST_WRITE hook handler.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.ipc.delivery import MessageProcessor

logger = logging.getLogger(__name__)


class MessageProcessorRegistry:
    """Registry for MessageProcessor instances (Tier 3 System Service).

    Maps agent_id → MessageProcessor and manages lifecycle (start/stop).
    Thread-safe via asyncio.Lock.

    Usage:
        registry = MessageProcessorRegistry()
        await registry.register("agent_123", processor)
        processor = registry.get("agent_123")
        await registry.unregister("agent_123")
    """

    def __init__(self) -> None:
        self._processors: dict[str, MessageProcessor] = {}
        self._lock = asyncio.Lock()

    async def register(self, agent_id: str, processor: MessageProcessor) -> None:
        """Register a MessageProcessor for an agent.

        If a processor already exists for this agent, it will be stopped
        and replaced with the new processor.

        Args:
            agent_id: Agent identifier.
            processor: MessageProcessor instance to register.
        """
        async with self._lock:
            # Stop old processor if exists
            if agent_id in self._processors:
                old_processor = self._processors[agent_id]
                await old_processor.stop()
                logger.info(
                    "Replaced MessageProcessor for agent %s",
                    agent_id,
                )

            self._processors[agent_id] = processor
            logger.info(
                "Registered MessageProcessor for agent %s",
                agent_id,
            )

    async def unregister(self, agent_id: str) -> bool:
        """Unregister and stop a MessageProcessor.

        Args:
            agent_id: Agent identifier.

        Returns:
            True if processor was found and removed, False otherwise.
        """
        async with self._lock:
            processor = self._processors.pop(agent_id, None)
            if processor is None:
                return False

            await processor.stop()
            logger.info(
                "Unregistered MessageProcessor for agent %s",
                agent_id,
            )
            return True

    def get(self, agent_id: str) -> MessageProcessor | None:
        """Get the MessageProcessor for an agent (no lock needed for read).

        Args:
            agent_id: Agent identifier.

        Returns:
            MessageProcessor instance or None if not registered.
        """
        return self._processors.get(agent_id)

    async def stop_all(self) -> None:
        """Stop all registered processors (for graceful shutdown)."""
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
        """Return list of agent IDs with registered processors."""
        return list(self._processors.keys())
