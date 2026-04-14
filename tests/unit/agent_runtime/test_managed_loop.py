"""Tests for AgentObserver + ManagedAgentLoop (everything-is-a-file).

Tests cover:
- AgentObserver: shared notification accumulation (text, usage, tool_calls, thinking)
- ManagedAgentLoop: VFS-native reasoning loop with mock kernel syscalls
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

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

    def test_thinking_accumulation(self) -> None:
        obs = AgentObserver()
        obs.reset_turn()

        obs.observe_update("thinking", {"content": "Let me "})
        obs.observe_update("thinking", {"content": "analyze..."})

        result = obs.finish_turn()
        assert result.thinking == "Let me analyze..."

    def test_thinking_none_when_empty(self) -> None:
        obs = AgentObserver()
        obs.reset_turn()
        obs.observe_update("agent_message_chunk", {"content": {"type": "text", "text": "hi"}})
        result = obs.finish_turn()
        assert result.thinking is None


# =============================================================================
# ManagedAgentLoop tests — everything-is-a-file
# =============================================================================


def _make_vfs_loop(
    frames: list[dict] | None = None,
    system_prompt: str = "",
    tools_json: str = "[]",
) -> tuple[Any, dict[str, Any]]:
    """Create ManagedAgentLoop with mocked VFS syscalls + LLM backend."""
    from nexus.services.agent_runtime.managed_loop import ManagedAgentLoop

    # Mock sys_read: return different content based on path
    read_store: dict[str, bytes] = {}
    write_store: dict[str, bytes] = {}

    def mock_sys_read(path: str) -> bytes:
        if path.endswith("/SYSTEM.md"):
            return system_prompt.encode()
        if path.endswith("/tools.json"):
            return tools_json.encode()
        if path in read_store:
            return read_store[path]
        raise FileNotFoundError(path)

    def mock_sys_write(path: str, data: bytes) -> None:
        write_store[path] = data

    # Default CC-format frames from LLM
    default_frames = frames or [
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": " there!"},
        {
            "type": "usage",
            "usage": {"total_tokens": 10, "prompt_tokens": 5, "completion_tokens": 5},
        },
        {"type": "stop", "stop_reason": "stop"},
    ]

    # Mock LLM backend with generate_streaming returning CC-format frames
    llm_backend = MagicMock()
    llm_backend.generate_streaming = MagicMock(return_value=iter(default_frames))

    sys_read_mock = MagicMock(side_effect=mock_sys_read)
    sys_write_mock = MagicMock(side_effect=mock_sys_write)

    loop = ManagedAgentLoop(
        sys_read=sys_read_mock,
        sys_write=sys_write_mock,
        llm_backend=llm_backend,
        agent_path="/zone/agents/test-agent",
        llm_path="/zone/llm/openai",
        conv_path="/zone/agents/test-agent/conversation",
        proc_path="/zone/proc/pid-123",
        model="gpt-4o",
    )

    mocks: dict[str, Any] = {
        "sys_read": sys_read_mock,
        "sys_write": sys_write_mock,
        "llm_backend": llm_backend,
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
    async def test_run_calls_llm_generate_streaming(self) -> None:
        """LLM call goes through generate_streaming() directly."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        await loop.run("Hi")

        # generate_streaming was called with request containing messages
        mocks["llm_backend"].generate_streaming.assert_called_once()
        call_args = mocks["llm_backend"].generate_streaming.call_args
        request = call_args[0][0]
        assert "messages" in request

    @pytest.mark.asyncio()
    async def test_run_iterates_cc_frames(self) -> None:
        """Tokens assembled from CC-format text frames."""
        loop, mocks = _make_vfs_loop()
        await loop.initialize()

        result = await loop.run("Hi")

        assert result.text == "Hello there!"

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

    @pytest.mark.asyncio()
    async def test_thinking_frames_accumulated(self) -> None:
        """Thinking frames from LLM are accumulated in observer."""
        frames = [
            {"type": "thinking", "thinking": "Let me think..."},
            {"type": "text", "text": "The answer is 42."},
            {"type": "usage", "usage": {"total_tokens": 20}},
            {"type": "stop", "stop_reason": "stop"},
        ]
        loop, mocks = _make_vfs_loop(frames=frames)
        await loop.initialize()

        result = await loop.run("What is the meaning of life?")
        assert result.text == "The answer is 42."
        assert result.thinking == "Let me think..."

    @pytest.mark.asyncio()
    async def test_error_frame_raises(self) -> None:
        """Error frames from LLM raise BackendError after retries exhausted."""
        from nexus.contracts.exceptions import BackendError

        frames = [
            {"type": "text", "text": "partial"},
            {"type": "error", "message": "unauthorized: invalid api key"},
        ]
        loop, mocks = _make_vfs_loop(frames=frames)
        await loop.initialize()

        # "unauthorized" triggers immediate failure (no retry)
        with pytest.raises(BackendError, match="invalid api key"):
            await loop.run("test")
