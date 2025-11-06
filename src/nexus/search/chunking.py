"""Document chunking for semantic search.

Implements various chunking strategies to split documents into searchable chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

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
    """A chunk of a document."""

    text: str
    chunk_index: int
    tokens: int
    start_offset: int
    end_offset: int


class DocumentChunker:
    """Document chunker for semantic search."""

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

    def chunk(self, content: str, file_path: str = "") -> list[DocumentChunk]:
        """Chunk document into searchable chunks.

        Args:
            content: Document content to chunk
            file_path: Path to the file (used for file-type specific chunking)

        Returns:
            List of document chunks
        """
        if self.strategy == ChunkStrategy.FIXED:
            return self._chunk_fixed(content)
        elif self.strategy == ChunkStrategy.SEMANTIC:
            return self._chunk_semantic(content, file_path)
        elif self.strategy == ChunkStrategy.OVERLAPPING:
            return self._chunk_overlapping(content)
        else:
            raise ValueError(f"Unknown chunking strategy: {self.strategy}")

    def _chunk_fixed(self, content: str) -> list[DocumentChunk]:
        """Chunk document into fixed-size chunks.

        Args:
            content: Document content

        Returns:
            List of chunks
        """
        chunks: list[DocumentChunk] = []
        words = content.split()
        current_chunk: list[str] = []
        current_tokens = 0
        current_offset = 0

        for word in words:
            word_tokens = self._count_tokens(word)

            if current_tokens + word_tokens > self.chunk_size and current_chunk:
                # Create chunk
                chunk_text = " ".join(current_chunk)
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
                current_offset = chunk_end + 1  # +1 for space
                current_chunk = []
                current_tokens = 0

            current_chunk.append(word)
            current_tokens += word_tokens

        # Add remaining chunk
        if current_chunk:
            chunk_text = " ".join(current_chunk)
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
