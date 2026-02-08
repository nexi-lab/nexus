"""Async local filesystem backend with CAS and directory support.

Phase 2 of async migration (Issue #940).
Uses asyncio.to_thread() for non-blocking file I/O.
"""

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import FileLock

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
    - Thread-safe file locking

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
        self._initialized = True

    async def close(self) -> None:
        """Close backend and release resources."""
        self._initialized = False

    # === Hash and Path Utilities ===

    def _compute_hash(self, content: bytes) -> str:
        """Compute content hash using BLAKE3."""
        return hash_content(content)

    def _hash_to_path(self, content_hash: str) -> Path:
        """
        Convert content hash to filesystem path.

        Uses two-level directory structure:
        cas/ab/cd/abcd1234...ef56

        Args:
            content_hash: SHA-256/BLAKE3 hash as hex string

        Returns:
            Path object for content file
        """
        if len(content_hash) < 4:
            raise ValueError(f"Invalid hash length: {content_hash}")

        dir1 = content_hash[:2]
        dir2 = content_hash[2:4]

        return self.cas_root / dir1 / dir2 / content_hash

    def _get_meta_path(self, content_hash: str) -> Path:
        """Get path to metadata file for content."""
        content_path = self._hash_to_path(content_hash)
        return content_path.with_suffix(".meta")

    def _get_lock_path(self, content_hash: str) -> Path:
        """Get the lock file path for a content hash."""
        return self._get_meta_path(content_hash).with_suffix(".lock")

    # === Metadata Operations ===

    async def _read_metadata(self, content_hash: str) -> dict[str, Any]:
        """Read metadata for content asynchronously."""
        meta_path = self._get_meta_path(content_hash)

        def _read() -> dict[str, Any]:
            if not meta_path.exists():
                return {"ref_count": 0, "size": 0}

            # Retry logic for file locking and race conditions
            max_retries = 10
            base_delay = 0.001  # 1ms base delay

            for attempt in range(max_retries):
                try:
                    content = meta_path.read_text(encoding="utf-8")
                    result: dict[str, Any] = json.loads(content)
                    return result
                except json.JSONDecodeError as e:
                    if attempt < max_retries - 1:
                        import random

                        delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                        time.sleep(delay)
                        continue
                    raise BackendError(
                        f"Failed to read metadata: {e}: {content_hash}",
                        backend="local",
                        path=content_hash,
                    ) from e
                except OSError as e:
                    if attempt < max_retries - 1:
                        import random

                        delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                        time.sleep(delay)
                        continue
                    raise BackendError(
                        f"Failed to read metadata: {e}",
                        backend="local",
                        path=content_hash,
                    ) from e

            raise BackendError(
                f"Failed to read metadata after {max_retries} retries",
                backend="local",
                path=content_hash,
            )

        return await asyncio.to_thread(_read)

    async def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content asynchronously."""
        meta_path = self._get_meta_path(content_hash)

        def _write() -> None:
            meta_path.parent.mkdir(parents=True, exist_ok=True)

            max_retries = 10
            base_delay = 0.001

            for attempt in range(max_retries):
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
                    return

                except PermissionError as e:
                    if tmp_path is not None and tmp_path.exists():
                        with contextlib.suppress(OSError):
                            tmp_path.unlink()

                    if attempt < max_retries - 1:
                        import random

                        delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                        time.sleep(delay)
                        continue
                    else:
                        raise BackendError(
                            f"Failed to write metadata: {e}: {content_hash}",
                            backend="local",
                            path=content_hash,
                        ) from e

                except OSError as e:
                    if tmp_path is not None and tmp_path.exists():
                        with contextlib.suppress(OSError):
                            tmp_path.unlink()
                    raise BackendError(
                        f"Failed to write metadata: {e}: {content_hash}",
                        backend="local",
                        path=content_hash,
                    ) from e

        await asyncio.to_thread(_write)

    # === Content Write Operations ===

    async def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> HandlerResponse[str]:
        """
        Write content to CAS storage and return its hash.

        If content already exists, increments reference count.
        Handles race conditions when multiple tasks write the same content.

        Args:
            content: File content as bytes
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content hash in data field
        """
        start_time = time.perf_counter()

        content_hash = await asyncio.to_thread(self._compute_hash, content)
        content_path = self._hash_to_path(content_hash)

        def _write_with_lock() -> HandlerResponse[str]:
            nonlocal start_time
            lock_path = self._get_lock_path(content_hash)
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                with FileLock(lock_path):
                    # Check if content already exists (inside lock)
                    if content_path.exists():
                        # Content exists - increment ref_count
                        meta_path = self._get_meta_path(content_hash)
                        if meta_path.exists():
                            meta_content = meta_path.read_text(encoding="utf-8")
                            metadata = json.loads(meta_content)
                        else:
                            metadata = {"ref_count": 0, "size": len(content)}
                        metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                        self._write_metadata_sync(content_hash, metadata)

                        # Add to cache
                        if self.content_cache is not None:
                            self.content_cache.put(content_hash, content)

                        return HandlerResponse.ok(
                            data=content_hash,
                            execution_time_ms=(time.perf_counter() - start_time) * 1000,
                            backend_name=self.name,
                            path=content_hash,
                        )

                    # Content doesn't exist - write atomically
                    content_path.parent.mkdir(parents=True, exist_ok=True)

                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="wb", dir=content_path.parent, delete=False
                        ) as tmp_file:
                            tmp_path = Path(tmp_file.name)
                            tmp_file.write(content)
                            tmp_file.flush()
                            os.fsync(tmp_file.fileno())

                        os.replace(str(tmp_path), str(content_path))
                        tmp_path = None

                        # Create metadata with ref_count=1
                        metadata = {"ref_count": 1, "size": len(content)}
                        self._write_metadata_sync(content_hash, metadata)

                        # Add to cache
                        if self.content_cache is not None:
                            self.content_cache.put(content_hash, content)

                        return HandlerResponse.ok(
                            data=content_hash,
                            execution_time_ms=(time.perf_counter() - start_time) * 1000,
                            backend_name=self.name,
                            path=content_hash,
                        )

                    finally:
                        if tmp_path is not None and tmp_path.exists():
                            with contextlib.suppress(OSError):
                                tmp_path.unlink()

            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        return await asyncio.to_thread(_write_with_lock)

    def _write_metadata_sync(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Synchronous metadata write for use inside locked sections."""
        meta_path = self._get_meta_path(content_hash)
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

        except Exception:
            if tmp_path is not None and tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise

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

        content_path = self._hash_to_path(content_hash)

        def _read() -> HandlerResponse[bytes]:
            max_retries = 3
            retry_delay = 0.01

            for attempt in range(max_retries):
                if not content_path.exists():
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    return HandlerResponse.not_found(
                        path=content_hash,
                        message=f"CAS content not found: {content_hash}",
                        execution_time_ms=(time.perf_counter() - start_time) * 1000,
                        backend_name=self.name,
                    )

                try:
                    content = content_path.read_bytes()

                    # Verify hash
                    actual_hash = self._compute_hash(content)
                    if actual_hash != content_hash:
                        msg = f"Content hash mismatch: expected {content_hash}, got {actual_hash}"
                        return HandlerResponse.error(
                            message=msg,
                            code=500,
                            execution_time_ms=(time.perf_counter() - start_time) * 1000,
                            backend_name=self.name,
                            path=content_hash,
                        )

                    # Add to cache
                    if self.content_cache is not None:
                        self.content_cache.put(content_hash, content)

                    return HandlerResponse.ok(
                        data=content,
                        execution_time_ms=(time.perf_counter() - start_time) * 1000,
                        backend_name=self.name,
                        path=content_hash,
                    )

                except OSError as e:
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    return HandlerResponse.from_exception(
                        e,
                        execution_time_ms=(time.perf_counter() - start_time) * 1000,
                        backend_name=self.name,
                        path=content_hash,
                    )

            return HandlerResponse.error(
                message=f"Failed to read content after {max_retries} retries",
                code=500,
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

        Args:
            content_hash: BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse indicating success or failure
        """
        start_time = time.perf_counter()
        content_path = self._hash_to_path(content_hash)

        def _delete_with_lock() -> HandlerResponse[None]:
            if not content_path.exists():
                return HandlerResponse.not_found(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )

            lock_path = self._get_lock_path(content_hash)
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            should_delete_lock = False
            try:
                with FileLock(lock_path):
                    meta_path = self._get_meta_path(content_hash)
                    if meta_path.exists():
                        meta_content = meta_path.read_text(encoding="utf-8")
                        metadata = json.loads(meta_content)
                    else:
                        metadata = {"ref_count": 1}
                    ref_count = metadata.get("ref_count", 1)

                    if ref_count <= 1:
                        # Last reference - delete file and metadata
                        content_path.unlink()

                        if meta_path.exists():
                            meta_path.unlink()

                        should_delete_lock = True

                        # Clean up empty directories
                        self._cleanup_empty_dirs(content_path.parent)
                    else:
                        # Decrement reference count
                        metadata["ref_count"] = ref_count - 1
                        self._write_metadata_sync(content_hash, metadata)

                # Clean up lock file after releasing the lock
                if should_delete_lock and lock_path.exists():
                    for attempt in range(3):
                        try:
                            lock_path.unlink()
                            break
                        except PermissionError:
                            if attempt < 2:
                                time.sleep(0.01 * (2**attempt))

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

        return await asyncio.to_thread(_delete_with_lock)

    def _cleanup_empty_dirs(self, dir_path: Path) -> None:
        """Remove empty parent directories up to CAS root."""
        try:
            current = dir_path
            while current != self.cas_root and current.exists():
                if not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
        except OSError:
            pass

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

        try:
            metadata = await self._read_metadata(content_hash)
            ref_count = int(metadata.get("ref_count", 0))
            return HandlerResponse.ok(
                data=ref_count,
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
            chunks = []
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
