"""Artifact callback factories for artifact indexing (Issue #1861).

Each factory creates an async callback that extracts content
from the artifact and delegates to the corresponding indexer adapter.
Errors are logged and suppressed inside the adapter.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nexus.bricks.artifact_index.extractors import extract_content
from nexus.bricks.artifact_index.protocol import ArtifactIndexerProtocol

logger = logging.getLogger(__name__)

# Callback type alias: (artifact, task_id, zone_id) -> None
ArtifactCallback = Callable[[Any, str, str], Awaitable[None]]


def make_memory_hook_handler(adapter: ArtifactIndexerProtocol) -> ArtifactCallback:
    """Create a callback that indexes artifact content into memory."""

    async def _handler(artifact: Any, task_id: str, zone_id: str) -> None:
        await _run_indexer(adapter, artifact, task_id, zone_id, "memory")

    return _handler


def make_tool_hook_handler(adapter: ArtifactIndexerProtocol) -> ArtifactCallback:
    """Create a callback that indexes tool schemas from artifacts."""

    async def _handler(artifact: Any, task_id: str, zone_id: str) -> None:
        await _run_indexer(adapter, artifact, task_id, zone_id, "tool")

    return _handler


def make_graph_hook_handler(adapter: ArtifactIndexerProtocol) -> ArtifactCallback:
    """Create a callback that indexes entities from artifacts."""

    async def _handler(artifact: Any, task_id: str, zone_id: str) -> None:
        await _run_indexer(adapter, artifact, task_id, zone_id, "graph")

    return _handler


async def _run_indexer(
    adapter: ArtifactIndexerProtocol,
    artifact: Any,
    task_id: str,
    zone_id: str,
    target: str,
) -> None:
    """Shared logic: extract content from artifact, call adapter.

    Parameters
    ----------
    adapter:
        The indexer adapter to delegate to.
    artifact:
        An ``Artifact`` instance.
    task_id:
        The owning task ID.
    zone_id:
        The zone scope.
    target:
        Label for logging (``"memory"``, ``"tool"``, ``"graph"``).
    """
    if artifact is None:
        return

    try:
        content = extract_content(
            artifact=artifact,
            task_id=task_id,
            zone_id=zone_id,
            max_bytes=100_000,
        )
        await adapter.index(content)
    except Exception:
        logger.exception(
            "[ARTIFACT-INDEX:%s] Unhandled error for artifact in task %s",
            target,
            task_id,
        )
