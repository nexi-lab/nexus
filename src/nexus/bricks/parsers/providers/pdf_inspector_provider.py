"""pdf-inspector parse provider (default local PDF provider)."""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.bricks.parsers.types import ParseResult
from nexus.bricks.parsers.utils import create_chunks, extract_structure
from nexus.contracts.exceptions import ParserError

logger = logging.getLogger(__name__)


class PdfInspectorProvider(ParseProvider):
    """Parse provider using pdf-inspector (Rust + PyO3).

    Fast text-based PDF extraction with Markdown output, table/heading
    detection, and per-page OCR-need classification. Surfaces
    ``pages_needing_ocr`` in metadata so future smart-routing layers
    can re-process scanned pages with an OCR-capable provider.

    Requires:
        - pdf-inspector package: pip install pdf-inspector

    Example:
        >>> from nexus.bricks.parsers.providers import ProviderConfig
        >>> config = ProviderConfig(name="pdf-inspector", priority=20)
        >>> provider = PdfInspectorProvider(config)
        >>> result = await provider.parse(content, "document.pdf")
    """

    DEFAULT_FORMATS = [".pdf"]

    def __init__(self, config: ProviderConfig | None = None) -> None:
        super().__init__(config)
        self._inspector: Any = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "pdf-inspector"

    @property
    def default_formats(self) -> list[str]:
        return self.DEFAULT_FORMATS.copy()

    def is_available(self) -> bool:
        try:
            import pdf_inspector  # noqa: F401

            return True
        except ImportError:
            logger.debug("pdf_inspector not available, PdfInspectorProvider unavailable")
            return False

    def _get_inspector(self) -> Any:
        """Get or create the pdf_inspector module reference (thread-safe)."""
        if self._inspector is not None:
            return self._inspector
        with self._init_lock:
            if self._inspector is not None:
                return self._inspector
            import pdf_inspector

            self._inspector = pdf_inspector
        return self._inspector

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse a PDF using pdf-inspector.

        Args:
            content: Raw PDF bytes.
            file_path: Original file path (used in metadata + errors).
            metadata: Optional caller metadata, merged into the result.

        Returns:
            ParseResult with markdown text, chunks, structure, and OCR-need
            flags in ``metadata``.

        Raises:
            ParserError: If pdf-inspector fails to process the bytes.
        """
        metadata = metadata or {}
        ext = Path(file_path).suffix.lower()
        inspector = self._get_inspector()

        try:
            # process_pdf_bytes is sync (PyO3); run off the event loop.
            result = await asyncio.to_thread(inspector.process_pdf_bytes, content)
        except Exception as e:
            raise ParserError(
                f"Failed to parse PDF with pdf-inspector: {e}",
                path=file_path,
                parser=self.name,
            ) from e

        text_content = result.markdown or ""
        pages_needing_ocr = list(result.pages_needing_ocr)

        return ParseResult(
            text=text_content,
            metadata={
                "parser": self.name,
                "format": ext,
                "original_path": file_path,
                "pdf_type": result.pdf_type,
                "pages_needing_ocr": pages_needing_ocr,
                "requires_ocr": bool(pages_needing_ocr),
                "has_encoding_issues": bool(result.has_encoding_issues),
                **metadata,
            },
            structure=extract_structure(text_content),
            chunks=create_chunks(text_content),
            raw_content=text_content,
        )
