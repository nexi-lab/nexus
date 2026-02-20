"""Tests for MemoryIndexerAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.artifact_index.memory_adapter import MemoryIndexerAdapter
from nexus.bricks.artifact_index.protocol import ArtifactContent


def _make_content(text: str = "test content") -> ArtifactContent:
    return ArtifactContent(
        text=text,
        metadata={"key": "val"},
        artifact_id="art-1",
        task_id="task-1",
        zone_id="zone-1",
    )


class TestMemoryIndexerAdapter:
    """Memory adapter success and error paths."""

    @pytest.mark.asyncio
    async def test_success_calls_store_via_to_thread(self) -> None:
        memory = MagicMock()
        memory.store.return_value = None
        adapter = MemoryIndexerAdapter(memory=memory)

        mock_to_thread = AsyncMock(return_value=None)
        with patch(
            "nexus.bricks.artifact_index.memory_adapter.asyncio.to_thread",
            mock_to_thread,
        ):
            await adapter.index(_make_content())
            mock_to_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_text_skips(self) -> None:
        memory = MagicMock()
        adapter = MemoryIndexerAdapter(memory=memory)
        await adapter.index(_make_content(text=""))
        memory.store.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_suppressed(self) -> None:
        memory = MagicMock()
        adapter = MemoryIndexerAdapter(memory=memory)

        with patch(
            "nexus.bricks.artifact_index.memory_adapter.asyncio.to_thread",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await adapter.index(_make_content())

    @pytest.mark.asyncio
    async def test_passes_scope_and_metadata(self) -> None:
        memory = MagicMock()
        adapter = MemoryIndexerAdapter(memory=memory)

        mock_to_thread = AsyncMock(return_value=None)
        with patch(
            "nexus.bricks.artifact_index.memory_adapter.asyncio.to_thread",
            mock_to_thread,
        ):
            await adapter.index(_make_content())
            call_args = mock_to_thread.call_args
            assert call_args[0][0] is memory.store
            assert call_args[0][1] == "test content"
            assert call_args[1]["scope"] == "artifact"
            assert call_args[1]["_metadata"]["artifact_id"] == "art-1"
