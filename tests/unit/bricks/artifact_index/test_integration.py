"""Integration test: ArtifactCallback handlers + mock adapters.

Validates the full chain: call handler(artifact, task_id, zone_id) →
extract content → call adapter.index().

Issue #907: Migrated from ScopedHookEngine to ArtifactCallback pattern.
"""

from unittest.mock import AsyncMock

import pytest

from nexus.bricks.artifact_index.hook_handlers import (
    make_graph_hook_handler,
    make_memory_hook_handler,
    make_tool_hook_handler,
)
from tests.unit.bricks.artifact_index.conftest import StubArtifact, StubTextPart


class TestArtifactIndexIntegration:
    """Full-chain integration: ArtifactCallback → handlers → adapters."""

    @pytest.mark.asyncio
    async def test_all_adapters_called(self) -> None:
        """Calling each handler invokes its adapter.index()."""
        memory_adapter = AsyncMock()
        tool_adapter = AsyncMock()
        graph_adapter = AsyncMock()

        memory_handler = make_memory_hook_handler(memory_adapter)
        tool_handler = make_tool_hook_handler(tool_adapter)
        graph_handler = make_graph_hook_handler(graph_adapter)

        artifact = StubArtifact(
            artifactId="art-1",
            parts=[StubTextPart(text="Hello world")],
        )

        for handler in (memory_handler, tool_handler, graph_handler):
            await handler(artifact, "t1", "z1")

        memory_adapter.index.assert_called_once()
        tool_adapter.index.assert_called_once()
        graph_adapter.index.assert_called_once()

    @pytest.mark.asyncio
    async def test_adapter_error_does_not_propagate(self) -> None:
        """Handler suppresses adapter errors (logs but does not raise)."""
        memory_adapter = AsyncMock()
        memory_adapter.index.side_effect = RuntimeError("memory down")
        tool_adapter = AsyncMock()

        memory_handler = make_memory_hook_handler(memory_adapter)
        tool_handler = make_tool_hook_handler(tool_adapter)

        artifact = StubArtifact(
            artifactId="art-2",
            parts=[StubTextPart(text="test")],
        )

        # Memory handler error should be suppressed
        await memory_handler(artifact, "t2", "z1")
        # Tool handler should still work independently
        await tool_handler(artifact, "t2", "z1")
        tool_adapter.index.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_artifact_skips(self) -> None:
        """Handler with None artifact skips adapter call."""
        adapter = AsyncMock()
        handler = make_memory_hook_handler(adapter)

        await handler(None, "t3", "z1")
        adapter.index.assert_not_called()
