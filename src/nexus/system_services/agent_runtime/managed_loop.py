"""ManagedAgentLoop — kernel-managed LLM reasoning loop.

1st-party agent: the kernel drives the LLM call → tool execution loop,
with full visibility into every I/O operation. Reuses AgentObserver for
notification accumulation (shared with 3rd-party AcpConnection).

Flow:
    1. Assemble messages → call LLM via OpenAICompatibleBackend
    2. Parse response: tool_calls? → execute via VFS → append result → goto 1
                       text? → return to caller
    3. Each step emits ACP-compatible observations for monitoring/audit.

DI dependencies:
    - backend: OpenAICompatibleBackend (LLM compute + CAS)
    - fs_read / fs_write: VFS syscall callables (for tool execution)
    - system_prompt: optional system prompt text
    - tools: tool definitions for function calling

References:
    - Task #1510: AgentService (Tier 1)
    - Task #1589: LLM backend driver design
    - system_services/acp/connection.py — AcpConnection pattern
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from nexus.system_services.agent_runtime.observer import AgentObserver, AgentTurnResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    class _LLMBackendProto:
        """Structural typing for LLM backend (avoids hard dep on PR #3158)."""

        def generate_streaming(
            self, request: dict[str, Any]
        ) -> "Iterator[tuple[str, dict[str, Any] | None]]": ...


logger = logging.getLogger(__name__)

# Type aliases matching AcpConnection's VFS I/O pattern.
FsReadFn = Callable[[str], Awaitable[str]]
FsWriteFn = Callable[[str, str], Awaitable[None]]

# Maximum reasoning turns before forced stop (prevent infinite loops).
_MAX_TURNS = 50


class ManagedAgentLoop:
    """Kernel-managed LLM reasoning loop with full I/O visibility.

    Unlike AcpConnection (which passively observes a 3rd-party agent's
    internal loop), ManagedAgentLoop actively drives:

    1. LLM calls via ``OpenAICompatibleBackend.generate_streaming()``
    2. Tool execution via VFS syscalls (``fs_read`` / ``fs_write``)
    3. Message history management (append-only conversation)

    Shares ``AgentObserver`` with AcpConnection for identical
    notification handling — external consumers see the same format.
    """

    def __init__(
        self,
        *,
        backend: "_LLMBackendProto",
        fs_read: FsReadFn | None = None,
        fs_write: FsWriteFn | None = None,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_turns: int = _MAX_TURNS,
    ) -> None:
        self._backend = backend
        self._fs_read = fs_read
        self._fs_write = fs_write
        self._system_prompt = system_prompt
        self._tools = tools
        self._model = model
        self._max_turns = max_turns

        # Shared observer (same logic as AcpConnection)
        self._observer = AgentObserver()

        # Conversation state (append-only)
        self._messages: list[dict[str, Any]] = []
        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

    @property
    def observer(self) -> AgentObserver:
        """Access the observer for external monitoring."""
        return self._observer

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Current conversation history (read-only view)."""
        return list(self._messages)

    async def run(self, prompt: str) -> AgentTurnResult:
        """Run the reasoning loop for a single user prompt.

        Sends the prompt to the LLM, executes tool calls if any,
        and loops until the LLM returns a text response or max_turns
        is reached.

        Args:
            prompt: User's input text.

        Returns:
            AgentTurnResult with accumulated text, usage, tool calls.
        """
        self._messages.append({"role": "user", "content": prompt})
        self._observer.reset_turn()

        turns = 0
        while turns < self._max_turns:
            turns += 1

            # Build LLM request
            request = self._build_request()

            # Call LLM (sync generator in thread would be needed for
            # streaming; for MVP we collect all tokens synchronously)
            response_text, tool_calls, meta = self._call_llm(request)

            # Emit observations
            if response_text:
                self._observer.observe_update(
                    "agent_message_chunk",
                    {"content": {"type": "text", "text": response_text}},
                )
            if meta:
                self._observer.observe_update("usage_update", {"usage": meta.get("usage", {})})
                self._observer.model_name = meta.get("model")

            # Append assistant message to conversation
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": response_text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self._messages.append(assistant_msg)

            # If no tool calls → done
            if not tool_calls:
                return self._observer.finish_turn(stop_reason="stop")

            # Execute tool calls and append results
            for tc in tool_calls:
                self._observer.observe_update("tool_call", tc)
                result = await self._execute_tool(tc)
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    }
                )

        # Max turns reached
        logger.warning(
            "ManagedAgentLoop: max turns (%d) reached, forcing stop",
            self._max_turns,
        )
        return self._observer.finish_turn(stop_reason="max_turns")

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _build_request(self) -> dict[str, Any]:
        """Build LLM request from current conversation state."""
        request: dict[str, Any] = {"messages": self._messages}
        if self._model:
            request["model"] = self._model
        if self._tools:
            request["tools"] = self._tools
        return request

    def _call_llm(
        self, request: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """Call LLM via backend and parse response.

        Returns:
            (response_text, tool_calls, metadata)
        """
        tokens: list[str] = []
        meta: dict[str, Any] | None = None

        for token, token_meta in self._backend.generate_streaming(request):
            if token:
                tokens.append(token)
            if token_meta is not None:
                meta = token_meta

        response_text = "".join(tokens)

        # Parse tool calls from the raw response text.
        # In streaming mode, OpenAI returns tool_calls in the completion
        # metadata, not in the text. For MVP, we check if the backend
        # returned structured tool_calls via a different mechanism.
        # TODO: Extract tool_calls from streaming chunks when OpenAI SDK
        # supports it in streaming mode.
        tool_calls: list[dict[str, Any]] = []

        return response_text, tool_calls, meta

    # ------------------------------------------------------------------
    # Tool execution via VFS
    # ------------------------------------------------------------------

    async def _execute_tool(self, tool_call: dict[str, Any]) -> str:
        """Execute a tool call via VFS syscalls.

        Routes tool calls to VFS-backed file I/O callables, matching
        the same pattern used by AcpConnection._handle_fs_request().

        Args:
            tool_call: Tool call dict with ``id``, ``function.name``,
                ``function.arguments``.

        Returns:
            Tool execution result as a string (JSON for structured data).
        """
        func = tool_call.get("function", {})
        name = func.get("name", "")
        try:
            args = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": f"Invalid arguments for tool {name}"})

        try:
            if name == "read_file" and self._fs_read:
                path = args.get("path", "")
                content = await self._fs_read(path)
                return content

            elif name == "write_file" and self._fs_write:
                path = args.get("path", "")
                content = args.get("content", "")
                await self._fs_write(path, content)
                return json.dumps({"status": "ok", "path": path})

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            logger.error("Tool execution failed: %s(%s): %s", name, args, exc)
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset conversation state for a new session."""
        self._messages.clear()
        if self._system_prompt:
            self._messages.append({"role": "system", "content": self._system_prompt})
        self._observer = AgentObserver()

    @property
    def session_id(self) -> str:
        """Generate a unique session identifier."""
        return str(uuid.uuid4())
