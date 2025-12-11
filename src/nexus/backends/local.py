"""Unified local filesystem backend with CAS and directory support."""

import contextlib
import errno
import json
import os
import platform
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


@register_connector(
    "local",
    description="Local filesystem with CAS deduplication",
    category="storage",
)
class LocalBackend(Backend):
    """
    Unified local filesystem backend.

    Combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Directory operations for filesystem compatibility

    Storage structure:
        root/
        ├── cas/              # Content storage (by hash)
        │   ├── ab/
        │   │   └── cd/
        │   │       ├── abcd1234...ef56        # Content file
        │   │       └── abcd1234...ef56.meta   # Metadata (ref count)
        └── dirs/             # Virtual directory structure
            ├── workspace/
            └── projects/

    Features:
    - Content deduplication (same content stored once)
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
        ),
    }

    def __init__(
        self,
        root_path: str | Path,
        content_cache: ContentCache | None = None,
        batch_read_workers: int = 8,
    ):
        """
        Initialize local backend.

        Args:
            root_path: Root directory for storage
            content_cache: Optional content cache for faster reads (default: None)
            batch_read_workers: Max parallel workers for batch reads (default: 8).
                               Use lower values (1-2) for HDDs, higher (8-16) for SSDs/NVMe.
        """
        self.root_path = Path(root_path).resolve()
        self.cas_root = self.root_path / "cas"  # CAS content storage
        self.dir_root = self.root_path / "dirs"  # Directory structure
        self.content_cache = content_cache  # Optional content cache for fast reads
        self.batch_read_workers = batch_read_workers  # Max parallel workers for batch reads
        self._ensure_roots()

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "local"

    def _ensure_roots(self) -> None:
        """Create root directories if they don't exist."""
        try:
            self.cas_root.mkdir(parents=True, exist_ok=True)
            self.dir_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackendError(
                f"Failed to create root directories: {e}", backend="local", path=str(self.root_path)
            ) from e

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
        """Read metadata for content."""
        meta_path = self._get_meta_path(content_hash)

        if not meta_path.exists():
            return {"ref_count": 0, "size": 0}

        # Retry logic for Windows file locking and race conditions
        # Increased retries for high-concurrency scenarios (50+ threads)
        max_retries = 10
        base_delay = 0.001  # 1ms base delay

        for attempt in range(max_retries):
            try:
                # Read directly without locking (metadata files are small and atomic)
                content = meta_path.read_text(encoding="utf-8")
                result: dict[str, Any] = json.loads(content)
                return result
            except json.JSONDecodeError as e:
                # Corrupted metadata - could be mid-write on Windows
                if attempt < max_retries - 1:
                    import random
                    import time

                    # Exponential backoff with jitter for high concurrency
                    delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                    time.sleep(delay)
                    continue
                # Last attempt failed - raise error
                raise BackendError(
                    f"Failed to read metadata: {e}: {content_hash}",
                    backend="local",
                    path=content_hash,
                ) from e
            except OSError as e:
                # File might be locked on Windows - retry with backoff
                if attempt < max_retries - 1:
                    import random
                    import time

                    # Exponential backoff with jitter for high concurrency
                    delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                    time.sleep(delay)
                    continue
                raise BackendError(
                    f"Failed to read metadata: {e}", backend="local", path=content_hash
                ) from e

        # Should never reach here
        raise BackendError(
            f"Failed to read metadata after {max_retries} retries",
            backend="local",
            path=content_hash,
        )

    def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content with retry logic for Windows file locking."""
        meta_path = self._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        # Retry logic for Windows PermissionError during concurrent writes
        # Increased retries for high-concurrency scenarios (50+ threads)
        max_retries = 10
        base_delay = 0.001  # 1ms base delay

        for attempt in range(max_retries):
            tmp_path = None
            try:
                # Atomic write: write to temp file, then replace
                # This avoids Windows file locking issues
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=meta_path.parent, delete=False, suffix=".tmp"
                ) as tmp_file:
                    tmp_path = Path(tmp_file.name)
                    tmp_file.write(json.dumps(metadata))
                    tmp_file.flush()
                    # Force write to disk on Windows
                    os.fsync(tmp_file.fileno())

                # Atomic replace (works better than move on Windows)
                os.replace(str(tmp_path), str(meta_path))
                tmp_path = None  # Successfully moved
                return  # Success!

            except PermissionError as e:
                # Windows-specific: another thread may be accessing the file
                # Clean up temp file
                if tmp_path is not None and tmp_path.exists():
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()

                if attempt < max_retries - 1:
                    # Exponential backoff with jitter
                    import random
                    import time

                    delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                    time.sleep(delay)
                    continue
                else:
                    # Last attempt failed - raise error
                    raise BackendError(
                        f"Failed to write metadata: {e}: {content_hash}",
                        backend="local",
                        path=content_hash,
                    ) from e

            except OSError as e:
                # Clean up temp file on error
                if tmp_path is not None and tmp_path.exists():
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                raise BackendError(
                    f"Failed to write metadata: {e}: {content_hash}",
                    backend="local",
                    path=content_hash,
                ) from e

    def _lock_file(self, path: Path) -> "FileLock":
        """Acquire lock on file."""
        return FileLock(path)

    def _get_lock_path(self, content_hash: str) -> Path:
        """Get the lock file path for a content hash."""
        return self._get_meta_path(content_hash).with_suffix(".lock")

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to CAS storage and return its hash.

        If content already exists, increments reference count.
        Handles race conditions when multiple threads write the same content.

        Args:
            content: File content as bytes
            _context: Operation context (ignored for local backend)
        """
        content_hash = self._compute_hash(content)
        content_path = self._hash_to_path(content_hash)

        # Fix #514: Acquire lock BEFORE checking if content exists
        # This prevents TOCTOU race where multiple threads see "not exists"
        # and all try to create initial metadata with ref_count=1
        lock_path = self._get_lock_path(content_hash)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with self._lock_file(lock_path):
                # Check if content already exists (inside lock)
                if content_path.exists():
                    # Content exists - increment ref_count
                    metadata = self._read_metadata(content_hash)
                    metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                    self._write_metadata(content_hash, metadata)
                    # Add to cache since we have the content in memory
                    if self.content_cache is not None:
                        self.content_cache.put(content_hash, content)
                    return content_hash

                # Content doesn't exist - write atomically
                content_path.parent.mkdir(parents=True, exist_ok=True)

                tmp_path = None
                try:
                    # Write to temp file first
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=content_path.parent, delete=False
                    ) as tmp_file:
                        tmp_path = Path(tmp_file.name)
                        tmp_file.write(content)
                        tmp_file.flush()  # Flush Python buffers
                        os.fsync(tmp_file.fileno())  # Force OS to write to disk

                    # Move to final location (atomic on Unix)
                    os.replace(str(tmp_path), str(content_path))
                    tmp_path = None  # Successfully moved

                    # Create metadata with ref_count=1
                    metadata = {"ref_count": 1, "size": len(content)}
                    self._write_metadata(content_hash, metadata)

                    # Add to cache since we have the content in memory
                    if self.content_cache is not None:
                        self.content_cache.put(content_hash, content)

                    return content_hash

                finally:
                    # Clean up temp file if it still exists
                    if tmp_path is not None and tmp_path.exists():
                        with contextlib.suppress(OSError):
                            tmp_path.unlink()

        except OSError as e:
            raise BackendError(
                f"Failed to write content: {e}", backend="local", path=content_hash
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """Read content by its hash with retry for Windows file locking.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for local backend)
        """
        # Check cache first for fast path
        if self.content_cache is not None:
            cached_content = self.content_cache.get(content_hash)
            if cached_content is not None:
                return cached_content

        # Cache miss - read from disk
        content_path = self._hash_to_path(content_hash)

        # Retry logic for Windows file locking issues
        max_retries = 3
        retry_delay = 0.01  # 10ms

        for attempt in range(max_retries):
            # Check if file exists (with retry for race conditions)
            if not content_path.exists():
                if attempt < max_retries - 1:
                    # File might be mid-write - retry
                    import time

                    time.sleep(retry_delay)
                    continue
                # File genuinely doesn't exist
                raise NexusFileNotFoundError(
                    path=content_hash,
                    message=f"CAS content not found: {content_hash}",
                )

            try:
                # Read directly without locking (content files are immutable after creation)
                content = content_path.read_bytes()

                # Verify hash
                actual_hash = self._compute_hash(content)
                if actual_hash != content_hash:
                    raise BackendError(
                        f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                        backend="local",
                        path=content_hash,
                    )

                # Add to cache for future reads
                if self.content_cache is not None:
                    self.content_cache.put(content_hash, content)

                return content

            except OSError as e:
                # File might be locked on Windows - retry
                if attempt < max_retries - 1:
                    import time

                    time.sleep(retry_delay)
                    continue
                raise BackendError(
                    f"Failed to read content: {e}", backend="local", path=content_hash
                ) from e

        # Should never reach here
        raise BackendError(
            f"Failed to read content after {max_retries} retries",
            backend="local",
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
                try:
                    content = self.read_content(uncached_hashes[0], context=context)
                    result[uncached_hashes[0]] = content
                except Exception:
                    result[uncached_hashes[0]] = None
            else:
                # Multiple files - use parallel reads
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def read_one(content_hash: str) -> tuple[str, bytes | None]:
                    """Read a single file, returning (hash, content) or (hash, None) on error."""
                    try:
                        content = self.read_content(content_hash, context=context)
                        return (content_hash, content)
                    except Exception:
                        return (content_hash, None)

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

    def write_stream(
        self,
        chunks: "Iterator[bytes]",
        context: "OperationContext | None" = None,
    ) -> str:
        """
        Write content from an iterator of chunks.

        Streams content to disk while collecting for hashing.
        Uses same hash algorithm as write_content() for consistency.

        Args:
            chunks: Iterator yielding byte chunks
            context: Operation context (ignored for local backend)

        Returns:
            Content hash (BLAKE3 or SHA-256 as hex string)
        """
        # Write to temp file while collecting chunks for hashing
        # Note: We collect chunks for hashing to ensure consistency with hash_content()
        # which may use Rust BLAKE3. True streaming hash requires matching incremental hasher.
        tmp_path = None
        collected_chunks: list[bytes] = []

        try:
            # Create temp file in CAS directory for atomic move
            self.cas_root.mkdir(parents=True, exist_ok=True)

            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.cas_root, delete=False
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)

                for chunk in chunks:
                    tmp_file.write(chunk)
                    collected_chunks.append(chunk)

                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            # Compute hash using same algorithm as write_content
            content = b"".join(collected_chunks)
            content_hash = self._compute_hash(content)
            total_size = len(content)
            content_path = self._hash_to_path(content_hash)

            # Acquire lock before checking/writing
            lock_path = self._get_lock_path(content_hash)
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            with self._lock_file(lock_path):
                if content_path.exists():
                    # Content exists - increment ref_count, discard temp file
                    metadata = self._read_metadata(content_hash)
                    metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                    self._write_metadata(content_hash, metadata)

                    # Clean up temp file
                    if tmp_path is not None and tmp_path.exists():
                        tmp_path.unlink()
                    tmp_path = None

                    return content_hash

                # Content doesn't exist - move temp file to final location
                content_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(tmp_path), str(content_path))
                tmp_path = None  # Successfully moved

                # Create metadata with ref_count=1
                metadata = {"ref_count": 1, "size": total_size}
                self._write_metadata(content_hash, metadata)

                return content_hash

        except OSError as e:
            raise BackendError(
                f"Failed to write stream: {e}", backend="local", path="stream"
            ) from e
        finally:
            # Clean up temp file if it still exists
            if tmp_path is not None and tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """Delete content by hash with reference counting.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for local backend)
        """
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            raise NexusFileNotFoundError(content_hash)

        # Fix #514: Use file locking to prevent race condition on ref_count
        lock_path = self._get_lock_path(content_hash)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        should_delete_lock = False
        try:
            with self._lock_file(lock_path):
                metadata = self._read_metadata(content_hash)
                ref_count = metadata.get("ref_count", 1)

                if ref_count <= 1:
                    # Last reference - delete file and metadata
                    content_path.unlink()

                    meta_path = self._get_meta_path(content_hash)
                    if meta_path.exists():
                        meta_path.unlink()

                    # Mark lock file for deletion after context manager releases it
                    should_delete_lock = True

                    # Clean up empty directories
                    self._cleanup_empty_dirs(content_path.parent)
                else:
                    # Decrement reference count
                    metadata["ref_count"] = ref_count - 1
                    self._write_metadata(content_hash, metadata)

            # Clean up lock file AFTER releasing the lock (fixes #562 - Windows PermissionError)
            # Retry logic for Windows file locking semantics
            if should_delete_lock and lock_path.exists():
                import time

                for attempt in range(3):
                    try:
                        lock_path.unlink()
                        break
                    except PermissionError:
                        if attempt < 2:
                            # Exponential backoff: 10ms, 20ms
                            time.sleep(0.01 * (2**attempt))
                        else:
                            # Last attempt failed - log warning but don't fail the operation
                            # Lock file will be orphaned but this is better than failing deletion
                            import logging

                            logging.warning(
                                f"Failed to delete lock file {lock_path} after 3 attempts. "
                                "File will be orphaned but content deletion succeeded."
                            )

        except OSError as e:
            raise BackendError(
                f"Failed to delete content: {e}", backend="local", path=content_hash
            ) from e

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

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """Check if content exists.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for local backend)
        """
        content_path = self._hash_to_path(content_hash)
        return content_path.exists()

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for local backend)
        """
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            raise NexusFileNotFoundError(content_hash)

        try:
            return content_path.stat().st_size
        except OSError as e:
            raise BackendError(
                f"Failed to get content size: {e}", backend="local", path=content_hash
            ) from e

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get reference count for content.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context (ignored for local backend)
        """
        if not self.content_exists(content_hash, context=context):
            raise NexusFileNotFoundError(content_hash)

        metadata = self._read_metadata(content_hash)
        return int(metadata.get("ref_count", 0))

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> None:
        """Create directory in virtual directory structure.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            _context: Operation context (ignored for local backend)
        """
        full_path = self.dir_root / path.lstrip("/")

        try:
            if parents:
                full_path.mkdir(parents=True, exist_ok=exist_ok)
            else:
                full_path.mkdir(exist_ok=exist_ok)
        except FileExistsError as e:
            if not exist_ok:
                raise e
        except FileNotFoundError as e:
            raise BackendError(
                f"Parent directory not found: {path}", backend="local", path=path
            ) from e
        except OSError as e:
            raise BackendError(
                f"Failed to create directory: {e}", backend="local", path=path
            ) from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> None:
        """Remove directory from virtual directory structure."""
        full_path = self.dir_root / path.lstrip("/")

        if not full_path.exists():
            raise NexusFileNotFoundError(path)

        if not full_path.is_dir():
            raise BackendError(f"Path is not a directory: {path}", backend="local", path=path)

        try:
            if recursive:
                shutil.rmtree(full_path)
            else:
                full_path.rmdir()
        except OSError as e:
            # Re-raise OSError for "directory not empty"
            if e.errno in (errno.ENOTEMPTY, 66):  # errno.ENOTEMPTY or macOS errno 66
                raise
            raise BackendError(
                f"Failed to remove directory: {e}", backend="local", path=path
            ) from e

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory."""
        try:
            full_path = self.dir_root / path.lstrip("/")
            return full_path.exists() and full_path.is_dir()
        except Exception:
            return False

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


class FileLock:
    """
    Context manager for file locking.

    Uses fcntl.flock on POSIX systems and msvcrt.locking on Windows.
    """

    def __init__(self, path: Path, timeout: float = 10.0):
        """Initialize file lock."""
        self.path = path
        self.timeout = timeout
        self.lock_file: Any = None

    def __enter__(self) -> "FileLock":
        """Acquire lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Use a+b mode: append+binary with read/write
        # This works better on Windows for newly created files
        self.lock_file = open(self.path, "a+b")

        # Platform-specific locking
        if platform.system() == "Windows":
            import msvcrt

            msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Release lock."""
        if self.lock_file:
            # Platform-specific unlocking
            if platform.system() == "Windows":
                import msvcrt

                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)

            self.lock_file.close()
