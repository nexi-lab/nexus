"""Abstract base class for document parsers."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from nexus.parsers.types import ParseResult


class Parser(ABC):
    """Abstract base class for all document parsers.

    Parsers are responsible for extracting structured data from various
    file formats. Each parser should implement format-specific parsing logic
    while maintaining a consistent interface.

    Example:
        >>> class MyParser(Parser):
        ...     def can_parse(self, file_path: str, mime_type: str) -> bool:
        ...         return file_path.endswith('.txt')
        ...
        ...     async def parse(self, content: bytes, metadata: dict) -> ParseResult:
        ...         text = content.decode('utf-8')
        ...         return ParseResult(text=text, metadata=metadata)
        ...
        ...     @property
        ...     def supported_formats(self) -> list[str]:
        ...         return ['.txt']
    """

    @abstractmethod
    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        """Check if this parser can handle the given file.

        Args:
            file_path: Path to the file to be parsed
            mime_type: Optional MIME type of the file

        Returns:
            True if this parser can handle the file, False otherwise
        """
        pass

    @abstractmethod
    async def parse(self, content: bytes, metadata: dict[str, Any] | None = None) -> ParseResult:
        """Parse file content and return structured data.

        Args:
            content: Raw file content as bytes
            metadata: Optional metadata about the file (path, MIME type, etc.)

        Returns:
            ParseResult containing extracted text, metadata, structure, etc.

        Raises:
            ParserError: If parsing fails
        """
        pass

    @property
    @abstractmethod
    def supported_formats(self) -> list[str]:
        """List of supported file extensions.

        Returns:
            List of file extensions (with dots) that this parser supports.
            Example: ['.pdf', '.docx', '.txt']
        """
        pass

    @property
    def name(self) -> str:
        """Get the parser name.

        Returns:
            The parser class name by default. Can be overridden.
        """
        return self.__class__.__name__

    @property
    def priority(self) -> int:
        """Get the parser priority.

        Higher priority parsers are tried first when multiple parsers
        support the same format. Default is 0.

        Returns:
            Priority level (default: 0)
        """
        return 0

    def _get_file_extension(self, file_path: str) -> str:
        """Helper method to extract file extension.

        Args:
            file_path: Path to the file

        Returns:
            File extension including the dot (e.g., '.pdf')
        """
        return Path(file_path).suffix.lower()
