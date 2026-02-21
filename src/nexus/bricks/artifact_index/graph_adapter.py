"""Graph indexer adapter (Issue #1861).

Indexes entities extracted from artifact text into the knowledge graph
via ``GraphStore.add_entity()``.  Uses session-per-call pattern.
Entity extraction is optional — gracefully degrades when NER is
unavailable (logs + skips).
"""

import logging
from collections.abc import Callable
from typing import Any

from nexus.bricks.artifact_index.protocol import ArtifactContent, ArtifactIndexerProtocol

logger = logging.getLogger(__name__)


class GraphIndexerAdapter:
    """Indexes entities from artifact content into the knowledge graph.

    Satisfies ``ArtifactIndexerProtocol`` via duck typing.

    Uses session-per-call: ``session_factory()`` creates a fresh
    ``AsyncSession`` for each invocation.  The ``graph_store_factory``
    creates a ``GraphStore`` bound to that session.

    When ``entity_extractor`` is ``None``, the adapter logs a DEBUG
    message and returns without error (graceful degradation).

    Args:
        session_factory: Async callable returning an ``AsyncSession``.
        graph_store_factory: Callable ``(session) -> GraphStore``.
        entity_extractor: Optional callable ``(text) -> list[dict]``.
            Each dict should have ``name`` and optionally ``type`` keys.
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        graph_store_factory: Callable[..., Any],
        entity_extractor: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._graph_store_factory = graph_store_factory
        self._entity_extractor = entity_extractor

    async def index(self, content: ArtifactContent) -> None:
        """Extract entities from content and add to the knowledge graph.

        When NER is unavailable, logs and returns.  On any other error,
        logs at ERROR level and returns (log-and-suppress).
        """
        if not content.text:
            return

        if self._entity_extractor is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[ARTIFACT-INDEX:graph] No entity extractor available, skipping artifact %s",
                    content.artifact_id,
                )
            return

        try:
            entities = self._entity_extractor(content.text)
        except Exception:
            logger.exception(
                "[ARTIFACT-INDEX:graph] Entity extraction failed for artifact %s",
                content.artifact_id,
            )
            return

        if not entities:
            return

        try:
            session = self._session_factory()
            async with session:
                graph_store = self._graph_store_factory(session)
                count = 0
                for entity in entities:
                    name = entity.get("name", "").strip()
                    if not name:
                        continue
                    await graph_store.add_entity(
                        name=name,
                        entity_type=entity.get("type"),
                        metadata={
                            "source": "artifact",
                            "artifact_id": content.artifact_id,
                            "task_id": content.task_id,
                        },
                    )
                    count += 1

                if count > 0:
                    logger.info(
                        "[ARTIFACT-INDEX:graph] Indexed %d entities from artifact %s",
                        count,
                        content.artifact_id,
                    )
        except Exception:
            logger.exception(
                "[ARTIFACT-INDEX:graph] Failed to index entities from artifact %s",
                content.artifact_id,
            )


# Ensure duck-type conformance at import time
assert isinstance(GraphIndexerAdapter.__new__(GraphIndexerAdapter), ArtifactIndexerProtocol)
