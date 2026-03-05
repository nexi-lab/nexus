"""Tests for agent_step() — single-turn extraction from agent_loop (Issue #2761).

Verifies:
    - StepResult action variants (CONTINUE, DONE, ERROR)
    - Tool dispatch within a step
    - Checkpoint callback invocation
    - agent_loop() as backward-compatible wrapper
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from nexus.contracts.agent_runtime_types import StepAction, StepResult
from nexus.contracts.llm_types import Message, MessageRole
from nexus.system_services.agent_runtime.loop import agent_loop, agent_step
from nexus.system_services.agent_runtime.types import (
    AgentContext,
    AgentProcessConfig,
    Completed,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
)

# ======================================================================
# Helpers
# ======================================================================


def _mock_tool_call(tool_id: str, name: str, arguments: str = "{}") -> MagicMock:
    """Create a mock ToolCall-like object."""
    tc = MagicMock()
    tc.id = tool_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_config(**overrides: object) -> AgentProcessConfig:
    return AgentProcessConfig(name="test-agent", **overrides)


def _make_context(
    messages: tuple[Message, ...] = (),
    system_prompt: str = "You are a test agent.",
) -> AgentContext:
    return AgentContext(
        system_prompt=system_prompt,
        messages=messages,
        tools=(),
    )


def _make_ctx() -> MagicMock:
    """Create a mock OperationContext."""
    ctx = MagicMock()
    ctx.user_id = "test-user"
    ctx.zone_id = "test-zone"
    return ctx


# ======================================================================
# agent_step tests
# ======================================================================


class TestAgentStepContinue:
    """agent_step returns CONTINUE when tool calls are dispatched."""

    async def test_step_returns_continue_on_tool_calls(self) -> None:
        llm = AsyncMock()
        tc = _mock_tool_call("tc-1", "read_file", '{"path": "/test.txt"}')
        response = MagicMock()
        response.tool_calls = [tc]
        response.content = None
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=100)

        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value="file contents")

        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="Read test.txt")]

        result = await agent_step(llm, dispatcher, context, config, ctx, messages, turn=0)

        assert isinstance(result, StepResult)
        assert result.action == StepAction.CONTINUE
        assert result.turn == 1
        assert result.error is None
        assert len(result.messages) > 1  # user + assistant + tool

    async def test_step_dispatches_tools(self) -> None:
        llm = AsyncMock()
        tc = _mock_tool_call("tc-1", "grep", '{"pattern": "TODO"}')
        response = MagicMock()
        response.tool_calls = [tc]
        response.content = None
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=100)

        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value="Found 3 matches")

        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="Search")]

        await agent_step(llm, dispatcher, context, config, ctx, messages, turn=0)

        dispatcher.dispatch.assert_called_once()


class TestAgentStepDone:
    """agent_step returns DONE on final response (no tool calls)."""

    async def test_step_returns_done_on_final_response(self) -> None:
        llm = AsyncMock()
        response = MagicMock()
        response.tool_calls = None
        response.content = "The answer is 42."
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=50)

        dispatcher = AsyncMock()
        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="What is 6*7?")]

        result = await agent_step(llm, dispatcher, context, config, ctx, messages, turn=0)

        assert result.action == StepAction.DONE
        assert result.error is None
        # Should have user + assistant messages
        assert any(m.content == "The answer is 42." for m in result.messages)


class TestAgentStepError:
    """agent_step returns ERROR when LLM call fails."""

    async def test_step_returns_error_on_llm_failure(self) -> None:
        llm = AsyncMock()
        llm.complete_async = AsyncMock(side_effect=RuntimeError("API timeout"))
        llm.count_tokens = MagicMock(return_value=50)

        dispatcher = AsyncMock()
        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="Hello")]

        result = await agent_step(llm, dispatcher, context, config, ctx, messages, turn=0)

        assert result.action == StepAction.ERROR
        assert result.error is not None
        assert "API timeout" in result.error


class TestAgentStepCheckpoint:
    """agent_step calls checkpoint after tool dispatch."""

    async def test_step_checkpoints_after_dispatch(self) -> None:
        llm = AsyncMock()
        tc = _mock_tool_call("tc-1", "read_file", '{"path": "/f.txt"}')
        response = MagicMock()
        response.tool_calls = [tc]
        response.content = None
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=100)

        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value="data")

        on_checkpoint = AsyncMock()

        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="Read")]

        await agent_step(
            llm,
            dispatcher,
            context,
            config,
            ctx,
            messages,
            turn=0,
            on_checkpoint=on_checkpoint,
        )

        on_checkpoint.assert_called_once()


class TestAgentStepEvents:
    """agent_step emits events via on_event callback."""

    async def test_step_emits_tool_events_on_continue(self) -> None:
        llm = AsyncMock()
        tc = _mock_tool_call("tc-1", "read_file", '{"path": "/f.txt"}')
        response = MagicMock()
        response.tool_calls = [tc]
        response.content = None
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=100)

        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value="data")

        events: list[object] = []
        on_event = AsyncMock(side_effect=lambda e: events.append(e))

        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="Read")]

        await agent_step(
            llm,
            dispatcher,
            context,
            config,
            ctx,
            messages,
            turn=0,
            on_event=on_event,
        )

        assert any(isinstance(e, ToolCallStart) for e in events)
        assert any(isinstance(e, ToolCallResult) for e in events)

    async def test_step_emits_done_events(self) -> None:
        llm = AsyncMock()
        response = MagicMock()
        response.tool_calls = None
        response.content = "Done."
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=50)

        dispatcher = AsyncMock()

        events: list[object] = []
        on_event = AsyncMock(side_effect=lambda e: events.append(e))

        config = _make_config(max_context_tokens=200_000)
        context = _make_context()
        ctx = _make_ctx()
        messages: list[Message] = [Message(role=MessageRole.USER, content="Done?")]

        await agent_step(
            llm,
            dispatcher,
            context,
            config,
            ctx,
            messages,
            turn=0,
            on_event=on_event,
        )

        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, Completed) for e in events)


# ======================================================================
# agent_loop wrapper tests
# ======================================================================


class TestAgentLoopWrapper:
    """agent_loop() wraps agent_step() in a backward-compatible loop."""

    async def test_agent_loop_calls_step_repeatedly(self) -> None:
        """Loop continues while step returns CONTINUE, stops on DONE."""
        llm = AsyncMock()

        # First call: tool call (CONTINUE)
        tc = _mock_tool_call("tc-1", "read_file", '{"path": "/f.txt"}')
        resp_tools = MagicMock()
        resp_tools.tool_calls = [tc]
        resp_tools.content = None

        # Second call: final answer (DONE)
        resp_done = MagicMock()
        resp_done.tool_calls = None
        resp_done.content = "Final answer."

        llm.complete_async = AsyncMock(side_effect=[resp_tools, resp_done])
        llm.count_tokens = MagicMock(return_value=100)

        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value="file data")

        config = _make_config(max_turns=10, max_context_tokens=200_000)
        context = _make_context(
            messages=(Message(role=MessageRole.USER, content="Read and summarize"),),
        )
        ctx = _make_ctx()

        result = await agent_loop(llm, dispatcher, context, config, ctx)

        assert isinstance(result, list)
        # Should have: user + assistant(tool) + tool_result + assistant(final)
        assert any(m.content == "Final answer." for m in result)
        assert llm.complete_async.call_count == 2

    async def test_agent_loop_stops_on_done(self) -> None:
        """Loop stops immediately when step returns DONE."""
        llm = AsyncMock()
        response = MagicMock()
        response.tool_calls = None
        response.content = "Immediate answer."
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=50)

        dispatcher = AsyncMock()
        config = _make_config(max_turns=100, max_context_tokens=200_000)
        context = _make_context(
            messages=(Message(role=MessageRole.USER, content="Quick question"),),
        )
        ctx = _make_ctx()

        result = await agent_loop(llm, dispatcher, context, config, ctx)

        assert llm.complete_async.call_count == 1
        assert any(m.content == "Immediate answer." for m in result)

    async def test_agent_loop_stops_on_max_turns(self) -> None:
        """Loop stops when max_turns is reached."""
        llm = AsyncMock()
        tc = _mock_tool_call("tc-1", "read_file", '{"path": "/f.txt"}')
        response = MagicMock()
        response.tool_calls = [tc]
        response.content = None
        llm.complete_async = AsyncMock(return_value=response)
        llm.count_tokens = MagicMock(return_value=100)

        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value="data")

        config = _make_config(max_turns=2, max_context_tokens=200_000)
        context = _make_context(
            messages=(Message(role=MessageRole.USER, content="Loop"),),
        )
        ctx = _make_ctx()

        result = await agent_loop(llm, dispatcher, context, config, ctx)

        # Should have called LLM exactly max_turns times (each CONTINUE)
        assert llm.complete_async.call_count == 2
        assert isinstance(result, list)
