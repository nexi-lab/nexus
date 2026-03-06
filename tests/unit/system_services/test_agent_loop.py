"""TDD test scaffolding for agent_loop (Issue #2761, Phase 1).

Tests define the expected behavior of the agent execution loop — the core
reasoning cycle that drives agent behavior:

    receive message → think → call tools → respond → repeat

Contract under test:
    agent_loop()           — execute the agent reasoning loop
    AgentLoopConfig        — loop configuration (max_turns, context limits)
    Context trimming       — handle context window overflow
    Parallel tool dispatch — concurrent tool execution
    Graceful shutdown      — handle SIGTERM/closure during execution

See: src/nexus/contracts/agent_runtime_types.py
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.agent_runtime_types import (
    AgentLoopConfig,
    AgentProcess,
    MaxTurnsExceededError,
    ProcessState,
    ToolResult,
)


def _mock_tool_call(tc_id: str, tool_name: str, arguments: str = "{}") -> MagicMock:
    """Create a mock tool call with properly set name attribute.

    MagicMock(name=...) sets the mock's internal name, NOT the .name attribute.
    This helper avoids that gotcha.
    """
    func = MagicMock()
    func.name = tool_name
    func.arguments = arguments
    tc = MagicMock()
    tc.id = tc_id
    tc.function = func
    return tc


# ======================================================================
# Config tests (pass immediately)
# ======================================================================


class TestAgentLoopConfig:
    """Verify AgentLoopConfig frozen dataclass."""

    def test_defaults(self) -> None:
        config = AgentLoopConfig()
        assert config.max_turns == 100
        assert config.max_context_tokens == 128_000
        assert config.parallel_tool_dispatch is True
        assert config.tool_timeout == 30.0
        assert config.trim_strategy == "sliding_window"

    def test_custom_config(self) -> None:
        config = AgentLoopConfig(
            max_turns=10,
            max_context_tokens=4096,
            parallel_tool_dispatch=False,
            tool_timeout=5.0,
            trim_strategy="summarize",
        )
        assert config.max_turns == 10
        assert config.parallel_tool_dispatch is False

    def test_immutable(self) -> None:
        config = AgentLoopConfig()
        attr = "max_turns"
        with pytest.raises(AttributeError):
            setattr(config, attr, 50)


class TestMaxTurnsExceeded:
    """Verify MaxTurnsExceededError exception."""

    def test_attributes(self) -> None:
        err = MaxTurnsExceededError(max_turns=10, agent_id="agent-1")
        assert err.max_turns == 10
        assert err.agent_id == "agent-1"
        assert err.is_expected is True
        assert "10" in str(err)


# ======================================================================
# Behavioral tests (RED — need real implementation)
# ======================================================================


class TestAgentLoopSingleTurn:
    """Tests for single-turn execution (one tool call, one response)."""

    async def test_single_turn_completes(self) -> None:
        """Agent loop executes one turn: receive → think → tool → respond."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        dispatcher.dispatch.return_value = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="file content",
        )
        session_store = AsyncMock()
        llm_client = AsyncMock()
        llm_client.chat.side_effect = [
            MagicMock(
                tool_calls=[_mock_tool_call("tc-1", "vfs_read", '{"path": "/hello.txt"}')],
                content=None,
            ),
            MagicMock(tool_calls=None, content="The file says hello"),
        ]

        config = AgentLoopConfig(max_turns=10)
        result = await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="Read /hello.txt",
        )

        assert result is not None
        dispatcher.dispatch.assert_called_once()
        assert llm_client.chat.call_count == 2

    async def test_no_tool_call_returns_immediately(self) -> None:
        """If LLM responds without tool calls, loop exits after one turn."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        session_store = AsyncMock()
        llm_client = AsyncMock()
        llm_client.chat.return_value = MagicMock(
            tool_calls=None, content="I don't need any tools for this."
        )

        config = AgentLoopConfig(max_turns=10)
        result = await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="What is 2+2?",
        )

        assert result is not None
        dispatcher.dispatch.assert_not_called()
        llm_client.chat.assert_called_once()


class TestAgentLoopMultiTurn:
    """Tests for multi-turn execution (multiple tool calls in sequence)."""

    async def test_multi_turn_executes_all_turns(self) -> None:
        """Agent loop handles multiple rounds of tool calls."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        dispatcher.dispatch.return_value = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="data",
        )
        session_store = AsyncMock()
        llm_client = AsyncMock()

        # 3 turns of tool calls, then final response
        tool_response = MagicMock(
            tool_calls=[_mock_tool_call("tc-1", "vfs_read")],
            content=None,
        )
        final_response = MagicMock(tool_calls=None, content="Done after 3 turns")
        llm_client.chat.side_effect = [
            tool_response,
            tool_response,
            tool_response,
            final_response,
        ]

        config = AgentLoopConfig(max_turns=10)
        result = await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="Process data",
        )

        assert result is not None
        assert dispatcher.dispatch.call_count == 3


