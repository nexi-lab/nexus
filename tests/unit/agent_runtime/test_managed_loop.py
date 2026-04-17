"""Tests for AgentObserver + ManagedAgentLoop (everything-is-a-file).

Tests cover:
- AgentObserver: shared notification accumulation (text, usage, tool_calls)
- ManagedAgentLoop: VFS-native reasoning loop with mock kernel syscalls
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.agent_runtime.observer import AgentObserver

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
        obs = AgentObserver()
        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": "ignored"}})
        assert obs.collected_text == ""

    def test_user_message_chunk_clears_text(self) -> None:
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
# ManagedAgentLoop tests — everything-is-a-file
# =============================================================================


def _make_vfs_loop(
    stream_tokens: list[bytes] | None = None,
    system_prompt: str = "",
    tools_json: str = "[]",
) -> tuple[Any, dict[str, AsyncMock]]:
    """Create ManagedAgentLoop with mocked VFS syscalls."""
    from nexus.services.agent_runtime.managed_loop import ManagedAgentLoop

    # Mock sys_read: return different content based on path
    read_store: dict[str, bytes] = {}
    write_store: dict[str, bytes] = {}

    async def mock_sys_read(path: str) -> bytes:
        if path.endswith("/SYSTEM.md"):
            return system_prompt.encode()
        if path.endswith("/tools.json"):
            return tools_json.encode()
        if path in read_store:
            return read_store[path]
        raise FileNotFoundError(path)

    async def mock_sys_write(path: str, data: bytes) -> None:
        write_store[path] = data

    # Mock stream_read: deliver tokens then "done" message
    tokens = stream_tokens or [
        b"Hello",
        b" there!",
        json.dumps(
            {"type": "done", "model": "gpt-4o", "usage": {"total_tokens": 10}, "latency_ms": 50}
        ).encode(),
    ]
    token_iter = iter(tokens)
    offset_counter = [0]

    def mock_stream_read(path: str, offset: int) -> tuple[bytes, int]:
        try:
            data = next(token_iter)
            new_offset = offset_counter[0] + len(data)
            offset_counter[0] = new_offset
            return data, new_offset
        except StopIteration:
            from nexus.core.stream import StreamClosedError

            raise StreamClosedError("stream closed") from None

    # Mock the Rust-kernel streaming entry point (ManagedAgentLoop now takes
    # a `llm_start_streaming` async callable; the test just needs it to return
    # immediately so the stream_read mock can drive the token loop).
    llm_start_streaming = AsyncMock(return_value=None)

    sys_read_mock = AsyncMock(side_effect=mock_sys_read)
    sys_write_mock = AsyncMock(side_effect=mock_sys_write)
    stream_read_mock = MagicMock(side_effect=mock_stream_read)

    loop = ManagedAgentLoop(
        sys_read=sys_read_mock,
        sys_write=sys_write_mock,
        stream_read=stream_read_mock,
        llm_start_streaming=llm_start_streaming,
        agent_path="/zone/agents/test-agent",
        llm_path="/zone/llm/openai",
        conv_path="/zone/agents/test-agent/conversation",
        proc_path="/zone/proc/pid-123",
        model="gpt-4o",
    )

    mocks: dict[str, Any] = {
        "sys_read": sys_read_mock,
        "sys_write": sys_write_mock,
        "stream_read": stream_read_mock,
        "llm_start_streaming": llm_start_streaming,
        "write_store": write_store,
        "read_store": read_store,
    }
    return loop, mocks


class TestManagedAgentLoop:
    """Test VFS-native reasoning loop."""

    @pytest.mark.asyncio()
    async def test_initialize_reads_system_prompt_from_vfs(self) -> None:
        """System prompt loaded via sys_read from VFS (includes env block)."""
        loop, mocks = _make_vfs_loop(system_prompt="You are helpful.")
        await loop.initialize()

        assert len(loop.messages) == 1
        assert loop.messages[0]["role"] == "system"
        assert "You are helpful." in loop.messages[0]["content"]
        assert "# Environment" in loop.messages[0]["content"]

    @pytest.mark.asyncio()
    async def test_initialize_no_system_prompt(self) -> None:
        """No SYSTEM.md → system message still has env block."""
        loop, _ = _make_vfs_loop(system_prompt="")
        await loop.initialize()
        # assemble_system_prompt always includes env block
        assert len(loop.messages) == 1
        assert "# Environment" in loop.messages[0]["content"]

    @pytest.mark.asyncio()
    async def test_run_calls_llm_via_streaming_service(self) -> None:
        """LLM call goes through the Rust llm_start_streaming entry point."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        await loop.run("Hi")

        # llm_start_streaming(request_bytes, stream_path) was called.
        mocks["llm_start_streaming"].assert_called_once()
        call_args = mocks["llm_start_streaming"].call_args
        request_bytes = call_args[0][0]
        request = json.loads(request_bytes)
        assert "messages" in request

    @pytest.mark.asyncio()
    async def test_run_reads_tokens_from_dt_stream(self) -> None:
        """Tokens read via stream_read (kernel DT_STREAM IPC)."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        result = await loop.run("Hi")

        assert result.text == "Hello there!"
        # stream_read was called multiple times
        assert mocks["stream_read"].call_count >= 2

    @pytest.mark.asyncio()
    async def test_conversation_persisted_via_sys_write(self) -> None:
        """Conversation persisted to VFS after each mutation."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        await loop.run("Hello")

        # sys_write called for conversation persistence
        write_store = mocks["write_store"]
        conv_key = "/zone/agents/test-agent/conversation"
        assert conv_key in write_store

        # Conversation contains system (if any) + user + assistant
        conv = json.loads(write_store[conv_key])
        roles = [m["role"] for m in conv]
        assert "user" in roles
        assert "assistant" in roles

    @pytest.mark.asyncio()
    async def test_result_persisted_to_proc_fs(self) -> None:
        """Turn result persisted to /{zone}/proc/{pid}/result via sys_write."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        await loop.run("Hello")

        write_store = mocks["write_store"]
        result_key = "/zone/proc/pid-123/result"
        assert result_key in write_store

        result_data = json.loads(write_store[result_key])
        assert "text" in result_data
        assert result_data["session_id"] == loop.session_id

    @pytest.mark.asyncio()
    async def test_observer_shared(self) -> None:
        """Observer accumulates during VFS-native loop execution."""
        loop, _ = _make_vfs_loop()
        await loop.initialize()

        result = await loop.run("Test")
        assert result.text == "Hello there!"
        assert result.stop_reason == "stop"

    @pytest.mark.asyncio()
    async def test_tool_execution_via_sys_read(self) -> None:
        """Tool call executes via sys_read (VFS syscall)."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        # Simulate a tool call
        tool_call = {
            "id": "tc1",
            "function": {"name": "read_file", "arguments": '{"path": "/zone/data/file.txt"}'},
        }

        # Pre-populate read store
        mocks["read_store"]["/zone/data/file.txt"] = b"file content"

        result = await loop._execute_tool(tool_call)
        assert result == "file content"

    @pytest.mark.asyncio()
    async def test_tool_execution_via_sys_write(self) -> None:
        """Tool call executes via sys_write (VFS syscall)."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        tool_call = {
            "id": "tc2",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "/zone/output.txt", "content": "hello"}',
            },
        }

        result = await loop._execute_tool(tool_call)
        assert json.loads(result)["status"] == "ok"
        assert mocks["write_store"]["/zone/output.txt"] == b"hello"

    @pytest.mark.asyncio()
    async def test_load_conversation_from_vfs(self) -> None:
        """Resume conversation from VFS."""
        loop, mocks = _make_vfs_loop()

        saved_conv = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Previous message"},
            {"role": "assistant", "content": "Previous response"},
        ]
        mocks["read_store"]["/zone/agents/test-agent/conversation"] = json.dumps(
            saved_conv
        ).encode()

        await loop.load_conversation()
        assert len(loop.messages) == 3
        assert loop.messages[1]["content"] == "Previous message"

    @pytest.mark.asyncio()
    async def test_reset_reinitializes_from_vfs(self) -> None:
        """Reset reloads config from VFS."""
        loop, _ = _make_vfs_loop(system_prompt="System prompt")
        await loop.initialize()
        assert len(loop.messages) == 1

        # Add some messages
        loop._messages.append({"role": "user", "content": "test"})
        assert len(loop.messages) == 2

        # Reset → re-reads SYSTEM.md from VFS
        await loop.reset()
        assert len(loop.messages) == 1
        assert loop.messages[0]["role"] == "system"
