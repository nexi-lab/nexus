"""Shared utilities for markdown parsing — extract_structure & create_chunks.

Deduplicated from MarkItDownParser, MarkItDownProvider, and LlamaParseProvider
(Issue #5A).

Issue #3718: Both functions now delegate to the canonical
``md_structure.parse_markdown_structure()`` parser, preserving their
original return shapes for backward compatibility.
"""

from typing import Any

from nexus.bricks.parsers.md_structure import parse_markdown_structure, slice_content
from nexus.bricks.parsers.types import TextChunk


def extract_structure(text: str) -> dict[str, Any]:
    """Extract document structure (headings) from markdown text.

    Args:
        text: Markdown text content

    Returns:
        Dictionary with ``headings``, ``has_headings``, and ``line_count``.
    """
    content = text.encode("utf-8")
    index = parse_markdown_structure(content)
    headings = [{"level": s.depth, "text": s.heading} for s in index.sections]
    return {
        "headings": headings,
        "has_headings": len(headings) > 0,
        "line_count": text.count("\n") + 1,
    }


def create_chunks(text: str) -> list[TextChunk]:
    """Create semantic chunks from markdown text by splitting on headers.

    Each heading starts a new **flat** (non-overlapping) chunk that extends
    to the next heading at any depth.  This preserves the original pre-#3718
    behaviour where chunks are contiguous and non-hierarchical.

    Args:
        text: Markdown text content

    Returns:
        List of TextChunk objects.  Falls back to a single chunk when the
        text contains no headers.
    """
    content = text.encode("utf-8")
    index = parse_markdown_structure(content)

    if not index.sections:
        return [TextChunk(text=text, start_index=0, end_index=len(text))]

    # Sort all sections by byte_start to get document order.
    ordered = sorted(index.sections, key=lambda s: s.byte_start)

    # Deduplicate positions (hierarchical sections can share a start).
    seen_starts: set[int] = set()
    unique: list[tuple[int, int]] = []  # (byte_start, depth)
    for sec in ordered:
        if sec.byte_start not in seen_starts:
            seen_starts.add(sec.byte_start)
            unique.append((sec.byte_start, sec.depth))

    chunks: list[TextChunk] = []

    # Pre-heading content (before the first heading).
    first_byte = unique[0][0]
    if first_byte > 0:
        pre_text = slice_content(content, 0, first_byte).strip()
        if pre_text:
            chunks.append(TextChunk(text=pre_text, start_index=0, end_index=first_byte))

    # Flat chunks: each heading → next heading (any depth) or EOF.
    for i, (byte_start, _depth) in enumerate(unique):
        byte_end = unique[i + 1][0] if i + 1 < len(unique) else len(content)
        chunk_text = slice_content(content, byte_start, byte_end).strip()
        if chunk_text:
            chunks.append(
                TextChunk(
                    text=chunk_text,
                    start_index=byte_start,
                    end_index=byte_end,
                )
            )

    return chunks if chunks else [TextChunk(text=text, start_index=0, end_index=len(text))]
