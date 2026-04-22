"""AgentObserver — shared notification handling for agent observation.

Extracted from AcpConnection so both 3rd-party agents (AcpConnection)
and 1st-party agents (ManagedAgentLoop) share the same accumulation
logic for text chunks, usage tracking, and tool call counting.

External consumers (monitoring, audit, UI) see identical notification
formats regardless of whether the agent is a 3rd-party CLI subprocess
or a kernel-managed reasoning loop.

References:
    - Task #1510: AgentService (Tier 1)
    - system_services/acp/connection.py — AcpConnection (first consumer)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentTurnResult:
    """Result from a single agent turn (prompt → response)."""

    text: str = ""
    stop_reason: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    num_turns: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking: str | None = None


class AgentObserver:
    """Shared notification handling for agent observation.

    Accumulates text chunks, usage metrics, and tool call counts from
    ACP-compatible notifications. Used by both:

    - **AcpConnection** (3rd-party): IPC reader calls ``observe_update()``
      when notifications arrive from the agent subprocess.
    - **ManagedAgentLoop** (1st-party): the reasoning loop calls
      ``observe_update()`` as it produces tokens and executes tools.

    Thread safety: not thread-safe. Designed for single-task usage
    within an async context (one observer per agent session).
    """

    def __init__(self, on_update: Any | None = None) -> None:
        self._accumulated_text: list[str] = []
        self._accumulated_thinking: list[str] = []
        self._accumulated_usage: dict[str, Any] = {}
        self._num_turns: int = 0
        self._model_name: str | None = None
        self._tool_calls: list[dict[str, Any]] = []
        self._prompt_active: bool = False
        self._on_update = on_update  # Push-mode callback for ACP streaming

    def reset_turn(self) -> None:
        """Reset per-turn accumulators. Call before each prompt."""
        self._accumulated_text.clear()
        self._accumulated_thinking.clear()
        self._tool_calls.clear()
        self._prompt_active = True

    def finish_turn(self, stop_reason: str | None = None) -> AgentTurnResult:
        """Finalize the current turn and return accumulated result."""
        self._prompt_active = False
        text = "".join(self._accumulated_text)
        thinking = "".join(self._accumulated_thinking) if self._accumulated_thinking else None
        model = self._accumulated_usage.pop("model", None) or self._model_name
        return AgentTurnResult(
            text=text,
            stop_reason=stop_reason,
            model=model,
            usage=dict(self._accumulated_usage),
            num_turns=self._num_turns,
            tool_calls=list(self._tool_calls),
            thinking=thinking,
        )

    def observe_update(self, update_type: str, update: dict[str, Any]) -> None:
        """Process a single ACP-compatible session/update notification.

        Args:
            update_type: One of ``agent_message_chunk``, ``usage_update``,
                ``tool_call``, ``thinking``, ``user_message_chunk``.
            update: The notification payload.
        """
        # Push to ACP transport if callback set
        if self._on_update:
            self._on_update(update_type, update)

        if update_type == "agent_message_chunk":
            if self._prompt_active:
                content = update.get("content", {})
                if content.get("type") == "text":
                    self._accumulated_text.append(content.get("text", ""))

        elif update_type == "usage_update":
            usage = update.get("usage", {})
            for key, val in usage.items():
                if isinstance(val, int | float):
                    self._accumulated_usage[key] = self._accumulated_usage.get(key, 0) + val
                else:
                    self._accumulated_usage[key] = val

        elif update_type == "thinking":
            if self._prompt_active:
                content = update.get("content", "")
                self._accumulated_thinking.append(content)

        elif update_type == "tool_call":
            self._num_turns += 1
            self._tool_calls.append(update)

        elif update_type == "user_message_chunk":
            # During active prompt, a user_message_chunk means history
            # replay — clear text so only model response survives.
            if self._prompt_active:
                self._accumulated_text.clear()

        else:
            logger.debug("AgentObserver: unknown update type=%s", update_type)

    @property
    def collected_text(self) -> str:
        """Current accumulated text (may be partial during streaming)."""
        return "".join(self._accumulated_text)

    @property
    def num_turns(self) -> int:
        """Number of tool_call turns observed."""
        return self._num_turns

    @property
    def model_name(self) -> str | None:
        return self._model_name

    @model_name.setter
    def model_name(self, value: str | None) -> None:
        self._model_name = value
