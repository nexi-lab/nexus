"""Async local filesystem backend with CAS and directory support.

Phase 2 of async migration (Issue #940).
Uses CASBlobStore via asyncio.to_thread() for non-blocking file I/O.
"""

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import AsyncBackend
from nexus.backends.cas_blob_store import CASBlobStore
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class AsyncLocalBackend(AsyncBackend):
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

    @property
    def has_root_path(self) -> bool:
        """This backend has a local root_path for physical storage."""
        return True

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

    # === Metadata Operations ===

    async def _read_metadata(self, content_hash: str) -> dict[str, Any]:
        """Read metadata for content asynchronously.

        Uses tenacity for async retry with exponential backoff + jitter.
        Each attempt runs blocking I/O in a thread; the backoff sleep
        uses asyncio.sleep so the thread pool slot is freed between retries.
        """
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        meta_path = self._get_meta_path(content_hash)

        def _single_read() -> dict[str, Any]:
            if not meta_path.exists():
                return {"ref_count": 0, "size": 0}
            content = meta_path.read_text(encoding="utf-8")
            result: dict[str, Any] = json.loads(content)
            return result

        @retry(
            stop=stop_after_attempt(10),
            wait=wait_exponential(multiplier=0.001, max=1.0),
            retry=retry_if_exception_type((json.JSONDecodeError, OSError)),
            reraise=True,
        )
        async def _read_with_retry() -> dict[str, Any]:
            return await asyncio.to_thread(_single_read)

        try:
            result: dict[str, Any] = await _read_with_retry()
            return result
        except (json.JSONDecodeError, OSError) as e:
            raise BackendError(
                f"Failed to read metadata: {e}: {content_hash}",
                backend="local",
                path=content_hash,
            ) from e

    async def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content asynchronously.

        Uses tenacity for async retry with exponential backoff.
        Only PermissionError is retried (Windows antivirus / lock contention);
        other OSErrors fail immediately.
        """
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        meta_path = self._get_meta_path(content_hash)

        def _single_write() -> None:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=meta_path.parent,
                    delete=False,
                    suffix=".tmp",
                ) as tmp_file:
                    tmp_path = Path(tmp_file.name)
                    tmp_file.write(json.dumps(metadata))
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())

                os.replace(str(tmp_path), str(meta_path))
                tmp_path = None
            except BaseException:
                if tmp_path is not None and tmp_path.exists():
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                raise

        @retry(
            stop=stop_after_attempt(10),
            wait=wait_exponential(multiplier=0.001, max=1.0),
            retry=retry_if_exception_type(PermissionError),
            reraise=True,
        )
        async def _write_with_retry() -> None:
            await asyncio.to_thread(_single_write)

        try:
            await _write_with_retry()
        except OSError as e:
            raise BackendError(
                f"Failed to write metadata: {e}: {content_hash}",
                backend="local",
                path=content_hash,
            ) from e

    # === Content Write Operations ===

    async def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> WriteResult:
        """
        Write content to CAS storage and return a WriteResult.

        If content already exists, increments reference count.
        Uses CASBlobStore via asyncio.to_thread for non-blocking I/O.

        Args:
            content: File content as bytes
            context: Operation context (ignored for local backend)

        Returns:
            WriteResult with content_hash and size.
        """
        content_hash = await asyncio.to_thread(self._compute_hash, content)

        assert self._cas is not None  # noqa: S101
        cas = self._cas

        def _store() -> WriteResult:
            cas.store(content_hash, content)

            # Add to cache
            if self.content_cache is not None:
                self.content_cache.put(content_hash, content)

            return WriteResult(content_hash=content_hash, size=len(content))

        return await asyncio.to_thread(_store)

    # === Content Read Operations ===

    async def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> bytes:
        """
        Read content by its hash asynchronously.

        Uses cache if available for fast reads.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
        """
        # Check cache first for fast path
        if self.content_cache is not None:
            cached_content = self.content_cache.get(content_hash)
            if cached_content is not None:
                return cached_content

        def _read() -> bytes:
            assert self._cas is not None  # noqa: S101
            if not self._cas.blob_exists(content_hash):
                raise NexusFileNotFoundError(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                )

            content = self._cas.read_blob(content_hash, verify=True)

            # Add to cache
            if self.content_cache is not None:
                self.content_cache.put(content_hash, content)

            return content

        return await asyncio.to_thread(_read)

    # === Batch Read Operations ===

    async def batch_read_content(
        self,
        content_hashes: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        """
        Optimized batch read for async backend with concurrent I/O.

        Args:
            content_hashes: List of BLAKE3 hashes as hex strings
            context: Operation context (ignored for local backend)
            contexts: Per-hash contexts (ignored — local backend uses CAS paths)

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
                    try:
                        data = await self.read_content(content_hash, context=context)
                    except NexusFileNotFoundError:
                        data = None
                    return (content_hash, data)

            tasks = [read_one(h) for h in uncached_hashes]
            read_results = await asyncio.gather(*tasks)

            for hash_key, file_content in read_results:
                result[hash_key] = file_content

        return result

    # === Content Delete Operations ===

    async def delete_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> None:
        """
        Delete content by hash with reference counting.

        Delegates to CASBlobStore.release() via asyncio.to_thread().

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Raises:
            NexusFileNotFoundError: If content does not exist.
        """
        content_path = self._hash_to_path(content_hash)

        assert self._cas is not None  # noqa: S101
        cas = self._cas

        def _delete() -> None:
            if not content_path.exists():
                raise NexusFileNotFoundError(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                )

            cas.release(content_hash)

        await asyncio.to_thread(_delete)

    # === Content Existence/Size Operations ===

    async def content_exists(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> bool:
        """
        Check if content exists asynchronously.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            True if content exists, False otherwise.
        """
        content_path = self._hash_to_path(content_hash)

        def _exists() -> bool:
            return content_path.exists()

        return await asyncio.to_thread(_exists)

    async def get_content_size(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> int:
        """
        Get content size in bytes asynchronously.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            Content size in bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
        """
        content_path = self._hash_to_path(content_hash)

        def _get_size() -> int:
            if not content_path.exists():
                raise NexusFileNotFoundError(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                )

            return content_path.stat().st_size

        return await asyncio.to_thread(_get_size)

    async def get_ref_count(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> int:
        """
        Get reference count for content asynchronously.

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            Reference count.

        Raises:
            NexusFileNotFoundError: If content does not exist.
        """
        exists = await self.content_exists(content_hash, context=context)
        if not exists:
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        assert self._cas is not None  # noqa: S101
        cas = self._cas

        def _read_ref() -> int:
            meta = cas.read_meta(content_hash)
            return meta.ref_count

        return await asyncio.to_thread(_read_ref)

    # === Streaming Operations ===

    async def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 65536,
        context: "OperationContext | None" = None,
    ) -> AsyncIterator[bytes]:
        """
        Stream content from disk in chunks using aiofiles (truly async).

        Args:
            content_hash: BLAKE3 hash as hex string
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context (ignored for local backend)

        Yields:
            Byte chunks of the content
        """
        import aiofiles

        content_path = self._hash_to_path(content_hash)

        if not await asyncio.to_thread(content_path.exists):
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        try:
            async with aiofiles.open(content_path, "rb") as f:
                while True:
                    chunk = await f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except OSError as e:
            raise BackendError(
                f"Failed to stream content: {e}",
                backend="local",
                path=content_hash,
            ) from e

    async def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 65536,
        context: "OperationContext | None" = None,
    ) -> AsyncIterator[bytes]:
        """Stream a byte range [start, end] inclusive from local CAS (async).

        Uses aiofiles with seek for truly async, bounded-memory I/O.

        Args:
            content_hash: Content hash (BLAKE3 hex)
            start: First byte position (inclusive, 0-based)
            end: Last byte position (inclusive, 0-based)
            chunk_size: Size of each yielded chunk in bytes
            context: Operation context (ignored for local backend)

        Yields:
            Byte chunks covering the requested range
        """
        import aiofiles

        content_path = self._hash_to_path(content_hash)

        if not await asyncio.to_thread(content_path.exists):
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        try:
            async with aiofiles.open(content_path, "rb") as f:
                await f.seek(start)
                bytes_remaining = end - start + 1
                while bytes_remaining > 0:
                    chunk = await f.read(min(chunk_size, bytes_remaining))
                    if not chunk:
                        break
                    bytes_remaining -= len(chunk)
                    yield chunk
        except OSError as e:
            raise BackendError(
                f"Failed to stream range: {e}",
                backend="local",
                path=content_hash,
            ) from e

    async def write_stream(
        self,
        chunks: AsyncIterator[bytes],
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """
        Write content from an async iterator of chunks.

        Collects async chunks, then delegates to CASBlobStore.store_streaming()
        in a thread for the actual streaming-to-disk + incremental hashing.

        Args:
            chunks: Async iterator yielding byte chunks
            context: Operation context (ignored for local backend)

        Returns:
            WriteResult with content_hash and size.
        """
        # Collect async chunks (async-to-sync boundary)
        collected: list[bytes] = []
        async for chunk in chunks:
            collected.append(chunk)

        assert self._cas is not None  # noqa: S101
        cas = self._cas

        # Run blocking CAS write in thread — store_streaming handles
        # temp file + incremental hash internally
        result = await asyncio.to_thread(lambda: cas.store_streaming(iter(collected)))

        # Add to cache if we have the content
        if self.content_cache is not None and collected:
            content = b"".join(collected)
            self.content_cache.put(result.content_hash, content)

        return WriteResult(content_hash=result.content_hash, size=result.size)

    # === Directory Operations ===

    async def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Create directory in virtual directory structure asynchronously.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context (ignored for local backend)

        Raises:
            BackendError: If directory creation fails.
        """
        full_path = self.dir_root / path.lstrip("/")

        def _mkdir() -> None:
            try:
                if parents:
                    full_path.mkdir(parents=True, exist_ok=exist_ok)
                else:
                    full_path.mkdir(exist_ok=exist_ok)
            except FileExistsError as e:
                raise BackendError(
                    f"Directory already exists: {path}",
                    backend=self.name,
                    path=path,
                ) from e

        await asyncio.to_thread(_mkdir)

    async def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if path is a directory asynchronously.

        Args:
            path: Path to check
            context: Operation context (ignored for local backend)

        Returns:
            True if path is a directory, False otherwise.
        """
        full_path = self.dir_root / path.lstrip("/")

        def _is_dir() -> bool:
            return full_path.is_dir()

        return await asyncio.to_thread(_is_dir)

    async def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Remove directory from virtual directory structure asynchronously.

        Args:
            path: Directory path (relative to backend root)
            recursive: Remove non-empty directory (like rm -rf)
            context: Operation context (ignored for local backend)

        Raises:
            NexusFileNotFoundError: If directory does not exist.
            BackendError: If directory removal fails.
        """
        import shutil

        full_path = self.dir_root / path.lstrip("/")

        def _rmdir() -> None:
            if not full_path.exists():
                raise NexusFileNotFoundError(
                    path=path,
                    message=f"Directory not found: {path}",
                )

            if not full_path.is_dir():
                raise BackendError(
                    f"Not a directory: {path}",
                    backend=self.name,
                    path=path,
                )

            try:
                if recursive:
                    shutil.rmtree(full_path)
                else:
                    full_path.rmdir()
            except OSError as e:
                raise BackendError(
                    f"Failed to remove directory: {e}",
                    backend=self.name,
                    path=path,
                ) from e

        await asyncio.to_thread(_rmdir)

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
