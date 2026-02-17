"""Document parsing system for Nexus.

This module provides an extensible parser system for processing various
document formats. The system is built around:

1. Parser: Abstract base class for all parsers
2. ParseResult: Structured output from parsing operations
3. ParserRegistry: Central registry for managing parsers

Example usage:
    >>> from nexus.parsers import ParserRegistry, MarkItDownParser
    >>>
    >>> # Create and configure registry
    >>> registry = ParserRegistry()
    >>> registry.register(MarkItDownParser())
    >>>
    >>> # Parse a document
    >>> with open("document.pdf", "rb") as f:
    ...     content = f.read()
    >>> parser = registry.get_parser("document.pdf")
    >>> result = await parser.parse(content, {"path": "document.pdf"})
    >>> print(result.text)
"""

from collections.abc import Callable

from nexus.parsers.base import Parser
from nexus.parsers.detection import (
    decompress_content,
    detect_encoding,
    detect_mime_type,
    is_compressed,
    prepare_content_for_parsing,
)
from nexus.parsers.markitdown_parser import MarkItDownParser
from nexus.parsers.registry import ParserRegistry
from nexus.parsers.types import ImageData, ParseResult, TextChunk

def create_default_parse_fn() -> Callable[[bytes, str], bytes | None]:
    """Create a parse callback using MarkItDownParser.

    Returns a ``(content, path) -> bytes | None`` callable suitable for
    passing as ``parse_fn`` to :func:`nexus.core.virtual_views.get_parsed_content`.
    This keeps the parser import out of the kernel layer.
    """
    import asyncio

    registry = ParserRegistry()
    registry.register(MarkItDownParser())

    def _parse(content: bytes, path: str) -> bytes | None:
        processed_content, effective_path, metadata = prepare_content_for_parsing(content, path)
        parser = registry.get_parser(effective_path)
        if not parser:
            return None
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        result = loop.run_until_complete(parser.parse(processed_content, metadata))
        if result and result.text:
            return result.text.encode("utf-8")
        return None

    return _parse

__all__ = [
    "Parser",
    "ParserRegistry",
    "ParseResult",
    "TextChunk",
    "ImageData",
    "MarkItDownParser",
    # Detection utilities
    "detect_mime_type",
    "detect_encoding",
    "is_compressed",
    "decompress_content",
    "prepare_content_for_parsing",
    # Parse callback factory
    "create_default_parse_fn",
]
