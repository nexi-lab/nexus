"""MarkItDown parse provider (local fallback)."""

import io
import logging
from pathlib import Path
from typing import Any

from nexus.core.exceptions import ParserError
from nexus.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.parsers.types import ParseResult, TextChunk

logger = logging.getLogger(__name__)


class MarkItDownProvider(ParseProvider):
    """Parse provider using Microsoft's MarkItDown library.

    MarkItDown is a local parsing library that converts various document
    formats to Markdown. It serves as the default fallback provider when
    API-based providers are not available.

    Requires:
        - markitdown package: pip install markitdown[all]

    Example:
        >>> from nexus.parsers.providers import ProviderConfig
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
        except ImportError:
            logger.debug("markitdown not installed, MarkItDown provider unavailable")
            return False

    def _get_markitdown(self) -> Any:
        """Get or create the MarkItDown converter instance."""
        if self._markitdown is None:
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
        """Create semantic chunks from markdown text.

        Splits on headers to create meaningful chunks.

        Args:
            text: Markdown text content

        Returns:
            List of TextChunk objects
        """
        chunks: list[TextChunk] = []
        lines = text.split("\n")

        current_chunk: list[str] = []
        current_start = 0

        for line in lines:
            # Start new chunk on headers
            if line.startswith("#") and current_chunk:
                chunk_text = "\n".join(current_chunk).strip()
                if chunk_text:
                    chunks.append(
                        TextChunk(
                            text=chunk_text,
                            start_index=current_start,
                            end_index=current_start + len(chunk_text),
                        )
                    )
                current_chunk = [line]
                current_start += len(chunk_text) + 1
            else:
                current_chunk.append(line)

        # Add final chunk
        if current_chunk:
            chunk_text = "\n".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    TextChunk(
                        text=chunk_text,
                        start_index=current_start,
                        end_index=current_start + len(chunk_text),
                    )
                )

        return chunks if chunks else [TextChunk(text=text, start_index=0, end_index=len(text))]

    def _extract_structure(self, text: str) -> dict[str, Any]:
        """Extract document structure from markdown text.

        Args:
            text: Markdown text content

        Returns:
            Dictionary containing structure information
        """
        lines = text.split("\n")
        headings = []

        for line in lines:
            if line.startswith("#"):
                level = len(line) - len(line.lstrip("#"))
                heading_text = line.lstrip("#").strip()
                if heading_text:
                    headings.append({"level": level, "text": heading_text})

        return {
            "headings": headings,
            "has_headings": len(headings) > 0,
            "line_count": len(lines),
        }
