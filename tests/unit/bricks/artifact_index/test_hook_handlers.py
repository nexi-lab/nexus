"""Tests for hook handler factories."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.bricks.artifact_index.hook_handlers import (
    make_graph_hook_handler,
    make_memory_hook_handler,
    make_tool_hook_handler,
)
from nexus.bricks.artifact_index.protocol import ArtifactContent
from nexus.services.protocols.hook_engine import HookContext
from tests.unit.bricks.artifact_index.conftest import StubArtifact, StubTextPart


def _make_hook_context(artifact: StubArtifact | None = None) -> HookContext:
    payload: dict[str, object] = {
        "task_id": "task-1",
        "zone_id": "zone-1",
    }
    if artifact is not None:
        payload["artifact"] = artifact
    return HookContext(
        phase="post_artifact_create",
        path=None,
        zone_id="zone-1",
        agent_id=None,
        payload=payload,
    )


class TestMakeMemoryHookHandler:
    """Memory hook handler creation and execution."""

    @pytest.mark.asyncio
    async def test_handler_calls_adapter_index(self) -> None:
        adapter = AsyncMock()
        handler = make_memory_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="hello")])
        ctx = _make_hook_context(artifact)

        result = await handler(ctx)

        assert result.proceed is True
        adapter.index.assert_called_once()
        call_arg = adapter.index.call_args[0][0]
        assert isinstance(call_arg, ArtifactContent)
        assert call_arg.text == "hello"

    @pytest.mark.asyncio
    async def test_handler_no_artifact_skips(self) -> None:
        adapter = AsyncMock()
        handler = make_memory_hook_handler(adapter)

        ctx = _make_hook_context(artifact=None)
        result = await handler(ctx)

        assert result.proceed is True
        adapter.index.assert_not_called()


class TestMakeToolHookHandler:
    """Tool hook handler creation and execution."""

    @pytest.mark.asyncio
    async def test_handler_returns_proceed_true(self) -> None:
        adapter = AsyncMock()
        handler = make_tool_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="data")])
        ctx = _make_hook_context(artifact)

        result = await handler(ctx)
        assert result.proceed is True


class TestMakeGraphHookHandler:
    """Graph hook handler creation and execution."""

    @pytest.mark.asyncio
    async def test_handler_returns_proceed_true(self) -> None:
        adapter = AsyncMock()
        handler = make_graph_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="text")])
        ctx = _make_hook_context(artifact)

        result = await handler(ctx)
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_handler_error_isolation(self) -> None:
        """Adapter errors should not propagate — handler returns proceed=True."""
        adapter = AsyncMock()
        adapter.index.side_effect = RuntimeError("boom")
        handler = make_graph_hook_handler(adapter)

        artifact = StubArtifact(artifactId="a1", parts=[StubTextPart(text="text")])
        ctx = _make_hook_context(artifact)

        result = await handler(ctx)
        assert result.proceed is True
