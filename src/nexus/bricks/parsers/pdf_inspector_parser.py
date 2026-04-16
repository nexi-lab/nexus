"""pdf-inspector parser — brick-level adapter over ``PdfInspectorProvider``.

Thin adapter so ``ParserRegistry`` (extension-keyed, used by ``create_parse_fn``
and ``AutoParseWriteHook``) can resolve ``.pdf`` when only pdf-inspector is
installed. Delegates all parse work to ``PdfInspectorProvider`` — no
duplicated logic.
"""

import logging
from typing import Any

from nexus.bricks.parsers.base import Parser
from nexus.bricks.parsers.providers.pdf_inspector_provider import PdfInspectorProvider
from nexus.bricks.parsers.types import ParseResult

logger = logging.getLogger(__name__)


class PdfInspectorParser(Parser):
    """Parser adapter wrapping ``PdfInspectorProvider``.

    Extension-keyed registration point for ``.pdf`` in the ``ParserRegistry``.
    All parsing is delegated to ``PdfInspectorProvider`` — keep business logic
    in the provider layer.
    """

    _SUPPORTED_FORMATS = [".pdf"]

    def __init__(self) -> None:
        self._provider = PdfInspectorProvider()

    @property
    def supported_formats(self) -> list[str]:
        return list(self._SUPPORTED_FORMATS)

    @property
    def priority(self) -> int:
        # Outrank MarkItDownParser (default 0) for .pdf
        return 20

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        _ = mime_type  # unused, part of Parser interface
        if not self._provider.is_available():
            return False
        ext = self._get_file_extension(file_path)
        return ext in self._SUPPORTED_FORMATS

    async def parse(self, content: bytes, metadata: dict[str, Any] | None = None) -> ParseResult:
        metadata = metadata or {}
        file_path = metadata.get("path", metadata.get("filename", "unknown.pdf"))
        return await self._provider.parse(content, str(file_path), metadata)
