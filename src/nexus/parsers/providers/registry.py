"""Provider registry for managing parse providers."""

import logging
import os
from typing import Any

from nexus.core.exceptions import ParserError
from nexus.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.parsers.types import ParseResult

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry for managing parse providers.

    The registry maintains a list of parse providers and selects the best
    available provider for each file type based on priority and availability.

    Example:
        >>> registry = ProviderRegistry()
        >>> registry.auto_discover()
        >>>
        >>> # Get best provider for a file
        >>> provider = registry.get_provider("/path/to/file.pdf")
        >>>
        >>> # Parse with automatic provider selection
        >>> result = await registry.parse("/path/to/file.pdf", content)
    """

    def __init__(self) -> None:
        """Initialize the provider registry."""
        self._providers: list[ParseProvider] = []
        self._providers_by_name: dict[str, ParseProvider] = {}

    def register(self, provider: ParseProvider) -> None:
        """Register a parse provider.

        Args:
            provider: Provider instance to register

        Raises:
            ValueError: If provider is not a valid ParseProvider instance
        """
        if not isinstance(provider, ParseProvider):
            raise ValueError(f"Provider must be a ParseProvider instance, got {type(provider)}")

        # Check if provider is available
        if not provider.is_available():
            logger.debug(f"Provider '{provider.name}' is not available, skipping registration")
            return

        self._providers.append(provider)
        self._providers_by_name[provider.name] = provider

        # Sort by priority (highest first)
        self._providers.sort(key=lambda p: p.priority, reverse=True)

        logger.info(
            f"Registered provider '{provider.name}' with priority {provider.priority}, "
            f"formats: {provider.supported_formats}"
        )

    def get_provider(self, file_path: str) -> ParseProvider | None:
        """Get the best available provider for a file.

        Selects the highest priority provider that can parse the file.

        Args:
            file_path: Path to the file

        Returns:
            Best matching provider, or None if no provider can handle the file
        """
        for provider in self._providers:
            if provider.can_parse(file_path):
                logger.debug(f"Selected provider '{provider.name}' for '{file_path}'")
                return provider

        logger.debug(f"No provider found for '{file_path}'")
        return None

    def get_provider_by_name(self, name: str) -> ParseProvider | None:
        """Get a provider by name.

        Args:
            name: Provider name

        Returns:
            Provider instance, or None if not found
        """
        return self._providers_by_name.get(name)

    async def parse(
        self,
        file_path: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
        provider_name: str | None = None,
    ) -> ParseResult:
        """Parse a file using the best available provider.

        Args:
            file_path: Path to the file (for format detection)
            content: Raw file content as bytes
            metadata: Optional metadata about the file
            provider_name: Optional specific provider to use

        Returns:
            ParseResult containing extracted text and structure

        Raises:
            ParserError: If no suitable provider found or parsing fails
        """
        # Use specific provider if requested
        if provider_name:
            provider = self.get_provider_by_name(provider_name)
            if not provider:
                raise ParserError(f"Provider '{provider_name}' not found", path=file_path)
            if not provider.can_parse(file_path):
                raise ParserError(
                    f"Provider '{provider_name}' does not support this file type",
                    path=file_path,
                )
        else:
            provider = self.get_provider(file_path)

        if not provider:
            raise ParserError(
                "No provider available for file type",
                path=file_path,
            )

        try:
            result = await provider.parse(content, file_path, metadata)
            # Add provider info to metadata
            result.metadata["provider"] = provider.name
            return result
        except Exception as e:
            logger.error(f"Provider '{provider.name}' failed to parse '{file_path}': {e}")
            raise ParserError(
                f"Parsing failed: {e}",
                path=file_path,
                parser=provider.name,
            ) from e

    def get_all_providers(self) -> list[ParseProvider]:
        """Get all registered providers.

        Returns:
            List of registered providers sorted by priority
        """
        return self._providers.copy()

    def get_supported_formats(self) -> list[str]:
        """Get all supported file formats across all providers.

        Returns:
            Sorted list of unique file extensions
        """
        formats = set()
        for provider in self._providers:
            formats.update(provider.supported_formats)
        return sorted(formats)

    def auto_discover(
        self,
        configs: list[ProviderConfig] | None = None,
    ) -> int:
        """Auto-discover and register available providers.

        Attempts to load and register all known providers based on
        availability (dependencies, API keys, etc.).

        Args:
            configs: Optional list of provider configurations.
                    If None, uses environment-based auto-discovery.

        Returns:
            Number of providers successfully registered
        """
        registered = 0

        # Build config lookup
        config_map: dict[str, ProviderConfig] = {}
        if configs:
            for cfg in configs:
                config_map[cfg.name] = cfg

        # Try to register Unstructured provider
        try:
            from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

            config = config_map.get("unstructured")
            if not config:
                # Auto-discover from environment
                api_key = os.getenv("UNSTRUCTURED_API_KEY")
                api_url = os.getenv(
                    "UNSTRUCTURED_WORKFLOW_ENDPOINT",
                    "https://api.unstructuredapp.io/general/v0/general",
                )
                if api_key:
                    config = ProviderConfig(
                        name="unstructured",
                        priority=100,  # Highest priority when available
                        api_key=api_key,
                        api_url=api_url,
                    )

            if config:
                unstructured_provider = UnstructuredProvider(config)
                if unstructured_provider.is_available():
                    self.register(unstructured_provider)
                    registered += 1
        except ImportError as e:
            logger.debug(f"Unstructured provider not available: {e}")

        # Try to register LlamaParse provider
        try:
            from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

            config = config_map.get("llamaparse")
            if not config:
                # Auto-discover from environment
                api_key = os.getenv("LLAMA_CLOUD_API_KEY")
                if api_key:
                    config = ProviderConfig(
                        name="llamaparse",
                        priority=90,  # Second priority
                        api_key=api_key,
                    )

            if config:
                llamaparse_provider = LlamaParseProvider(config)
                if llamaparse_provider.is_available():
                    self.register(llamaparse_provider)
                    registered += 1
        except ImportError as e:
            logger.debug(f"LlamaParse provider not available: {e}")

        # Always register MarkItDown as fallback
        try:
            from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

            config = config_map.get("markitdown", ProviderConfig(name="markitdown", priority=10))
            markitdown_provider = MarkItDownProvider(config)
            if markitdown_provider.is_available():
                self.register(markitdown_provider)
                registered += 1
        except ImportError as e:
            logger.warning(f"MarkItDown provider not available: {e}")

        logger.info(f"Auto-discovered {registered} parse providers")
        return registered

    def clear(self) -> None:
        """Clear all registered providers."""
        self._providers.clear()
        self._providers_by_name.clear()
        logger.debug("Cleared all providers from registry")

    def __repr__(self) -> str:
        provider_names = [p.name for p in self._providers]
        return f"ProviderRegistry(providers={provider_names})"
