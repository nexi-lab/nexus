"""IPC hook handlers for filesystem-as-IPC integration (Issue #2037).

Registers POST_WRITE hooks that trigger MessageProcessor when files are written
to agent inbox directories via VFS (not just REST API).

Enables true "filesystem-as-IPC" where ANY write to `/agents/{agent_id}/inbox/`
automatically triggers message delivery.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.services.protocols.hook_engine import (
    POST_WRITE,
    HookContext,
    HookResult,
    HookSpec,
)

if TYPE_CHECKING:
    from nexus.bricks.ipc.registry import MessageProcessorRegistry

logger = logging.getLogger(__name__)


async def inbox_write_hook(
    context: HookContext,
    processor_registry: "MessageProcessorRegistry",
) -> HookResult:
    """POST_WRITE hook handler for inbox writes.

    Triggered when a file is written to `/agents/{agent_id}/inbox/*.json`.
    Extracts the agent_id from the path and triggers MessageProcessor.process_inbox().

    Args:
        context: Hook context with path and zone_id.
        processor_registry: Registry to look up MessageProcessor for the agent.

    Returns:
        HookResult with proceed=True (never blocks the write).
    """
    if context.path is None or not context.path.startswith("/agents/"):
        return HookResult(proceed=True, modified_context=None, error=None)

    # Extract agent_id from path: /agents/{agent_id}/inbox/...
    parts = context.path.split("/")
    if len(parts) < 4 or parts[3] != "inbox":
        return HookResult(proceed=True, modified_context=None, error=None)

    agent_id = parts[2]
    processor = processor_registry.get(agent_id)
    if processor is None:
        logger.debug(
            "No MessageProcessor registered for agent %s (inbox write: %s)",
            agent_id,
            context.path,
        )
        return HookResult(proceed=True, modified_context=None, error=None)

    # Trigger inbox processing in background (non-blocking)
    try:
        await processor.process_inbox()
        logger.debug(
            "Triggered inbox processing for agent %s via POST_WRITE hook",
            agent_id,
        )
    except Exception as exc:
        logger.warning(
            "POST_WRITE hook: inbox processing failed for agent %s: %s",
            agent_id,
            exc,
            exc_info=True,
        )

    # Always proceed (hook doesn't block the write)
    return HookResult(proceed=True, modified_context=None, error=None)


async def register_ipc_hooks(
    hook_engine: Any,
    processor_registry: "MessageProcessorRegistry",
) -> None:
    """Register IPC hooks with the HookEngine.

    Args:
        hook_engine: HookEngineProtocol instance (duck-typed).
        processor_registry: MessageProcessorRegistry for looking up processors.
    """
    # Register POST_WRITE hook for inbox pattern
    spec = HookSpec(
        phase=POST_WRITE,
        handler_name="ipc_inbox_write",
        priority=100,  # Higher priority to run before other hooks
        agent_scope=None,  # Global - applies to all agents
    )

    async def handler(ctx: HookContext) -> HookResult:
        return await inbox_write_hook(ctx, processor_registry)

    await hook_engine.register_hook(spec, handler)
    logger.info("Registered IPC POST_WRITE hook for inbox pattern")
