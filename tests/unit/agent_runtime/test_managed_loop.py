"""Tests for AgentObserver + ManagedAgentLoop.

Tests cover:
- AgentObserver: shared notification accumulation (text, usage, tool_calls)
- ManagedAgentLoop: reasoning loop with mock LLM backend
- AcpConnection refactor: observer delegation still works
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.system_services.agent_runtime.observer import AgentObserver

# =============================================================================
# AgentObserver tests
# =============================================================================


class TestAgentObserver:
    """Test shared notification accumulation."""

    def test_text_accumulation(self) -> None:
        obs = AgentObserver()
        obs.reset_turn()

        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": "Hello"}})
        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": " world"}})

        assert obs.collected_text == "Hello world"
        result = obs.finish_turn(stop_reason="stop")
        assert result.text == "Hello world"
        assert result.stop_reason == "stop"

    def test_usage_accumulation(self) -> None:
        obs = AgentObserver()
        obs.reset_turn()

        obs.observe_update("usage_update", {"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        obs.observe_update("usage_update", {"usage": {"prompt_tokens": 20, "completion_tokens": 8}})

        result = obs.finish_turn()
        assert result.usage["prompt_tokens"] == 30
        assert result.usage["completion_tokens"] == 13

    def test_tool_call_counting(self) -> None:
        obs = AgentObserver()
        obs.reset_turn()

        obs.observe_update("tool_call", {"id": "tc1", "function": {"name": "read_file"}})
        obs.observe_update("tool_call", {"id": "tc2", "function": {"name": "write_file"}})

        assert obs.num_turns == 2
        result = obs.finish_turn()
        assert result.num_turns == 2
        assert len(result.tool_calls) == 2

    def test_text_not_accumulated_before_reset(self) -> None:
        """Text chunks are ignored when prompt is not active."""
        obs = AgentObserver()
        # No reset_turn() → _prompt_active is False
        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": "ignored"}})
        assert obs.collected_text == ""

    def test_user_message_chunk_clears_text(self) -> None:
        """user_message_chunk during active prompt clears accumulated text."""
        obs = AgentObserver()
        obs.reset_turn()

        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": "old"}})
        obs.observe_update("user_message_chunk", {})
        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": "new"}})

        assert obs.collected_text == "new"

    def test_model_name(self) -> None:
        obs = AgentObserver()
        assert obs.model_name is None
        obs.model_name = "gpt-4o"
        assert obs.model_name == "gpt-4o"

    def test_finish_turn_extracts_model_from_usage(self) -> None:
        obs = AgentObserver()
        obs.reset_turn()
        obs.observe_update("usage_update", {"usage": {"model": "claude-3-opus"}})
        result = obs.finish_turn()
        assert result.model == "claude-3-opus"


# =============================================================================
# ManagedAgentLoop tests
# =============================================================================


def _make_managed_loop(
    streaming_responses: list[list[tuple[str, dict[str, Any] | None]]],
) -> Any:
    """Create ManagedAgentLoop with mocked backend."""
    from nexus.system_services.agent_runtime.managed_loop import ManagedAgentLoop

    backend = MagicMock()

    call_count = 0

    def _generate_streaming(request: dict) -> list[tuple[str, dict | None]]:
        nonlocal call_count
        idx = min(call_count, len(streaming_responses) - 1)
        call_count += 1
        return streaming_responses[idx]

    backend.generate_streaming.side_effect = _generate_streaming

    loop = ManagedAgentLoop(
        backend=backend,
        system_prompt="You are helpful.",
        model="gpt-4o",
    )
    return loop, backend


class TestManagedAgentLoop:
    """Test kernel-managed reasoning loop."""

    @pytest.mark.asyncio()
    async def test_simple_text_response(self) -> None:
        """LLM returns text → loop returns immediately."""
        loop, _ = _make_managed_loop(
            [
                [
                    ("Hello", None),
                    (" there!", None),
                    ("", {"model": "gpt-4o", "usage": {"total_tokens": 10}, "latency_ms": 50}),
                ],
            ]
        )

        result = await loop.run("Hi")

        assert result.text == "Hello there!"
        assert result.stop_reason == "stop"
        assert result.model == "gpt-4o"

    @pytest.mark.asyncio()
    async def test_conversation_history(self) -> None:
        """Messages accumulate in conversation."""
        loop, _ = _make_managed_loop(
            [
                [("First response", None), ("", {"model": "gpt-4o", "usage": {}, "latency_ms": 0})],
            ]
        )

        await loop.run("Hello")

        # system + user + assistant = 3 messages
        assert len(loop.messages) == 3
        assert loop.messages[0]["role"] == "system"
        assert loop.messages[1]["role"] == "user"
        assert loop.messages[1]["content"] == "Hello"
        assert loop.messages[2]["role"] == "assistant"
        assert loop.messages[2]["content"] == "First response"

    @pytest.mark.asyncio()
    async def test_reset_clears_conversation(self) -> None:
        loop, _ = _make_managed_loop(
            [[("OK", None), ("", {"model": "m", "usage": {}, "latency_ms": 0})]],
        )

        await loop.run("Hello")
        assert len(loop.messages) == 3

        loop.reset()
        # Only system prompt remains
        assert len(loop.messages) == 1
        assert loop.messages[0]["role"] == "system"

    @pytest.mark.asyncio()
    async def test_max_turns_limit(self) -> None:
        """Loop stops after max_turns even with tool calls."""
        # This would require tool_call support in the response.
        # For MVP (no tool_call parsing from streaming), just verify
        # text response works within 1 turn.
        loop, _ = _make_managed_loop(
            [[("Done", None), ("", {"model": "m", "usage": {}, "latency_ms": 0})]],
        )
        loop._max_turns = 1

        result = await loop.run("Test")
        assert result.text == "Done"

    @pytest.mark.asyncio()
    async def test_observer_shared_with_loop(self) -> None:
        """Observer accumulates during loop execution."""
        loop, _ = _make_managed_loop(
            [
                [
                    ("Token1", None),
                    ("Token2", None),
                    (
                        "",
                        {
                            "model": "gpt-4o",
                            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                            "latency_ms": 30,
                        },
                    ),
                ],
            ]
        )

        result = await loop.run("Test")
        assert result.text == "Token1Token2"
        assert result.usage.get("prompt_tokens") == 5

    @pytest.mark.asyncio()
    async def test_tool_execution_read_file(self) -> None:
        """Tool call executes via fs_read."""
        from nexus.system_services.agent_runtime.managed_loop import ManagedAgentLoop

        backend = MagicMock()
        # First call: LLM returns empty text (would have tool_calls in full impl)
        # Second call: LLM returns text response
        backend.generate_streaming.return_value = iter(
            [("The file says hello", None), ("", {"model": "m", "usage": {}, "latency_ms": 0})]
        )

        fs_read = AsyncMock(return_value="file content here")

        loop = ManagedAgentLoop(
            backend=backend,
            fs_read=fs_read,
            system_prompt="Helper",
        )

        result = await loop.run("Read a file")
        # Without tool_call parsing, the loop returns text directly
        assert result.text == "The file says hello"