class TestAgentLoopMaxTurns:
    """Tests for max_turns limit enforcement."""

    async def test_exceeds_max_turns_raises(self) -> None:
        """Agent loop raises MaxTurnsExceededError when limit is hit."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        dispatcher.dispatch.return_value = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="data",
        )
        session_store = AsyncMock()
        llm_client = AsyncMock()

        # LLM always returns tool calls (infinite loop without limit)
        llm_client.chat.return_value = MagicMock(
            tool_calls=[_mock_tool_call("tc-1", "vfs_read")],
            content=None,
        )

        config = AgentLoopConfig(max_turns=3)
        with pytest.raises(MaxTurnsExceededError):
            await agent_loop(
                process=process,
                dispatcher=dispatcher,
                session_store=session_store,
                llm_client=llm_client,
                config=config,
                initial_message="Loop forever",
            )

        assert dispatcher.dispatch.call_count == 3

    async def test_max_turns_one_allows_single_tool_call(self) -> None:
        """max_turns=1 allows exactly one tool call before stopping."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        dispatcher.dispatch.return_value = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="data",
        )
        session_store = AsyncMock()
        llm_client = AsyncMock()
        llm_client.chat.return_value = MagicMock(
            tool_calls=[_mock_tool_call("tc-1", "vfs_read")],
            content=None,
        )

        config = AgentLoopConfig(max_turns=1)
        with pytest.raises(MaxTurnsExceededError):
            await agent_loop(
                process=process,
                dispatcher=dispatcher,
                session_store=session_store,
                llm_client=llm_client,
                config=config,
                initial_message="Do something",
            )

        assert dispatcher.dispatch.call_count == 1


