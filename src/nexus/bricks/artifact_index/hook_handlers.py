"""Hook handler factories for artifact indexing (Issue #1861).

Each factory creates an async handler function that extracts content
from the hook context payload and delegates to the corresponding
indexer adapter.  Returns ``HookResult(proceed=True)`` always —
errors are logged and suppressed inside the adapter.
"""

import logging
from collections.abc import Awaitable, Callable

from nexus.bricks.artifact_index.extractors import extract_content
from nexus.bricks.artifact_index.protocol import ArtifactIndexerProtocol
from nexus.services.protocols.hook_engine import HookContext, HookResult

logger = logging.getLogger(__name__)

# Return type alias for hook handlers
HookHandler = Callable[[HookContext], Awaitable[HookResult]]

_OK = HookResult(proceed=True, modified_context=None, error=None)


def make_memory_hook_handler(adapter: ArtifactIndexerProtocol) -> HookHandler:
    """Create a hook handler that indexes artifact content into memory."""

    async def _handler(context: HookContext) -> HookResult:
        return await _run_indexer(adapter, context, "memory")

    return _handler


def make_tool_hook_handler(adapter: ArtifactIndexerProtocol) -> HookHandler:
    """Create a hook handler that indexes tool schemas from artifacts."""

    async def _handler(context: HookContext) -> HookResult:
        return await _run_indexer(adapter, context, "tool")

    return _handler


def make_graph_hook_handler(adapter: ArtifactIndexerProtocol) -> HookHandler:
    """Create a hook handler that indexes entities from artifacts."""

    async def _handler(context: HookContext) -> HookResult:
        return await _run_indexer(adapter, context, "graph")

    return _handler


async def _run_indexer(
    adapter: ArtifactIndexerProtocol,
    context: HookContext,
    target: str,
) -> HookResult:
    """Shared logic: extract content from context payload, call adapter.

    The payload is expected to contain:
    - ``artifact``: An ``Artifact`` instance
    - ``task_id``: The owning task ID
    - ``zone_id``: The zone scope (falls back to context.zone_id)
    - ``max_content_bytes``: Optional truncation limit
    """
    payload = context.payload
    artifact = payload.get("artifact")
    if artifact is None:
        return _OK

    task_id = payload.get("task_id", "")
    zone_id = payload.get("zone_id") or context.zone_id or ""
    max_bytes = payload.get("max_content_bytes", 100_000)

    try:
        content = extract_content(
            artifact=artifact,
            task_id=task_id,
            zone_id=zone_id,
            max_bytes=max_bytes,
        )
        await adapter.index(content)
    except Exception:
        logger.exception(
            "[ARTIFACT-INDEX:%s] Unhandled error for artifact in task %s",
            target,
            task_id,
        )

    return _OK
