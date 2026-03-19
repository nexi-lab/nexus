"""MarkItDown parse provider (local fallback)."""

import io
import logging
import threading
from pathlib import Path
from typing import Any

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.bricks.parsers.types import ParseResult, TextChunk
from nexus.bricks.parsers.utils import create_chunks, extract_structure
from nexus.contracts.exceptions import ParserError

logger = logging.getLogger(__name__)


class MarkItDownProvider(ParseProvider):
    """Parse provider using Microsoft's MarkItDown library.

    MarkItDown is a local parsing library that converts various document
    formats to Markdown. It serves as the default fallback provider when
    API-based providers are not available.

    Requires:
        - markitdown package: pip install markitdown[all]

    Example:
        >>> from nexus.bricks.parsers.providers import ProviderConfig
        >>> config = ProviderConfig(
        ...     name="markitdown",
        ...     priority=10,  # Low priority (fallback)
        ... )
        >>> provider = MarkItDownProvider(config)
        >>> result = await provider.parse(content, "document.pdf")
    """

    # Supported formats based on MarkItDown capabilities
    DEFAULT_FORMATS = [
        # Office documents
        ".pdf",
        ".pptx",
        ".ppt",
        ".docx",
        ".doc",
        ".xlsx",
        ".xls",
        # Images
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        # Web and structured data
        ".html",
        ".htm",
        ".xml",
        ".json",
        ".csv",
        # EPub and archives
        ".epub",
        ".zip",
        # Text
        ".txt",
        ".md",
        ".markdown",
    ]

    def __init__(self, config: ProviderConfig | None = None) -> None:
        """Initialize the MarkItDown provider.

        Args:
            config: Provider configuration
        """
        super().__init__(config)
        self._markitdown: Any = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "markitdown"

    @property
    def default_formats(self) -> list[str]:
        return self.DEFAULT_FORMATS.copy()

    def is_available(self) -> bool:
        """Check if MarkItDown provider is available.

        Returns True if markitdown is installed.
        """
        try:
            from markitdown import MarkItDown  # noqa: F401

            return True
        except Exception:
            logger.debug("markitdown not available, MarkItDown provider unavailable")
            return False

    def _get_markitdown(self) -> Any:
        """Get or create the MarkItDown converter instance (thread-safe)."""
        if self._markitdown is not None:
            return self._markitdown
        with self._init_lock:
            if self._markitdown is not None:
                return self._markitdown
            from markitdown import MarkItDown

            self._markitdown = MarkItDown()
        return self._markitdown

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse document using MarkItDown.

        Args:
            content: Raw file content as bytes
            file_path: Original file path (for format detection)
            metadata: Optional metadata about the file

        Returns:
            ParseResult containing extracted text and structure

        Raises:
            ParserError: If parsing fails
        """
        metadata = metadata or {}
        ext = Path(file_path).suffix.lower()

        try:
            # For markdown files, just return them as-is
            if ext in [".md", ".markdown"]:
                text_content = content.decode("utf-8", errors="replace")
                chunks = self._create_chunks(text_content)
                structure = self._extract_structure(text_content)

                return ParseResult(
                    text=text_content,
                    metadata={
                        "parser": self.name,
                        "format": ext,
                        "original_path": file_path,
                        **metadata,
                    },
                    structure=structure,
                    chunks=chunks,
                    raw_content=text_content,
                )

            # For plain text, decode and return
            if ext == ".txt":
                text_content = content.decode("utf-8", errors="replace")
                return ParseResult(
                    text=text_content,
                    metadata={
                        "parser": self.name,
                        "format": ext,
                        "original_path": file_path,
                        **metadata,
                    },
                    structure={"line_count": len(text_content.split("\n"))},
                    chunks=[
                        TextChunk(text=text_content, start_index=0, end_index=len(text_content))
                    ],
                    raw_content=text_content,
                )

            # Use MarkItDown for other formats
            markitdown = self._get_markitdown()

            # Create a BytesIO stream with a name attribute
            file_stream = io.BytesIO(content)
            file_stream.name = f"temp{ext}"

            # Convert to markdown
            result = markitdown.convert_stream(file_stream)

            # Extract text content
            text_content = result.text_content if hasattr(result, "text_content") else str(result)

            # Create chunks from the markdown text
            chunks = self._create_chunks(text_content)

            # Extract structure
            structure = self._extract_structure(text_content)

            return ParseResult(
                text=text_content,
                metadata={
                    "parser": self.name,
                    "format": ext,
                    "original_path": file_path,
                    **metadata,
                },
                structure=structure,
                chunks=chunks,
                raw_content=text_content,
            )

        except Exception as e:
            if isinstance(e, ParserError):
                raise
            raise ParserError(
                f"Failed to parse with MarkItDown: {e}",
                path=file_path,
                parser=self.name,
            ) from e

    def _create_chunks(self, text: str) -> list[TextChunk]:
        """Delegate to shared ``create_chunks`` utility."""
        return create_chunks(text)

    def _extract_structure(self, text: str) -> dict[str, Any]:
        """Delegate to shared ``extract_structure`` utility."""
        return extract_structure(text)
