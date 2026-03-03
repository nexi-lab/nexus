"""Agent loop — the core execution cycle (~100 LOC).

Sends messages+tools to LLM, dispatches tool calls, repeats until
no more tool calls or turn limit reached.

Uses LLMProviderProtocol.complete_async() for tool-call turns.
Emits AgentEvent callbacks for streaming to the API layer.

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §7.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nexus.contracts.agent_process import (
    Completed,
    Error,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
)
from nexus.contracts.llm_types import Message, MessageRole, ToolCall, ToolFunction

if TYPE_CHECKING:
    from nexus.contracts.agent_process import (
        AgentContext,
        AgentEvent,
        AgentProcessConfig,
    )
    from nexus.contracts.types import OperationContext
    from nexus.contracts.protocols.llm_provider import LLMProviderProtocol
    from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


async def agent_loop(
    llm: LLMProviderProtocol,
    dispatcher: ToolDispatcher,
    context: AgentContext,
    config: AgentProcessConfig,
    ctx: OperationContext,
    *,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    cwd: str | None = None,
    sandbox_id: str | None = None,
) -> list[Message]:
    """Run the agent loop until LLM produces a final response or limits hit.

    Args:
        llm: LLM provider for inference.
        dispatcher: Tool call router (maps tool names to VFS/sandbox).
        context: AgentContext with system prompt, messages, and tool schemas.
        config: Process config with limits (max_turns, etc.).
        ctx: OperationContext for VFS permission checks.
        on_event: Optional async callback for streaming events.
        cwd: Agent's current working directory for path resolution.
        sandbox_id: Sandbox ID for bash/python tool execution.

    Returns:
        Updated message list (includes all new assistant + tool messages).
    """
    messages = list(context.messages)
    tools = list(context.tools)
    turn = 0

    while turn < config.max_turns:
        # Prepare messages for LLM (convert to dicts)
        llm_messages = _serialize_messages(context.system_prompt, messages)

        # Call LLM with tools
        try:
            response = await llm.complete_async(
                llm_messages,
                tools=tools if tools else None,
            )
        except Exception as exc:
            error_msg = f"LLM call failed: {exc}"
            logger.error(error_msg)
            if on_event:
                await on_event(Error(error=error_msg))
            break

        # Extract tool calls and content from response
        tool_calls = _extract_tool_calls(response)
        content = _extract_content(response)

        if tool_calls:
            # Append assistant message with tool calls
            assistant_msg = Message(
                role=MessageRole.ASSISTANT,
                content=content,
                tool_calls=tool_calls,
            )
            messages.append(assistant_msg)

            # Execute each tool call
            for tc in tool_calls:
                if on_event:
                    await on_event(ToolCallStart(tool_call=tc))

                result = await dispatcher.dispatch(
                    ctx,
                    tc,
                    cwd=cwd,
                    sandbox_id=sandbox_id,
                    sandbox_timeout=config.sandbox_timeout,
                )

                if on_event:
                    await on_event(ToolCallResult(tool_call=tc, result=result))

                # Add tool result message
                messages.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=result,
                        tool_call_id=tc.id,
                    )
                )

            turn += 1
            continue

        # No tool calls -> final response
        if content:
            messages.append(Message(role=MessageRole.ASSISTANT, content=content))
            if on_event:
                await on_event(TextDelta(text=content))

        if on_event:
            await on_event(Completed(message_count=len(messages)))
        break

    return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_messages(system_prompt: str, messages: list[Message]) -> list[dict]:
    """Convert system prompt + messages to LLM-compatible dicts."""
    result: list[dict] = [{"role": "system", "content": system_prompt}]

    for msg in messages:
        d = msg.model_dump()
        # Ensure tool_calls are serialized for function-calling
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        result.append(d)

    return result


def _extract_tool_calls(response: object) -> list[ToolCall]:
    """Extract ToolCall objects from an LLM response (various formats)."""
    # Handle dict response
    if isinstance(response, dict):
        raw_calls = response.get("tool_calls") or []
        return [_parse_tool_call(tc) for tc in raw_calls]

    # Handle object with tool_calls attribute
    raw = getattr(response, "tool_calls", None)
    if raw:
        return [_parse_tool_call(tc) for tc in raw]

    # Handle object with choices (OpenAI-style)
    choices = getattr(response, "choices", None)
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg:
            raw = getattr(msg, "tool_calls", None)
            if raw:
                return [_parse_tool_call(tc) for tc in raw]

    return []


def _parse_tool_call(tc: object) -> ToolCall:
    """Parse a single tool call from various formats into ToolCall."""
    if isinstance(tc, ToolCall):
        return tc

    if isinstance(tc, dict):
        fn = tc.get("function", {})
        return ToolCall(
            id=tc.get("id", ""),
            function=ToolFunction(
                name=fn.get("name", ""),
                arguments=fn.get("arguments", "{}"),
            ),
        )

    # Object with attributes
    fn = getattr(tc, "function", None)
    if fn:
        return ToolCall(
            id=getattr(tc, "id", ""),
            function=ToolFunction(
                name=getattr(fn, "name", ""),
                arguments=getattr(fn, "arguments", "{}"),
            ),
        )

    # Last resort: try JSON
    try:
        d = json.loads(str(tc))
        return _parse_tool_call(d)
    except (json.JSONDecodeError, TypeError):
        return ToolCall(
            id="unknown",
            function=ToolFunction(name="unknown", arguments="{}"),
        )


def _extract_content(response: object) -> str | None:
    """Extract text content from an LLM response."""
    if isinstance(response, dict):
        return response.get("content")

    # Direct content attribute
    content = getattr(response, "content", None)
    if content is not None:
        return str(content) if not isinstance(content, str) else content

    # OpenAI-style choices
    choices = getattr(response, "choices", None)
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg:
            return getattr(msg, "content", None)

    return None
