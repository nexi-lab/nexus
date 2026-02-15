"""Unified local filesystem backend with CAS and directory support."""

import errno
import json
import logging
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.cas_blob_store import CASBlobStore
from nexus.backends.chunked_storage import ChunkedStorageMixin
from nexus.backends.multipart_upload_mixin import MultipartUploadMixin
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.response import HandlerResponse
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus_fast import BloomFilter

    from nexus.core.permissions import OperationContext
    from nexus.services.permissions.permissions_enhanced import EnhancedOperationContext

logger = logging.getLogger(__name__)

# Default Bloom filter settings for CAS
DEFAULT_CAS_BLOOM_CAPACITY = 100_000
DEFAULT_CAS_BLOOM_FP_RATE = 0.01  # 1% false positive rate


@register_connector(
    "local",
    description="Local filesystem with CAS deduplication",
    category="storage",
)
class LocalBackend(Backend, ChunkedStorageMixin, MultipartUploadMixin):
    """
    Unified local filesystem backend with CDC chunked storage.

    Combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Content-Defined Chunking (CDC) for large files (>=16MB)
    - Directory operations for filesystem compatibility

    Storage structure:
        root/
        ├── cas/              # Content storage (by hash)
        │   ├── ab/
        │   │   └── cd/
        │   │       ├── abcd1234...ef56        # Content file or chunk
        │   │       ├── abcd1234...ef56.meta   # Metadata (ref count, is_chunk)
        │   │       ├── 5678efgh...            # Chunked manifest (JSON)
        │   │       └── 5678efgh...meta        # Manifest metadata
        └── dirs/             # Virtual directory structure
            ├── workspace/
            └── projects/

    Features:
    - Content deduplication (same content stored once)
    - CDC chunking for large files (Issue #1074)
    - Per-chunk reference counting for cross-file deduplication
    - Parallel chunk I/O for better throughput
    - Reference counting for safe deletion
    - Atomic write operations
    - Thread-safe file locking
    - Directory support for compatibility
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "root_path": ConnectionArg(
            type=ArgType.PATH,
            description="Root directory for storage",
            required=True,
            config_key="data_dir",
        ),
    }

    _cas_bloom: "BloomFilter | None"

    def __init__(
        self,
        root_path: str | Path,
        content_cache: ContentCache | None = None,
        batch_read_workers: int = 8,
        bloom_capacity: int = DEFAULT_CAS_BLOOM_CAPACITY,
        bloom_fp_rate: float = DEFAULT_CAS_BLOOM_FP_RATE,
        on_write_callback: Any | None = None,
    ):
        """
        Initialize local backend.

        Args:
            root_path: Root directory for storage
            content_cache: Optional content cache for faster reads (default: None)
            batch_read_workers: Max parallel workers for batch reads (default: 8).
                               Use lower values (1-2) for HDDs, higher (8-16) for SSDs/NVMe.
            bloom_capacity: Expected number of CAS entries (default: 100,000)
            bloom_fp_rate: Target false positive rate (default: 0.01 = 1%)
            on_write_callback: Optional callback(path: str) for write notifications
                              (e.g., zoekt reindex). Injected by factory (Issue #1520).
        """
        self.root_path = Path(root_path).resolve()
        self.cas_root = self.root_path / "cas"  # CAS content storage
        self.dir_root = self.root_path / "dirs"  # Directory structure
        self.content_cache = content_cache  # Optional content cache for fast reads
        self.batch_read_workers = batch_read_workers  # Max parallel workers for batch reads
        self._cas_bloom = None
        self._bloom_capacity = bloom_capacity
        self._bloom_fp_rate = bloom_fp_rate
        self._on_write_callback = on_write_callback
        self._ensure_roots()
        self._cas = CASBlobStore(self.cas_root)
        self._init_cas_bloom_filter()

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "local"

    # --- Capability flags ---

    @property
    def has_root_path(self) -> bool:
        """LocalBackend has a local root_path for physical storage."""
        return True

    @property
    def supports_parallel_mmap_read(self) -> bool:
        """LocalBackend supports Rust-accelerated parallel mmap reads."""
        return True

    def _ensure_roots(self) -> None:
        """Create root directories if they don't exist."""
        try:
            self.cas_root.mkdir(parents=True, exist_ok=True)
            self.dir_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackendError(
                f"Failed to create root directories: {e}", backend="local", path=str(self.root_path)
            ) from e

    def _init_cas_bloom_filter(self) -> None:
        """Initialize Bloom filter for fast CAS content existence checks."""
        try:
            from nexus_fast import BloomFilter

            self._cas_bloom = BloomFilter(self._bloom_capacity, self._bloom_fp_rate)
            self._populate_cas_bloom_from_disk()
            logger.debug(
                f"CAS Bloom filter initialized: capacity={self._bloom_capacity}, "
                f"fp_rate={self._bloom_fp_rate}, memory={self._cas_bloom.memory_bytes} bytes"
            )
        except ImportError:
            logger.warning("nexus_fast not available, CAS Bloom filter disabled")
            self._cas_bloom = None
        except Exception as e:
            logger.warning(f"Failed to initialize CAS Bloom filter: {e}")
            self._cas_bloom = None

    def _populate_cas_bloom_from_disk(self) -> None:
        """Populate Bloom filter from existing CAS entries on disk.

        Scans the CAS directory and adds all content hashes to the Bloom filter.
        This is called on startup to ensure the filter reflects disk state.
        """
        if self._cas_bloom is None or not self.cas_root.exists():
            return

        keys: list[str] = []
        try:
            # Scan all content files in CAS directory (excluding .meta and .lock files)
            for content_file in self.cas_root.rglob("*"):
                if content_file.is_file() and content_file.suffix not in (".meta", ".lock"):
                    # The filename is the content hash
                    content_hash = content_file.name
                    keys.append(content_hash)

            if keys:
                self._cas_bloom.add_bulk(keys)
                logger.info(f"CAS Bloom filter populated with {len(keys)} entries from disk")
        except Exception as e:
            logger.warning(f"Failed to populate CAS Bloom filter from disk: {e}")

    def _cas_bloom_check(self, content_hash: str) -> bool:
        """Check Bloom filter for possible CAS content existence.

        Returns:
            True if content might exist (need to check disk)
            False if content definitely does not exist (skip disk I/O)
        """
        if self._cas_bloom is None:
            return True  # No Bloom filter, always check disk
        return bool(self._cas_bloom.might_exist(content_hash))

    def _cas_bloom_add(self, content_hash: str) -> None:
        """Add content hash to Bloom filter after writing to disk."""
        if self._cas_bloom is not None:
            self._cas_bloom.add(content_hash)

    # === Content Operations (CAS) ===

    def _compute_hash(self, content: bytes) -> str:
        """Compute BLAKE3 hash of content (Rust-accelerated).

        Uses BLAKE3 for ~3x faster hashing than SHA-256.
        Falls back to SHA-256 if Rust extension is not available.
        """
        return hash_content(content)

    def _hash_to_path(self, content_hash: str) -> Path:
        """
        Convert content hash to filesystem path.

        Uses two-level directory structure:
        cas/ab/cd/abcd1234...ef56

        Args:
            content_hash: SHA-256 hash as hex string

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

    def _read_metadata(self, content_hash: str) -> dict[str, Any]:
        """Read metadata for content. Delegates to CASBlobStore."""
        return self._cas.read_meta(content_hash).to_dict()

    def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content. Delegates to CASBlobStore."""
        from nexus.backends.cas_blob_store import CASMeta

        self._cas.write_meta(content_hash, CASMeta.from_dict(metadata))

    def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> HandlerResponse[str]:
        """
        Write content to CAS storage and return its hash.

        Large files (>=16MB by default) are automatically chunked using
        Content-Defined Chunking (CDC) for better deduplication across
        file versions and parallel I/O.

        If content already exists, increments reference count.

        Args:
            content: File content as bytes
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content hash in data field
        """
        start_time = time.perf_counter()

        # Route large files to chunked storage (Issue #1074)
        if self._should_chunk(content):
            try:
                content_hash = self._write_chunked(content, context)
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
                    path="chunked",
                )

        # Small files: lock-free CAS via CASBlobStore
        content_hash = self._compute_hash(content)

        try:
            is_new = self._cas.store(content_hash, content)

            # Add to cache since we have the content in memory
            if self.content_cache is not None:
                self.content_cache.put(content_hash, content)

            # Add to Bloom filter for fast future lookups
            self._cas_bloom_add(content_hash)

            # Notify search brick of write (e.g., Zoekt reindex) via callback (Issue #1520)
            if is_new and self._on_write_callback is not None:
                content_path = self._hash_to_path(content_hash)
                self._on_write_callback(str(content_path))

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

    def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bytes]:
        """Read content by its hash with retry for Windows file locking.

        Transparently handles both single-blob and chunked content.
        Chunked content is automatically reassembled from its chunks.

        Uses Bloom filter for fast miss detection after checking in-memory cache.

        Args:
            content_hash: SHA-256/BLAKE3 hash as hex string
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

        # Check if this is chunked content (Issue #1074)
        if self._is_chunked_content(content_hash):
            try:
                content = self._read_chunked(content_hash, context)
                # Add to cache for future reads
                if self.content_cache is not None:
                    self.content_cache.put(content_hash, content)
                return HandlerResponse.ok(
                    data=content,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )
            except FileNotFoundError:
                return HandlerResponse.not_found(
                    path=content_hash,
                    message=f"Chunked content not found: {content_hash}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )
            except Exception as e:
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        # Note: We intentionally do NOT use Bloom filter for early rejection here
        # because another process/instance sharing the same root may have written
        # content that isn't in our Bloom filter. The Bloom filter is only used
        # for content_exists() optimization.
        # TODO: Consider shared Bloom filter for multi-process scenarios.

        # Cache miss - read single-blob from disk
        content_path = self._hash_to_path(content_hash)

        # Retry logic for Windows file locking issues
        max_retries = 3
        retry_delay = 0.01  # 10ms

        for attempt in range(max_retries):
            # Check if file exists (with retry for race conditions)
            if not content_path.exists():
                if attempt < max_retries - 1:
                    # File might be mid-write - retry
                    time.sleep(retry_delay)
                    continue
                # File genuinely doesn't exist
                return HandlerResponse.not_found(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )

            try:
                # Read using mmap for better performance (immutable content files)
                try:
                    from nexus_fast import read_file

                    content = read_file(str(content_path))
                    if content is None:
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay)
                            continue
                        return HandlerResponse.not_found(
                            path=content_hash,
                            message=f"CAS content not found: {content_hash}",
                            execution_time_ms=(time.perf_counter() - start_time) * 1000,
                            backend_name=self.name,
                        )
                except ImportError:
                    # Fallback to standard read
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

                # Add to cache for future reads
                if self.content_cache is not None:
                    self.content_cache.put(content_hash, content)

                return HandlerResponse.ok(
                    data=content,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

            except OSError as e:
                # File might be locked on Windows - retry
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return HandlerResponse.from_exception(
                    e,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=content_hash,
                )

        # Should never reach here
        return HandlerResponse.error(
            message=f"Failed to read content after {max_retries} retries",
            code=500,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
            path=content_hash,
        )

    def batch_read_content(
        self, content_hashes: list[str], context: "OperationContext | None" = None
    ) -> dict[str, bytes | None]:
        """
        Optimized batch read for local backend with parallel disk I/O.

        Leverages content cache to reduce disk I/O operations, then reads
        uncached content in parallel using a thread pool for improved performance
        on SSDs and network storage.

        Args:
            content_hashes: List of SHA-256 hashes as hex strings
            context: Operation context (ignored for local backend)

        Performance:
            - Cache hits: O(1) per file
            - Cache misses: Parallel disk reads (up to 8 concurrent)
            - Expected speedup: 3-8x for batch reads with cache misses
        """
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

        # Second pass: read uncached content from disk in parallel
        if uncached_hashes:
            # Use parallel reads for better I/O throughput on SSDs
            # Limit workers to configured max or file count, whichever is smaller
            max_workers = min(self.batch_read_workers, len(uncached_hashes))

            if max_workers == 1:
                # Single file - no need for thread pool overhead
                response = self.read_content(uncached_hashes[0], context=context)
                result[uncached_hashes[0]] = response.data if response.success else None
            else:
                # Multiple files - use parallel reads
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def read_one(content_hash: str) -> tuple[str, bytes | None]:
                    """Read a single file, returning (hash, content) or (hash, None) on error."""
                    response = self.read_content(content_hash, context=context)
                    return (content_hash, response.data if response.success else None)

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(read_one, h): h for h in uncached_hashes}
                    for future in as_completed(futures):
                        hash_key, file_content = future.result()
                        result[hash_key] = file_content

        return result

    def stream_content(
        self, content_hash: str, chunk_size: int = 8192, context: "OperationContext | None" = None
    ) -> Any:
        """
        Stream content from disk in chunks without loading entire file into memory.

        This is optimized for local filesystem to use native file streaming.
        For very large files (GB+), this prevents memory exhaustion.

        Args:
            content_hash: SHA-256 hash as hex string
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context (ignored for local backend)
        """
        content_path = self._hash_to_path(content_hash)

        # Check if file exists
        if not content_path.exists():
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        # Stream file in chunks directly from disk
        try:
            with open(content_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except OSError as e:
            raise BackendError(
                f"Failed to stream content: {e}", backend="local", path=content_hash
            ) from e

    def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> "Iterator[bytes]":
        """Efficient seek-based range streaming for local CAS.

        Uses file seek to jump directly to the requested offset, avoiding
        reading unnecessary bytes. Much faster than the default read+slice
        for large files.

        Args:
            content_hash: Content hash (BLAKE3 hex)
            start: First byte position (inclusive, 0-based)
            end: Last byte position (inclusive, 0-based)
            chunk_size: Size of each yielded chunk in bytes
            context: Operation context (ignored for local backend)

        Yields:
            bytes: Chunks covering the requested range
        """
        content_path = self._hash_to_path(content_hash)
        if not content_path.exists():
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        bytes_remaining = end - start + 1
        try:
            with open(content_path, "rb") as f:
                f.seek(start)
                while bytes_remaining > 0:
                    chunk = f.read(min(chunk_size, bytes_remaining))
                    if not chunk:
                        break
                    bytes_remaining -= len(chunk)
                    yield chunk
        except OSError as e:
            raise BackendError(
                f"Failed to stream range: {e}", backend="local", path=content_hash
            ) from e

    def write_stream(
        self,
        chunks: "Iterator[bytes]",
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[str]:
        """
        Write content from an iterator of chunks.

        Collects chunks, computes hash, then delegates to CASBlobStore.

        Args:
            chunks: Iterator yielding byte chunks
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content hash in data field
        """
        # Collect all chunks, then delegate to write_content
        collected_chunks: list[bytes] = []
        for chunk in chunks:
            collected_chunks.append(chunk)
        content = b"".join(collected_chunks)
        return self.write_content(content, context=context)

    def delete_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[None]:
        """Delete content by hash with reference counting.

        Handles both single-blob and chunked content. For chunked content,
        decrements ref_count on all chunks and deletes those with ref_count=0.

        Args:
            content_hash: SHA-256/BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse indicating success or failure
        """
        start_time = time.perf_counter()
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            return HandlerResponse.not_found(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        # Handle chunked content (Issue #1074)
        if self._is_chunked_content(content_hash):
            try:
                self._delete_chunked(content_hash, context)
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

        # Single-blob: delegate to CASBlobStore (no FileLock)
        try:
            self._cas.release(content_hash)

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

    def content_exists(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bool]:
        """Check if content exists.

        Uses Bloom filter for fast miss detection - avoids disk I/O
        for content that definitely doesn't exist.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with True if content exists, False otherwise
        """
        start_time = time.perf_counter()

        # Fast path: Bloom filter says content definitely doesn't exist
        if not self._cas_bloom_check(content_hash):
            return HandlerResponse.ok(
                data=False,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=content_hash,
            )

        content_path = self._hash_to_path(content_hash)
        exists = content_path.exists()

        return HandlerResponse.ok(
            data=exists,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
            path=content_hash,
        )

    def get_content_size(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[int]:
        """Get content size in bytes.

        For chunked content, returns the original file size (not manifest size).

        Args:
            content_hash: SHA-256/BLAKE3 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with content size in bytes
        """
        start_time = time.perf_counter()
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            return HandlerResponse.not_found(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        try:
            # For chunked content, size is stored in metadata (original file size)
            if self._is_chunked_content(content_hash):
                size = self._get_content_size_chunked(content_hash)
            else:
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

    def get_ref_count(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[int]:
        """Get reference count for content.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse with reference count
        """
        start_time = time.perf_counter()

        exists_response = self.content_exists(content_hash, context=context)
        if not exists_response.success or not exists_response.data:
            return HandlerResponse.not_found(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        try:
            metadata = self._read_metadata(content_hash)
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

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Create directory in virtual directory structure.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            _context: Operation context (ignored for local backend)

        Returns:
            HandlerResponse indicating success or failure
        """
        start_time = time.perf_counter()
        full_path = self.dir_root / path.lstrip("/")

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
            if exist_ok:
                return HandlerResponse.ok(
                    data=None,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=path,
                )
            return HandlerResponse.error(
                message=f"Directory already exists: {path}",
                code=409,
                is_expected=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )
        except FileNotFoundError:
            return HandlerResponse.error(
                message=f"Parent directory not found: {path}",
                code=404,
                is_expected=True,
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

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Remove directory from virtual directory structure.

        Returns:
            HandlerResponse indicating success or failure
        """
        start_time = time.perf_counter()
        full_path = self.dir_root / path.lstrip("/")

        if not full_path.exists():
            return HandlerResponse.not_found(
                path=path,
                message=f"Directory not found: {path}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        if not full_path.is_dir():
            return HandlerResponse.error(
                message=f"Path is not a directory: {path}",
                code=400,
                is_expected=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )

        try:
            if recursive:
                shutil.rmtree(full_path)
            else:
                full_path.rmdir()
            return HandlerResponse.ok(
                data=None,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )
        except OSError as e:
            # Directory not empty
            if e.errno in (errno.ENOTEMPTY, 66):  # errno.ENOTEMPTY or macOS errno 66
                return HandlerResponse.error(
                    message=f"Directory not empty: {path}",
                    code=400,
                    is_expected=True,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=path,
                )
            return HandlerResponse.from_exception(
                e,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )

    def is_directory(
        self, path: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bool]:
        """Check if path is a directory.

        Returns:
            HandlerResponse with True if path is a directory, False otherwise
        """
        start_time = time.perf_counter()
        try:
            full_path = self.dir_root / path.lstrip("/")
            is_dir = full_path.exists() and full_path.is_dir()
            return HandlerResponse.ok(
                data=is_dir,
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

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """List directory contents using local filesystem."""
        try:
            full_path = self.dir_root / path.lstrip("/")
            if not full_path.exists():
                raise FileNotFoundError(f"Directory not found: {path}")
            if not full_path.is_dir():
                raise NotADirectoryError(f"Not a directory: {path}")

            entries = []
            for entry in full_path.iterdir():
                name = entry.name
                # Mark directories with trailing slash
                if entry.is_dir():
                    name += "/"
                entries.append(name)

            return sorted(entries)
        except (FileNotFoundError, NotADirectoryError):
            raise
        except Exception as e:
            raise OSError(f"Failed to list directory {path}: {e}") from e

    # === Multipart Upload Operations (Issue #788) ===

    def init_multipart(
        self,
        backend_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Initialize a multipart upload by creating a temp directory.

        Args:
            backend_path: Logical path for the upload target.
            content_type: MIME type (stored in metadata).
            metadata: Optional key-value metadata.

        Returns:
            Upload ID (UUID-based directory name).
        """
        import uuid

        upload_id = str(uuid.uuid4())
        upload_dir = self.root_path / "uploads" / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Store metadata for the upload
        meta = {"content_type": content_type, "backend_path": backend_path}
        if metadata:
            meta.update(metadata)

        meta_path = upload_dir / "_meta.json"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        logger.debug(f"Initialized multipart upload {upload_id} for {backend_path}")
        return upload_id

    def upload_part(
        self,
        backend_path: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> dict[str, Any]:
        """Write a part file to the upload temp directory.

        Args:
            backend_path: Logical path (unused for local, kept for interface).
            upload_id: Upload ID from init_multipart().
            part_number: 1-based part number.
            data: Raw bytes for this chunk.

        Returns:
            Dict with "etag" (hash of the part data) and "part_number".
        """
        upload_dir = self.root_path / "uploads" / upload_id
        if not upload_dir.exists():
            raise BackendError(
                f"Upload directory not found: {upload_id}",
                backend="local",
                path=backend_path,
            )

        part_path = upload_dir / f"part_{part_number:06d}"
        part_path.write_bytes(data)

        part_hash = hash_content(data)
        return {"etag": part_hash, "part_number": part_number}

    def complete_multipart(
        self,
        backend_path: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> str:
        """Assemble all parts into final content and write via CAS.

        Concatenates parts in order, writes to CAS via write_content(),
        then cleans up the temp directory.

        Args:
            backend_path: Logical path (unused for local CAS).
            upload_id: Upload ID from init_multipart().
            parts: Ordered list of part dicts (from upload_part responses).

        Returns:
            Content hash of the assembled file.
        """
        upload_dir = self.root_path / "uploads" / upload_id
        if not upload_dir.exists():
            raise BackendError(
                f"Upload directory not found: {upload_id}",
                backend="local",
                path=backend_path,
            )

        # Sort parts by part_number and concatenate
        sorted_parts = sorted(parts, key=lambda p: p["part_number"])
        assembled = bytearray()
        for part_info in sorted_parts:
            part_path = upload_dir / f"part_{part_info['part_number']:06d}"
            if not part_path.exists():
                raise BackendError(
                    f"Part file not found: part_{part_info['part_number']:06d}",
                    backend="local",
                    path=backend_path,
                )
            assembled.extend(part_path.read_bytes())

        # Write assembled content to CAS
        content = bytes(assembled)
        response = self.write_content(content)
        if not response.success or response.data is None:
            raise BackendError(
                "Failed to write assembled content",
                backend="local",
                path=backend_path,
            )

        content_hash: str = response.data

        # Clean up temp directory
        shutil.rmtree(upload_dir, ignore_errors=True)
        logger.debug(f"Completed multipart upload {upload_id} -> {content_hash}")

        return content_hash

    def abort_multipart(
        self,
        backend_path: str,
        upload_id: str,
    ) -> None:
        """Abort a multipart upload and remove the temp directory.

        Args:
            backend_path: Logical path (unused for cleanup).
            upload_id: Upload ID from init_multipart().
        """
        upload_dir = self.root_path / "uploads" / upload_id
        if upload_dir.exists():
            shutil.rmtree(upload_dir, ignore_errors=True)
            logger.debug(f"Aborted multipart upload {upload_id}")
