"""Unstructured.io parse provider."""

import logging
from typing import Any

from nexus.core.exceptions import ParserError
from nexus.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.parsers.types import ParseResult, TextChunk

logger = logging.getLogger(__name__)


class UnstructuredProvider(ParseProvider):
    """Parse provider using Unstructured.io API.

    Unstructured.io provides high-quality document parsing with support for
    a wide range of formats including PDFs, Office documents, images, and more.

    Requires:
        - UNSTRUCTURED_API_KEY environment variable or api_key in config
        - Optional: UNSTRUCTURED_WORKFLOW_ENDPOINT for custom endpoint

    Example:
        >>> from nexus.parsers.providers import ProviderConfig
        >>> config = ProviderConfig(
        ...     name="unstructured",
        ...     api_key="your-api-key",
        ...     priority=100,
        ... )
        >>> provider = UnstructuredProvider(config)
        >>> result = await provider.parse(content, "document.pdf")
    """

    # Comprehensive list of supported formats from Unstructured.io documentation
    # https://docs.unstructured.io/api-reference/supported-file-types
    DEFAULT_FORMATS = [
        # Documents
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".xlsx",
        ".xls",
        ".odt",
        ".rtf",
        # Text & Markup
        ".txt",
        ".md",
        ".rst",
        ".xml",
        ".html",
        ".htm",
        ".org",
        # Images (with OCR)
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".heic",
        ".tiff",
        ".tif",
        # Data formats
        ".csv",
        ".tsv",
        ".json",
        # Email
        ".eml",
        ".msg",
        # E-books
        ".epub",
    ]

    def __init__(self, config: ProviderConfig | None = None) -> None:
        """Initialize the Unstructured provider.

        Args:
            config: Provider configuration with api_key and optional api_url
        """
        super().__init__(config)
        self._api_key = config.api_key if config else None
        self._api_url = (
            config.api_url
            if config and config.api_url
            else "https://api.unstructuredapp.io/general/v0/general"
        )

    @property
    def name(self) -> str:
        return "unstructured"

    @property
    def default_formats(self) -> list[str]:
        return self.DEFAULT_FORMATS.copy()

    def is_available(self) -> bool:
        """Check if Unstructured provider is available.

        Returns True if API key is set and httpx is available.
        """
        if not self._api_key:
            logger.debug("Unstructured API key not configured")
            return False

        try:
            import httpx  # noqa: F401

            return True
        except ImportError:
            logger.debug("httpx not installed, Unstructured provider unavailable")
            return False

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse document using Unstructured.io API.

        Args:
            content: Raw file content as bytes
            file_path: Original file path (for format detection)
            metadata: Optional metadata about the file

        Returns:
            ParseResult containing extracted text and structure

        Raises:
            ParserError: If parsing fails
        """
        from pathlib import Path

        import httpx

        metadata = metadata or {}
        filename = Path(file_path).name

        try:
            # Prepare multipart form data
            files = {
                "files": (filename, content),
            }

            # API parameters
            # Note: output_format only supports 'application/json' or 'text/csv'
            # We use JSON and convert to markdown ourselves
            data = {
                "strategy": "auto",  # auto, fast, hi_res, ocr_only
            }

            # Make API request
            assert self._api_key is not None, "API key must be set"
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    self._api_url,
                    headers={
                        "unstructured-api-key": self._api_key,
                        "accept": "application/json",
                    },
                    files=files,
                    data=data,
                )

                if response.status_code != 200:
                    error_msg = f"Unstructured API error: {response.status_code}"
                    try:
                        error_data = response.json()
                        if "detail" in error_data:
                            error_msg = f"{error_msg} - {error_data['detail']}"
                    except Exception:
                        error_msg = f"{error_msg} - {response.text[:200]}"
                    raise ParserError(error_msg, path=file_path, parser=self.name)

                # Parse response
                elements = response.json()

            # Convert elements to text
            text_parts = []
            chunks = []
            current_pos = 0

            for element in elements:
                element_type = element.get("type", "")
                element_text = element.get("text", "")

                if not element_text:
                    continue

                # Format based on element type
                if element_type == "Title":
                    formatted = f"# {element_text}\n\n"
                elif element_type == "Header":
                    formatted = f"## {element_text}\n\n"
                elif element_type == "ListItem":
                    formatted = f"- {element_text}\n"
                elif element_type == "Table":
                    formatted = f"\n{element_text}\n\n"
                elif element_type == "FigureCaption":
                    formatted = f"*{element_text}*\n\n"
                else:
                    formatted = f"{element_text}\n\n"

                text_parts.append(formatted)

                # Create chunk
                chunk_start = current_pos
                current_pos += len(formatted)
                chunks.append(
                    TextChunk(
                        text=element_text,
                        start_index=chunk_start,
                        end_index=current_pos,
                        metadata={
                            "type": element_type,
                            "element_id": element.get("element_id"),
                        },
                    )
                )

            full_text = "".join(text_parts).strip()

            # Extract structure from elements
            structure = self._extract_structure(elements)

            return ParseResult(
                text=full_text,
                metadata={
                    "parser": self.name,
                    "format": Path(file_path).suffix.lower(),
                    "original_path": file_path,
                    "element_count": len(elements),
                    **metadata,
                },
                structure=structure,
                chunks=chunks if chunks else [],
                raw_content=full_text,
            )

        except httpx.TimeoutException as e:
            raise ParserError(
                f"Unstructured API timeout: {e}",
                path=file_path,
                parser=self.name,
            ) from e
        except httpx.HTTPError as e:
            raise ParserError(
                f"Unstructured API error: {e}",
                path=file_path,
                parser=self.name,
            ) from e
        except Exception as e:
            if isinstance(e, ParserError):
                raise
            raise ParserError(
                f"Failed to parse with Unstructured: {e}",
                path=file_path,
                parser=self.name,
            ) from e

    def _extract_structure(self, elements: list[dict]) -> dict[str, Any]:
        """Extract document structure from Unstructured elements.

        Args:
            elements: List of parsed elements from API

        Returns:
            Structure dictionary with headings, sections, etc.
        """
        headings = []
        element_types: dict[str, int] = {}

        for element in elements:
            element_type = element.get("type", "Unknown")
            element_types[element_type] = element_types.get(element_type, 0) + 1

            if element_type in ("Title", "Header"):
                level = 1 if element_type == "Title" else 2
                headings.append(
                    {
                        "level": level,
                        "text": element.get("text", ""),
                    }
                )

        return {
            "headings": headings,
            "has_headings": len(headings) > 0,
            "element_count": len(elements),
            "element_types": element_types,
        }
