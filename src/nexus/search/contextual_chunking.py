"""Contextual chunking with surrounding context (Issue #1192).

Implements Anthropic's Contextual Retrieval pattern: uses an LLM to generate
a short "situating context" for each chunk before embedding, making every chunk
self-contained by resolving pronouns, adding entity context, and summarising
the chunk's role in the document.

Design decisions (from plan review):
- Callable `context_generator` — no direct LLM coupling
- Pydantic for validated LLM output (ChunkContext)
- Dataclass for internal structures (ContextualChunk, ContextualChunkResult)
- Bounded parallelism via asyncio.Semaphore
- Graceful degradation per-chunk with single retry
- Heuristic fallback when no LLM is available
- Context and text stored separately, composed at index-time for embedding
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from nexus.search.chunking import DocumentChunk, DocumentChunker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class ChunkContext(BaseModel):
    """Validated LLM output — the situating context for a single chunk."""

    situating_context: str = Field(
        ...,
        description="Brief context that makes the chunk self-contained",
    )
    resolved_references: list[dict[str, str]] = Field(
        default_factory=list,
        description="Pronoun / reference resolutions, e.g. [{'original': 'he', 'resolved': 'John'}]",
    )
    key_entities: list[str] = Field(
        default_factory=list,
        description="Key entities mentioned in the chunk",
    )


@dataclass(frozen=True)
class ContextualChunk:
    """A document chunk enriched with LLM-generated context."""

    chunk: DocumentChunk  # Original chunk from the base chunker
    context: ChunkContext | None  # None when the LLM call failed
    position: int  # 0-based position in the document
    doc_summary: str  # Document-level summary used for generation

    @property
    def contextual_text(self) -> str:
        """Compose the full text used for embedding (context + original)."""
        if self.context and self.context.situating_context:
            return f"{self.context.situating_context}\n\n{self.chunk.text}"
        return self.chunk.text


@dataclass(frozen=True)
class ContextualChunkResult:
    """Result of contextual chunking — chunks plus stats."""

    chunks: list[ContextualChunk]
    total_chunks: int
    chunks_with_context: int
    chunks_without_context: int  # Failed / degraded LLM calls
    source_document_id: str  # Shared ID linking all chunks to the same doc

    @property
    def context_rate(self) -> float:
        """Percentage of chunks that received context (0.0 – 100.0)."""
        if self.total_chunks == 0:
            return 0.0
        return (self.chunks_with_context / self.total_chunks) * 100.0


@dataclass(frozen=True)
class ContextualChunkingConfig:
    """Configuration for contextual chunking."""

    enabled: bool = False
    max_context_length: int = 200  # Max chars for situating context
    batch_concurrency: int = 5  # Max parallel LLM calls
    use_heuristic_fallback: bool = True  # Use heuristic when LLM fails


# Type alias for the context generator callable.
# Args: (doc_summary, chunk_text, prev_chunks_texts, next_chunks_texts)
# Returns: ChunkContext
ContextGenerator = Callable[
    [str, str, list[str], list[str]],
    Awaitable[ChunkContext],
]


# ---------------------------------------------------------------------------
# ContextualChunker
# ---------------------------------------------------------------------------


class ContextualChunker:
    """Enriches document chunks with LLM-generated situating context.

    The chunker delegates raw chunking to a base ``DocumentChunker`` and then
    calls a user-supplied ``context_generator`` callable for each chunk,
    bounded by an ``asyncio.Semaphore`` to limit concurrency.

    On failure the chunker retries once, then degrades gracefully by storing
    the chunk without context (``context = None``).
    """

    def __init__(
        self,
        context_generator: ContextGenerator,
        config: ContextualChunkingConfig | None = None,
        base_chunker: DocumentChunker | None = None,
    ) -> None:
        self._context_generator = context_generator
        self._config = config or ContextualChunkingConfig(enabled=True)

        if base_chunker is None:
            from nexus.search.chunking import DocumentChunker

            base_chunker = DocumentChunker()
        self._base_chunker = base_chunker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chunk_with_context(
        self,
        document: str,
        doc_summary: str,
        file_path: str = "",
        compute_lines: bool = True,
    ) -> ContextualChunkResult:
        """Chunk *document* and generate situating context for every chunk.

        Args:
            document: Full document text.
            doc_summary: Pre-generated summary of the entire document.
            file_path: Optional path (forwarded to the base chunker).
            compute_lines: Whether to compute line numbers in chunks.

        Returns:
            ``ContextualChunkResult`` with enriched chunks and stats.
        """
        source_document_id = str(uuid.uuid4())

        # 1. Chunk with base chunker
        base_chunks = self._base_chunker.chunk(document, file_path, compute_lines)

        if not base_chunks:
            return ContextualChunkResult(
                chunks=[],
                total_chunks=0,
                chunks_with_context=0,
                chunks_without_context=0,
                source_document_id=source_document_id,
            )

        # 2. Build surrounding-text lists for each chunk
        chunk_texts = [c.text for c in base_chunks]

        # 3. Generate context in bounded parallel
        semaphore = asyncio.Semaphore(self._config.batch_concurrency)
        tasks = [
            self._generate_single_context(
                semaphore=semaphore,
                doc_summary=doc_summary,
                chunk=base_chunks[i],
                prev_chunks=chunk_texts[max(0, i - 2) : i],
                next_chunks=chunk_texts[i + 1 : i + 2],
                position=i,
            )
            for i in range(len(base_chunks))
        ]
        results: list[ChunkContext | None] = await asyncio.gather(*tasks)

        # 4. Assemble ContextualChunk list
        contextual_chunks: list[ContextualChunk] = []
        with_ctx = 0
        without_ctx = 0

        for i, (chunk, ctx) in enumerate(zip(base_chunks, results, strict=True)):
            # Truncate context if needed
            if ctx is not None and len(ctx.situating_context) > self._config.max_context_length:
                ctx = ChunkContext(
                    situating_context=ctx.situating_context[: self._config.max_context_length],
                    resolved_references=ctx.resolved_references,
                    key_entities=ctx.key_entities,
                )

            if ctx is not None:
                with_ctx += 1
            else:
                without_ctx += 1

            contextual_chunks.append(
                ContextualChunk(
                    chunk=chunk,
                    context=ctx,
                    position=i,
                    doc_summary=doc_summary,
                )
            )

        result = ContextualChunkResult(
            chunks=contextual_chunks,
            total_chunks=len(base_chunks),
            chunks_with_context=with_ctx,
            chunks_without_context=without_ctx,
            source_document_id=source_document_id,
        )

        logger.info(
            "[CONTEXTUAL-CHUNK] %s: %d chunks, %d with context (%.1f%%), %d without",
            file_path or "<inline>",
            result.total_chunks,
            result.chunks_with_context,
            result.context_rate,
            result.chunks_without_context,
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_single_context(
        self,
        semaphore: asyncio.Semaphore,
        doc_summary: str,
        chunk: DocumentChunk,
        prev_chunks: list[str],
        next_chunks: list[str],
        position: int,
    ) -> ChunkContext | None:
        """Generate context for one chunk with single retry + heuristic fallback."""
        async with semaphore:
            for attempt in range(2):  # At most one retry
                try:
                    ctx = await self._context_generator(
                        doc_summary,
                        chunk.text,
                        prev_chunks,
                        next_chunks,
                    )
                    return ctx
                except Exception:
                    if attempt == 0:
                        logger.debug(
                            "[CONTEXTUAL-CHUNK] Retry for chunk %d after error",
                            position,
                            exc_info=True,
                        )
                    else:
                        logger.warning(
                            "[CONTEXTUAL-CHUNK] LLM failed for chunk %d after retry",
                            position,
                            exc_info=True,
                        )

        # Heuristic fallback when LLM is unavailable
        if self._config.use_heuristic_fallback:
            return _heuristic_context(doc_summary, chunk.text, prev_chunks, position)
        return None


# ---------------------------------------------------------------------------
# Heuristic Context Generator (no LLM required)
# ---------------------------------------------------------------------------

# Simple regex patterns for entity extraction
_CAPITALIZED_WORDS = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
_HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _heuristic_context(
    doc_summary: str,
    chunk_text: str,
    prev_chunks: list[str],
    position: int,
) -> ChunkContext:
    """Generate basic situating context using heuristics (no LLM).

    Extracts:
    - Position info (first chunk, middle, etc.)
    - Key entities via capitalized-word regex
    - Nearby heading if present
    - Brief doc summary prefix

    This is intentionally simple — it provides better-than-nothing context
    when the LLM is unavailable, at zero API cost.
    """
    # 1. Position context
    position_text = "Opening section" if position == 0 else f"Section {position + 1}"

    # 2. Extract entities from chunk (capitalized multi-word phrases)
    entities = list(dict.fromkeys(_CAPITALIZED_WORDS.findall(chunk_text)))[:5]

    # 3. Check for headings in the chunk or previous chunks
    heading = ""
    heading_match = _HEADING_PATTERN.search(chunk_text)
    if heading_match:
        heading = heading_match.group(1).strip()
    elif prev_chunks:
        # Check last previous chunk for a heading
        prev_match = _HEADING_PATTERN.search(prev_chunks[-1])
        if prev_match:
            heading = prev_match.group(1).strip()

    # 4. Build context string
    parts: list[str] = [position_text]
    if doc_summary:
        # Take first sentence of summary
        first_sentence = doc_summary.split(". ")[0].strip()
        if first_sentence and not first_sentence.endswith("."):
            first_sentence += "."
        parts.append(f"From: {first_sentence}")
    if heading:
        parts.append(f"Under: {heading}")

    situating_context = ". ".join(parts)

    return ChunkContext(
        situating_context=situating_context,
        resolved_references=[],
        key_entities=entities,
    )


def create_heuristic_generator() -> ContextGenerator:
    """Create a ``ContextGenerator`` that uses heuristics only (no LLM calls).

    Useful when:
    - No LLM API key is available
    - Cost must be minimized
    - Documents are well-structured (headings, clear entities)

    Returns:
        A ``ContextGenerator`` suitable for ``ContextualChunker``.
    """

    async def _generate(
        doc_summary: str,
        chunk_text: str,
        prev_chunks: list[str],
        _next_chunks: list[str],
    ) -> ChunkContext:
        # Position is not directly available here, but we can infer from prev_chunks
        position = len(prev_chunks)
        return _heuristic_context(doc_summary, chunk_text, prev_chunks, position)

    return _generate


# ---------------------------------------------------------------------------
# LLM Context Generator Factory
# ---------------------------------------------------------------------------


async def create_context_generator(
    llm_generate: Callable[[str], Awaitable[str]],
) -> ContextGenerator:
    """Build a ``ContextGenerator`` callable from a simple LLM text-in/text-out function.

    ``llm_generate`` should accept a prompt string and return the raw LLM
    response text.  The returned callable parses the JSON response into a
    ``ChunkContext``.

    Args:
        llm_generate: Async function ``(prompt: str) -> str``.

    Returns:
        A ``ContextGenerator`` suitable for ``ContextualChunker``.
    """

    async def _generate(
        doc_summary: str,
        chunk_text: str,
        prev_chunks: list[str],
        next_chunks: list[str],
    ) -> ChunkContext:
        prev_text = " ".join(prev_chunks[-2:]) if prev_chunks else ""
        next_text = " ".join(next_chunks[:1]) if next_chunks else ""

        prompt = (
            "<document_summary>\n"
            f"{doc_summary}\n"
            "</document_summary>\n\n"
            "<previous_text>\n"
            f"{prev_text}\n"
            "</previous_text>\n\n"
            "<current_chunk>\n"
            f"{chunk_text}\n"
            "</current_chunk>\n\n"
            "<next_text>\n"
            f"{next_text}\n"
            "</next_text>\n\n"
            "Generate a brief context to make this chunk self-contained. Return JSON:\n"
            '{"situating_context": "...", '
            '"resolved_references": [{"original": "he", "resolved": "John"}], '
            '"key_entities": ["entity1"]}'
        )

        raw = await llm_generate(prompt)
        return ChunkContext.model_validate_json(raw)

    return _generate
