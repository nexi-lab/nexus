"""NexusFS LLM integration mixin."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.llm.citation import DocumentReadResult
    from nexus.llm.document_reader import LLMDocumentReader
    from nexus.llm.provider import LLMProvider


class NexusFSLLMMixin:
    """Mixin for LLM-powered document reading capabilities.

    This mixin adds LLM-powered document reading to NexusFS.
    """

    def _get_llm_reader(
        self,
        provider: LLMProvider | None = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ) -> LLMDocumentReader:
        """Get or create LLM document reader.

        Args:
            provider: LLM provider instance (optional - will create default if None)
            model: Model name (default: claude-sonnet-4)
            api_key: API key for provider
            system_prompt: Custom system prompt
            max_context_tokens: Max tokens for context

        Returns:
            LLMDocumentReader instance
        """
        from nexus.llm.config import LLMConfig
        from nexus.llm.document_reader import LLMDocumentReader
        from nexus.llm.provider import LiteLLMProvider

        # Create provider if not provided
        if provider is None:
            import os

            model = model or "claude-sonnet-4"

            # Handle OpenRouter specifically
            if model and model.startswith("anthropic/"):
                # OpenRouter model - need to configure for OpenRouter
                if not api_key and os.getenv("OPENROUTER_API_KEY"):
                    api_key = os.getenv("OPENROUTER_API_KEY")

                # Set custom_llm_provider for OpenRouter
                config = LLMConfig(
                    model=model,
                    api_key=api_key,  # type: ignore[arg-type]
                    custom_llm_provider="openrouter",
                )
            else:
                config = LLMConfig(model=model, api_key=api_key)  # type: ignore[arg-type]

            provider = LiteLLMProvider(config)

        # Get semantic search if available
        search = None
        if hasattr(self, "_semantic_search"):
            search = self._semantic_search

        # Create document reader
        return LLMDocumentReader(
            nx=self,  # type: ignore[arg-type]
            provider=provider,
            search=search,
            system_prompt=system_prompt,
            max_context_tokens=max_context_tokens,
        )

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

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model (default: claude-sonnet-4)
            max_tokens: Max response tokens
            api_key: API key for LLM provider
            use_search: Use semantic search for context retrieval
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            provider: Optional pre-configured LLM provider

        Returns:
            LLM's answer to the question

        Example:
            ```python
            answer = await nx.llm_read(
                "/reports/q4.pdf",
                "What were the top 3 challenges?",
                model="claude-sonnet-4"
            )
            print(answer)
            ```
        """
        reader = self._get_llm_reader(provider=provider, model=model, api_key=api_key)

        result = await reader.read(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            use_search=use_search,
            search_mode=search_mode,
        )

        return result.answer

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

        Returns full DocumentReadResult with answer, citations, sources, and metadata.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model (default: claude-sonnet-4)
            max_tokens: Max response tokens
            api_key: API key for LLM provider
            use_search: Use semantic search for context retrieval
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            search_limit: Max search results to use
            include_citations: Extract and include citations
            provider: Optional pre-configured LLM provider

        Returns:
            DocumentReadResult with answer, citations, sources, tokens, cost

        Example:
            ```python
            result = await nx.llm_read_detailed(
                "/docs/**/*.md",
                "How does authentication work?",
                model="claude-sonnet-4"
            )

            print(result.answer)
            print(f"\\nSources ({len(result.citations)}):")
            for citation in result.citations:
                print(f"- {citation.path} (score: {citation.score:.2f})")
            print(f"\\nCost: ${result.cost:.4f}")
            ```
        """
        reader = self._get_llm_reader(provider=provider, model=model, api_key=api_key)

        return await reader.read(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            use_search=use_search,
            search_mode=search_mode,
            search_limit=search_limit,
            include_citations=include_citations,
        )

    @rpc_expose(description="Stream document reading response")
    async def llm_read_stream(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: LLMProvider | None = None,
    ) -> AsyncIterator[str]:
        """Stream document reading response.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model (default: claude-sonnet-4)
            max_tokens: Max response tokens
            api_key: API key for LLM provider
            use_search: Use semantic search for context retrieval
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            provider: Optional pre-configured LLM provider

        Yields:
            Response chunks as strings

        Example:
            ```python
            async for chunk in nx.llm_read_stream(
                "/report.pdf",
                "Summarize the key findings"
            ):
                print(chunk, end="", flush=True)
            ```
        """
        reader = self._get_llm_reader(provider=provider, model=model, api_key=api_key)

        async for chunk in reader.stream(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            use_search=use_search,
            search_mode=search_mode,
        ):
            yield chunk

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

        For users who want to create a reader instance and customize it further.

        Args:
            provider: LLM provider instance
            model: Model name
            api_key: API key
            system_prompt: Custom system prompt
            max_context_tokens: Max tokens for context

        Returns:
            LLMDocumentReader instance

        Example:
            ```python
            reader = nx.create_llm_reader(
                model="claude-opus-4",
                system_prompt="You are a technical document expert..."
            )

            result = await reader.read(
                path="/docs/**/*.md",
                prompt="Explain the architecture"
            )
            ```
        """
        return self._get_llm_reader(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=system_prompt,
            max_context_tokens=max_context_tokens,
        )
