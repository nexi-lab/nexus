"""Tests for ToolIndexerAdapter."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.artifact_index.protocol import ArtifactContent
from nexus.bricks.artifact_index.tool_adapter import ToolIndexerAdapter


@dataclass
class _FakeToolInfo:
    """Stub for ToolInfo used in tests."""

    name: str = ""
    description: str = ""
    server: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


def _make_content(
    text: str = "",
    artifact_id: str = "art-1",
    metadata: dict[str, Any] | None = None,
) -> ArtifactContent:
    return ArtifactContent(
        text=text,
        metadata=metadata or {},
        artifact_id=artifact_id,
        task_id="task-1",
        zone_id="zone-1",
    )


class TestToolIndexerAdapter:
    """Tool adapter parsing and indexing."""

    @pytest.mark.asyncio
    async def test_single_tool_schema(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        schema = {"name": "my_tool", "description": "Does stuff"}
        content = _make_content(text=json.dumps(schema))
        await adapter.index(content)

        tool_index.add_tool.assert_called_once()
        tool_arg = tool_index.add_tool.call_args[0][0]
        assert tool_arg.name == "my_tool"
        assert tool_arg.description == "Does stuff"

    @pytest.mark.asyncio
    async def test_tool_list(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        schemas = [
            {"name": "tool_a", "description": "A"},
            {"name": "tool_b", "description": "B"},
        ]
        content = _make_content(text=json.dumps(schemas))
        await adapter.index(content)

        assert tool_index.add_tool.call_count == 2

    @pytest.mark.asyncio
    async def test_nested_tools_key(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        data = {"tools": [{"name": "t1", "description": "d1"}]}
        content = _make_content(text=json.dumps(data))
        await adapter.index(content)

        tool_index.add_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_json_skipped(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        content = _make_content(text="just plain text, not JSON")
        await adapter.index(content)

        tool_index.add_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        content = _make_content(text="")
        await adapter.index(content)

        tool_index.add_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_json_without_tool_keys_skipped(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        content = _make_content(text=json.dumps({"foo": "bar"}))
        await adapter.index(content)

        tool_index.add_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_defaults_to_artifact_prefix(self) -> None:
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(
            tool_index=tool_index,
            tool_info_factory=_FakeToolInfo,
        )

        schema = {"name": "x", "description": "y"}
        content = _make_content(text=json.dumps(schema), artifact_id="art-99")
        await adapter.index(content)

        tool_arg = tool_index.add_tool.call_args[0][0]
        assert tool_arg.server == "artifact:art-99"

    @pytest.mark.asyncio
    async def test_no_factory_skips(self) -> None:
        """When tool_info_factory is None, should skip gracefully."""
        tool_index = MagicMock()
        adapter = ToolIndexerAdapter(tool_index=tool_index, tool_info_factory=None)

        schema = {"name": "x", "description": "y"}
        content = _make_content(text=json.dumps(schema))
        await adapter.index(content)

        tool_index.add_tool.assert_not_called()
