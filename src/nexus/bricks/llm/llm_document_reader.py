"""LLM-powered document reading for Nexus (service layer).

Moved from nexus.bricks.llm.document_reader (Issue #1521).
Orchestrates semantic search, LLM providers, and content parsing
to answer questions about documents.

Key refactoring (Issue #1521):
- Extracted _prepare_context() to DRY read()/stream() (Issue 5)
- Uses ChunkLike Protocol instead of SemanticSearchResult (Issue 14)
- Moved to services/ as an orchestration concern (Issue 1)
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.llm_types import Message, MessageRole, TextContent

from .llm_citation import Citation, CitationExtractor, DocumentReadResult
from .llm_context_builder import ChunkLike, ContextBuilder

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.bricks.llm.provider import LLMProvider
    from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC

# SemanticSearch typed as Any — avoids cross-brick import from search brick.
SemanticSearch = Any


@dataclass
class ReadChunk:
    """Simple chunk from direct file reading that satisfies ChunkLike."""

    path: str
    chunk_text: str
    chunk_index: int | None = None
    score: float | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class LLMDocumentReader:
    """LLM-powered document reading.

    Combines:
    - Content extraction via NexusFS parsers
    - Semantic search for relevant context
    - LLM processing for answers
    - Citation extraction
    """

    def __init__(
        self,
        nx: "NexusFilesystemABC",
        provider: "LLMProvider",
        search: "SemanticSearch | None" = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ):
        """Initialize document reader.

        Args:
            nx: NexusFilesystemABC instance
            provider: LLM provider
            search: Semantic search instance (optional - if None, only direct reading works)
            system_prompt: Custom system prompt (optional)
            max_context_tokens: Maximum tokens for context (default: 3000)
        """
        self.nx = nx
        self.provider = provider
        self.search = search
        self.context_builder = ContextBuilder(max_context_tokens=max_context_tokens)
        self.citation_extractor = CitationExtractor()

        # Default system prompt
        self.system_prompt = system_prompt or (
            "You are a helpful document assistant. "
            "Answer questions based on the provided context from documents. "
            "Be concise and accurate. "
            "When referencing information, mention the source document path."
        )

    async def _prepare_context(
        self,
        path: str,
        prompt: str,
        use_search: bool,
        search_limit: int,
        search_mode: str,
        context: Any = None,
    ) -> tuple[list[Message], list[ChunkLike], list[str]]:
        """Shared context preparation for read() and stream().

        Gathers chunks (from search or direct file reading), builds context,
        and constructs LLM messages.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            use_search: Use semantic search for context
            search_limit: Max search results to use
            search_mode: Search mode - "semantic", "keyword", or "hybrid"

        Returns:
            Tuple of (messages, chunks, sources)

        Raises:
            ValueError: If no content found for path
        """
        chunks: list[ChunkLike] = []
        sources: list[str] = []

        # Use semantic search if available and requested
        if use_search and self.search:
            search_results = await self.search.search(
                query=prompt, path=path, limit=search_limit, search_mode=search_mode
            )

            if search_results:
                # SemanticSearchResult satisfies ChunkLike via duck typing
                chunks = cast(list[ChunkLike], list(search_results))
                sources = list({r.path for r in search_results})
            else:
                # No results found, fall back to direct reading
                use_search = False
        elif use_search and not self.search:
            # Search requested but not available, fall back to direct reading
            use_search = False

        # If not using search, read document directly
        if not use_search:
            file_paths = self.nx.glob(path, context=context) if "*" in path else [path]

            for file_path in file_paths[:search_limit]:
                try:
                    content = self.nx.read(file_path, context=context)
                    if isinstance(content, bytes):
                        content_str = content.decode("utf-8", errors="ignore")
                    elif isinstance(content, dict):
                        content_str = str(content.get("text", content))
                    else:
                        content_str = str(content)

                    # Truncate to max context
                    max_chars = self.context_builder.max_context_tokens * 4
                    if len(content_str) > max_chars:
                        content_str = content_str[:max_chars] + "\n[Content truncated...]"

                    chunks.append(ReadChunk(path=file_path, chunk_text=content_str, chunk_index=0))
                    sources.append(file_path)
                except Exception as e:
                    logger.warning("Failed to read %s: %s", file_path, e)

        if not chunks:
            raise ValueError(f"No content found for path: {path}")

        # Build context from chunks (ChunkLike objects — no SemanticSearchResult needed)
        context = self.context_builder.build_context(chunks)

        # Build messages
        messages = [
            Message(
                role=MessageRole.SYSTEM,
                content=[TextContent(text=self.system_prompt)],
            ),
            Message(
                role=MessageRole.USER,
                content=[
                    TextContent(text=f"Context from documents:\n\n{context}\n\nQuestion: {prompt}")
                ],
            ),
        ]

        return messages, chunks, sources

    async def read(
        self,
        path: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1000,
        use_search: bool = True,
        search_limit: int = 10,
        search_mode: str = "semantic",
        include_citations: bool = True,
        context: Any = None,
    ) -> DocumentReadResult:
        """Read document(s) with LLM.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model to use (uses provider default if None)
            max_tokens: Max response tokens
            use_search: Use semantic search for context (default: True)
            search_limit: Max search results to use (default: 10)
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            include_citations: Extract and include citations (default: True)

        Returns:
            DocumentReadResult with answer, citations, sources

        Raises:
            ValueError: If semantic search is required but not available
            NexusFileNotFoundError: If file doesn't exist
        """
        messages, chunks, sources = await self._prepare_context(
            path, prompt, use_search, search_limit, search_mode, context=context
        )

        # Call LLM (async)
        kwargs: dict[str, Any] = {}
        original_model: str | None = None
        if model:
            original_model = self.provider.config.model
            self.provider.config.model = model

        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        try:
            response = await self.provider.complete_async(messages, **kwargs)
        finally:
            if model and original_model is not None:
                self.provider.config.model = original_model

        # Extract answer
        answer = response.content or ""

        # Extract citations if requested
        citations: list[Citation] = []
        if include_citations:
            citations = self.citation_extractor.extract_citations(
                answer, chunks, include_all_sources=True
            )

        # Get token usage and cost
        usage = response.usage
        tokens_used = usage.get("total_tokens") if usage else None
        cost = response.cost

        return DocumentReadResult(
            answer=answer,
            citations=citations,
            sources=sources,
            tokens_used=tokens_used,
            cost=cost,
            cached=False,
        )

    async def stream(
        self,
        path: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1000,
        use_search: bool = True,
        search_limit: int = 10,
        search_mode: str = "semantic",
        context: Any = None,
    ) -> AsyncIterator[str]:
        """Stream document reading response.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model to use
            max_tokens: Max response tokens
            use_search: Use semantic search for context
            search_limit: Max search results to use
            search_mode: Search mode - "semantic", "keyword", or "hybrid"

        Yields:
            Response chunks as strings
        """
        messages, _chunks, _sources = await self._prepare_context(
            path, prompt, use_search, search_limit, search_mode, context=context
        )

        # Stream response
        kwargs: dict[str, Any] = {}
        original_model: str | None = None
        if model:
            original_model = self.provider.config.model
            self.provider.config.model = model

        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        try:
            async for chunk in self.provider.stream_async(messages, **kwargs):
                yield chunk
        finally:
            if model and original_model is not None:
                self.provider.config.model = original_model
