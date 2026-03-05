"""Agent loop — the core execution cycle.

Provides ``agent_step()`` (single turn) and ``agent_loop()`` (multi-turn
wrapper).  ``agent_step()`` is the yield point that lets a scheduler
drain the inbox, honor signals, and do fair scheduling between turns.

Uses LLMProviderProtocol.complete_async() for tool-call turns.
Emits AgentEvent callbacks for streaming to the API layer.

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §7.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.agent_runtime_types import StepAction, StepResult
from nexus.contracts.llm_types import Message, MessageRole, ToolCall, ToolFunction
from nexus.system_services.agent_runtime.types import (
    Completed,
    Error,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
)

# Message passed to the LLM; the provider's _format_messages() handles
# serialisation (vision flags, tool-call flags, etc.), so we must hand
# it real Message objects — NOT pre-serialised dicts.

if TYPE_CHECKING:
    from nexus.contracts.protocols.llm_provider import LLMProviderProtocol
    from nexus.contracts.types import OperationContext
    from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher
    from nexus.system_services.agent_runtime.types import (
        AgentContext,
        AgentEvent,
        AgentProcessConfig,
    )

logger = logging.getLogger(__name__)

# Tools that are safe to run concurrently (no filesystem mutations)
_READ_ONLY_TOOLS = frozenset({"read_file", "grep", "glob", "list_dir"})


# ======================================================================
# agent_step — single turn
# ======================================================================


async def agent_step(
    llm: LLMProviderProtocol,
    dispatcher: ToolDispatcher,
    context: AgentContext,
    config: AgentProcessConfig,
    ctx: OperationContext,
    messages: list[Message],
    turn: int,
    *,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    on_checkpoint: Callable[[list[Message]], Awaitable[None]] | None = None,
    cwd: str | None = None,
    sandbox_id: str | None = None,
) -> StepResult:
    """Execute ONE turn of the agent loop.

    Returns StepResult indicating whether to continue, stop, or report error.
    The caller (agent_loop or scheduler) decides what to do next.

    Args:
        llm: LLM provider for inference.
        dispatcher: Tool call router (maps tool names to VFS/sandbox).
        context: AgentContext with system prompt and tool schemas.
        config: Process config with limits (max_turns, etc.).
        ctx: OperationContext for VFS permission checks.
        messages: Current conversation messages (mutable, will be appended to).
        turn: Current turn number (0-based).
        on_event: Optional async callback for streaming events.
        on_checkpoint: Optional async callback to save messages after tool dispatch.
        cwd: Agent's current working directory for path resolution.
        sandbox_id: Sandbox ID for bash/python tool execution.

    Returns:
        StepResult with action, updated messages tuple, and turn number.
    """
    tools = list(context.tools)
    system_msg = Message(role=MessageRole.SYSTEM, content=context.system_prompt)

    # Trim context window if over budget
    llm_messages = _trim_to_budget(llm, system_msg, messages, config.max_context_tokens)

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
        return StepResult(
            action=StepAction.ERROR,
            messages=tuple(messages),
            turn=turn,
            error=error_msg,
        )

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

        # Dispatch tools (parallel for read-only batches, sequential otherwise)
        results = await _dispatch_tools(
            dispatcher,
            ctx,
            tool_calls,
            cwd=cwd,
            sandbox_id=sandbox_id,
            sandbox_timeout=config.sandbox_timeout,
        )

        # Emit events and build tool result messages in original order
        for tc, result in zip(tool_calls, results, strict=True):
            if on_event:
                await on_event(ToolCallStart(tool_call=tc))
                await on_event(ToolCallResult(tool_call=tc, result=result))

            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    name=tc.function.name,
                    content=result,
                    tool_call_id=tc.id,
                )
            )

        # Checkpoint after each tool dispatch round for crash recovery
        if on_checkpoint:
            await on_checkpoint(messages)

        return StepResult(
            action=StepAction.CONTINUE,
            messages=tuple(messages),
            turn=turn + 1,
        )

    # No tool calls -> final response
    if content:
        messages.append(Message(role=MessageRole.ASSISTANT, content=content))
        if on_event:
            await on_event(TextDelta(text=content))

    if on_event:
        await on_event(Completed(message_count=len(messages)))

    return StepResult(
        action=StepAction.DONE,
        messages=tuple(messages),
        turn=turn,
    )


# ======================================================================
# agent_loop — backward-compatible multi-turn wrapper
# ======================================================================


async def agent_loop(
    llm: LLMProviderProtocol,
    dispatcher: ToolDispatcher,
    context: AgentContext,
    config: AgentProcessConfig,
    ctx: OperationContext,
    *,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    on_checkpoint: Callable[[list[Message]], Awaitable[None]] | None = None,
    cwd: str | None = None,
    sandbox_id: str | None = None,
) -> list[Message]:
    """Run the agent loop until LLM produces a final response or limits hit.

    Backward-compatible wrapper around ``agent_step()``.  All existing
    callers (ProcessManager.resume(), tests, README examples) keep working.

    Args:
        llm: LLM provider for inference.
        dispatcher: Tool call router (maps tool names to VFS/sandbox).
        context: AgentContext with system prompt, messages, and tool schemas.
        config: Process config with limits (max_turns, etc.).
        ctx: OperationContext for VFS permission checks.
        on_event: Optional async callback for streaming events.
        on_checkpoint: Optional async callback to save messages after each tool
            dispatch round. Enables crash recovery mid-conversation.
        cwd: Agent's current working directory for path resolution.
        sandbox_id: Sandbox ID for bash/python tool execution.

    Returns:
        Updated message list (includes all new assistant + tool messages).
    """
    messages = list(context.messages)
    turn = 0

    while turn < config.max_turns:
        result = await agent_step(
            llm,
            dispatcher,
            context,
            config,
            ctx,
            messages,
            turn,
            on_event=on_event,
            on_checkpoint=on_checkpoint,
            cwd=cwd,
            sandbox_id=sandbox_id,
        )
        messages = list(result.messages)
        if result.action != StepAction.CONTINUE:
            break
        turn = result.turn

    return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trim_to_budget(
    llm: LLMProviderProtocol,
    system_msg: Message,
    messages: list[Message],
    max_tokens: int,
) -> list[Message]:
    """Drop oldest messages if the conversation exceeds the token budget.

    Keeps system message + as many recent messages as fit within *max_tokens*.
    Returns the trimmed list ready to send to the LLM.
    """
    full = [system_msg, *messages]
    try:
        total = llm.count_tokens(full)
    except Exception:
        return full  # can't count → send everything

    if total <= max_tokens:
        return full

    # Binary search for the earliest start index that fits within budget.
    # Avoids O(n²) list.pop(0) and minimises count_tokens() calls to O(log n).
    lo, hi = 0, len(messages) - 1  # keep at least the last message
    best = hi  # worst case: only last message
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = [system_msg, *messages[mid:]]
        try:
            if llm.count_tokens(candidate) <= max_tokens:
                best = mid
                hi = mid - 1  # try keeping more messages
            else:
                lo = mid + 1  # need to drop more
        except Exception:
            return candidate
    return [system_msg, *messages[best:]]


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


async def _dispatch_tools(
    dispatcher: ToolDispatcher,
    ctx: OperationContext,
    tool_calls: list[ToolCall],
    **kwargs: Any,
) -> list[str]:
    """Dispatch tool calls — parallel for read-only batches, sequential otherwise."""
    all_readonly = all(tc.function.name in _READ_ONLY_TOOLS for tc in tool_calls)
    results: list[str] = [""] * len(tool_calls)

    if all_readonly and len(tool_calls) > 1:
        async with asyncio.TaskGroup() as tg:
            for i, tc in enumerate(tool_calls):

                async def _run(idx: int = i, tool: ToolCall = tc) -> None:
                    results[idx] = await dispatcher.dispatch(ctx, tool, **kwargs)

                tg.create_task(_run())
    else:
        for i, tc in enumerate(tool_calls):
            results[i] = await dispatcher.dispatch(ctx, tc, **kwargs)

    return results


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
