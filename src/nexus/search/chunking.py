"""Document chunking for semantic search.

Implements various chunking strategies to split documents into searchable chunks.

Performance:
- Uses optimized hierarchical merge algorithm
- Only tokenizes merged chunks, not the full document
- ~150ms for 250KB documents

Issue #1024: Entropy-aware chunking filters redundant/low-information chunks
before embedding, reducing storage costs and improving retrieval quality.
Based on SimpleMem paper (arXiv:2601.02553).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import tiktoken as tiktoken_module

    from nexus.search.embeddings import EmbeddingProvider

    TIKTOKEN_AVAILABLE: bool
else:
    try:
        import tiktoken as tiktoken_module

        TIKTOKEN_AVAILABLE = True
    except ImportError:
        tiktoken_module = None  # type: ignore[assignment]
        TIKTOKEN_AVAILABLE = False


class ChunkStrategy(StrEnum):
    """Chunking strategy."""

    FIXED = "fixed"  # Fixed-size chunks
    SEMANTIC = "semantic"  # Semantic chunks (paragraphs/sections)
    OVERLAPPING = "overlapping"  # Overlapping fixed-size chunks


@dataclass
class DocumentChunk:
    """A chunk of a document with source location metadata."""

    text: str
    chunk_index: int
    tokens: int
    start_offset: int  # Character offset in original document
    end_offset: int  # End character offset
    line_start: int | None = None  # Line number where chunk starts (1-indexed)
    line_end: int | None = None  # Line number where chunk ends (1-indexed)


def _offset_to_line(content: str, offset: int) -> int:
    """Convert character offset to line number (1-indexed).

    Args:
        content: Full document content
        offset: Character offset

    Returns:
        Line number (1-indexed)
    """
    if offset <= 0:
        return 1
    return content[:offset].count("\n") + 1


def _compute_line_numbers(content: str, start_offset: int, end_offset: int) -> tuple[int, int]:
    """Compute line numbers for a chunk.

    Args:
        content: Full document content
        start_offset: Start character offset
        end_offset: End character offset

    Returns:
        Tuple of (line_start, line_end)
    """
    line_start = _offset_to_line(content, start_offset)
    line_end = _offset_to_line(content, end_offset)
    return line_start, line_end


def _build_line_offsets(content: str) -> list[int]:
    """Pre-compute line start offsets for O(1) line number lookup.

    Args:
        content: Full document content

    Returns:
        List of character offsets where each line starts (0-indexed lines)
    """
    offsets = [0]
    for i, c in enumerate(content):
        if c == "\n":
            offsets.append(i + 1)
    return offsets


def _offset_to_line_fast(offset: int, line_offsets: list[int]) -> int:
    """Convert character offset to line number using pre-computed table.

    O(log n) lookup via binary search instead of O(n) counting.

    Args:
        offset: Character offset
        line_offsets: Pre-computed line offset table from _build_line_offsets()

    Returns:
        Line number (1-indexed)
    """
    import bisect

    if offset <= 0:
        return 1
    # bisect_right returns insertion point; that's the line number (1-indexed)
    return bisect.bisect_right(line_offsets, offset)


def _compute_line_numbers_fast(
    start_offset: int, end_offset: int, line_offsets: list[int]
) -> tuple[int, int]:
    """Compute line numbers using pre-computed offset table.

    Args:
        start_offset: Start character offset
        end_offset: End character offset
        line_offsets: Pre-computed line offset table

    Returns:
        Tuple of (line_start, line_end)
    """
    line_start = _offset_to_line_fast(start_offset, line_offsets)
    line_end = _offset_to_line_fast(end_offset, line_offsets)
    return line_start, line_end


class DocumentChunker:
    """Document chunker for semantic search.

    Uses optimized hierarchical merge algorithm that only tokenizes
    merged chunks, not the full document. This is faster than tokenizing
    the entire document upfront.
    """

    encoding: Any  # tiktoken.Encoding or None

    def __init__(
        self,
        chunk_size: int = 1024,
        overlap_size: int = 128,
        strategy: ChunkStrategy = ChunkStrategy.FIXED,
        encoding_name: str = "cl100k_base",
    ):
        """Initialize document chunker.

        Args:
            chunk_size: Target chunk size in tokens
            overlap_size: Overlap size in tokens for overlapping strategy
            strategy: Chunking strategy to use
            encoding_name: Tiktoken encoding name (default: cl100k_base for GPT-4/Claude)
        """
        self.chunk_size = chunk_size
        self.overlap_size = overlap_size
        self.strategy = strategy
        self.encoding_name = encoding_name

        # Initialize tokenizer
        if tiktoken_module is not None:
            try:
                self.encoding = tiktoken_module.get_encoding(encoding_name)
            except Exception:
                # Fallback to approximate tokenization
                self.encoding = None
        else:
            self.encoding = None

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text.

        Args:
            text: Text to count tokens in

        Returns:
            Number of tokens
        """
        if self.encoding is not None:
            return len(self.encoding.encode(text))
        else:
            # Rough approximation: 1 token ≈ 4 characters
            return len(text) // 4

    def _approx_tokens(self, text: str) -> int:
        """Fast approximate token count (1 token ≈ 4 chars).

        Args:
            text: Text to estimate tokens for

        Returns:
            Approximate number of tokens
        """
        return len(text) // 4

    def _fits_in_chunk(self, text: str) -> bool:
        """Check if text fits within chunk_size using smart approximation.

        Uses fast approximation for clear cases, only calls expensive
        tiktoken for boundary cases. This reduces tokenize calls by ~90%.

        Args:
            text: Text to check

        Returns:
            True if text fits within chunk_size
        """
        approx = self._approx_tokens(text)

        # Clear cases - skip expensive tokenization
        if approx < self.chunk_size * 0.7:
            return True  # Definitely fits
        if approx > self.chunk_size * 1.5:
            return False  # Definitely too big

        # Gray zone - use accurate count
        return self._count_tokens(text) <= self.chunk_size

    def _exceeds_chunk_size(self, text: str) -> bool:
        """Check if text exceeds chunk_size using smart approximation.

        Args:
            text: Text to check

        Returns:
            True if text exceeds chunk_size
        """
        return not self._fits_in_chunk(text)

    def chunk(
        self,
        content: str,
        file_path: str = "",
        compute_lines: bool = True,
    ) -> list[DocumentChunk]:
        """Chunk document into searchable chunks.

        Args:
            content: Document content to chunk
            file_path: Path to the file (used for file-type specific chunking)
            compute_lines: If True, compute line numbers for each chunk (default: True)

        Returns:
            List of document chunks with line numbers if compute_lines=True
        """
        if self.strategy == ChunkStrategy.FIXED:
            chunks = self._chunk_fixed(content)
        elif self.strategy == ChunkStrategy.SEMANTIC:
            chunks = self._chunk_semantic(content, file_path)
        elif self.strategy == ChunkStrategy.OVERLAPPING:
            chunks = self._chunk_overlapping(content)
        else:
            raise ValueError(f"Unknown chunking strategy: {self.strategy}")

        # Add line numbers to chunks using pre-computed offset table (O(log n) per chunk)
        if compute_lines and chunks:
            line_offsets = _build_line_offsets(content)
            for chunk in chunks:
                line_start, line_end = _compute_line_numbers_fast(
                    chunk.start_offset, chunk.end_offset, line_offsets
                )
                chunk.line_start = line_start
                chunk.line_end = line_end

        return chunks

    def _chunk_fixed(self, content: str) -> list[DocumentChunk]:
        """Chunk document into fixed-size chunks using recursive splitting.

        Uses a recursive approach similar to LangChain's RecursiveCharacterTextSplitter:
        tries to split at semantic boundaries (paragraphs, sentences, words) while
        respecting the chunk_size limit.

        This is much more efficient than per-word tokenization as it only counts
        tokens on merged chunks, not individual words.

        Args:
            content: Document content

        Returns:
            List of chunks
        """
        # Separators in order of preference (most semantic to least)
        separators = ["\n\n", "\n", ". ", ", ", " "]
        return self._recursive_split(content, separators, start_offset=0)

    def _recursive_split(
        self, text: str, separators: list[str], start_offset: int
    ) -> list[DocumentChunk]:
        """Recursively split text using separators until chunks fit within chunk_size.

        Args:
            text: Text to split
            separators: List of separators to try, in order of preference
            start_offset: Starting offset in the original document

        Returns:
            List of chunks
        """
        if not text.strip():
            return []

        # Check if text already fits using smart approximation (avoids tokenization)
        if self._fits_in_chunk(text):
            # Only count tokens when we create the final chunk
            text_tokens = self._count_tokens(text)
            return [
                DocumentChunk(
                    text=text,
                    chunk_index=0,  # Will be renumbered later
                    tokens=text_tokens,
                    start_offset=start_offset,
                    end_offset=start_offset + len(text),
                )
            ]

        # Try each separator
        for sep in separators:
            if sep in text:
                return self._split_and_merge(text, sep, separators, start_offset)

        # Last resort: split by characters (shouldn't normally reach here)
        return self._split_by_chars(text, start_offset)

    def _split_and_merge(
        self, text: str, separator: str, separators: list[str], start_offset: int
    ) -> list[DocumentChunk]:
        """Split text by separator and merge segments to fit chunk_size.

        Args:
            text: Text to split
            separator: Separator to use
            separators: All separators for recursive splitting
            start_offset: Starting offset in the original document

        Returns:
            List of chunks
        """
        chunks: list[DocumentChunk] = []
        parts = text.split(separator)

        current_parts: list[str] = []
        current_approx_tokens = 0  # Use approximation for running total
        current_offset = start_offset
        sep_approx_tokens = self._approx_tokens(separator)

        for part in parts:
            if not part:
                continue

            part_approx_tokens = self._approx_tokens(part)

            # If single part exceeds chunk_size, recursively split it
            # Use smart check that may call tiktoken for boundary cases
            if self._exceeds_chunk_size(part):
                # First, finalize current chunk if any
                if current_parts:
                    chunk_text = separator.join(current_parts)
                    # Count actual tokens only when creating final chunk
                    actual_tokens = self._count_tokens(chunk_text)
                    chunks.append(
                        DocumentChunk(
                            text=chunk_text,
                            chunk_index=len(chunks),
                            tokens=actual_tokens,
                            start_offset=current_offset,
                            end_offset=current_offset + len(chunk_text),
                        )
                    )
                    current_offset += len(chunk_text) + len(separator)
                    current_parts = []
                    current_approx_tokens = 0

                # Recursively split the large part with remaining separators
                remaining_seps = separators[separators.index(separator) + 1 :]
                if remaining_seps:
                    sub_chunks = self._recursive_split(part, remaining_seps, current_offset)
                    for sub_chunk in sub_chunks:
                        sub_chunk.chunk_index = len(chunks)
                        chunks.append(sub_chunk)
                    current_offset += len(part) + len(separator)
                else:
                    # No more separators, split by chars
                    sub_chunks = self._split_by_chars(part, current_offset)
                    for sub_chunk in sub_chunks:
                        sub_chunk.chunk_index = len(chunks)
                        chunks.append(sub_chunk)
                    current_offset += len(part) + len(separator)
                continue

            # Check if adding this part would exceed chunk_size (use approximation)
            sep_to_add = sep_approx_tokens if current_parts else 0
            if (
                current_approx_tokens + sep_to_add + part_approx_tokens > self.chunk_size
                and current_parts
            ):
                # Finalize current chunk - count actual tokens now
                chunk_text = separator.join(current_parts)
                actual_tokens = self._count_tokens(chunk_text)
                chunks.append(
                    DocumentChunk(
                        text=chunk_text,
                        chunk_index=len(chunks),
                        tokens=actual_tokens,
                        start_offset=current_offset,
                        end_offset=current_offset + len(chunk_text),
                    )
                )
                current_offset += len(chunk_text) + len(separator)
                current_parts = []
                current_approx_tokens = 0

            current_parts.append(part)
            current_approx_tokens += part_approx_tokens + (
                sep_approx_tokens if len(current_parts) > 1 else 0
            )

        # Add remaining parts as final chunk
        if current_parts:
            chunk_text = separator.join(current_parts)
            final_tokens = self._count_tokens(chunk_text)
            chunks.append(
                DocumentChunk(
                    text=chunk_text,
                    chunk_index=len(chunks),
                    tokens=final_tokens,
                    start_offset=current_offset,
                    end_offset=current_offset + len(chunk_text),
                )
            )

        # Renumber chunk indices
        for i, chunk in enumerate(chunks):
            chunk.chunk_index = i

        return chunks

    def _split_by_chars(self, text: str, start_offset: int) -> list[DocumentChunk]:
        """Split text by characters when no separator works.

        Args:
            text: Text to split
            start_offset: Starting offset in the original document

        Returns:
            List of chunks
        """
        chunks: list[DocumentChunk] = []

        # Estimate chars per token (roughly 4 chars per token)
        chars_per_chunk = self.chunk_size * 4
        current_offset = start_offset

        for i in range(0, len(text), chars_per_chunk):
            chunk_text = text[i : i + chars_per_chunk]
            tokens = self._count_tokens(chunk_text)
            chunks.append(
                DocumentChunk(
                    text=chunk_text,
                    chunk_index=len(chunks),
                    tokens=tokens,
                    start_offset=current_offset,
                    end_offset=current_offset + len(chunk_text),
                )
            )
            current_offset += len(chunk_text)

        return chunks

    def _chunk_semantic(self, content: str, file_path: str) -> list[DocumentChunk]:
        """Chunk document semantically (by paragraphs/sections).

        Args:
            content: Document content
            file_path: Path to the file

        Returns:
            List of chunks
        """
        # Determine file type
        if file_path.endswith((".md", ".markdown")):
            return self._chunk_markdown(content)
        elif file_path.endswith((".py", ".js", ".ts", ".java", ".go", ".rs")):
            return self._chunk_code(content)
        else:
            return self._chunk_paragraphs(content)

    def _chunk_markdown(self, content: str) -> list[DocumentChunk]:
        """Chunk markdown by sections.

        Args:
            content: Markdown content

        Returns:
            List of chunks
        """
        chunks: list[DocumentChunk] = []
        # Split by headings
        sections = re.split(r"\n(?=#{1,6}\s)", content)
        current_offset = 0

        for section in sections:
            if not section.strip():
                continue

            tokens = self._count_tokens(section)

            # If section is too large, split it further
            if tokens > self.chunk_size:
                sub_chunks = self._chunk_paragraphs(section)
                for sub_chunk in sub_chunks:
                    sub_chunk.chunk_index = len(chunks)
                    sub_chunk.start_offset += current_offset
                    sub_chunk.end_offset += current_offset
                    chunks.append(sub_chunk)
            else:
                chunks.append(
                    DocumentChunk(
                        text=section,
                        chunk_index=len(chunks),
                        tokens=tokens,
                        start_offset=current_offset,
                        end_offset=current_offset + len(section),
                    )
                )

            current_offset += len(section) + 1  # +1 for newline

        return chunks

    def _chunk_code(self, content: str) -> list[DocumentChunk]:
        """Chunk code by functions/classes.

        Args:
            content: Code content

        Returns:
            List of chunks
        """
        # For now, use paragraph-based chunking
        # TODO: Implement AST-based chunking for better code structure preservation
        return self._chunk_paragraphs(content)

    def _chunk_paragraphs(self, content: str) -> list[DocumentChunk]:
        """Chunk by paragraphs.

        Args:
            content: Content to chunk

        Returns:
            List of chunks
        """
        chunks: list[DocumentChunk] = []
        paragraphs = content.split("\n\n")
        current_chunk: list[str] = []
        current_tokens = 0
        current_offset = 0

        for para in paragraphs:
            if not para.strip():
                continue

            para_tokens = self._count_tokens(para)

            if current_tokens + para_tokens > self.chunk_size and current_chunk:
                # Create chunk
                chunk_text = "\n\n".join(current_chunk)
                chunk_end = current_offset + len(chunk_text)
                chunks.append(
                    DocumentChunk(
                        text=chunk_text,
                        chunk_index=len(chunks),
                        tokens=current_tokens,
                        start_offset=current_offset,
                        end_offset=chunk_end,
                    )
                )
                current_offset = chunk_end + 2  # +2 for \n\n
                current_chunk = []
                current_tokens = 0

            current_chunk.append(para)
            current_tokens += para_tokens

        # Add remaining chunk
        if current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            chunk_end = current_offset + len(chunk_text)
            chunks.append(
                DocumentChunk(
                    text=chunk_text,
                    chunk_index=len(chunks),
                    tokens=current_tokens,
                    start_offset=current_offset,
                    end_offset=chunk_end,
                )
            )

        return chunks

    def _chunk_overlapping(self, content: str) -> list[DocumentChunk]:
        """Chunk document with overlapping windows.

        Args:
            content: Document content

        Returns:
            List of chunks
        """
        chunks: list[DocumentChunk] = []
        words = content.split()
        current_offset = 0

        # Calculate step size (chunk_size - overlap_size)
        step_size = self.chunk_size - self.overlap_size

        i = 0
        while i < len(words):
            # Take chunk_size words
            chunk_words = words[i : i + self.chunk_size]
            chunk_text = " ".join(chunk_words)
            tokens = self._count_tokens(chunk_text)

            chunks.append(
                DocumentChunk(
                    text=chunk_text,
                    chunk_index=len(chunks),
                    tokens=tokens,
                    start_offset=current_offset,
                    end_offset=current_offset + len(chunk_text),
                )
            )

            # Move forward by step_size
            i += step_size
            current_offset += len(" ".join(words[i - step_size : i])) + 1

        return chunks


