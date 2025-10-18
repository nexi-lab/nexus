"""Parser registry for managing and selecting document parsers."""

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path

from nexus.core.exceptions import ParserError
from nexus.parsers.base import Parser

logger = logging.getLogger(__name__)


class ParserRegistry:
    """Registry for managing document parsers.

    The registry allows registering multiple parsers and automatically
    selecting the appropriate parser based on file extension and MIME type.

    Example:
        >>> registry = ParserRegistry()
        >>> registry.register(MyTextParser())
        >>> registry.register(MyPDFParser())
        >>> parser = registry.get_parser("document.pdf")
        >>> result = await parser.parse(content, metadata)
    """

    def __init__(self) -> None:
        """Initialize the parser registry."""
        self._parsers: list[Parser] = []
        self._parsers_by_extension: dict[str, list[Parser]] = {}

    def register(self, parser: Parser) -> None:
        """Register a new parser.

        Args:
            parser: Parser instance to register

        Raises:
            ValueError: If parser is not a valid Parser instance
        """
        if not isinstance(parser, Parser):
            raise ValueError(f"Parser must be an instance of Parser, got {type(parser)}")

        self._parsers.append(parser)

        # Index by supported extensions
        for ext in parser.supported_formats:
            ext_lower = ext.lower()
            if ext_lower not in self._parsers_by_extension:
                self._parsers_by_extension[ext_lower] = []
            self._parsers_by_extension[ext_lower].append(parser)

        # Sort parsers by priority (highest first)
        self._parsers.sort(key=lambda p: p.priority, reverse=True)
        for parsers_list in self._parsers_by_extension.values():
            parsers_list.sort(key=lambda p: p.priority, reverse=True)

        logger.info(f"Registered parser '{parser.name}' for formats: {parser.supported_formats}")

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
        # Get file extension
        ext = Path(file_path).suffix.lower()

        # Try parsers registered for this extension first
        if ext in self._parsers_by_extension:
            for parser in self._parsers_by_extension[ext]:
                if parser.can_parse(file_path, mime_type):
                    logger.debug(f"Selected parser '{parser.name}' for '{file_path}'")
                    return parser

        # Fall back to checking all parsers
        for parser in self._parsers:
            if parser.can_parse(file_path, mime_type):
                logger.debug(f"Selected parser '{parser.name}' for '{file_path}' (fallback)")
                return parser

        # No parser found
        raise ParserError(
            f"No parser found for file with extension '{ext}' and MIME type '{mime_type}'",
            path=file_path,
        )

    def get_supported_formats(self) -> list[str]:
        """Get list of all supported file formats.

        Returns:
            Sorted list of supported file extensions
        """
        formats = set()
        for parser in self._parsers:
            formats.update(parser.supported_formats)
        return sorted(formats)

    def get_parsers(self) -> list[Parser]:
        """Get all registered parsers.

        Returns:
            List of registered parser instances
        """
        return self._parsers.copy()

    def clear(self) -> None:
        """Clear all registered parsers.

        Useful for testing or reconfiguration.
        """
        self._parsers.clear()
        self._parsers_by_extension.clear()
        logger.info("Cleared all parsers from registry")

    def discover_parsers(self, package_name: str = "nexus.parsers") -> int:
        """Auto-discover and register parsers from a package.

        Scans the specified package for Parser subclasses and automatically
        registers them. This allows for easy plugin-style parser addition.

        Args:
            package_name: Name of the package to scan for parsers

        Returns:
            Number of parsers discovered and registered

        Example:
            >>> registry = ParserRegistry()
            >>> count = registry.discover_parsers("nexus.parsers")
            >>> print(f"Discovered {count} parsers")
        """
        discovered_count = 0

        try:
            # Import the package
            package = importlib.import_module(package_name)
            package_path = package.__path__

            # Iterate through all modules in the package
            for _, module_name, is_pkg in pkgutil.iter_modules(package_path):
                if is_pkg:
                    continue  # Skip sub-packages

                # Import the module
                full_module_name = f"{package_name}.{module_name}"
                try:
                    module = importlib.import_module(full_module_name)

                    # Find all Parser subclasses in the module
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        # Check if it's a Parser subclass (but not Parser itself)
                        if (
                            issubclass(obj, Parser)
                            and obj is not Parser
                            and obj.__module__ == full_module_name
                        ):
                            # Try to instantiate and register the parser
                            try:
                                parser_instance = obj()
                                self.register(parser_instance)
                                discovered_count += 1
                                logger.debug(f"Discovered and registered parser: {name}")
                            except Exception as e:
                                logger.warning(
                                    f"Failed to instantiate parser {name} from {full_module_name}: {e}"
                                )

                except Exception as e:
                    logger.warning(f"Failed to import module {full_module_name}: {e}")

        except Exception as e:
            logger.error(f"Failed to discover parsers from package {package_name}: {e}")

        if discovered_count > 0:
            logger.info(f"Auto-discovered {discovered_count} parsers from {package_name}")

        return discovered_count

    def __repr__(self) -> str:
        """String representation of the registry."""
        parser_names = [p.name for p in self._parsers]
        return f"ParserRegistry(parsers={parser_names})"
