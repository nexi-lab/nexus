"""Search brick protocols for dependency inversion (Issue #1520).

Defines FileReaderProtocol to decouple search modules from the NexusFS
kernel object. Instead of depending on the concrete NexusFilesystem class,
search components accept any object satisfying FileReaderProtocol.

This enables:
- Zero kernel imports in the search brick
- Easy testing with mock file readers
- Pluggable backends (local, GCS, S3, etc.)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FileReaderProtocol(Protocol):
    """Narrow interface for file reading needed by search components.

    Replaces the broad NexusFilesystem dependency with a minimal contract.
    Only 6 methods (vs NexusFS's 30+) — keeps coupling surface small.

    Implementations:
    - _NexusFSFileReader (nexus.factory): Wraps NexusFS for production
    - Mock objects: For testing without kernel
    """

    def read_text(self, path: str) -> str:
        """Read file content as text.

        Args:
            path: Virtual file path.

        Returns:
            File content as string.
        """
        ...

    def get_searchable_text(self, path: str) -> str | None:
        """Get pre-processed searchable text for a file.

        Returns cached/parsed text when available (content_cache, parsed_text).
        Returns None if no pre-processed text is available, signaling the
        caller to fall back to read_text().

        Args:
            path: Virtual file path.

        Returns:
            Searchable text or None.
        """
        ...

    def list_files(self, path: str, recursive: bool = True) -> list[Any]:
        """List files in a directory.

        Args:
            path: Directory path.
            recursive: If True, list recursively.

        Returns:
            List of file paths or file info dicts.
        """
        ...

    def get_session(self) -> Any:
        """Return a context manager that yields a database session.

        Usage::

            with file_reader.get_session() as session:
                result = session.execute(stmt)

        Returns:
            Context manager yielding a SQLAlchemy Session.
        """
        ...

    def get_path_id(self, path: str) -> str | None:
        """Get the path_id for a virtual path.

        Args:
            path: Virtual file path.

        Returns:
            Path ID string, or None if not found.
        """
        ...

    def get_content_hash(self, path: str) -> str | None:
        """Get the content hash for a virtual path.

        Args:
            path: Virtual file path.

        Returns:
            Content hash string, or None if not available.
        """
        ...