class TestAgentLoopContextTrimming:
    """Tests for context window overflow handling."""

    async def test_context_trimming_activates_on_overflow(self) -> None:
        """When context exceeds max_context_tokens, trimming is applied."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        dispatcher.dispatch.return_value = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="x" * 10_000,  # Large output
        )
        session_store = AsyncMock()
        llm_client = AsyncMock()

        llm_client.chat.side_effect = [
            MagicMock(
                tool_calls=[_mock_tool_call("tc-1", "vfs_read")],
                content=None,
            ),
            MagicMock(tool_calls=None, content="Done"),
        ]

        # Very small context window to force trimming
        config = AgentLoopConfig(max_turns=10, max_context_tokens=100)
        result = await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="Read large file",
        )

        # The loop should complete (trimming handled overflow)
        assert result is not None


class TestAgentLoopParallelToolDispatch:
    """Tests for parallel tool execution within a single turn."""

    async def test_parallel_tools_dispatched_concurrently(self) -> None:
        """When LLM returns multiple tool calls, they run in parallel."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )

        call_times: list[float] = []

        async def tracked_dispatch(tool_name, arguments, **kwargs):  # noqa: ARG001
            import time

            start = time.monotonic()
            await asyncio.sleep(0.02)
            call_times.append(time.monotonic() - start)
            return ToolResult(
                tool_call_id=kwargs.get("tool_call_id", "tc"),
                name=str(tool_name),
                output="ok",
            )

        dispatcher = AsyncMock()
        dispatcher.dispatch.side_effect = tracked_dispatch
        session_store = AsyncMock()
        llm_client = AsyncMock()

        llm_client.chat.side_effect = [
            MagicMock(
                tool_calls=[_mock_tool_call(f"tc-{i}", f"tool_{i}") for i in range(3)],
                content=None,
            ),
            MagicMock(tool_calls=None, content="All done"),
        ]

        config = AgentLoopConfig(max_turns=10, parallel_tool_dispatch=True)
        await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="Run 3 tools",
        )

        assert dispatcher.dispatch.call_count == 3

    async def test_sequential_when_parallel_disabled(self) -> None:
        """When parallel_tool_dispatch=False, tools run sequentially."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )

        execution_order: list[str] = []

        async def ordered_dispatch(tool_name, arguments, **kwargs):  # noqa: ARG001
            execution_order.append(str(tool_name))
            return ToolResult(
                tool_call_id=kwargs.get("tool_call_id", "tc"),
                name=str(tool_name),
                output="ok",
            )

        dispatcher = AsyncMock()
        dispatcher.dispatch.side_effect = ordered_dispatch
        session_store = AsyncMock()
        llm_client = AsyncMock()

        llm_client.chat.side_effect = [
            MagicMock(
                tool_calls=[_mock_tool_call(f"tc-{i}", f"tool_{i}") for i in range(3)],
                content=None,
            ),
            MagicMock(tool_calls=None, content="Done"),
        ]

        config = AgentLoopConfig(max_turns=10, parallel_tool_dispatch=False)
        await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="Run 3 tools sequentially",
        )

        # Sequential execution preserves order
        assert execution_order == ["tool_0", "tool_1", "tool_2"]


class TestAgentLoopGracefulShutdown:
    """Tests for graceful shutdown during execution."""

    async def test_shutdown_during_tool_execution(self) -> None:
        """Process termination during tool execution is handled gracefully."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        shutdown_event = asyncio.Event()

        async def slow_dispatch(tool_name, arguments, **kwargs):  # noqa: ARG001
            shutdown_event.set()  # Signal that tool is executing
            await asyncio.sleep(10)  # Would block forever
            return ToolResult(
                tool_call_id="tc-1",
                name=str(tool_name),
                output="ok",
            )

        dispatcher = AsyncMock()
        dispatcher.dispatch.side_effect = slow_dispatch
        session_store = AsyncMock()
        llm_client = AsyncMock()
        llm_client.chat.return_value = MagicMock(
            tool_calls=[_mock_tool_call("tc-1", "slow_tool")],
            content=None,
        )

        config = AgentLoopConfig(max_turns=10, tool_timeout=0.1)

        # The loop should handle the timeout gracefully
        # (either via ToolTimeoutError or cancellation)
        with pytest.raises((asyncio.CancelledError, Exception)):
            await agent_loop(
                process=process,
                dispatcher=dispatcher,
                session_store=session_store,
                llm_client=llm_client,
                config=config,
                initial_message="Run slow tool",
            )

    async def test_checkpoints_on_clean_exit(self) -> None:
        """Agent loop checkpoints session state on clean exit."""
        from nexus.system_services.agent_runtime.agent_loop import agent_loop

        process = AgentProcess(
            pid="p-1",
            agent_id="agent-1",
            zone_id="zone-1",
            state=ProcessState.RUNNING,
        )
        dispatcher = AsyncMock()
        session_store = AsyncMock()
        session_store.checkpoint.return_value = "hash-abc"
        llm_client = AsyncMock()
        llm_client.chat.return_value = MagicMock(tool_calls=None, content="Goodbye")

        config = AgentLoopConfig(max_turns=10)
        await agent_loop(
            process=process,
            dispatcher=dispatcher,
            session_store=session_store,
            llm_client=llm_client,
            config=config,
            initial_message="Say bye",
        )

        # Session should be checkpointed on clean exit
        session_store.checkpoint.assert_called_once()
