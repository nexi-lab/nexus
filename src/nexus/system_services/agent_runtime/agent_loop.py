"""agent_loop — agent execution loop (Issue #2761).

The core reasoning cycle that drives agent behavior:
    receive message → think (LLM) → call tools → respond → repeat

Maps to a process's main() function. The loop runs until:
    1. The LLM responds without tool calls (natural completion)
    2. max_turns is exceeded (MaxTurnsExceededError)
    3. The process is terminated (cancellation)

Context trimming: When the conversation exceeds max_context_tokens,
the sliding_window strategy drops oldest messages (preserving system prompt).
"""

import asyncio
import json
import logging
from typing import Any

from nexus.contracts.agent_runtime_types import (
    AgentLoopConfig,
    AgentProcess,
    MaxTurnsExceededError,
    ToolResult,
)

logger = logging.getLogger(__name__)


async def agent_loop(
    *,
    process: AgentProcess,
    dispatcher: Any,
    session_store: Any,
    llm_client: Any,
    config: AgentLoopConfig,
    initial_message: str,
) -> str | None:
    """Execute the agent reasoning loop.

    Args:
        process: The agent process descriptor.
        dispatcher: ToolDispatcher for routing tool calls.
        session_store: SessionStore for checkpointing.
        llm_client: LLM client with a chat() method.
        config: Loop configuration.
        initial_message: The initial user message to process.

    Returns:
        The final assistant response text, or None if terminated.

    Raises:
        MaxTurnsExceededError: If max_turns is exceeded.
    """
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_message},
    ]
    turn_count = 0

    while True:
        # Trim context if needed
        _trim_context(messages, config.max_context_tokens)

        # Call LLM
        response = await llm_client.chat(messages)

        # Check for tool calls
        if not response.tool_calls:
            # Natural completion — LLM responded without tools
            final_text: str | None = response.content
            messages.append({"role": "assistant", "content": final_text})

            # Checkpoint on clean exit
            await session_store.checkpoint(
                process.pid,
                {"messages": messages, "turn_count": turn_count},
                agent_id=process.agent_id,
            )
            return final_text

        turn_count += 1
        if turn_count > config.max_turns:
            raise MaxTurnsExceededError(config.max_turns, process.agent_id)

        # Dispatch tool calls
        tool_calls = response.tool_calls
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        if config.parallel_tool_dispatch and len(tool_calls) > 1:
            # Parallel dispatch
            results = await asyncio.gather(
                *(_dispatch_tool(dispatcher, tc, process, config.tool_timeout) for tc in tool_calls)
            )
        else:
            # Sequential dispatch
            results = []
            for tc in tool_calls:
                result = await _dispatch_tool(dispatcher, tc, process, config.tool_timeout)
                results.append(result)

        # Add tool results to conversation
        for tc, result in zip(tool_calls, results, strict=True):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result.output) if result.success else f"Error: {result.error}",
                }
            )


async def _dispatch_tool(
    dispatcher: Any,
    tool_call: Any,
    process: AgentProcess,
    timeout: float,
) -> ToolResult:
    """Dispatch a single tool call via the dispatcher with timeout enforcement."""
    try:
        arguments = (
            json.loads(tool_call.function.arguments)
            if isinstance(tool_call.function.arguments, str)
            else tool_call.function.arguments
        )
    except (json.JSONDecodeError, TypeError):
        arguments = {}

    result: ToolResult = await asyncio.wait_for(
        dispatcher.dispatch(
            tool_call.function.name,
            arguments,
            agent_id=process.agent_id,
            zone_id=process.zone_id,
            tool_call_id=tool_call.id,
        ),
        timeout=timeout,
    )
    return result


def _trim_context(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> None:
    """Trim conversation context using sliding window strategy.

    Drops oldest messages (preserving the first user message) when
    the estimated token count exceeds max_tokens.

    Rough estimate: 1 token ≈ 4 characters.
    """
    chars_per_token = 4
    max_chars = max_tokens * chars_per_token

    total_chars = sum(
        len(str(m.get("content", ""))) + len(str(m.get("tool_calls", ""))) for m in messages
    )

    while total_chars > max_chars and len(messages) > 1:
        # Remove the second message (preserve first = system/user prompt)
        removed = messages.pop(1)
        total_chars -= len(str(removed.get("content", ""))) + len(
            str(removed.get("tool_calls", ""))
        )
