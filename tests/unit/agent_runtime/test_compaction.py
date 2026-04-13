"""Tests for context compaction (nexus-agent-plan §4.1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.agent_runtime.compaction import (
    DefaultCompactionStrategy,
    estimate_tokens,
)


class TestEstimateTokens:
    def test_empty(self) -> None:
        assert estimate_tokens([]) == 0

    def test_simple_message(self) -> None:
        msgs = [{"role": "user", "content": "hello world"}]  # 11 chars → ~2 tokens
        assert estimate_tokens(msgs) > 0

    def test_includes_tool_calls(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "1", "function": {"name": "read_file", "arguments": '{"path":"/foo"}'}}
                ],
            },
        ]
        assert estimate_tokens(msgs) > 0


class TestMicroCompact:
    def test_no_tool_messages_noop(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        original = [m.copy() for m in msgs]
        strategy.micro_compact(msgs)
        assert msgs == original

    def test_keeps_recent_3_tool_results(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "tool", "tool_call_id": "1", "content": "a" * 200},
            {"role": "tool", "tool_call_id": "2", "content": "b" * 200},
            {"role": "tool", "tool_call_id": "3", "content": "c" * 200},
        ]
        strategy.micro_compact(msgs)
        # All 3 are "recent" → none cleared
        assert msgs[0]["content"] == "a" * 200
        assert msgs[1]["content"] == "b" * 200
        assert msgs[2]["content"] == "c" * 200

    def test_clears_old_tool_results(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "tool", "tool_call_id": "1", "content": "old1" * 50},  # >100 chars
            {"role": "tool", "tool_call_id": "2", "content": "old2" * 50},
            {"role": "tool", "tool_call_id": "3", "content": "recent1" * 50},
            {"role": "tool", "tool_call_id": "4", "content": "recent2" * 50},
            {"role": "tool", "tool_call_id": "5", "content": "recent3" * 50},
        ]
        strategy.micro_compact(msgs)
        assert msgs[0]["content"] == "[cleared]"
        assert msgs[1]["content"] == "[cleared]"
        assert msgs[2]["content"] == "recent1" * 50
        assert msgs[3]["content"] == "recent2" * 50
        assert msgs[4]["content"] == "recent3" * 50

    def test_short_old_results_not_cleared(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "tool", "tool_call_id": "1", "content": "ok"},  # <100 chars
            {"role": "tool", "tool_call_id": "2", "content": "recent1" * 50},
            {"role": "tool", "tool_call_id": "3", "content": "recent2" * 50},
            {"role": "tool", "tool_call_id": "4", "content": "recent3" * 50},
        ]
        strategy.micro_compact(msgs)
        assert msgs[0]["content"] == "ok"  # short, not cleared

    def test_preserves_non_tool_messages(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 200},
            {"role": "assistant", "content": "response"},
            {"role": "tool", "tool_call_id": "2", "content": "y" * 200},
            {"role": "tool", "tool_call_id": "3", "content": "z" * 200},
            {"role": "tool", "tool_call_id": "4", "content": "w" * 200},
        ]
        strategy.micro_compact(msgs)
        assert msgs[0]["content"] == "hi"  # user preserved
        assert msgs[1]["content"] == "[cleared]"  # old tool cleared
        assert msgs[2]["content"] == "response"  # assistant preserved


class TestShouldAutoCompact:
    def test_below_threshold(self) -> None:
        strategy = DefaultCompactionStrategy(token_threshold=100)
        msgs = [{"role": "user", "content": "hi"}]
        assert strategy.should_auto_compact(msgs) is False

    def test_above_threshold(self) -> None:
        strategy = DefaultCompactionStrategy(token_threshold=10)
        msgs = [{"role": "user", "content": "x" * 1000}]  # ~250 tokens
        assert strategy.should_auto_compact(msgs) is True


class TestAutoCompact:
    @pytest.mark.asyncio
    async def test_preserves_system_message(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = await strategy.auto_compact(msgs)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are helpful."
        assert len(result) == 2  # system + compressed

    @pytest.mark.asyncio
    async def test_no_system_message(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = await strategy.auto_compact(msgs)
        assert len(result) == 1  # compressed only
        assert "[Previous conversation compressed]" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_saves_transcript(self) -> None:
        sys_write = MagicMock()
        strategy = DefaultCompactionStrategy(
            sys_write=sys_write,
            agent_path="/zone/agents/test",
        )
        msgs = [{"role": "user", "content": "hi"}]
        await strategy.auto_compact(msgs)

        sys_write.assert_called_once()
        call_path = sys_write.call_args[0][0]
        assert call_path.startswith("/zone/agents/test/transcripts/")
        call_data = sys_write.call_args[0][1]
        assert b'"role":"user"' in call_data

    @pytest.mark.asyncio
    async def test_uses_llm_for_summary(self) -> None:
        llm_call = AsyncMock(return_value="Summary: user said hi, assistant said hello.")
        strategy = DefaultCompactionStrategy(llm_call=llm_call)
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = await strategy.auto_compact(msgs)

        llm_call.assert_called_once()
        assert "Summary: user said hi" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_fallback_when_no_llm(self) -> None:
        strategy = DefaultCompactionStrategy()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = await strategy.auto_compact(msgs)
        assert "1 user messages" in result[0]["content"]
        assert "1 assistant responses" in result[0]["content"]
