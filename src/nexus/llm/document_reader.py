"""LLM-powered document reading for Nexus.

Combines semantic search, LLM providers, and content parsing
to answer questions about documents.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, TypedDict, cast

from nexus.llm.citation import Citation, CitationExtractor, DocumentReadResult
from nexus.llm.context_builder import ContextBuilder
from nexus.llm.message import Message, MessageRole, TextContent

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.llm.provider import LLMProvider
    from nexus.search.semantic import SemanticSearch


class ChunkDict(TypedDict):
    """Type for chunk dictionaries used internally."""

    path: str
    chunk_index: int | None
    chunk_text: str
    score: float | None
    start_offset: int | None
    end_offset: int | None


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
        nx: NexusFS,
        provider: LLMProvider,
        search: SemanticSearch | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ):
        """Initialize document reader.

        Args:
            nx: NexusFS instance
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
    ) -> DocumentReadResult:
        """Read document(s) with LLM.

        Args:
            path: Path to document or glob pattern
            prompt: Question or instruction
            model: LLM model to use (uses provider default if None)
            max_tokens: Max response tokens
            use_search: Use semantic search for context (default: True)
            search_limit: Max search results to use (default: 10)
            search_mode: Search mode - "semantic", "keyword", or "hybrid" (default: "semantic")
            include_citations: Extract and include citations (default: True)

        Returns:
            DocumentReadResult with answer, citations, sources

        Raises:
            ValueError: If semantic search is required but not available
            NexusFileNotFoundError: If file doesn't exist
        """
        chunks = []
        sources = []

        # Use semantic search if available and requested
        if use_search and self.search:
            # Search for relevant content
            search_results = await self.search.search(
                query=prompt, path=path, limit=search_limit, search_mode=search_mode
            )

            if not search_results:
                # No results found, try direct reading
                use_search = False
            else:
                # Convert search results to dict format for context builder
                chunks = [
                    {
                        "path": r.path,
                        "chunk_index": r.chunk_index,
                        "chunk_text": r.chunk_text,
                        "score": r.score,
                        "start_offset": r.start_offset,
                        "end_offset": r.end_offset,
                    }
                    for r in search_results
                ]

                # Extract unique sources
                sources = list({r.path for r in search_results})
        elif use_search and not self.search:
            # Search requested but not available, fall back to direct reading
            use_search = False

        # If not using search, read document directly
        if not use_search:
            # Handle glob patterns
            file_paths = self.nx.glob(path) if "*" in path else [path]

            # Read each file
            for file_path in file_paths[:search_limit]:  # Limit number of files
                try:
                    content = self.nx.read(file_path)
                    if isinstance(content, bytes):
                        content_str = content.decode("utf-8", errors="ignore")
                    elif isinstance(content, dict):
                        # Handle parsed content
                        content_str = str(content.get("text", content))
                    else:
                        content_str = str(content)

                    # Truncate to max context
                    max_chars = self.context_builder.max_context_tokens * 4
                    if len(content_str) > max_chars:
                        content_str = content_str[:max_chars] + "\n[Content truncated...]"

                    chunks.append(
                        {
                            "path": file_path,
                            "chunk_index": 0,
                            "chunk_text": content_str,
                            "score": None,
                            "start_offset": None,
                            "end_offset": None,
                        }
                    )
                    sources.append(file_path)
                except Exception as e:
                    # Log error but continue
                    import warnings

                    warnings.warn(f"Failed to read {file_path}: {e}", stacklevel=2)

        if not chunks:
            raise ValueError(f"No content found for path: {path}")

        # Build context from chunks
        from nexus.search.semantic import SemanticSearchResult

        search_result_objects = [
            SemanticSearchResult(
                path=c["path"],
                chunk_index=c["chunk_index"] if c["chunk_index"] is not None else 0,
                chunk_text=c["chunk_text"],
                score=c["score"] if c["score"] is not None else 0.0,
                start_offset=c["start_offset"],
                end_offset=c["end_offset"],
            )
            for c in cast(list[ChunkDict], chunks)
        ]

        context = self.context_builder.build_context(search_result_objects)

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

        # Call LLM (async)
        kwargs: dict[str, Any] = {}
        if model:
            # Temporarily override model in provider config
            original_model = self.provider.config.model
            self.provider.config.model = model

        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        try:
            response = await self.provider.complete_async(messages, **kwargs)
        finally:
            if model:
                # Restore original model
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
        chunks = []

        # Use semantic search if available and requested
        if use_search and self.search:
            search_results = await self.search.search(
                query=prompt, path=path, limit=search_limit, search_mode=search_mode
            )

            if not search_results:
                use_search = False
            else:
                chunks = [
                    {
                        "path": r.path,
                        "chunk_index": r.chunk_index,
                        "chunk_text": r.chunk_text,
                        "score": r.score,
                        "start_offset": r.start_offset,
                        "end_offset": r.end_offset,
                    }
                    for r in search_results
                ]
        elif use_search and not self.search:
            # Search requested but not available, fall back to direct reading
            use_search = False

        # If not using search, read document directly
        if not use_search:
            file_paths = self.nx.glob(path) if "*" in path else [path]

            for file_path in file_paths[:search_limit]:
                try:
                    content = self.nx.read(file_path)
                    if isinstance(content, bytes):
                        content_str = content.decode("utf-8", errors="ignore")
                    elif isinstance(content, dict):
                        content_str = str(content.get("text", content))
                    else:
                        content_str = str(content)

                    max_chars = self.context_builder.max_context_tokens * 4
                    if len(content_str) > max_chars:
                        content_str = content_str[:max_chars] + "\n[Content truncated...]"

                    chunks.append(
                        {
                            "path": file_path,
                            "chunk_index": 0,
                            "chunk_text": content_str,
                            "score": None,
                            "start_offset": None,
                            "end_offset": None,
                        }
                    )
                except Exception:
                    pass

        if not chunks:
            raise ValueError(f"No content found for path: {path}")

        # Build context
        from nexus.search.semantic import SemanticSearchResult

        search_result_objects = [
            SemanticSearchResult(
                path=c["path"],
                chunk_index=c["chunk_index"] if c["chunk_index"] is not None else 0,
                chunk_text=c["chunk_text"],
                score=c["score"] if c["score"] is not None else 0.0,
                start_offset=c["start_offset"],
                end_offset=c["end_offset"],
            )
            for c in cast(list[ChunkDict], chunks)
        ]

        context = self.context_builder.build_context(search_result_objects)

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

        # Stream response
        kwargs: dict[str, Any] = {}
        if model:
            original_model = self.provider.config.model
            self.provider.config.model = model

        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        try:
            async for chunk in self.provider.stream_async(messages, **kwargs):
                yield chunk
        finally:
            if model:
                self.provider.config.model = original_model
