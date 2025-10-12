"""Abstract interface for Nexus filesystem operations."""

from abc import ABC, abstractmethod
from typing import Any, Optional


class NexusInterface(ABC):
    """
    Abstract base class for Nexus filesystem interface.

    All deployment modes (embedded, monolithic, distributed) implement
    this interface, allowing code to be deployment-mode agnostic.

    Example:
        async with nexus.connect() as nx:
            await nx.write("/workspace/data.txt", b"Hello")
            content = await nx.read("/workspace/data.txt")
    """

    @abstractmethod
    async def __aenter__(self) -> "NexusInterface":
        """Enter async context manager."""
        ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        ...

    @abstractmethod
    async def read(self, path: str) -> bytes:
        """
        Read file content.

        Args:
            path: Virtual path to read

        Returns:
            File content as bytes

        Raises:
            FileNotFoundError: If file does not exist
            PermissionError: If access is denied
        """
        ...

    @abstractmethod
    async def write(self, path: str, content: bytes) -> None:
        """
        Write file content.

        Args:
            path: Virtual path to write
            content: File content as bytes

        Raises:
            PermissionError: If access is denied
        """
        ...

    @abstractmethod
    async def delete(self, path: str) -> None:
        """
        Delete a file.

        Args:
            path: Virtual path to delete

        Raises:
            FileNotFoundError: If file does not exist
            PermissionError: If access is denied
        """
        ...

    @abstractmethod
    async def list(self, path: str = "/", recursive: bool = False) -> list[dict[str, Any]]:
        """
        List directory contents.

        Args:
            path: Virtual path to list
            recursive: Whether to list recursively

        Returns:
            List of file/directory metadata
        """
        ...

    @abstractmethod
    async def glob(self, pattern: str, path: str = "/") -> list[str]:
        """
        Find files matching glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py")
            path: Base path to search from

        Returns:
            List of matching file paths
        """
        ...

    @abstractmethod
    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: Optional[str] = None,
    ) -> List[dict[str, Any]]:
        """
        Search file contents with regex.

        Args:
            pattern: Regex pattern to search
            path: Base path to search
            file_pattern: Optional glob pattern to filter files

        Returns:
            List of matches with file, line, and content
        """
        ...

    @abstractmethod
    async def semantic_search(
        self,
        path: str,
        query: str,
        limit: int = 10,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """
        Semantic search across documents.

        Args:
            path: Base path to search
            query: Natural language search query
            limit: Maximum number of results
            filters: Optional metadata filters

        Returns:
            List of search results with path, score, and content
        """
        ...

    @abstractmethod
    async def llm_read(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
    ) -> str:
        """
        Read and process file with LLM.

        Args:
            path: Virtual path to read
            prompt: Question or instruction for LLM
            model: Model to use (claude-sonnet-4, gpt-4, etc.)
            max_tokens: Maximum tokens in response

        Returns:
            LLM response
        """
        ...

    # Additional methods to be implemented
    async def copy(self, src: str, dst: str) -> None:
        """Copy a file."""
        raise NotImplementedError()

    async def move(self, src: str, dst: str) -> None:
        """Move a file."""
        raise NotImplementedError()

    async def exists(self, path: str) -> bool:
        """Check if a file exists."""
        raise NotImplementedError()

    async def stat(self, path: str) -> dict[str, Any]:
        """Get file metadata."""
        raise NotImplementedError()
