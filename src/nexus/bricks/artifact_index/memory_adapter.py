"""Memory indexer adapter (Issue #1861).

Wraps ``Memory.store()`` (synchronous) in ``asyncio.to_thread()`` —
one thread per artifact, no batching.  Errors are logged and suppressed
so hook execution is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.bricks.artifact_index.protocol import ArtifactContent, ArtifactIndexerProtocol

logger = logging.getLogger(__name__)


class MemoryIndexerAdapter:
    """Indexes artifact text into semantic memory via ``Memory.store()``.

    Satisfies ``ArtifactIndexerProtocol`` via duck typing.

    Args:
        memory: A ``Memory`` service instance (``bricks/memory/service.py``).
            Must have a synchronous ``store(content, ...)`` method.
    """

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    async def index(self, content: ArtifactContent) -> None:
        """Store artifact text in semantic memory.

        Wraps the synchronous ``Memory.store()`` in ``asyncio.to_thread()``.
        On any error, logs at ERROR level and returns (log-and-suppress).
        """
        if not content.text:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[ARTIFACT-INDEX:memory] Skipping empty content for artifact %s",
                    content.artifact_id,
                )
            return

        try:
            await asyncio.to_thread(
                self._memory.store,
                content.text,
                scope="artifact",
                _metadata={
                    "artifact_id": content.artifact_id,
                    "task_id": content.task_id,
                    "zone_id": content.zone_id,
                    **content.metadata,
                },
            )
            logger.info(
                "[ARTIFACT-INDEX:memory] Indexed artifact %s",
                content.artifact_id,
            )
        except Exception:
            logger.exception(
                "[ARTIFACT-INDEX:memory] Failed to index artifact %s",
                content.artifact_id,
            )


# Ensure duck-type conformance at import time
assert isinstance(MemoryIndexerAdapter.__new__(MemoryIndexerAdapter), ArtifactIndexerProtocol)
