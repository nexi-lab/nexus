"""Parse providers for document parsing.

This module provides a provider-based parsing system that supports multiple
parsing backends:
- UnstructuredProvider: Uses Unstructured.io API
- LlamaParseProvider: Uses LlamaParse API
- MarkItDownProvider: Local parsing with MarkItDown (fallback)

Example:
    >>> from nexus.parsers.providers import ProviderRegistry
    >>>
    >>> registry = ProviderRegistry()
    >>> registry.auto_discover()  # Discovers and registers available providers
    >>>
    >>> # Parse with best available provider
    >>> result = await registry.parse("/path/to/file.pdf", content)
"""

from nexus.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.parsers.providers.registry import ProviderRegistry

__all__ = [
    "ParseProvider",
    "ProviderConfig",
    "ProviderRegistry",
]
