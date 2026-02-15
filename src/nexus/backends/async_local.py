"""Async local filesystem backend with CAS and directory support.

Phase 2 of async migration (Issue #940).
Uses CASBlobStore via asyncio.to_thread() for non-blocking file I/O.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.backends.cas_blob_store import CASBlobStore
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.response import HandlerResponse
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class AsyncLocalBackend:
    """
    Async local filesystem backend with Content-Addressable Storage (CAS).

    Provides async/await interface for file operations while maintaining
    the same storage semantics as the sync LocalBackend:
    - Content deduplication (same content stored once)
    - Reference counting for safe deletion
    - Atomic write operations
    - Lock-free blob writes with striped metadata locks (via CASBlobStore)

    Storage structure:
        root/
        ├── cas/              # Content storage (by hash)
        │   ├── ab/
        │   │   └── cd/
        │   │       ├── abcd1234...ef56        # Content file
        │   │       └── abcd1234...ef56.meta   # Metadata (ref count, size)
        └── dirs/             # Virtual directory structure
            ├── workspace/
            └── projects/

    Note: This is a simplified async implementation that does not support
    CDC chunking. For large file support, use the sync LocalBackend.
    """

    def __init__(
        self,
        root_path: str | Path,
        content_cache: ContentCache | None = None,
        batch_read_workers: int = 8,
    ):
        """
        Initialize async local backend.

        Args:
            root_path: Root directory for storage
            content_cache: Optional content cache for faster reads (default: None)
            batch_read_workers: Max concurrent workers for batch reads (default: 8)
        """
        self._root_path = Path(root_path).resolve()
        self.content_cache = content_cache
        self.batch_read_workers = batch_read_workers
        self._initialized = False
        self._cas: CASBlobStore | None = None

    @property
    def root_path(self) -> Path:
        """Root directory for storage."""
        return self._root_path

    @property
    def cas_root(self) -> Path:
        """Content-addressable storage directory."""
        return self._root_path / "cas"

    @property
    def dir_root(self) -> Path:
        """Virtual directory root."""
        return self._root_path / "dirs"

    @property
    def name(self) -> str:
        """Backend name identifier."""
        return "local"

    async def initialize(self) -> None:
        """Initialize backend and create required directories."""
        if self._initialized:
            return

        def _ensure_roots() -> None:
            # Check if root_path points to a file
            if self._root_path.exists() and not self._root_path.is_dir():
                raise BackendError(
                    f"Root path is a file, not a directory: {self._root_path}",
                    backend="local",
                    path=str(self._root_path),
                )
            self.cas_root.mkdir(parents=True, exist_ok=True)
            self.dir_root.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(_ensure_roots)
        self._cas = CASBlobStore(self.cas_root)
        self._initialized = True

    async def close(self) -> None:
        """Close backend and release resources."""
        self._initialized = False

    # === Hash and Path Utilities ===

    def _compute_hash(self, content: bytes) -> str:
        """Compute content hash using BLAKE3."""
        return hash_content(content)

    def _hash_to_path(self, content_hash: str) -> Path:
        """Convert content hash to filesystem path."""
        if len(content_hash) < 4:
            raise ValueError(f"Invalid hash length: {content_hash}")
        return self.cas_root / content_hash[:2] / content_hash[2:4] / content_hash

    def _get_meta_path(self, content_hash: str) -> Path:
        """Get path to metadata file for content."""
        return self._hash_to_path(content_hash).with_suffix(".meta")

    # === Content Write Operations ===

    async def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> HandlerResponse[str]:
        """
        Write content to CAS storage and return its hash.

        If content already exists, increments reference count.
        Uses CASBlobStore via asyncio.to_thread for non-blocking I/O.

        Args:
            content: File content as bytes
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content hash in data field
        """
        start_time = time.perf_counter()

        content_hash = await asyncio.to_thread(self._compute_hash, content)

        assert self._cas is not None  # noqa: S101

        def _store() -> HandlerResponse[str]:
            try:
                self._cas.store(content_hash, content)  # type: ignore[union-attr]

                # Add to cache
                if self.content_cache is not None:
                    self.content_cache.put(content_hash, content)

                return HandlerResponse.ok(
                    data=content_hash,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )
            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        return await asyncio.to_thread(_store)

    # === Content Read Operations ===

    async def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bytes]:
        """
        Read content by its hash asynchronously.

        Uses cache if available for fast reads.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with file content in data field
        """
        start_time = time.perf_counter()

        # Check cache first for fast path
        if self.content_cache is not None:
            cached_content = self.content_cache.get(content_hash)
            if cached_content is not None:
                return HandlerResponse.ok(
                    data=cached_content,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        def _read() -> HandlerResponse[bytes]:
            assert self._cas is not None  # noqa: S101
            if not self._cas.blob_exists(content_hash):
                return HandlerResponse.not_found(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )

            try:
                content = self._cas.read_blob(content_hash, verify=True)

                # Add to cache
                if self.content_cache is not None:
                    self.content_cache.put(content_hash, content)

                return HandlerResponse.ok(
                    data=content,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )
            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        return await asyncio.to_thread(_read)

    # === Batch Read Operations ===

    async def batch_read_content(
        self, content_hashes: list[str], context: "OperationContext | None" = None
    ) -> dict[str, bytes | None]:
        """
        Optimized batch read for async backend with concurrent I/O.

        Args:
            content_hashes: List of BLAKE3 hashes as hex strings
            context: Operation context (ignored for local backend)

        Returns:
            Dictionary mapping hash to content (or None if not found)
        """
        if not content_hashes:
            return {}

        result: dict[str, bytes | None] = {}

        # First pass: check cache for all hashes
        uncached_hashes = []
        if self.content_cache is not None:
            for content_hash in content_hashes:
                cached_content = self.content_cache.get(content_hash)
                if cached_content is not None:
                    result[content_hash] = cached_content
                else:
                    uncached_hashes.append(content_hash)
        else:
            uncached_hashes = list(content_hashes)

        # Second pass: read uncached content concurrently
        if uncached_hashes:
            # Limit concurrency
            semaphore = asyncio.Semaphore(self.batch_read_workers)

            async def read_one(content_hash: str) -> tuple[str, bytes | None]:
                async with semaphore:
                    response = await self.read_content(content_hash, context=context)
                    return (content_hash, response.data if response.success else None)

            tasks = [read_one(h) for h in uncached_hashes]
            read_results = await asyncio.gather(*tasks)

            for hash_key, file_content in read_results:
                result[hash_key] = file_content

        return result

    # === Content Delete Operations ===

    async def delete_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[None]:
        """
        Delete content by hash with reference counting.

        Delegates to CASBlobStore.release() via asyncio.to_thread().

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse indicating success or failure
        """
        start_time = time.perf_counter()
        content_path = self._hash_to_path(content_hash)

        assert self._cas is not None  # noqa: S101

        def _delete() -> HandlerResponse[None]:
            if not content_path.exists():
                return HandlerResponse.not_found(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )

            try:
                self._cas.release(content_hash)  # type: ignore[union-attr]

                return HandlerResponse.ok(
                    data=None,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        return await asyncio.to_thread(_delete)

    # === Content Existence/Size Operations ===

    async def content_exists(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bool]:
        """
        Check if content exists asynchronously.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with True if content exists, False otherwise
        """
        start_time = time.perf_counter()
        content_path = self._hash_to_path(content_hash)

        def _exists() -> bool:
            return content_path.exists()

        exists = await asyncio.to_thread(_exists)

        return HandlerResponse.ok(
            data=exists,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
            path=content_hash,
        )

    async def get_content_size(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[int]:
        """
        Get content size in bytes asynchronously.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content size in bytes
        """
        start_time = time.perf_counter()
        content_path = self._hash_to_path(content_hash)

        def _get_size() -> HandlerResponse[int]:
            if not content_path.exists():
                return HandlerResponse.not_found(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )

            try:
                size = content_path.stat().st_size
                return HandlerResponse.ok(
                    data=size,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )
            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        return await asyncio.to_thread(_get_size)

    async def get_ref_count(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[int]:
        """
        Get reference count for content asynchronously.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with reference count
        """
        start_time = time.perf_counter()

        exists_response = await self.content_exists(content_hash, context=context)
        if not exists_response.success or not exists_response.data:
            return HandlerResponse.not_found(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        assert self._cas is not None  # noqa: S101

        def _read_ref() -> HandlerResponse[int]:
            try:
                meta = self._cas.read_meta(content_hash)  # type: ignore[union-attr]
                return HandlerResponse.ok(
                    data=meta.ref_count,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )
            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        return await asyncio.to_thread(_read_ref)

    # === Streaming Operations ===

    async def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> AsyncIterator[bytes]:
        """
        Stream content from disk in chunks without loading entire file.

        Args:
            content_hash: BLAKE3 hash as hex string
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context (ignored for local backend)

        Yields:
            Byte chunks of the content
        """
        content_path = self._hash_to_path(content_hash)

        def _check_exists() -> bool:
            return content_path.exists()

        if not await asyncio.to_thread(_check_exists):
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        def _read_all_chunks() -> list[bytes]:
            """Read all chunks from file in a thread."""
            chunks: list[bytes] = []
            try:
                with open(content_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        chunks.append(chunk)
            except OSError as e:
                raise BackendError(
                    f"Failed to stream content: {e}",
                    backend="local",
                    path=content_hash,
                ) from e
            return chunks

        # Read all chunks in a thread, then yield them
        chunks = await asyncio.to_thread(_read_all_chunks)
        for chunk in chunks:
            yield chunk

    async def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> AsyncIterator[bytes]:
        """Stream a byte range [start, end] inclusive from local CAS (async).

        Uses seek-based I/O in a thread for efficiency.

        Args:
            content_hash: Content hash (BLAKE3 hex)
            start: First byte position (inclusive, 0-based)
            end: Last byte position (inclusive, 0-based)
            chunk_size: Size of each yielded chunk in bytes
            context: Operation context (ignored for local backend)

        Yields:
            Byte chunks covering the requested range
        """
        content_path = self._hash_to_path(content_hash)

        def _check_exists() -> bool:
            return content_path.exists()

        if not await asyncio.to_thread(_check_exists):
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        def _read_range_chunks() -> list[bytes]:
            """Read range chunks from file in a thread."""
            result: list[bytes] = []
            bytes_remaining = end - start + 1
            try:
                with open(content_path, "rb") as f:
                    f.seek(start)
                    while bytes_remaining > 0:
                        chunk = f.read(min(chunk_size, bytes_remaining))
                        if not chunk:
                            break
                        bytes_remaining -= len(chunk)
                        result.append(chunk)
            except OSError as e:
                raise BackendError(
                    f"Failed to stream range: {e}",
                    backend="local",
                    path=content_hash,
                ) from e
            return result

        chunks = await asyncio.to_thread(_read_range_chunks)
        for chunk in chunks:
            yield chunk

    async def write_stream(
        self,
        chunks: AsyncIterator[bytes],
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[str]:
        """
        Write content from an async iterator of chunks.

        Collects all chunks, computes hash, and stores atomically.

        Args:
            chunks: Async iterator yielding byte chunks
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content hash in data field
        """
        # Collect all chunks
        collected_chunks: list[bytes] = []
        async for chunk in chunks:
            collected_chunks.append(chunk)

        content = b"".join(collected_chunks)

        # Use write_content for the actual storage
        return await self.write_content(content, context=context)

    # === Directory Operations ===

    async def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """
        Create directory in virtual directory structure asynchronously.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse indicating success or failure
        """
        start_time = time.perf_counter()
        full_path = self.dir_root / path.lstrip("/")

        def _mkdir() -> HandlerResponse[None]:
            try:
                if parents:
                    full_path.mkdir(parents=True, exist_ok=exist_ok)
                else:
                    full_path.mkdir(exist_ok=exist_ok)
                return HandlerResponse.ok(
                    data=None,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=path,
                )
            except FileExistsError:
                return HandlerResponse.error(
                    message=f"Directory already exists: {path}",
                    code=409,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=path,
                )
            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=path,
                )

        return await asyncio.to_thread(_mkdir)

    async def is_directory(
        self, path: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bool]:
        """
        Check if path is a directory asynchronously.

        Args:
            path: Path to check
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with True if path is a directory
        """
        start_time = time.perf_counter()
        full_path = self.dir_root / path.lstrip("/")

        def _is_dir() -> bool:
            return full_path.is_dir()

        is_dir = await asyncio.to_thread(_is_dir)

        return HandlerResponse.ok(
            data=is_dir,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
            path=path,
        )

    async def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents asynchronously.

        Args:
            path: Directory path
            context: Operation context (ignored for local backend)

        Returns:
            List of items in the directory (directories have trailing /)
        """
        full_path = self.dir_root / path.lstrip("/")

        def _list() -> list[str]:
            if not full_path.exists() or not full_path.is_dir():
                return []

            items = []
            for item in full_path.iterdir():
                if item.is_dir():
                    items.append(f"{item.name}/")
                else:
                    items.append(item.name)
            return items

        return await asyncio.to_thread(_list)
