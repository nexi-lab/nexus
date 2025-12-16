"""Base class for parse providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from nexus.parsers.types import ParseResult


@dataclass
class ProviderConfig:
    """Configuration for a parse provider.

    Attributes:
        name: Provider name (e.g., "unstructured", "llamaparse", "markitdown")
        enabled: Whether the provider is enabled
        priority: Priority for provider selection (higher = preferred)
        api_key: API key for the provider (if required)
        api_url: API endpoint URL (if configurable)
        supported_formats: List of supported file extensions (e.g., [".pdf", ".docx"])
                          If None, uses provider's default supported formats
        extra: Additional provider-specific configuration
    """

    name: str
    enabled: bool = True
    priority: int = 50
    api_key: str | None = None
    api_url: str | None = None
    supported_formats: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ParseProvider(ABC):
    """Abstract base class for parse providers.

    A parse provider converts document content to structured text (typically markdown).
    Providers can be local (MarkItDown) or API-based (Unstructured, LlamaParse).

    Example:
        >>> class MyProvider(ParseProvider):
        ...     @property
        ...     def name(self) -> str:
        ...         return "my_provider"
        ...
        ...     @property
        ...     def default_formats(self) -> list[str]:
        ...         return [".pdf", ".docx"]
        ...
        ...     async def parse(self, content, path, metadata) -> ParseResult:
        ...         # Parse content and return result
        ...         pass
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        """Initialize the provider.

        Args:
            config: Provider configuration. If None, uses defaults.
        """
        self._config = config or ProviderConfig(name=self.name)

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name identifier."""
        ...

    @property
    @abstractmethod
    def default_formats(self) -> list[str]:
        """Default list of supported file extensions."""
        ...

    @property
    def supported_formats(self) -> list[str]:
        """Get supported file formats.

        Returns configured formats if set, otherwise default formats.
        """
        if self._config.supported_formats:
            return self._config.supported_formats
        return self.default_formats

    @property
    def priority(self) -> int:
        """Get provider priority."""
        return self._config.priority

    @property
    def enabled(self) -> bool:
        """Check if provider is enabled."""
        return self._config.enabled

    @property
    def config(self) -> ProviderConfig:
        """Get provider configuration."""
        return self._config

    def can_parse(self, file_path: str) -> bool:
        """Check if this provider can parse the given file.

        Args:
            file_path: Path to the file

        Returns:
            True if the file extension is supported
        """
        if not self.enabled:
            return False

        ext = self._get_extension(file_path)
        return ext in self.supported_formats

    def _get_extension(self, file_path: str) -> str:
        """Extract file extension from path."""
        from pathlib import Path

        return Path(file_path).suffix.lower()

    @abstractmethod
    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse document content.

        Args:
            content: Raw file content as bytes
            file_path: Original file path (for format detection)
            metadata: Optional metadata about the file

        Returns:
            ParseResult containing extracted text and structure

        Raises:
            ParserError: If parsing fails
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is available (dependencies installed, API key set, etc.).

        Returns:
            True if the provider can be used
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, priority={self.priority}, enabled={self.enabled})"
