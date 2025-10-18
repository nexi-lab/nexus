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
]
