"""Document parsing system for Nexus.

This module provides an extensible parser system for processing various
document formats. The system is built around:

1. Parser: Abstract base class for all parsers
2. ParseResult: Structured output from parsing operations
3. ParserRegistry: Central registry for managing parsers
4. ParsersBrick: Facade that owns both registries (recommended entry point)

Example usage:
    >>> from nexus.bricks.parsers.brick import ParsersBrick
    >>> brick = ParsersBrick()
    >>> parse_fn = brick.create_parse_fn()
"""

from collections.abc import Callable

from nexus.bricks.parsers.base import Parser
from nexus.bricks.parsers.detection import (
    decompress_content,
    detect_encoding,
    detect_mime_type,
    is_compressed,
    prepare_content_for_parsing,
)
from nexus.bricks.parsers.registry import ParserRegistry
from nexus.bricks.parsers.types import ImageData, ParseResult, TextChunk


def create_default_parse_fn() -> Callable[[bytes, str], bytes | None]:
    """Create a parse callback using PdfInspectorParser.

    Returns a ``(content, path) -> bytes | None`` callable suitable for
    passing as ``parse_fn`` to :func:`nexus.lib.virtual_views.get_parsed_content`.

    .. deprecated::
        Prefer ``ParsersBrick().create_parse_fn()`` which shares registries.
    """
    from nexus.bricks.parsers.brick import ParsersBrick

    return ParsersBrick().create_parse_fn()


__all__ = [
    "Parser",
    "ParserRegistry",
    "ParseResult",
    "TextChunk",
    "ImageData",
    # Detection utilities
    "detect_mime_type",
    "detect_encoding",
    "is_compressed",
    "decompress_content",
    "prepare_content_for_parsing",
    # Parse callback factory
    "create_default_parse_fn",
]
