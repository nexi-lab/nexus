"""LLM provider protocol (Issue #1521: Extract LLM module into LLM brick).

Defines the brick-level contract for LLM provider operations.
The LLM brick exposes this protocol; consumers depend on it
rather than the concrete LiteLLMProvider implementation.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3.3 Brick Independence Rules
    - Issue #1521: Extract LLM module into LLM brick
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """Brick-level contract for LLM provider operations.

    Provides low-level LLM access: completions (sync/async),
    streaming (sync/async), token counting, and capability queries.

    Implementations must support:
    - Synchronous and asynchronous completion
    - Synchronous and asynchronous streaming
    - Token counting with caching
    - Capability introspection (vision, function calling, prompt caching)
    - Metrics reset
    """

    def complete(
        self, messages: list[Any], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Any:
        """Send a synchronous completion request."""
        ...

    async def complete_async(
        self, messages: list[Any], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Any:
        """Send an asynchronous completion request."""
        ...

    def stream(
        self, messages: list[Any], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Iterator[str]:
        """Stream a synchronous completion response."""
        ...

    def stream_async(
        self, messages: list[Any], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> AsyncIterator[str]:
        """Stream an asynchronous completion response."""
        ...

    def count_tokens(self, messages: list[Any]) -> int:
        """Count tokens in messages."""
        ...

    def vision_is_active(self) -> bool:
        """Check if vision capabilities are enabled."""
        ...

    def is_function_calling_active(self) -> bool:
        """Check if function calling is enabled."""
        ...

    def is_caching_prompt_active(self) -> bool:
        """Check if prompt caching is supported and enabled."""
        ...

    def reset_metrics(self) -> None:
        """Reset accumulated metrics."""
        ...
