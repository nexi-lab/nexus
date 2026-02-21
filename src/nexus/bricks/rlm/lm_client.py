"""NexusLMClient — routes RLM's LLM calls through Nexus's LiteLLMProvider.

This client bridges the RLM iteration loop (sync) with Nexus's async
LLM provider. Since the RLM loop runs in a thread pool (via run_in_executor),
we can safely use asyncio.run() inside chat() without event loop conflicts.

Architecture Decision: Issue 3B — custom client wrapping LiteLLMProvider
for full observability, cost tracking, and caching.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.bricks.rlm.types import RLMInfrastructureError

logger = logging.getLogger(__name__)


class NexusLMClient:
    """LLM client that routes calls through Nexus's LiteLLMProvider.

    This runs inside a thread pool executor, so asyncio.run() is safe
    (no pre-existing event loop in the thread).

    Attributes:
        total_tokens_used: Running count of tokens consumed across all calls.
    """

    def __init__(
        self,
        provider: Any,  # LiteLLMProvider or mock
        model: str,
    ) -> None:
        self._provider = provider
        self._model = model
        self.total_tokens_used: int = 0

    def get_model(self) -> str:
        """Return the configured model name."""
        return self._model

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Send a chat completion request through LiteLLMProvider.

        Args:
            messages: List of message dicts with "role" and "content" keys.
            model: Model override (optional, uses configured model if None).
            **kwargs: Additional arguments passed to the provider.

        Returns:
            The assistant's response text.

        Raises:
            RLMInfrastructureError: If the LLM provider is unavailable.
        """
        effective_model = model or self._model
        try:
            # Convert dict messages to the format expected by LiteLLMProvider
            formatted_messages = self._format_messages(messages)

            # Run async provider in this thread's event loop
            # Safe because we're in a ThreadPoolExecutor (no pre-existing loop)
            response = asyncio.run(
                self._provider.complete_async(formatted_messages, model=effective_model, **kwargs)
            )

            # Extract response text
            text = self._extract_response_text(response)

            # Track token usage
            token_count = self._provider.count_tokens(formatted_messages)
            self.total_tokens_used += token_count

            return text

        except RLMInfrastructureError:
            raise
        except Exception as exc:
            raise RLMInfrastructureError(f"LLM provider error: {exc}") from exc

    def count_tokens(self, messages: list[dict[str, str]]) -> int:
        """Count tokens for a set of messages.

        Args:
            messages: List of message dicts.

        Returns:
            Token count estimate.
        """
        formatted = self._format_messages(messages)
        return int(self._provider.count_tokens(formatted))

    def _format_messages(self, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Convert rlm-style message dicts to LiteLLMProvider format.

        The rlm library uses simple {"role": "...", "content": "..."} dicts.
        LiteLLMProvider accepts the same format (litellm standard).
        """
        return [
            {"role": msg.get("role", "user"), "content": msg.get("content", "")} for msg in messages
        ]

    def _extract_response_text(self, response: Any) -> str:
        """Extract text content from a LiteLLM response object."""
        try:
            # Standard litellm ModelResponse format
            if hasattr(response, "choices") and response.choices:
                choice = response.choices[0]
                if hasattr(choice, "message") and hasattr(choice.message, "content"):
                    return choice.message.content or ""
            # Fallback: try dict-like access
            if hasattr(response, "get"):
                choices = response.get("choices", [])
                if choices:
                    return choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError):
            pass
        return ""
