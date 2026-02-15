"""LLM service protocol (Issue #1287: Extract domain services).

Defines the contract for LLM-powered document reading operations.
Existing implementation: ``nexus.services.llm_service.LLMService``.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProtocol(Protocol):
    """Service contract for LLM-powered document reading.

    Provides three interaction modes:
    - Simple: ``llm_read`` returns a plain answer string
    - Detailed: ``llm_read_detailed`` returns full result with citations
    - Streaming: ``llm_read_stream`` yields chunks for real-time display

    Plus a factory method for advanced reader configuration.
    """

    async def llm_read(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> str: ...

    async def llm_read_detailed(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        search_limit: int = 10,
        include_citations: bool = True,
        provider: Any = None,
    ) -> Any: ...

    async def llm_read_stream(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> AsyncIterator[str]: ...

    def create_llm_reader(
        self,
        provider: Any = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ) -> Any: ...
