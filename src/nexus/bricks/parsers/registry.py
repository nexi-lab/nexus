"""Parser registry for managing and selecting document parsers."""

import logging
from pathlib import Path

from nexus.bricks.parsers.base import Parser
from nexus.contracts.exceptions import ParserError
from nexus.lib.registry import BaseRegistry

logger = logging.getLogger(__name__)


class ParserRegistry(BaseRegistry[Parser]):
    """Registry for managing document parsers.

    Inherits generic register/get/list/clear from ``BaseRegistry`` and adds
    extension-based indexing and priority-ordered selection on top.

    Uses immutable rebuild on ``register()`` — each mutation creates new
    frozen tuples/dicts rather than mutating in place.

    Example:
        >>> registry = ParserRegistry()
        >>> registry.register(MyTextParser())
        >>> registry.register(MyPDFParser())
        >>> parser = registry.get_parser("document.pdf")
        >>> result = await parser.parse(content, metadata)
    """

    def __init__(self) -> None:
        """Initialize the parser registry."""
        super().__init__(name="parsers")
        self._parsers: tuple[Parser, ...] = ()
        self._parsers_by_extension: dict[str, tuple[Parser, ...]] = {}

    def register(self, parser: Parser, **kw: object) -> None:  # type: ignore[override]  # noqa: ARG002
        """Register a new parser.

        Args:
            parser: Parser instance to register

        Raises:
            ValueError: If parser is not a valid Parser instance
        """
        if not isinstance(parser, Parser):
            raise ValueError(f"Parser must be an instance of Parser, got {type(parser)}")

        # Store in BaseRegistry (keyed by name, overwriting by name).
        super().register(parser.name, parser, allow_overwrite=True)

        existing_parsers = tuple(p for p in self._parsers if p.name != parser.name)

        # Immutable rebuild: create new sorted tuple with added parser
        self._parsers = tuple(
            sorted([*existing_parsers, parser], key=lambda p: p.priority, reverse=True)
        )

        # Immutable rebuild: create new extension index
        new_ext_index = {
            ext: tuple(p for p in parsers if p.name != parser.name)
            for ext, parsers in self._parsers_by_extension.items()
        }
        for ext in parser.supported_formats:
            ext_lower = ext.lower()
            existing = new_ext_index.get(ext_lower, ())
            new_ext_index[ext_lower] = tuple(
                sorted([*existing, parser], key=lambda p: p.priority, reverse=True)
            )
        self._parsers_by_extension = {
            ext: parsers for ext, parsers in new_ext_index.items() if parsers
        }

        logger.info("Registered parser %r for formats: %s", parser.name, parser.supported_formats)

    def get_parser(self, file_path: str, mime_type: str | None = None) -> Parser:
        """Get the appropriate parser for a file.

        Args:
            file_path: Path to the file to parse
            mime_type: Optional MIME type of the file

        Returns:
            Parser instance capable of handling the file

        Raises:
            ParserError: If no suitable parser is found
        """
        ext = Path(file_path).suffix.lower()

        # Try parsers registered for this extension first
        if ext in self._parsers_by_extension:
            for parser in self._parsers_by_extension[ext]:
                if parser.can_parse(file_path, mime_type):
                    logger.debug("Selected parser %r for %r", parser.name, file_path)
                    return parser

        # Fall back to checking all parsers
        for parser in self._parsers:
            if parser.can_parse(file_path, mime_type):
                logger.debug("Selected parser %r for %r (fallback)", parser.name, file_path)
                return parser

        raise ParserError(
            f"No parser found for file with extension '{ext}' and MIME type '{mime_type}'",
            path=file_path,
        )

    def get_supported_formats(self) -> list[str]:
        """Get list of all supported file formats.

        Returns:
            Sorted list of supported file extensions
        """
        formats: set[str] = set()
        for parser in self._parsers:
            formats.update(parser.supported_formats)
        return sorted(formats)

    def get_parsers(self) -> list[Parser]:
        """Get all registered parsers.

        Returns:
            List of registered parser instances
        """
        return list(self._parsers)

    def unregister(self, key: str) -> Parser | None:
        """Remove a parser by name.

        Cleans up both the base registry and the domain-specific stores.
        """
        item = super().unregister(key)
        if item is not None:
            # Immutable rebuild
            self._parsers = tuple(p for p in self._parsers if p.name != key)
            self._parsers_by_extension = {
                ext: tuple(p for p in parsers if p.name != key)
                for ext, parsers in self._parsers_by_extension.items()
            }
        return item

    def clear(self) -> None:
        """Clear all registered parsers."""
        super().clear()
        self._parsers = ()
        self._parsers_by_extension = {}
        logger.info("Cleared all parsers from registry")

    def __repr__(self) -> str:
        """String representation of the registry."""
        parser_names = [p.name for p in self._parsers]
        return f"ParserRegistry(parsers={parser_names})"
