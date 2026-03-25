"""Agent state event system for scheduler awareness (Issue #1274).

Provides an in-process observer pattern for agent state transitions.
The emitter isolates handler exceptions so a failing handler does not
block other handlers or the caller.

Designed as fallback when NATS is unavailable.
"""

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

AgentStateHandler = Callable[["AgentStateEvent"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AgentStateEvent:
    """Immutable event emitted on agent state transitions.

    Attributes:
        agent_id: The agent whose state changed.
        previous_state: State before the transition.
        new_state: State after the transition.
        generation: Session generation counter after the transition.
        zone_id: Agent's zone/org ID (if known).
    """

    agent_id: str
    previous_state: str
    new_state: str
    generation: int = 0
    zone_id: str | None = None


class AgentStateEmitter:
    """In-process observer for agent state change events.

    Handlers are called concurrently via asyncio. Each handler's
    exceptions are logged and suppressed to prevent cascade failures.
    """

    def __init__(self) -> None:
        self._handlers: list[AgentStateHandler] = []

    def add_handler(self, handler: AgentStateHandler) -> None:
        """Register a handler for state change events."""
        if handler not in self._handlers:
            self._handlers.append(handler)

    def remove_handler(self, handler: AgentStateHandler) -> None:
        """Unregister a handler."""
        with contextlib.suppress(ValueError):
            self._handlers.remove(handler)

    async def emit(self, event: AgentStateEvent) -> None:
        """Emit an event to all registered handlers.

        Each handler is called independently; exceptions are logged
        and do not propagate to the caller or other handlers.
        """
        for handler in self._handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "AgentStateEmitter handler %s failed for event %s",
                    handler,
                    event,
                )

    @property
    def handler_count(self) -> int:
        """Number of registered handlers."""
        return len(self._handlers)
