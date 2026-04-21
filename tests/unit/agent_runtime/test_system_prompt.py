"""Tests for system prompt assembly (nexus-agent-plan §4.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.agent_runtime.system_prompt import (
    _generate_env_block,
    assemble_system_prompt,
)


def _make_sys_read(files: dict[str, str]) -> AsyncMock:
    """Create a mock sys_read that returns bytes for known paths."""

    async def _read(path: str) -> bytes:
        if path in files:
            return files[path].encode("utf-8")
        raise FileNotFoundError(path)

    return AsyncMock(side_effect=_read)


class TestAssembleSystemPrompt:
    @pytest.mark.asyncio
    async def test_system_md_only(self) -> None:
        sys_read = _make_sys_read(
            {
                "/root/agents/test/SYSTEM.md": "You are helpful.",
            }
        )
        result = await assemble_system_prompt(
            sys_read=sys_read,
            zone_id=ROOT_ZONE_ID,
            agent_id="test",
        )
        assert "You are helpful." in result
        assert "# Environment" in result  # env block always present

    @pytest.mark.asyncio
    async def test_no_system_md(self) -> None:
        sys_read = _make_sys_read({})
        result = await assemble_system_prompt(
            sys_read=sys_read,
            zone_id=ROOT_ZONE_ID,
            agent_id="test",
        )
        # Only env block, no system prompt
        assert "# Environment" in result
        assert "You are helpful" not in result

    @pytest.mark.asyncio
    async def test_includes_prompt_fragments(self) -> None:
        sys_read = _make_sys_read(
            {
                "/root/agents/test/SYSTEM.md": "Identity.",
                "/root/agents/test/prompts/output_efficiency.md": "Be concise.",
                "/root/agents/test/prompts/tool_batching.md": "Batch tools.",
            }
        )
        result = await assemble_system_prompt(
            sys_read=sys_read,
            zone_id=ROOT_ZONE_ID,
            agent_id="test",
        )
        assert "Identity." in result
        assert "Be concise." in result
        assert "Batch tools." in result

    @pytest.mark.asyncio
    async def test_includes_project_context(self) -> None:
        sys_read = _make_sys_read(
            {
                "/root/agents/test/SYSTEM.md": "Identity.",
                "/workspace/.nexus/agent.md": "Project: Nexus. Always use Python.",
            }
        )
        result = await assemble_system_prompt(
            sys_read=sys_read,
            zone_id=ROOT_ZONE_ID,
            agent_id="test",
            cwd="/workspace",
        )
        assert "Project: Nexus" in result

    @pytest.mark.asyncio
    async def test_model_in_env_block(self) -> None:
        sys_read = _make_sys_read({})
        result = await assemble_system_prompt(
            sys_read=sys_read,
            zone_id=ROOT_ZONE_ID,
            agent_id="test",
            model="claude-opus-4",
        )
        assert "claude-opus-4" in result

    @pytest.mark.asyncio
    async def test_missing_fragments_silently_skipped(self) -> None:
        sys_read = _make_sys_read(
            {
                "/root/agents/test/SYSTEM.md": "Identity.",
                # No prompt fragments exist
            }
        )
        result = await assemble_system_prompt(
            sys_read=sys_read,
            zone_id=ROOT_ZONE_ID,
            agent_id="test",
        )
        assert "Identity." in result
        # Should not crash on missing fragments


class TestGenerateEnvBlock:
    def test_basic(self) -> None:
        block = _generate_env_block()
        assert "# Environment" in block
        assert "Platform:" in block

    def test_with_model(self) -> None:
        block = _generate_env_block(model="gpt-4o")
        assert "gpt-4o" in block

    def test_with_cwd(self) -> None:
        block = _generate_env_block(cwd="/tmp/test")
        assert "/tmp/test" in block
