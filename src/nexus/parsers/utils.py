"""Shared utilities for markdown parsing — extract_structure & create_chunks.

Deduplicated from MarkItDownParser, MarkItDownProvider, and LlamaParseProvider
(Issue #5A).
"""

from __future__ import annotations

from typing import Any

from nexus.parsers.types import TextChunk


def extract_structure(text: str) -> dict[str, Any]:
    """Extract document structure (headings) from markdown text.

    Args:
        text: Markdown text content

    Returns:
        Dictionary with ``headings``, ``has_headings``, and ``line_count``.
    """
    lines = text.split("\n")
    headings: list[dict[str, Any]] = []

    for line in lines:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            heading_text = line.lstrip("#").strip()
            # Skip empty headings (e.g. a line that is just "###")
            if heading_text:
                headings.append({"level": level, "text": heading_text})

    return {
        "headings": headings,
        "has_headings": len(headings) > 0,
        "line_count": len(lines),
    }


def create_chunks(text: str) -> list[TextChunk]:
    """Create semantic chunks from markdown text by splitting on headers.

    Args:
        text: Markdown text content

    Returns:
        List of TextChunk objects.  Falls back to a single chunk when the
        text contains no headers.
    """
    chunks: list[TextChunk] = []
    lines = text.split("\n")

    current_chunk: list[str] = []
    current_start = 0

    for line in lines:
        # Start a new chunk on headers
        if line.startswith("#") and current_chunk:
            chunk_text = "\n".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    TextChunk(
                        text=chunk_text,
                        start_index=current_start,
                        end_index=current_start + len(chunk_text),
                    )
                )
            current_chunk = [line]
            current_start += len(chunk_text) + 1
        else:
            current_chunk.append(line)

    # Add final chunk
    if current_chunk:
        chunk_text = "\n".join(current_chunk).strip()
        if chunk_text:
            chunks.append(
                TextChunk(
                    text=chunk_text,
                    start_index=current_start,
                    end_index=current_start + len(chunk_text),
                )
            )

    return chunks if chunks else [TextChunk(text=text, start_index=0, end_index=len(text))]
