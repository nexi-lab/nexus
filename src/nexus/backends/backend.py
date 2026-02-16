"""Unified backend interface for Nexus storage.

This module provides a single, unified interface for all storage backends,
combining content-addressable storage (CAS) with directory operations.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.permissions.permissions_enhanced import EnhancedOperationContext


@dataclass
class FileInfo:
    """File metadata for delta sync change detection (Issue #1127).

    Used by get_file_info() to return file metadata for comparison
    during incremental sync operations.

    Attributes:
        size: File size in bytes
        mtime: Last modification time (from backend)
        backend_version: Backend-specific version identifier
            - GCS: generation number (monotonically increasing)
            - S3: version ID (if versioning enabled)
            - Local: inode + mtime as string
        content_hash: Optional content hash if already computed
    """

    size: int
    mtime: datetime | None = None
    backend_version: str | None = None
    content_hash: str | None = None


@dataclass
class HandlerStatusResponse:
    """Response from backend connection health checks.

    Inspired by MindsDB's handler pattern for consistent health monitoring
    across all backend implementations.

    Attributes:
        success: Whether the connection/operation succeeded
        error_message: Human-readable error description if failed
        latency_ms: Time taken for the health check in milliseconds
        details: Additional backend-specific status information
    """

    success: bool
    error_message: str | None = None
    latency_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {"success": self.success}
        if self.error_message:
            result["error_message"] = self.error_message
        if self.latency_ms is not None:
            result["latency_ms"] = self.latency_ms
        if self.details:
            result["details"] = self.details
        return result


class Backend(ABC):
    """
    Unified backend interface for storage operations.

    All storage backends (LocalFS, S3, GCS, etc.) implement this interface.
    It combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Directory operations for filesystem compatibility

    Content Operations:
    - Files stored by SHA-256 hash
    - Automatic deduplication (same content = stored once)
    - Reference counting for safe deletion

    Directory Operations:
    - Virtual directory structure (metadata-based or backend-native)
    - Compatible with path router and mounting
    """

    @staticmethod
    def resolve_database_url(db_param: str) -> str:
        """
        Resolve database URL with TOKEN_MANAGER_DB environment variable priority.

        This utility method is used by connector backends (GDrive, Gmail, X) to
        resolve the database URL for TokenManager, giving priority to the
        TOKEN_MANAGER_DB environment variable over the provided parameter.

        Args:
            db_param: Database URL or path provided to the connector

        Returns:
            Resolved database URL (from env var if set, otherwise db_param)

        Examples:
            >>> import os
            >>> os.environ['TOKEN_MANAGER_DB'] = 'postgresql://localhost/nexus'
            >>> Backend.resolve_database_url('sqlite:///local.db')
            'postgresql://localhost/nexus'
        """
        import os

        return os.getenv("TOKEN_MANAGER_DB") or db_param

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Backend identifier name.

        Returns:
            Backend name (e.g., "local", "gcs", "s3")
        """
        pass

    @property
    def user_scoped(self) -> bool:
        """
        Whether this backend requires per-user credentials (OAuth-based).

        User-scoped backends (e.g., Google Drive, OneDrive) use different
        credentials for each user. The backend will receive OperationContext
        to determine which user's credentials to use.

        Non-user-scoped backends (e.g., GCS, S3) use shared service account
        credentials and ignore the context parameter.

        Returns:
            True if backend requires per-user credentials, False otherwise
            Default: False (shared credentials)

        Examples:
            >>> # Shared credentials (GCS, S3)
            >>> gcs_backend.user_scoped
            False

            >>> # Per-user OAuth (Google Drive, OneDrive)
            >>> gdrive_backend.user_scoped
            True
        """
        return False

    @property
    def is_connected(self) -> bool:
        """
        Whether the backend is currently connected.

        For stateless backends (e.g., local filesystem), this always returns True.
        For stateful backends (e.g., databases, cloud services with sessions),
        this reflects the actual connection state.

        Returns:
            True if connected/ready, False otherwise
            Default: True (for stateless backends)
        """
        return True

    @property
    def thread_safe(self) -> bool:
        """
        Whether this backend is safe for concurrent access from multiple threads.

        Thread-safe backends can share a single instance across threads.
        Non-thread-safe backends require per-thread instances or connection pooling.

        Returns:
            True if thread-safe, False otherwise
            Default: True (most backends are thread-safe)
        """
        return True

    # === Capability Flags ===
    # These properties declare backend capabilities so that core code can use
    # polymorphic dispatch instead of hasattr/isinstance/getattr checks.
    # Each defaults to False; concrete backends override to return True.

    @property
    def supports_rename(self) -> bool:
        """Whether this backend supports direct file rename/move.

        Backends with path-based storage (e.g., blob connectors) can move
        files at the storage level. CAS backends only need metadata rename.

        Returns:
            True if backend implements rename_file(), False otherwise
            Default: False
        """
        return False

    @property
    def has_virtual_filesystem(self) -> bool:
        """Whether this backend uses a virtual filesystem (e.g., API-backed).

        Virtual filesystem backends (e.g., HackerNews, local_connector)
        map API data or external files to virtual directory structures.
        These backends use path-based reads instead of content-hash reads.

        Returns:
            True if backend uses virtual filesystem, False otherwise
            Default: False
        """
        return False

    @property
    def has_root_path(self) -> bool:
        """Whether this backend has a local root_path for physical storage.

        Only LocalBackend has a root_path attribute pointing to local
        disk storage with CAS and directory subdirectories.

        Returns:
            True if backend has root_path attribute, False otherwise
            Default: False
        """
        return False

    @property
    def has_token_manager(self) -> bool:
        """Whether this backend manages OAuth tokens.

        OAuth-based connectors (Google Drive, Gmail, X, Slack, etc.)
        use a TokenManager for credential management.

        Returns:
            True if backend has a token_manager, False otherwise
            Default: False
        """
        return False

    @property
    def has_data_dir(self) -> bool:
        """Whether this backend has a data_dir for ancillary data storage.

        Some backends provide a data_dir attribute for storing
        non-content data like skills, configs, etc.

        Returns:
            True if backend has data_dir attribute, False otherwise
            Default: False
        """
        return False

    @property
    def is_passthrough(self) -> bool:
        """Whether this backend is a PassthroughBackend for same-box mode.

        PassthroughBackend supports OS-native file watching, in-memory
        advisory locking, and stable pointer paths.

        Returns:
            True if backend is a passthrough backend, False otherwise
            Default: False
        """
        return False

    @property
    def supports_parallel_mmap_read(self) -> bool:
        """Whether this backend supports Rust-accelerated parallel mmap reads.

        Only LocalBackend supports this via the nexus_fast native module.

        Returns:
            True if backend supports parallel mmap reads, False otherwise
            Default: False
        """
        return False

    # === Connection Management ===

    def connect(self, context: "OperationContext | None" = None) -> "HandlerStatusResponse":
        """
        Establish connection to the backend.

        For stateless backends (e.g., local filesystem), this is a no-op.
        For stateful backends (e.g., OAuth services, databases), this
        initializes the connection and validates credentials.

        Args:
            context: Operation context for user-scoped backends (OAuth)

        Returns:
            HandlerStatusResponse with connection status

        Note:
            Default implementation returns success for stateless backends.
            Override in backends that require connection initialization.
        """
        return HandlerStatusResponse(success=True, details={"backend": self.name})

    def disconnect(self, context: "OperationContext | None" = None) -> None:  # noqa: B027
        """
        Close connection and release resources.

        For stateless backends, this is a no-op.
        For stateful backends, this closes connections and cleans up.

        Args:
            context: Operation context for user-scoped backends

        Note:
            Default implementation is no-op for stateless backends.
            Override in backends that hold connections or resources.
        """
        pass

    def check_connection(
        self, context: "OperationContext | None" = None
    ) -> "HandlerStatusResponse":
        """
        Verify the backend connection is healthy.

        Performs a lightweight health check to verify the backend is
        accessible and credentials are valid. For user-scoped backends,
        this checks the specific user's credentials.

        Args:
            context: Operation context for user-scoped backends

        Returns:
            HandlerStatusResponse with health status and latency

        Note:
            Default implementation returns success based on is_connected.
            Override in backends that need active health verification.
        """
        import time

        start = time.perf_counter()
        success = self.is_connected
        latency_ms = (time.perf_counter() - start) * 1000

        return HandlerStatusResponse(
            success=success,
            latency_ms=latency_ms,
            details={"backend": self.name, "user_scoped": self.user_scoped},
        )

    # === Content Operations (CAS) ===

    @abstractmethod
    def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> "HandlerResponse[str]":
        """
        Write content to storage and return its content hash.

        If content already exists (same hash), increments reference count
        instead of writing duplicate data.

        Args:
            content: File content as bytes
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse with content hash (SHA-256 as hex string) in data field

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[bytes]":
        """
        Read content by its hash.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse with file content as bytes in data field

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    def batch_read_content(
        self, content_hashes: list[str], context: "OperationContext | None" = None
    ) -> dict[str, bytes | None]:
        """
        Read multiple content items by their hashes (batch operation).

        This is an optimization to reduce round-trips for backends that support
        batch operations. Default implementation calls read_content() for each hash.
        Backends should override this for better performance.

        Args:
            content_hashes: List of SHA-256 hashes as hex strings
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            Dictionary mapping content_hash -> content bytes
            Returns None for hashes that don't exist (instead of raising)

        Note:
            Unlike read_content(), this does NOT raise on missing content.
            Missing content is indicated by None values in the result dict.
        """
        result: dict[str, bytes | None] = {}
        for content_hash in content_hashes:
            response = self.read_content(content_hash, context=context)
            if response.success:
                result[content_hash] = response.data
            else:
                result[content_hash] = None
        return result

    def stream_content(
        self, content_hash: str, chunk_size: int = 8192, context: "OperationContext | None" = None
    ) -> Any:
        """
        Stream content by its hash in chunks (generator).

        This is a memory-efficient alternative to read_content() for large files.
        Instead of loading entire file into memory, yields chunks as an iterator.

        Args:
            content_hash: SHA-256 hash as hex string
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If read operation fails

        Example:
            >>> # Stream large file without loading into memory
            >>> for chunk in backend.stream_content(content_hash):
            ...     process_chunk(chunk)  # Process incrementally
        """
        # Default implementation: read entire file and yield in chunks
        # Backends can override for true streaming from storage
        response = self.read_content(content_hash, context=context)
        content = response.unwrap()  # Raises on error
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> "Iterator[bytes]":
        """Stream a byte range [start, end] inclusive from stored content.

        Default implementation reads full content and slices. Backends with
        seekable storage should override for efficiency.

        Args:
            content_hash: Content identifier (hash)
            start: First byte position (inclusive, 0-based)
            end: Last byte position (inclusive, 0-based)
            chunk_size: Size of each yielded chunk in bytes
            context: Operation context (optional)

        Yields:
            bytes: Chunks covering the requested range
        """
        response = self.read_content(content_hash, context=context)
        content = response.unwrap()
        sliced = content[start : end + 1]
        for i in range(0, len(sliced), chunk_size):
            yield sliced[i : i + chunk_size]

    def write_stream(
        self,
        chunks: Iterator[bytes],
        context: "OperationContext | None" = None,
    ) -> "HandlerResponse[str]":
        """
        Write content from an iterator of chunks and return its content hash.

        This is a memory-efficient alternative to write_content() for large files.
        Instead of requiring entire content in memory, accepts chunks as an iterator.
        Computes hash incrementally while streaming.

        Args:
            chunks: Iterator yielding byte chunks
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse with content hash (SHA-256 as hex string) in data field

        Example:
            >>> # Stream large file without loading into memory
            >>> def file_chunks(path, chunk_size=8192):
            ...     with open(path, 'rb') as f:
            ...         while chunk := f.read(chunk_size):
            ...             yield chunk
            >>> response = backend.write_stream(file_chunks('/large/file.bin'))
            >>> content_hash = response.unwrap()

        Note:
            Default implementation collects all chunks and calls write_content().
            Backends should override for true streaming with incremental hashing.
        """
        # Default implementation: collect chunks and call write_content()
        # Backends can override for true streaming with incremental hashing
        content = b"".join(chunks)
        return self.write_content(content, context=context)

    @abstractmethod
    def delete_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[None]":
        """
        Delete content by hash.

        Decrements reference count. Only deletes actual file when
        reference count reaches zero.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse indicating success or failure

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def content_exists(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[bool]":
        """
        Check if content exists.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse with True if content exists, False otherwise in data field

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def get_content_size(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[int]":
        """
        Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse with content size in bytes in data field

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def get_ref_count(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[int]":
        """
        Get reference count for content.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse with number of references in data field

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    # === Directory Operations ===

    @abstractmethod
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> "HandlerResponse[None]":
        """
        Create a directory.

        For backends without native directory support (e.g., S3),
        this may be a no-op or create marker objects.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            HandlerResponse indicating success or failure

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> "HandlerResponse[None]":
        """
        Remove a directory.

        Args:
            path: Directory path
            recursive: Remove non-empty directory (like rm -rf)
            context: Operation context for authentication (optional)

        Returns:
            HandlerResponse indicating success or failure
        """
        pass

    @abstractmethod
    def is_directory(
        self, path: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[bool]":
        """
        Check if path is a directory.

        Args:
            path: Path to check
            context: Operation context for authentication (optional)

        Returns:
            HandlerResponse with True if path is a directory, False otherwise in data field
        """
        pass

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List immediate contents of a directory.

        Returns entry names (not full paths) with directories marked
        by a trailing '/' to distinguish them from files.

        This is an optional method that backends can implement to support
        efficient directory listing. If not implemented, the filesystem
        layer will infer directories from file metadata.

        Args:
            path: Directory path to list (relative to backend root)
            context: Operation context for authentication (optional)

        Returns:
            List of entry names (directories have trailing '/')
            Example: ["file.txt", "subdir/", "image.png"]

        Raises:
            FileNotFoundError: If directory doesn't exist
            NotADirectoryError: If path is not a directory
            NotImplementedError: If backend doesn't support directory listing

        Note:
            The default implementation raises NotImplementedError.
            Backends that support efficient directory listing should override this.
        """
        raise NotImplementedError(f"Backend '{self.name}' does not support directory listing")

    # === Delta Sync Support (Issue #1127) ===

    def get_file_info(
        self, path: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[FileInfo]":
        """
        Get file metadata for delta sync change detection (Issue #1127).

        Returns file size, modification time, and backend-specific version
        identifier for efficient change detection during incremental sync.

        Change Detection Strategy (rsync-inspired):
        1. Quick check: Compare size + mtime first (fastest, no I/O)
        2. Backend version: Compare GCS generation or S3 version ID
        3. Content hash: Fallback if above not available

        Args:
            path: File path relative to backend root
            context: Operation context for authentication (optional)

        Returns:
            HandlerResponse with FileInfo containing:
            - size: File size in bytes
            - mtime: Last modification time
            - backend_version: Backend-specific version (GCS generation, S3 version ID)
            - content_hash: Optional content hash if readily available

        Note:
            Default implementation raises NotImplementedError.
            Backends should override to provide efficient metadata retrieval
            without reading file content.

        Example:
            >>> info = backend.get_file_info("data/file.txt").unwrap()
            >>> if info.backend_version != cached_version:
            ...     # File changed, needs sync
            ...     sync_file(path)
        """
        raise NotImplementedError(
            f"Backend '{self.name}' does not support get_file_info for delta sync"
        )

    # === ReBAC Object Type Mapping ===

    def get_object_type(self, _backend_path: str) -> str:
        """
        Map backend path to ReBAC object type.

        Used by the permission enforcer to determine what type of object
        is being accessed for ReBAC permission checks. This allows different
        backends to have different permission models.

        Args:
            _backend_path: Path relative to backend (no mount point prefix)

        Returns:
            ReBAC object type string

        Examples:
            LocalBackend: "file"
            PostgresBackend: "postgres:table" or "postgres:row"
            RedisBackend: "redis:instance" or "redis:key"

        Note:
            Default implementation returns "file" for file storage backends.
            Database/API backends should override to return appropriate types.
        """
        return "file"

    def get_object_id(self, backend_path: str) -> str:
        """
        Map backend path to ReBAC object identifier.

        Used by the permission enforcer to identify the specific object
        being accessed in ReBAC permission checks.

        Args:
            backend_path: Path relative to backend

        Returns:
            Object identifier for ReBAC

        Examples:
            LocalBackend: backend_path (full relative path)
            PostgresBackend: "public/users" (schema/table)
            RedisBackend: "prod-cache" (instance name)

        Note:
            Default implementation returns the path as-is.
            Backends can override to return more appropriate identifiers.
        """
        return backend_path


@runtime_checkable
class AsyncBackend(Protocol):
    """Async variant of Backend — ObjectStore ABC for async drivers.

    Mirrors the Backend interface with async method signatures.
    Required by the Four Pillars architecture: all ObjectStore drivers
    must implement the pillar ABC for interchangeability.
    """

    @property
    def name(self) -> str: ...

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> HandlerResponse[str]: ...

    async def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bytes]: ...

    async def delete_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[None]: ...

    async def content_exists(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bool]: ...

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> AsyncIterator[bytes]: ...
