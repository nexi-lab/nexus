"""pdf-inspector parse provider (default local PDF provider)."""

import logging
import threading
from typing import Any

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.bricks.parsers.types import ParseResult

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

            return pdf_inspector is not None
        except Exception:
            logger.debug("pdf_inspector not available, PdfInspectorProvider unavailable")
            return False

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse document using pdf-inspector. Implemented in Task 5."""
        raise NotImplementedError("parse() will be implemented in Task 5")
