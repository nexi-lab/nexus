"""Auto-discoverable brick factory for artifact indexing (Issue #1861).

Tier: ``dependent`` — requires Memory, ToolIndex, and GraphStore from
other bricks.  Discovered by ``_discover_brick_factories("dependent")``.

Creates indexer adapters and hook handlers, then registers them with
the hook engine for ``post_artifact_create`` and ``post_artifact_update``.

Cross-brick imports are avoided by accepting pre-built factories from
the boot layer (``_boot_dependent_bricks``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "dependent"
RESULT_KEY = "artifact_index_handlers"

logger = logging.getLogger(__name__)


def create(
    ctx: Any,
    _system: dict[str, Any],
    bricks: dict[str, Any],
    *,
    tool_info_factory: Callable[..., Any] | None = None,
    graph_store_factory: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Create artifact indexing adapters and return hook handlers.

    Args:
        ctx: Boot context with shared dependencies.
        _system: System services dict (unused).
        bricks: Brick services dict (provides memory, tool_index, etc.).
        tool_info_factory: Callable to create ToolInfo objects (injected
            from factory layer to avoid cross-brick imports).
        graph_store_factory: Callable ``(session) -> GraphStore`` (injected
            from factory layer to avoid cross-brick imports).

    Returns:
        Dict with ``handlers`` list for hook registration, or None.
    """
    from nexus.bricks.artifact_index.config import ArtifactIndexConfig

    config = ArtifactIndexConfig()
    handlers: list[dict[str, Any]] = []

    # --- Memory adapter ---
    if config.memory_enabled:
        try:
            memory = bricks.get("memory_service")
            if memory is not None:
                from nexus.bricks.artifact_index.hook_handlers import make_memory_hook_handler
                from nexus.bricks.artifact_index.memory_adapter import MemoryIndexerAdapter

                mem_adapter = MemoryIndexerAdapter(memory=memory)
                handlers.append(
                    {
                        "handler": make_memory_hook_handler(mem_adapter),
                        "handler_name": "artifact_index:memory",
                    }
                )
                logger.debug("[BOOT:BRICK] Artifact memory indexer created")
            else:
                logger.debug("[BOOT:BRICK] Memory service not available for artifact indexing")
        except Exception as exc:
            logger.debug("[BOOT:BRICK] Artifact memory indexer unavailable: %s", exc)

    # --- Tool adapter ---
    if config.tool_enabled:
        try:
            tool_index = bricks.get("tool_index")
            if tool_index is not None and tool_info_factory is not None:
                from nexus.bricks.artifact_index.hook_handlers import make_tool_hook_handler
                from nexus.bricks.artifact_index.tool_adapter import ToolIndexerAdapter

                tool_adapter = ToolIndexerAdapter(
                    tool_index=tool_index,
                    tool_info_factory=tool_info_factory,
                )
                handlers.append(
                    {
                        "handler": make_tool_hook_handler(tool_adapter),
                        "handler_name": "artifact_index:tool",
                    }
                )
                logger.debug("[BOOT:BRICK] Artifact tool indexer created")
            else:
                logger.debug("[BOOT:BRICK] ToolIndex not available for artifact indexing")
        except Exception as exc:
            logger.debug("[BOOT:BRICK] Artifact tool indexer unavailable: %s", exc)

    # --- Graph adapter ---
    if config.graph_enabled:
        try:
            session_factory = getattr(ctx.record_store, "async_session_factory", None)
            if session_factory is not None and graph_store_factory is not None:
                from nexus.bricks.artifact_index.graph_adapter import GraphIndexerAdapter
                from nexus.bricks.artifact_index.hook_handlers import make_graph_hook_handler

                # Entity extractor is optional — pass None for graceful degradation
                entity_extractor = bricks.get("entity_extractor")

                graph_adapter = GraphIndexerAdapter(
                    session_factory=session_factory,
                    graph_store_factory=graph_store_factory,
                    entity_extractor=entity_extractor,
                )
                handlers.append(
                    {
                        "handler": make_graph_hook_handler(graph_adapter),
                        "handler_name": "artifact_index:graph",
                    }
                )
                logger.debug("[BOOT:BRICK] Artifact graph indexer created")
            else:
                logger.debug("[BOOT:BRICK] No session factory for artifact graph indexing")
        except Exception as exc:
            logger.debug("[BOOT:BRICK] Artifact graph indexer unavailable: %s", exc)

    if not handlers:
        logger.debug("[BOOT:BRICK] No artifact indexers created")
        return None

    logger.debug("[BOOT:BRICK] Artifact indexing: %d handlers ready", len(handlers))
    return {"handlers": handlers}
