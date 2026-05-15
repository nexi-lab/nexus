"""Parse providers for document parsing.

This module provides a provider-based parsing system that supports multiple
parsing backends:
- UnstructuredProvider: Uses Unstructured.io API
- LlamaParseProvider: Uses LlamaParse API
- PdfInspectorProvider: Local PDF parsing with pdf-inspector (Rust + PyO3)

Example:
    >>> from nexus.bricks.parsers.providers import ProviderRegistry
    >>>
    >>> registry = ProviderRegistry()
    >>> registry.auto_discover()  # Discovers and registers available providers
    >>>
    >>> # Parse with best available provider
    >>> result = await registry.parse("/path/to/file.pdf", content)
"""

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.bricks.parsers.providers.registry import ProviderRegistry

__all__ = [
    "ParseProvider",
    "ProviderConfig",
    "ProviderRegistry",
]
