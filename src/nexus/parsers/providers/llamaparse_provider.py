"""LlamaParse parse provider."""

import logging
from typing import Any

from nexus.core.exceptions import ParserError
from nexus.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.parsers.types import ParseResult, TextChunk

logger = logging.getLogger(__name__)


class LlamaParseProvider(ParseProvider):
    """Parse provider using LlamaParse API.

    LlamaParse is a GenAI-native document parsing platform from LlamaIndex
    that excels at parsing complex documents with tables, charts, and figures.

    Requires:
        - LLAMA_CLOUD_API_KEY environment variable or api_key in config
        - llama-parse package: pip install llama-parse

    Example:
        >>> from nexus.parsers.providers import ProviderConfig
        >>> config = ProviderConfig(
        ...     name="llamaparse",
        ...     api_key="your-api-key",
        ...     priority=90,
        ... )
        >>> provider = LlamaParseProvider(config)
        >>> result = await provider.parse(content, "document.pdf")
    """

    # LlamaParse supported formats
    # https://docs.llamaindex.ai/en/stable/llama_cloud/llama_parse/
    DEFAULT_FORMATS = [
        # Primary formats (best support)
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".xlsx",
        ".xls",
        # Additional formats
        ".html",
        ".htm",
        ".txt",
        ".md",
        ".rtf",
        ".epub",
        # Images (with vision models)
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
    ]

    def __init__(self, config: ProviderConfig | None = None) -> None:
        """Initialize the LlamaParse provider.

        Args:
            config: Provider configuration with api_key
        """
        super().__init__(config)
        self._api_key = config.api_key if config else None
        self._parser = None

    @property
    def name(self) -> str:
        return "llamaparse"

    @property
    def default_formats(self) -> list[str]:
        return self.DEFAULT_FORMATS.copy()

    def is_available(self) -> bool:
        """Check if LlamaParse provider is available.

        Returns True if API key is set and llama-parse is installed.
        """
        if not self._api_key:
            logger.debug("LlamaParse API key not configured")
            return False

        try:
            from llama_parse import LlamaParse  # noqa: F401

            return True
        except ImportError:
            logger.debug("llama-parse not installed, LlamaParse provider unavailable")
            return False

    def _get_parser(self) -> Any:
        """Get or create the LlamaParse parser instance."""
        if self._parser is None:
            from llama_parse import LlamaParse

            self._parser = LlamaParse(
                api_key=self._api_key,
                result_type="markdown",
                verbose=False,
            )
        return self._parser

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse document using LlamaParse API.

        Args:
            content: Raw file content as bytes
            file_path: Original file path (for format detection)
            metadata: Optional metadata about the file

        Returns:
            ParseResult containing extracted text and structure

        Raises:
            ParserError: If parsing fails
        """
        import asyncio
        import tempfile
        from pathlib import Path

        metadata = metadata or {}
        ext = Path(file_path).suffix.lower()

        try:
            parser = self._get_parser()

            # LlamaParse requires a file path, so write to temp file
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            try:
                # Parse the document
                # LlamaParse has both sync and async APIs
                # Use async if available
                if hasattr(parser, "aload_data"):
                    documents = await parser.aload_data(tmp_path)
                else:
                    # Fall back to sync in thread
                    documents = await asyncio.to_thread(parser.load_data, tmp_path)
            finally:
                # Clean up temp file
                Path(tmp_path).unlink(missing_ok=True)

            if not documents:
                return ParseResult(
                    text="",
                    metadata={
                        "parser": self.name,
                        "format": ext,
                        "original_path": file_path,
                        "warning": "No content extracted",
                        **metadata,
                    },
                )

            # Combine all document texts
            text_parts = []
            chunks = []
            current_pos = 0

            for i, doc in enumerate(documents):
                doc_text = doc.text if hasattr(doc, "text") else str(doc)

                if doc_text:
                    text_parts.append(doc_text)

                    # Create chunk for each document/page
                    chunk_start = current_pos
                    current_pos += len(doc_text) + 2  # +2 for separator
                    chunks.append(
                        TextChunk(
                            text=doc_text,
                            start_index=chunk_start,
                            end_index=current_pos,
                            metadata={
                                "page": i + 1,
                                "doc_id": getattr(doc, "doc_id", None),
                            },
                        )
                    )

            full_text = "\n\n".join(text_parts).strip()

            # Extract structure from markdown
            structure = self._extract_structure(full_text)

            return ParseResult(
                text=full_text,
                metadata={
                    "parser": self.name,
                    "format": ext,
                    "original_path": file_path,
                    "page_count": len(documents),
                    **metadata,
                },
                structure=structure,
                chunks=chunks if chunks else [],
                raw_content=full_text,
            )

        except Exception as e:
            if isinstance(e, ParserError):
                raise
            raise ParserError(
                f"Failed to parse with LlamaParse: {e}",
                path=file_path,
                parser=self.name,
            ) from e

    def _extract_structure(self, text: str) -> dict[str, Any]:
        """Extract document structure from markdown text.

        Args:
            text: Markdown text content

        Returns:
            Structure dictionary with headings info
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
