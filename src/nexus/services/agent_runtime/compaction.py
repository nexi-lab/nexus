"""Context compaction for ManagedAgentLoop (nexus-agent-plan §4.1).

Three-layer compaction strategy matching Claude Code's context management:

    Layer 1 — micro_compact (every turn, sync, no LLM call):
        Older tool results (beyond last 3) with content > 100 chars → [cleared].
        In-place mutation, ~0μs.

    Layer 2 — auto_compact (token threshold trigger, async, LLM call):
        Save full transcript to VFS, LLM summarizes, replace messages with summary.
        Triggered when estimated tokens > threshold.

    Layer 3 — manual compact (/compact slash command or model tool call):
        Same logic as auto_compact but user/model triggered.

Pluggable via CompactionStrategy protocol. Default implementation is
CC-compatible (DefaultCompactionStrategy).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Type aliases for injected kernel callables
SysWriteFn = Callable[[str, bytes], Awaitable[Any]]
LLMCallFn = Callable[[list[dict[str, Any]]], Awaitable[str]]

# Defaults
_DEFAULT_TOKEN_THRESHOLD = 100_000
_MICRO_COMPACT_KEEP_RECENT = 3
_MICRO_COMPACT_MIN_LENGTH = 100
_AUTO_COMPACT_SUMMARY_CHARS = 80_000  # last N chars sent to LLM for summary
_AUTO_COMPACT_MAX_SUMMARY_TOKENS = 2000


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: ~4 chars per token (English average)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(str(part.get("text", "")))
        # tool_calls contribute to token count
        if "tool_calls" in msg:
            total += len(json.dumps(msg["tool_calls"]))
    return total // 4


@runtime_checkable
class CompactionStrategy(Protocol):
    """Pluggable compaction strategy for ManagedAgentLoop."""

    def micro_compact(self, messages: list[dict[str, Any]]) -> None:
        """Layer 1: in-place mutation, every turn, no LLM call."""
        ...

    def should_auto_compact(self, messages: list[dict[str, Any]]) -> bool:
        """Check if auto_compact should trigger."""
        ...

    async def auto_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Layer 2: LLM-assisted summarization, returns new message list."""
        ...


class DefaultCompactionStrategy:
    """CC-compatible three-layer compaction.

    DI dependencies (no god-object access):
        sys_write: for saving transcripts to VFS
        llm_call: for generating summaries (Layer 2)
        agent_path: VFS base path for transcript storage
        token_threshold: trigger point for auto_compact
    """

    def __init__(
        self,
        *,
        sys_write: SysWriteFn | None = None,
        llm_call: LLMCallFn | None = None,
        agent_path: str = "",
        token_threshold: int = _DEFAULT_TOKEN_THRESHOLD,
    ) -> None:
        self._sys_write = sys_write
        self._llm_call = llm_call
        self._agent_path = agent_path
        self._token_threshold = token_threshold

    # ------------------------------------------------------------------
    # Layer 1 — micro_compact (every turn, sync)
    # ------------------------------------------------------------------

    def micro_compact(self, messages: list[dict[str, Any]]) -> None:
        """Clear old tool results beyond the last 3, in-place.

        Scan for role="tool" entries. Keep the last 3 at full fidelity.
        Older ones with content > 100 chars → replace with "[cleared]".
        """
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]

        if len(tool_indices) <= _MICRO_COMPACT_KEEP_RECENT:
            return

        old_indices = tool_indices[:-_MICRO_COMPACT_KEEP_RECENT]
        for i in old_indices:
            content = messages[i].get("content", "")
            if isinstance(content, str) and len(content) > _MICRO_COMPACT_MIN_LENGTH:
                messages[i] = {**messages[i], "content": "[cleared]"}

    # ------------------------------------------------------------------
    # Layer 2 — auto_compact (token threshold, async)
    # ------------------------------------------------------------------

    def should_auto_compact(self, messages: list[dict[str, Any]]) -> bool:
        """Check if estimated token count exceeds threshold."""
        return estimate_tokens(messages) > self._token_threshold

    async def auto_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Save transcript, summarize via LLM, return compressed messages.

        1. Save full transcript to VFS
        2. Build summary prompt from last ~80K chars
        3. Call LLM to generate summary (max 2000 tokens)
        4. Return [system_msg_if_any, compressed_summary_msg]
        """
        # Save transcript to VFS
        await self._save_transcript(messages)

        # Build summary
        summary = await self._generate_summary(messages)

        # Reconstruct: keep system message (if any) + compressed summary
        result: list[dict[str, Any]] = []

        # Preserve system message
        if messages and messages[0].get("role") == "system":
            result.append(messages[0])

        # Add compressed summary as user message with boundary marker
        result.append(
            {
                "role": "user",
                "content": (
                    "[Previous conversation compressed]\n\n"
                    f"{summary}\n\n"
                    "[Conversation continues below]"
                ),
            }
        )

        logger.info(
            "auto_compact: %d messages → %d (saved transcript, %d tokens → ~%d)",
            len(messages),
            len(result),
            estimate_tokens(messages),
            estimate_tokens(result),
        )

        return result

    async def _save_transcript(self, messages: list[dict[str, Any]]) -> None:
        """Save full conversation transcript to VFS for audit trail."""
        if self._sys_write is None or not self._agent_path:
            return

        timestamp = str(int(time.time()))
        path = f"{self._agent_path}/transcripts/{timestamp}.jsonl"
        lines = [json.dumps(m, separators=(",", ":")) for m in messages]
        content = "\n".join(lines).encode("utf-8")

        try:
            await self._sys_write(path, content)
            logger.debug("Transcript saved: %s (%d messages)", path, len(messages))
        except Exception as exc:
            logger.warning("Failed to save transcript: %s", exc)

    async def _generate_summary(self, messages: list[dict[str, Any]]) -> str:
        """Generate conversation summary via LLM call."""
        if self._llm_call is None:
            return self._fallback_summary(messages)

        # Build context: last ~80K chars of conversation
        conversation_text = self._messages_to_text(messages)
        if len(conversation_text) > _AUTO_COMPACT_SUMMARY_CHARS:
            conversation_text = conversation_text[-_AUTO_COMPACT_SUMMARY_CHARS:]

        summary_messages = [
            {
                "role": "user",
                "content": (
                    "Summarize this conversation concisely. Focus on: what was accomplished, "
                    "key decisions made, current state of work, and any pending tasks. "
                    f"Keep under {_AUTO_COMPACT_MAX_SUMMARY_TOKENS} tokens.\n\n"
                    f"Conversation:\n{conversation_text}"
                ),
            }
        ]

        try:
            return await self._llm_call(summary_messages)
        except Exception as exc:
            logger.warning("LLM summary failed, using fallback: %s", exc)
            return self._fallback_summary(messages)

    @staticmethod
    def _fallback_summary(messages: list[dict[str, Any]]) -> str:
        """Simple fallback when LLM summarization is unavailable."""
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        return (
            f"Previous conversation: {len(user_msgs)} user messages, "
            f"{len(assistant_msgs)} assistant responses, "
            f"{len(tool_msgs)} tool calls."
        )

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        """Convert messages to plain text for summarization."""
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str) and content and content != "[cleared]":
                parts.append(f"[{role}]: {content}")
        return "\n".join(parts)
