"""Nexus client for interacting with the filesystem."""

from typing import Any, Optional

import httpx

from nexus.interface import NexusInterface


class NexusClient(NexusInterface):
    """
    Client for interacting with Nexus filesystem via REST API.

    Supports async context manager for automatic connection management.

    Example:
        async with NexusClient(api_key="nexus_...") as client:
            content = await client.read("/workspace/data.txt")
            await client.write("/workspace/output.txt", b"Hello World")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8080",
        timeout: float = 30.0,
    ):
        """
        Initialize Nexus client.

        Args:
            api_key: Nexus API key
            base_url: Base URL of Nexus server
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "NexusClient":
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        if self._client:
            await self._client.aclose()

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
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.get(f"/api/v1/files{path}")
        response.raise_for_status()
        return response.content

    async def write(self, path: str, content: bytes) -> None:
        """
        Write file content.

        Args:
            path: Virtual path to write
            content: File content as bytes

        Returns:
            Metadata about written file
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.put(
            f"/api/v1/files{path}",
            content=content,
        )
        response.raise_for_status()

    async def delete(self, path: str) -> None:
        """
        Delete a file.

        Args:
            path: Virtual path to delete
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.delete(f"/api/v1/files{path}")
        response.raise_for_status()

    async def list(self, path: str = "/", recursive: bool = False) -> list[dict[str, Any]]:
        """
        List directory contents.

        Args:
            path: Virtual path to list
            recursive: Whether to list recursively

        Returns:
            List of file/directory metadata
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.get(
            f"/api/v1/files{path}",
            params={"recursive": recursive},
        )
        response.raise_for_status()
        return response.json()

    async def glob(self, pattern: str, path: str = "/") -> list[str]:
        """
        Find files matching glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py")
            path: Base path to search from

        Returns:
            List of matching file paths
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.post(
            "/api/v1/tools/glob",
            json={"pattern": pattern, "path": path},
        )
        response.raise_for_status()
        return response.json()["files"]

    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Search file contents with regex.

        Args:
            pattern: Regex pattern to search
            path: Base path to search
            file_pattern: Optional glob pattern to filter files

        Returns:
            List of matches with file, line, and content
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.post(
            "/api/v1/tools/grep",
            json={
                "pattern": pattern,
                "path": path,
                "file_pattern": file_pattern,
            },
        )
        response.raise_for_status()
        return response.json()["matches"]

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
            query: Natural language search query
            path: Base path to search
            limit: Maximum number of results
            filters: Optional metadata filters

        Returns:
            List of search results with path, score, and content
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.post(
            "/api/v1/search/semantic",
            json={
                "query": query,
                "path": path,
                "limit": limit,
                "filters": filters or {},
            },
        )
        response.raise_for_status()
        return response.json()["results"]

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
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        response = await self._client.post(
            "/api/v1/llm/read",
            json={
                "path": path,
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()
        return response.json()["response"]
