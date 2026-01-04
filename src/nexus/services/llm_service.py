"""LLM Service - Extracted from NexusFSLLMMixin.

This service handles all LLM-powered document reading operations:
- Read documents with LLM and return answers
- Stream LLM responses in real-time
- Get detailed results with citations and sources
- Create custom LLM document readers

Phase 2: Core Refactoring (Issue #988, Task 2.9)
Extracted from: nexus_fs_llm.py (286 lines)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.llm.citation import DocumentReadResult
    from nexus.llm.document_reader import LLMDocumentReader
    from nexus.llm.provider import LLMProvider


class LLMService:
    """Independent LLM service extracted from NexusFS.

    Handles all LLM-powered document reading operations:
    - Simple Q&A over documents
    - Streaming responses for real-time interaction
    - Detailed results with citations and cost tracking
    - Custom reader creation for advanced use cases

    Architecture:
        - Works with LLMDocumentReader for document processing
        - Integrates with semantic search for context retrieval
        - Supports multiple LLM providers (Claude, OpenAI, OpenRouter, etc.)
        - Clean dependency injection

    Example:
        ```python
        llm_service = LLMService(nexus_fs=nx)

        # Simple Q&A
        answer = await llm_service.llm_read(
            path="/reports/q4.pdf",
            prompt="What were the top 3 challenges?",
            model="claude-sonnet-4"
        )
        print(answer)

        # Detailed result with citations
        result = await llm_service.llm_read_detailed(
            path="/docs/**/*.md",
            prompt="How does authentication work?",
            include_citations=True
        )
        print(result.answer)
        for citation in result.citations:
            print(f"Source: {citation.path}")

        # Streaming response
        async for chunk in llm_service.llm_read_stream(
            path="/report.pdf",
            prompt="Summarize key findings"
        ):
            print(chunk, end="", flush=True)

        # Custom reader for advanced usage
        reader = llm_service.create_llm_reader(
            model="claude-opus-4",
            system_prompt="You are a technical expert..."
        )
        result = await reader.read(path="/docs/", prompt="Explain architecture")
        ```
    """

    def __init__(
        self,
        nexus_fs: Any | None = None,
    ):
        """Initialize LLM service.

        Args:
            nexus_fs: NexusFS instance for filesystem operations and search
        """
        self.nexus_fs = nexus_fs

        logger.info("[LLMService] Initialized")

    # =========================================================================
    # Public API: LLM Document Reading
    # =========================================================================

    @rpc_expose(description="Read document with LLM and return answer")
    async def llm_read(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: LLMProvider | None = None,
    ) -> str:
        """Read document with LLM and return answer.

        Simple convenience method that returns just the answer text.
        Uses semantic search to find relevant context from the document(s)
        and then asks the LLM to answer the question.

        Args:
            path: Path to document or glob pattern (e.g., "/docs/**/*.md")
            prompt: Question or instruction
            model: LLM model name (default: claude-sonnet-4)
            max_tokens: Maximum response tokens
            api_key: API key for LLM provider (uses env var if not provided)
            use_search: Use semantic search for context retrieval
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            provider: Optional pre-configured LLM provider

        Returns:
            LLM's answer to the question (str)

        Examples:
            # Simple question over single document
            answer = await service.llm_read(
                path="/reports/q4.pdf",
                prompt="What were the top 3 challenges?"
            )
            print(answer)

            # Question over multiple documents with glob
            answer = await service.llm_read(
                path="/docs/**/*.md",
                prompt="How does authentication work?",
                model="claude-sonnet-4"
            )

            # With custom API key
            answer = await service.llm_read(
                path="/data/analysis.txt",
                prompt="Summarize findings",
                api_key="sk-..."
            )

        Note:
            This is the simplest method - use llm_read_detailed() if you need
            citations, sources, cost tracking, or other metadata.
        """
        # TODO: Extract llm_read implementation
        raise NotImplementedError("llm_read() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Read document with LLM and return detailed result")
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
        provider: LLMProvider | None = None,
    ) -> DocumentReadResult:
        """Read document with LLM and return detailed result.

        Returns full DocumentReadResult with answer, citations, sources,
        token counts, and cost information. Use this for production applications
        that need full observability.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model name (default: claude-sonnet-4)
            max_tokens: Maximum response tokens
            api_key: API key for LLM provider
            use_search: Use semantic search for context retrieval
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            search_limit: Maximum number of search results to use as context
            include_citations: Extract and include source citations
            provider: Optional pre-configured LLM provider

        Returns:
            DocumentReadResult with:
                - answer: LLM's answer (str)
                - citations: List of source citations with paths and scores
                - sources: List of source documents used
                - tokens: Token counts (prompt + completion)
                - cost: Estimated API cost in USD (float)
                - model: Model name used (str)
                - search_results: Raw search results if available

        Examples:
            # Get detailed result with citations
            result = await service.llm_read_detailed(
                path="/docs/**/*.md",
                prompt="How does authentication work?",
                include_citations=True
            )

            print(result.answer)
            print(f"\\nSources ({len(result.citations)}):")
            for citation in result.citations:
                print(f"- {citation.path} (score: {citation.score:.2f})")
            print(f"\\nTokens: {result.tokens}")
            print(f"Cost: ${result.cost:.4f}")

            # Control search behavior
            result = await service.llm_read_detailed(
                path="/research/**/*.pdf",
                prompt="What are the main conclusions?",
                search_mode="hybrid",
                search_limit=20,
                max_tokens=2000
            )

        Note:
            Citations are extracted by parsing the LLM's response for
            references to source documents. Accuracy depends on the model
            following citation instructions in the system prompt.
        """
        # TODO: Extract llm_read_detailed implementation
        raise NotImplementedError("llm_read_detailed() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Stream document reading response")
    async def llm_read_stream(
        self,
        _path: str,
        _prompt: str,
        _model: str = "claude-sonnet-4",
        _max_tokens: int = 1000,
        _api_key: str | None = None,
        _use_search: bool = True,
        _search_mode: str = "semantic",
        _provider: LLMProvider | None = None,
    ) -> AsyncIterator[str]:
        """Stream document reading response.

        Returns an async iterator that yields response chunks as they arrive
        from the LLM. Useful for real-time user interfaces where you want to
        display results progressively.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model name (default: claude-sonnet-4)
            max_tokens: Maximum response tokens
            api_key: API key for LLM provider
            use_search: Use semantic search for context retrieval
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            provider: Optional pre-configured LLM provider

        Yields:
            Response chunks as strings

        Examples:
            # Stream response with real-time display
            async for chunk in service.llm_read_stream(
                path="/report.pdf",
                prompt="Summarize the key findings"
            ):
                print(chunk, end="", flush=True)
            print()  # Newline after stream completes

            # Collect streamed response
            chunks = []
            async for chunk in service.llm_read_stream(
                path="/docs/**/*.md",
                prompt="How does the API work?"
            ):
                chunks.append(chunk)
            full_response = "".join(chunks)

            # Stream with custom model
            async for chunk in service.llm_read_stream(
                path="/data/analysis.txt",
                prompt="What are the trends?",
                model="claude-opus-4",
                max_tokens=2000
            ):
                process_chunk(chunk)

        Note:
            Streaming provides a better user experience but doesn't include
            detailed metadata like citations or cost. Use llm_read_detailed()
            if you need that information.
        """
        # TODO: Extract llm_read_stream implementation
        raise NotImplementedError("llm_read_stream() not yet implemented - Phase 2 in progress")
        yield  # pragma: no cover

    @rpc_expose(description="Create an LLM document reader for advanced usage")
    def create_llm_reader(
        self,
        provider: LLMProvider | None = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ) -> LLMDocumentReader:
        """Create an LLM document reader for advanced usage.

        Factory method that creates an LLMDocumentReader instance for users
        who want more control over the reading process. The reader can be
        customized and reused across multiple queries.

        Args:
            provider: LLM provider instance (creates default if None)
            model: Model name (default: claude-sonnet-4)
            api_key: API key for provider
            system_prompt: Custom system prompt for specialized behavior
            max_context_tokens: Maximum tokens to use for document context

        Returns:
            LLMDocumentReader instance ready for use

        Examples:
            # Create reader with custom system prompt
            reader = service.create_llm_reader(
                model="claude-opus-4",
                system_prompt=\"\"\"You are a technical documentation expert.
                Always provide code examples and explain trade-offs.\"\"\"
            )

            result = await reader.read(
                path="/docs/**/*.md",
                prompt="Explain the caching architecture"
            )
            print(result.answer)

            # Create reader for specific provider
            from nexus.llm.provider import LiteLLMProvider
            from nexus.llm.config import LLMConfig

            config = LLMConfig(
                model="gpt-4",
                api_key="sk-...",
                temperature=0.3
            )
            provider = LiteLLMProvider(config)

            reader = service.create_llm_reader(provider=provider)
            result = await reader.read(
                path="/research/*.pdf",
                prompt="What are the key findings?"
            )

            # Reuse reader for multiple queries
            reader = service.create_llm_reader(
                model="claude-sonnet-4",
                max_context_tokens=5000
            )

            for question in questions:
                result = await reader.read(path="/docs/", prompt=question)
                print(f"Q: {question}")
                print(f"A: {result.answer}\\n")

        Note:
            The reader has access to the full NexusFS instance and can read
            any files the current user has permission to access.
        """
        # TODO: Extract create_llm_reader implementation
        raise NotImplementedError("create_llm_reader() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_llm_reader(
        self,
        _provider: LLMProvider | None = None,
        _model: str | None = None,
        _api_key: str | None = None,
        _system_prompt: str | None = None,
        _max_context_tokens: int = 3000,
    ) -> LLMDocumentReader:
        """Get or create LLM document reader.

        Internal helper that creates an LLMDocumentReader with proper
        configuration and integrations (semantic search, filesystem access).

        Args:
            _provider: LLM provider instance (creates default if None)
            _model: Model name (default: claude-sonnet-4)
            _api_key: API key for provider
            _system_prompt: Custom system prompt
            _max_context_tokens: Maximum tokens for document context

        Returns:
            LLMDocumentReader instance

        Note:
            Automatically integrates with semantic search if available
            on the NexusFS instance. Handles provider-specific configuration
            like OpenRouter custom_llm_provider setting.
        """
        # TODO: Extract LLM reader getter
        raise NotImplementedError("_get_llm_reader() not yet implemented - Phase 2 in progress")


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Skeleton created ✅
#
# TODO (in order of priority):
# 1. [ ] Extract llm_read() for simple Q&A
# 2. [ ] Extract llm_read_detailed() with citations and cost tracking
# 3. [ ] Extract llm_read_stream() for real-time streaming
# 4. [ ] Extract create_llm_reader() factory method
# 5. [ ] Extract _get_llm_reader() helper with provider auto-configuration
# 6. [ ] Add unit tests for LLMService
# 7. [ ] Update NexusFS to use composition
# 8. [ ] Add backward compatibility shims with deprecation warnings
# 9. [ ] Update documentation and migration guide
#
# Lines extracted: 0 / 286 (0%)
# Files affected: 1 created, 0 modified
#
# This is a phased extraction to maintain working code at each step.
#
