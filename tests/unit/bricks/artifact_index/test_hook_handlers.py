"""Tests for artifact callback handler factories.

Issue #907: Migrated from HookContext/HookResult to ArtifactCallback pattern.
Handlers now take (artifact, task_id, zone_id) directly and return None.
"""

from unittest.mock import AsyncMock

import pytest

from nexus.bricks.artifact_index.hook_handlers import (
    make_graph_hook_handler,
    make_memory_hook_handler,
    make_tool_hook_handler,
)
from nexus.bricks.artifact_index.protocol import ArtifactContent
from tests.unit.bricks.artifact_index.conftest import StubArtifact, StubTextPart


class TestMakeMemoryHookHandler:
    """Memory hook handler creation and execution."""

    @pytest.mark.asyncio
    async def test_handler_calls_adapter_index(self) -> None:
        adapter = AsyncMock()
        handler = make_memory_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="hello")])

        await handler(artifact, "task-1", "zone-1")

        adapter.index.assert_called_once()
        call_arg = adapter.index.call_args[0][0]
        assert isinstance(call_arg, ArtifactContent)
        assert call_arg.text == "hello"

    @pytest.mark.asyncio
    async def test_handler_no_artifact_skips(self) -> None:
        adapter = AsyncMock()
        handler = make_memory_hook_handler(adapter)

        await handler(None, "task-1", "zone-1")

        adapter.index.assert_not_called()


class TestMakeToolHookHandler:
    """Tool hook handler creation and execution."""

    @pytest.mark.asyncio
    async def test_handler_calls_adapter(self) -> None:
        adapter = AsyncMock()
        handler = make_tool_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="data")])

        await handler(artifact, "task-1", "zone-1")

        adapter.index.assert_called_once()


class TestMakeGraphHookHandler:
    """Graph hook handler creation and execution."""

    @pytest.mark.asyncio
    async def test_handler_calls_adapter(self) -> None:
        adapter = AsyncMock()
        handler = make_graph_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="text")])

        await handler(artifact, "task-1", "zone-1")

        adapter.index.assert_called_once()

    @pytest.mark.asyncio
    async def test_handler_error_isolation(self) -> None:
        """Adapter errors should not propagate — handler suppresses them."""
        adapter = AsyncMock()
        adapter.index.side_effect = RuntimeError("boom")
        handler = make_graph_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="text")])

        # Should not raise
        await handler(artifact, "task-1", "zone-1")