@dataclass
class EntropyFilterResult:
    """Result of entropy-aware filtering.

    Issue #1024: Tracks filtering statistics for benchmarking.
    """

    chunks: list[DocumentChunk]
    original_count: int
    filtered_count: int
    scores: list[float] = field(default_factory=list)

    @property
    def reduction_percent(self) -> float:
        """Calculate chunk reduction percentage."""
        if self.original_count == 0:
            return 0.0
        return ((self.original_count - self.filtered_count) / self.original_count) * 100


def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity (-1 to 1)
    """
    if len(vec1) != len(vec2):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=False))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


class EntropyAwareChunker:
    """Entropy-aware chunker that filters redundant/low-information chunks.

    Issue #1024: Implements SimpleMem's entropy-aware filtering formula:
        H(W_t) = α · |ℰ_new|/|W_t| + (1-α) · (1 - cos(E(W_t), E(H_prev)))

    Where:
    - |ℰ_new|: New entities in current chunk not seen in history
    - |W_t|: Current chunk size (word count)
    - cos(E(W_t), E(H_prev)): Semantic similarity to previous chunks
    - α: Balance between entity novelty and semantic divergence

    Reference: SimpleMem paper (arXiv:2601.02553)

    Usage:
        chunker = EntropyAwareChunker(
            redundancy_threshold=0.35,
            alpha=0.5,
            embedding_provider=embedding_provider,
        )
        result = await chunker.chunk_with_filtering(content)
        print(f"Filtered {result.reduction_percent:.1f}% redundant chunks")
    """

    # Regex patterns for lightweight entity extraction
    # Matches capitalized words/phrases, numbers, URLs, emails, etc.
    ENTITY_PATTERNS = [
        # Capitalized words (proper nouns) - at least 2 chars
        r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b",
        # ALL CAPS acronyms (2+ chars)
        r"\b[A-Z]{2,}\b",
        # Numbers with context (dates, versions, IDs)
        r"\b\d{4}[-/]\d{2}[-/]\d{2}\b",  # Dates
        r"\bv?\d+\.\d+(?:\.\d+)?\b",  # Versions
        r"\b\d+(?:,\d{3})*(?:\.\d+)?\b",  # Large numbers
        # Technical identifiers
        r"\b[a-z_][a-z0-9_]*(?:_[a-z0-9]+)+\b",  # snake_case
        r"\b[a-z]+(?:[A-Z][a-z]+)+\b",  # camelCase
        # URLs and emails (simplified)
        r"https?://[^\s]+",
        r"\b[\w.-]+@[\w.-]+\.\w+\b",
    ]

    def __init__(
        self,
        redundancy_threshold: float = 0.35,
        alpha: float = 0.5,
        embedding_provider: EmbeddingProvider | None = None,
        base_chunker: DocumentChunker | None = None,
        history_window: int = 5,
    ):
        """Initialize entropy-aware chunker.

        Args:
            redundancy_threshold: Filter chunks with score below this (default: 0.35)
                                  SimpleMem paper uses τ_redundant = 0.35
            alpha: Balance between entity novelty (α) and semantic novelty (1-α)
                   Default 0.5 gives equal weight to both signals
            embedding_provider: Provider for semantic embeddings (optional)
                                If None, only entity novelty is used (α=1.0 effectively)
            base_chunker: Base chunker to use for initial chunking
                          Default: DocumentChunker with SEMANTIC strategy
            history_window: Number of previous chunks to consider for novelty
                            Default: 5 chunks
        """
        self.redundancy_threshold = redundancy_threshold
        self.alpha = alpha
        self.embedding_provider = embedding_provider
        self.history_window = history_window

        # Use provided chunker or create default
        self.base_chunker = base_chunker or DocumentChunker(
            chunk_size=1024,
            strategy=ChunkStrategy.SEMANTIC,
            overlap_size=128,
        )

        # Compile regex patterns for efficiency
        self._entity_pattern = re.compile(
            "|".join(f"({p})" for p in self.ENTITY_PATTERNS),
            re.MULTILINE,
        )

    def extract_entities(self, text: str) -> set[str]:
        """Extract entities from text using lightweight regex patterns.

        This is a fast, dependency-free alternative to full NER.
        Captures proper nouns, technical terms, dates, versions, etc.

        Args:
            text: Text to extract entities from

        Returns:
            Set of extracted entity strings (normalized to lowercase)
        """
        entities = set()

        for match in self._entity_pattern.finditer(text):
            entity = match.group(0).strip()
            if entity and len(entity) >= 2:
                # Normalize to lowercase for comparison
                entities.add(entity.lower())

        return entities

    def _entity_novelty_score(
        self,
        chunk_entities: set[str],
        history_entities: set[str],
        chunk_word_count: int,
    ) -> float:
        """Calculate entity novelty score.

        Formula: |ℰ_new| / |W_t|

        Args:
            chunk_entities: Entities in current chunk
            history_entities: Entities seen in previous chunks
            chunk_word_count: Number of words in current chunk

        Returns:
            Entity novelty score (0.0 to 1.0)
        """
        if chunk_word_count == 0:
            return 0.0

        new_entities = chunk_entities - history_entities
        # Normalize by word count, cap at 1.0
        return min(len(new_entities) / max(chunk_word_count, 1), 1.0)

    def _semantic_novelty_score(
        self,
        chunk_embedding: list[float],
        history_embeddings: list[list[float]],
    ) -> float:
        """Calculate semantic novelty score.

        Formula: 1 - max(cos(E(W_t), E(H_i)) for H_i in history)

        Args:
            chunk_embedding: Embedding of current chunk
            history_embeddings: Embeddings of previous chunks

        Returns:
            Semantic novelty score (0.0 to 1.0)
        """
        if not history_embeddings:
            return 1.0  # First chunk is fully novel

        # Find maximum similarity to any history chunk
        max_similarity = max(
            _cosine_similarity(chunk_embedding, hist_emb) for hist_emb in history_embeddings
        )

        # Novelty = 1 - similarity
        return 1.0 - max(0.0, min(max_similarity, 1.0))

    def information_score(
        self,
        chunk_entities: set[str],
        history_entities: set[str],
        chunk_word_count: int,
        chunk_embedding: list[float] | None = None,
        history_embeddings: list[list[float]] | None = None,
    ) -> float:
        """Calculate information density score for a chunk.

        Formula: H(W_t) = α · entity_novelty + (1-α) · semantic_novelty

        Args:
            chunk_entities: Entities in current chunk
            history_entities: Entities seen in previous chunks
            chunk_word_count: Number of words in current chunk
            chunk_embedding: Embedding of current chunk (optional)
            history_embeddings: Embeddings of previous chunks (optional)

        Returns:
            Information score (0.0 to 1.0), higher = more informative
        """
        entity_score = self._entity_novelty_score(
            chunk_entities, history_entities, chunk_word_count
        )

        # If no embeddings, use only entity score
        if chunk_embedding is None or history_embeddings is None:
            return entity_score

        semantic_score = self._semantic_novelty_score(chunk_embedding, history_embeddings)

        # Weighted combination
        return self.alpha * entity_score + (1 - self.alpha) * semantic_score

    async def chunk_with_filtering(
        self,
        content: str,
        file_path: str = "",
        compute_lines: bool = True,
    ) -> EntropyFilterResult:
        """Chunk content and filter redundant chunks.

        Args:
            content: Document content to chunk
            file_path: Path to file (for file-type specific chunking)
            compute_lines: If True, compute line numbers for chunks

        Returns:
            EntropyFilterResult with filtered chunks and statistics
        """
        # First, chunk using base chunker
        all_chunks = self.base_chunker.chunk(content, file_path, compute_lines)

        if not all_chunks:
            return EntropyFilterResult(
                chunks=[],
                original_count=0,
                filtered_count=0,
                scores=[],
            )

        # Get embeddings if provider available
        embeddings: list[list[float]] | None = None
        if self.embedding_provider:
            chunk_texts = [chunk.text for chunk in all_chunks]
            embeddings = await self.embedding_provider.embed_texts(chunk_texts)

        # Filter chunks based on information score
        filtered_chunks: list[DocumentChunk] = []
        scores: list[float] = []
        history_entities: set[str] = set()
        history_embeddings: list[list[float]] = []

        for i, chunk in enumerate(all_chunks):
            # Extract entities from current chunk
            chunk_entities = self.extract_entities(chunk.text)
            chunk_word_count = len(chunk.text.split())

            # Get embedding if available
            chunk_embedding = embeddings[i] if embeddings else None

            # Calculate information score
            score = self.information_score(
                chunk_entities=chunk_entities,
                history_entities=history_entities,
                chunk_word_count=chunk_word_count,
                chunk_embedding=chunk_embedding,
                history_embeddings=history_embeddings[-self.history_window :]
                if history_embeddings
                else None,
            )
            scores.append(score)

            # Keep chunk if above threshold OR if it's the first chunk
            # The first chunk is always kept since there's no history to compare against
            is_first_chunk = i == 0
            if is_first_chunk or score >= self.redundancy_threshold:
                # Update chunk index to be sequential in filtered list
                chunk.chunk_index = len(filtered_chunks)
                filtered_chunks.append(chunk)

            # Always update history (even for filtered chunks)
            # This ensures we don't re-include similar content later
            history_entities.update(chunk_entities)
            if chunk_embedding:
                history_embeddings.append(chunk_embedding)

        logger.info(
            "[ENTROPY-CHUNKER] Filtered %d/%d chunks (%.1f%% reduction), "
            "threshold=%.2f, avg_score=%.3f",
            len(all_chunks) - len(filtered_chunks),
            len(all_chunks),
            ((len(all_chunks) - len(filtered_chunks)) / len(all_chunks) * 100) if all_chunks else 0,
            self.redundancy_threshold,
            sum(scores) / len(scores) if scores else 0,
        )

        return EntropyFilterResult(
            chunks=filtered_chunks,
            original_count=len(all_chunks),
            filtered_count=len(filtered_chunks),
            scores=scores,
        )

    def chunk_with_filtering_sync(
        self,
        content: str,
        file_path: str = "",
        compute_lines: bool = True,
    ) -> EntropyFilterResult:
        """Synchronous version of chunk_with_filtering (entity-only, no embeddings).

        Use this when you don't need semantic novelty scoring.

        Args:
            content: Document content to chunk
            file_path: Path to file (for file-type specific chunking)
            compute_lines: If True, compute line numbers for chunks

        Returns:
            EntropyFilterResult with filtered chunks and statistics
        """
        # First, chunk using base chunker
        all_chunks = self.base_chunker.chunk(content, file_path, compute_lines)

        if not all_chunks:
            return EntropyFilterResult(
                chunks=[],
                original_count=0,
                filtered_count=0,
                scores=[],
            )

        # Filter chunks based on entity-only information score
        filtered_chunks: list[DocumentChunk] = []
        scores: list[float] = []
        history_entities: set[str] = set()

        for i, chunk in enumerate(all_chunks):
            # Extract entities from current chunk
            chunk_entities = self.extract_entities(chunk.text)
            chunk_word_count = len(chunk.text.split())

            # Calculate entity-only information score (no embeddings)
            score = self._entity_novelty_score(chunk_entities, history_entities, chunk_word_count)
            scores.append(score)

            # Keep chunk if above threshold OR if it's the first chunk
            # The first chunk is always kept since there's no history to compare against
            is_first_chunk = i == 0
            if is_first_chunk or score >= self.redundancy_threshold:
                chunk.chunk_index = len(filtered_chunks)
                filtered_chunks.append(chunk)

            # Always update history
            history_entities.update(chunk_entities)

        return EntropyFilterResult(
            chunks=filtered_chunks,
            original_count=len(all_chunks),
            filtered_count=len(filtered_chunks),
            scores=scores,
        )
