"""Tests for GraphIndexerAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.artifact_index.graph_adapter import GraphIndexerAdapter
from nexus.bricks.artifact_index.protocol import ArtifactContent


def _make_content(text: str = "some text") -> ArtifactContent:
    return ArtifactContent(
        text=text,
        metadata={},
        artifact_id="art-1",
        task_id="task-1",
        zone_id="zone-1",
    )


class TestGraphIndexerAdapter:
    """Graph adapter with session-per-call and optional NER."""

    @pytest.mark.asyncio
    async def test_no_extractor_skips(self) -> None:
        """When entity_extractor is None, should skip gracefully."""
        adapter = GraphIndexerAdapter(
            session_factory=MagicMock(),
            graph_store_factory=MagicMock(),
            entity_extractor=None,
        )
        # Should not raise
        await adapter.index(_make_content())

    @pytest.mark.asyncio
    async def test_empty_text_skips(self) -> None:
        adapter = GraphIndexerAdapter(
            session_factory=MagicMock(),
            graph_store_factory=MagicMock(),
            entity_extractor=lambda t: [{"name": "X"}],
        )
        await adapter.index(_make_content(text=""))

    @pytest.mark.asyncio
    async def test_entities_indexed(self) -> None:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_graph_store = AsyncMock()
        mock_graph_store.add_entity = AsyncMock(return_value=("eid-1", True))

        adapter = GraphIndexerAdapter(
            session_factory=lambda: mock_session,
            graph_store_factory=lambda s: mock_graph_store,
            entity_extractor=lambda t: [
                {"name": "Alice", "type": "PERSON"},
                {"name": "Acme", "type": "ORG"},
            ],
        )

        await adapter.index(_make_content())

        assert mock_graph_store.add_entity.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_entity_name_skipped(self) -> None:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_graph_store = AsyncMock()

        adapter = GraphIndexerAdapter(
            session_factory=lambda: mock_session,
            graph_store_factory=lambda s: mock_graph_store,
            entity_extractor=lambda t: [{"name": ""}, {"name": "  "}],
        )

        await adapter.index(_make_content())
        mock_graph_store.add_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_extractor_error_suppressed(self) -> None:
        def _bad_extractor(text: str) -> list[dict[str, object]]:
            raise ValueError("NER failed")

        adapter = GraphIndexerAdapter(
            session_factory=MagicMock(),
            graph_store_factory=MagicMock(),
            entity_extractor=_bad_extractor,
        )

        # Should not raise
        await adapter.index(_make_content())

    @pytest.mark.asyncio
    async def test_no_entities_returned(self) -> None:
        adapter = GraphIndexerAdapter(
            session_factory=MagicMock(),
            graph_store_factory=MagicMock(),
            entity_extractor=lambda t: [],
        )
        # Should not raise, session should not be created
        await adapter.index(_make_content())
