"""Document chunking for semantic search.

Implements various chunking strategies to split documents into searchable chunks.

Performance:
- Uses optimized hierarchical merge algorithm
- Only tokenizes merged chunks, not the full document
- ~150ms for 250KB documents
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import tiktoken as tiktoken_module

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
