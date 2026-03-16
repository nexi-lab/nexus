"""Parse service protocol (Issue #988: Extract domain services).

Defines the contract for document parsing operations.
Existing implementation: ``nexus.parsers.registry.ParserRegistry``.

Storage Affinity: **ObjectStore** — parsed content derives from stored files.

References:
    - docs/design/KERNEL-ARCHITECTURE.md
    - ops-scenario-matrix.md (parsing is a core service domain)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ParseProtocol(Protocol):
    """Service contract for document parsing.

    Provides the registry-level interface for managing parsers
    and resolving which parser handles a given file format.

    Callers use ``get_parser()`` to find a parser for a file,
    then call ``parser.parse(content, metadata)`` on the result.
    """

    def register(self, parser: Any, **kw: object) -> None:
        """Register a new parser.

        Args:
            parser: Parser instance to register.
        """
        ...

    def get_parser(self, file_path: str, mime_type: str | None = None) -> Any:
        """Get the appropriate parser for a file.

        Args:
            file_path: Path to the file to parse.
            mime_type: Optional MIME type of the file.

        Returns:
            Parser instance capable of handling the file.

        Raises:
            ParserError: If no suitable parser is found.
        """
        ...

    def get_supported_formats(self) -> list[str]:
        """Get list of all supported file formats.

        Returns:
            Sorted list of supported file extensions.
        """
        ...
