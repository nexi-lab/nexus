"""ParsersBrick — single entry point for document parsing (Issue #1523).

Follows the ``pay/`` exemplary brick pattern:
- Zero runtime imports from ``nexus.core``
- Constructor injection for configuration
- Provides ``parse_fn`` callback for virtual views
- Owns both ``ParserRegistry`` and ``ProviderRegistry``
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nexus.bricks.parsers.detection import prepare_content_for_parsing
from nexus.bricks.parsers.markitdown_parser import MarkItDownParser
from nexus.bricks.parsers.pdf_inspector_parser import PdfInspectorParser
from nexus.bricks.parsers.providers.base import ProviderConfig
from nexus.bricks.parsers.providers.registry import ProviderRegistry
from nexus.bricks.parsers.registry import ParserRegistry

logger = logging.getLogger(__name__)


class ParsersBrick:
    """Facade for document parsing — owns both registries.

    Following the ``pay/`` exemplary brick pattern:
    - Zero imports from ``nexus.core``
    - Constructor injection for config
    - Provides ``parse_fn`` callback for virtual views
    - Shares a single ``MarkItDownParser`` across registries

    Example::

        brick = ParsersBrick()
        parse_fn = brick.create_parse_fn()
        result = parse_fn(raw_bytes, "document.pdf")
    """

    def __init__(
        self,
        parsing_config: Any | None = None,
    ) -> None:
        """Initialize the ParsersBrick.

        Args:
            parsing_config: Optional ParseConfig (or duck-typed equivalent)
                with ``providers`` and ``auto_parse`` fields.
        """
        # Parser registry — extension-based selection.
        # PdfInspectorParser registered first so it wins priority sort for .pdf.
        # MarkItDownParser covers non-PDF formats (when markitdown is installed).
        self._parser_registry = ParserRegistry()
        self._parser_registry.register(PdfInspectorParser())
        self._parser_registry.register(MarkItDownParser())

        # Provider registry — API-provider selection
        self._provider_registry = ProviderRegistry()

        if parsing_config is not None:
            raw_providers = (
                [dict(p) for p in parsing_config.providers]
                if getattr(parsing_config, "providers", None)
                else None
            )
            if raw_providers:
                configs = [
                    ProviderConfig(
                        name=p.get("name", "unknown"),
                        enabled=p.get("enabled", True),
                        priority=p.get("priority", 50),
                        api_key=p.get("api_key"),
                        api_url=p.get("api_url"),
                        supported_formats=p.get("supported_formats"),
                    )
                    for p in raw_providers
                ]
                self._provider_registry.auto_discover(configs)
            else:
                self._provider_registry.auto_discover()
        else:
            self._provider_registry.auto_discover()

    @property
    def parser_registry(self) -> ParserRegistry:
        """Extension-based parser registry."""
        return self._parser_registry

    @property
    def provider_registry(self) -> ProviderRegistry:
        """API-provider registry with priority ordering."""
        return self._provider_registry

    def create_parse_fn(self) -> Callable[[bytes, str], bytes | None]:
        """Create a sync parse callback for virtual views.

        Uses the shared ``ParserRegistry`` (no redundant MarkItDown instance).
        Detects whether a running event loop already exists and delegates via
        ``run_in_executor`` when it does (Issue #13A).

        Returns:
            ``(content: bytes, path: str) -> bytes | None`` callable.
        """
        registry = self._parser_registry

        def _parse(content: bytes, path: str) -> bytes | None:
            processed_content, effective_path, metadata = prepare_content_for_parsing(content, path)
            parser = registry.get_parser(effective_path)
            if not parser:
                return None

            coro = parser.parse(processed_content, metadata)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is None:
                # No event loop — safe to use asyncio.run()
                result = asyncio.run(coro)
            else:
                # Already inside an event loop — run in a worker thread
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, coro)
                    result = future.result()

            if result and result.text:
                return result.text.encode("utf-8")
            return None

        return _parse
