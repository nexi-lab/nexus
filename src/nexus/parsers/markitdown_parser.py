"""MarkItDown parser for converting various file formats to markdown."""

import io
import logging
from pathlib import Path
from typing import Any

from nexus.core.exceptions import ParserError
from nexus.parsers.base import Parser
from nexus.parsers.types import ParseResult, TextChunk

logger = logging.getLogger(__name__)


class MarkItDownParser(Parser):
    """Parser using Microsoft's MarkItDown library.

    Supports a wide range of formats including:
    - Office documents (PDF, PowerPoint, Word, Excel)
    - Images (with OCR capabilities)
    - Audio files (with transcription)
    - Web content (HTML)
    - Structured data (CSV, JSON, XML)
    - EPub and archives

    The parser converts documents to Markdown format, preserving structure
    like headings, lists, and tables.
    """

    # Supported formats based on MarkItDown capabilities
    _SUPPORTED_FORMATS = [
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

    def __init__(self, enable_ocr: bool = False, enable_transcription: bool = False) -> None:
        """Initialize the MarkItDown parser.

        Args:
            enable_ocr: Enable OCR for images (requires tesseract)
            enable_transcription: Enable audio transcription (requires speech recognition)
        """
        self._enable_ocr = enable_ocr
        self._enable_transcription = enable_transcription
        self._markitdown: Any = None
        self._initialize_markitdown()

    def _initialize_markitdown(self) -> None:
        """Lazily initialize the MarkItDown converter."""
        try:
            from markitdown import MarkItDown

            self._markitdown = MarkItDown()
            logger.info("MarkItDown parser initialized successfully")
        except ImportError as e:
            logger.error("Failed to import MarkItDown. Install with: pip install markitdown")
            raise ParserError(
                "MarkItDown library not available. Install with: pip install markitdown",
                parser=self.name,
            ) from e

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        """Check if this parser can handle the file.

        Args:
            file_path: Path to the file
            mime_type: Optional MIME type (currently not used for MarkItDown)

        Returns:
            True if the file extension is supported
        """
        _ = mime_type  # Currently unused, but part of Parser interface
        ext = self._get_file_extension(file_path)
        return ext in self._SUPPORTED_FORMATS

    async def parse(self, content: bytes, metadata: dict[str, Any] | None = None) -> ParseResult:
        """Parse file content using MarkItDown.

        Args:
            content: Raw file content as bytes
            metadata: Optional metadata (should include 'path' or 'filename')

        Returns:
            ParseResult with markdown text and metadata

        Raises:
            ParserError: If parsing fails
        """
        if self._markitdown is None:
            raise ParserError("MarkItDown not initialized", parser=self.name)

        metadata = metadata or {}
        file_path = metadata.get("path", metadata.get("filename", "unknown"))

        try:
            # Get file extension for MarkItDown to detect format
            ext = Path(str(file_path)).suffix.lower()

            # Default to .txt for unknown/missing extensions
            if not ext:
                ext = ".txt"

            # Check if extension is supported
            if ext not in self._SUPPORTED_FORMATS:
                raise ParserError(
                    f"Unsupported file extension: '{ext}'",
                    path=str(file_path),
                    parser=self.name,
                )

            # For markdown files, just return them as-is (no conversion needed)
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

            # Create a BytesIO stream for MarkItDown
            file_stream = io.BytesIO(content)

            # MarkItDown expects a file-like object with a name attribute
            file_stream.name = f"temp{ext}"

            # Convert to markdown
            result = self._markitdown.convert_stream(file_stream)

            # Extract text content
            text_content = result.text_content if hasattr(result, "text_content") else str(result)

            # Create chunks from the markdown text
            chunks = self._create_chunks(text_content)

            # Build metadata
            parse_metadata = {
                "parser": self.name,
                "format": ext,
                "original_path": file_path,
                **metadata,
            }

            # Extract structure (basic markdown structure)
            structure = self._extract_structure(text_content)

            return ParseResult(
                text=text_content,
                metadata=parse_metadata,
                structure=structure,
                chunks=chunks,
                raw_content=text_content,  # Markdown is human-readable
            )

        except Exception as e:
            logger.error(f"Failed to parse file '{file_path}': {e}")
            raise ParserError(
                f"Failed to parse file: {e}",
                path=str(file_path),
                parser=self.name,
            ) from e

    def _create_chunks(self, text: str) -> list[TextChunk]:
        """Create semantic chunks from markdown text.

        Splits on headers and paragraphs to create meaningful chunks.

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
                # Extract heading level and text
                level = len(line) - len(line.lstrip("#"))
                heading_text = line.lstrip("#").strip()
                headings.append({"level": level, "text": heading_text})

        return {
            "headings": headings,
            "has_headings": len(headings) > 0,
            "line_count": len(lines),
        }

    @property
    def supported_formats(self) -> list[str]:
        """Get list of supported file formats.

        Returns:
            List of supported file extensions
        """
        return self._SUPPORTED_FORMATS.copy()

    @property
    def name(self) -> str:
        """Get parser name.

        Returns:
            Parser name
        """
        return "MarkItDownParser"

    @property
    def priority(self) -> int:
        """Get parser priority.

        MarkItDown is a general-purpose parser, so it has medium priority.
        Specialized parsers can override with higher priority.

        Returns:
            Priority level (50)
        """
        return 50
