"""Unified backend interface for Nexus storage.

This module provides a single, unified interface for all storage backends,
combining content-addressable storage (CAS) with directory operations.

Backend inherits from ObjectStoreABC (the kernel contract) and adds
service-level methods (describe, capabilities, check_connection, etc.).
"""

from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from nexus.core.object_store import ObjectStoreABC, WriteResult

if TYPE_CHECKING:
    from nexus.contracts.backend_features import BackendFeature
    from nexus.contracts.types import OperationContext


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


class Backend(ObjectStoreABC):
    """
    Unified backend interface for storage operations.

    Inherits from ObjectStoreABC (the kernel contract) and adds service-level
    methods: check_connection, describe, capabilities,
    get_object_type, get_object_id, get_file_info, list_dir.

    All storage backends (LocalFS, S3, GCS, etc.) implement this interface.
    It combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Directory operations for filesystem compatibility

    Content Operations:
    - Files stored by SHA-256 hash
    - Automatic deduplication (same content = stored once)
    - Reachability-based GC for safe cleanup

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

    # ------------------------------------------------------------------
    # ``name`` is inherited as abstract from ObjectStoreABC -- subclasses
    # must implement it.  No redeclaration needed here.
    # ------------------------------------------------------------------

    # === Service-level properties (not on ObjectStoreABC) ===

    @property
    def is_connected(self) -> bool:
        """Whether the backend is currently connected.

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
        """Whether this backend is safe for concurrent access from multiple threads.

        Thread-safe backends can share a single instance across threads.
        Non-thread-safe backends require per-thread instances or connection pooling.

        Returns:
            True if thread-safe, False otherwise
            Default: True (most backends are thread-safe)
        """
        return True

    # === Service-level capability flags (not on ObjectStoreABC) ===

    @property
    def has_root_path(self) -> bool:
        """Whether this backend has a local root_path for physical storage.

        Only CASLocalBackend has a root_path attribute pointing to local
        disk storage with CAS and directory subdirectories.

        Returns:
            True if backend has root_path attribute, False otherwise
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

    # === Capability Discovery (Issue #2069) ===

    _BACKEND_FEATURES: ClassVar["frozenset[BackendFeature]"] = frozenset()
    """Capabilities declared by this backend class.

    Subclasses override to declare their supported capabilities.
    Consumers query via ``has_feature()`` or ``cap in backend.backend_features``.
    """

    @property
    def backend_features(self) -> "frozenset[BackendFeature]":
        """All features supported by this backend."""
        return self._BACKEND_FEATURES

    def has_feature(self, cap: "BackendFeature") -> bool:
        """Check whether this backend supports a specific capability."""
        return cap in self._BACKEND_FEATURES

    # === Chain Introspection (Issue #1449) ===

    def describe(self) -> str:
        """Return a human-readable description of this backend for debugging.

        Leaf backends return their ``name``.  Wrappers override to build
        the full composition chain, e.g. ``"cache → logging → s3"``.

        See NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping Rule #3.
        """
        return self.name

    # === Connection Management ===

    def close(self) -> None:
        """Release resources (ObjectStoreABC lifecycle)."""

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
            details={"backend": self.name, "user_scoped": getattr(self, "user_scoped", False)},
        )

    # === Content Operations (CAS) ===

    @abstractmethod
    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """
        Write content to storage and return a WriteResult.

        If content already exists (same identifier), skips the write
        (CAS deduplication).

        Args:
            content: File content as bytes
            content_id: Target address for the content.
                CAS backends: ignored (address = hash of content).
                PAS backends: blob path where content will be stored.
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            WriteResult with content_id, version, and size.

        Raises:
            BackendError: If write operation fails.
        """
        pass

    @abstractmethod
    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read content by its opaque identifier.

        Args:
            content_id: Opaque content identifier (e.g. SHA-256 hash for CAS,
                version ID for path-based backends)
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If read operation fails.
        """
        pass

    def batch_read_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        """
        Read multiple content items by their identifiers (batch operation).

        This is an optimization to reduce round-trips for backends that support
        batch operations. Default implementation calls read_content() for each id.
        Backends should override this for better performance.

        Args:
            content_ids: List of opaque content identifiers
            context: Shared operation context (used when per-id context is not available)
            contexts: Per-id operation contexts mapping content_id -> OperationContext.
                     Used by path-based backends (S3, etc.) that need per-file backend_path.
                     Falls back to ``context`` for ids not in this dict.

        Returns:
            Dictionary mapping content_id -> content bytes
            Returns None for ids that don't exist (instead of raising)

        Note:
            Unlike read_content(), this does NOT raise on missing content.
            Missing content is indicated by None values in the result dict.
        """
        result: dict[str, bytes | None] = {}
        for content_id in content_ids:
            ctx = contexts.get(content_id, context) if contexts else context
            try:
                result[content_id] = self.read_content(content_id, context=ctx)
            except Exception:
                result[content_id] = None
        return result

    def stream_content(
        self, content_id: str, chunk_size: int = 8192, context: "OperationContext | None" = None
    ) -> "Iterator[bytes]":
        """
        Stream content by its identifier in chunks (generator).

        This is a memory-efficient alternative to read_content() for large files.
        Instead of loading entire file into memory, yields chunks as an iterator.

        Args:
            content_id: Opaque content identifier
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If read operation fails

        Example:
            >>> # Stream large file without loading into memory
            >>> for chunk in backend.stream_content(content_id):
            ...     process_chunk(chunk)  # Process incrementally
        """
        # Default implementation: read entire file and yield in chunks
        # Backends can override for true streaming from storage
        self._validate_stream_chunk_size(chunk_size)
        content = self.read_content(content_id, context=context)
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def stream_range(
        self,
        content_id: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> "Iterator[bytes]":
        """Stream a byte range [start, end] inclusive from stored content.

        Default implementation reads full content and slices. Backends with
        seekable storage should override for efficiency.

        Args:
            content_id: Opaque content identifier
            start: First byte position (inclusive, 0-based)
            end: Last byte position (inclusive, 0-based)
            chunk_size: Size of each yielded chunk in bytes
            context: Operation context (optional)

        Yields:
            bytes: Chunks covering the requested range
        """
        self._validate_stream_range(start, end, chunk_size)
        content = self.read_content(content_id, context=context)
        sliced = content[start : end + 1]
        for i in range(0, len(sliced), chunk_size):
            yield sliced[i : i + chunk_size]

    def write_stream(
        self,
        chunks: Iterator[bytes],
        content_id: str = "",
        *,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """
        Write content from an iterator of chunks and return a WriteResult.

        This is a memory-efficient alternative to write_content() for large files.
        Instead of requiring entire content in memory, accepts chunks as an iterator.
        Computes hash incrementally while streaming.

        Args:
            chunks: Iterator yielding byte chunks
            content_id: Target address (see ``write_content``).
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Returns:
            WriteResult with content_id, version, and size.

        Note:
            Default implementation collects all chunks and calls write_content().
            Backends should override for true streaming with incremental hashing.
        """
        # Default implementation: collect chunks and call write_content()
        # Backends can override for true streaming with incremental hashing
        content = b"".join(chunks)
        return self.write_content(content, content_id, context=context)

    @abstractmethod
    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        """
        Delete content by identifier.

        Physically deletes the content blob. For CAS backends, this is
        typically called by explicit delete_content, not by kernel
        (kernel uses metadata-only deletion with GC for cleanup).

        Args:
            content_id: Opaque content identifier
            context: Operation context with user/zone info (optional, for user-scoped backends)

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If delete operation fails.
        """
        pass

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        """Check if content exists.

        This is a service-level method, NOT part of the kernel contract.
        The kernel uses metastore to check existence, not the backend directly.

        Subclasses should override if they support existence checks.

        Args:
            content_id: Opaque content identifier
            context: Operation context (optional)

        Returns:
            True if content exists, False otherwise.
        """
        raise NotImplementedError(f"Backend '{self.name}' does not implement content_exists")

    @abstractmethod
    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        """
        Get content size in bytes.

        Args:
            content_id: Opaque content identifier
            context: Operation context (optional)

        Returns:
            Content size in bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
        """
        pass

    # === Directory Operations ===

    @abstractmethod
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Create a directory.

        For backends without native directory support (e.g., S3),
        this may be a no-op or create marker objects.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Operation context (optional)

        Raises:
            BackendError: If directory creation fails.
        """
        pass

    @abstractmethod
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Remove a directory.

        Args:
            path: Directory path
            recursive: Remove non-empty directory (like rm -rf)
            context: Operation context for authentication (optional)

        Raises:
            NexusFileNotFoundError: If directory does not exist.
            BackendError: If directory removal fails.
        """
        pass

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory.

        This is a service-level method, NOT part of the kernel contract.
        The kernel uses ``meta.mime_type == "inode/directory"`` instead.

        Subclasses should override if they support directory checks.

        Args:
            path: Path to check
            context: Operation context (optional)

        Returns:
            True if path is a directory, False otherwise.
        """
        raise NotImplementedError(f"Backend '{self.name}' does not implement is_directory")

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

    def get_file_info(self, path: str, context: "OperationContext | None" = None) -> "FileInfo":
        """Get file metadata for delta sync change detection (Issue #1127).

        Returns file size, modification time, and backend-specific version
        identifier for efficient change detection during incremental sync.

        Args:
            path: File path relative to backend root
            context: Operation context for authentication (optional)

        Returns:
            FileInfo with size, mtime, backend_version, content_hash.

        Raises:
            NotImplementedError: If backend doesn't support delta sync.
            NexusFileNotFoundError: If file not found.
            BackendError: If metadata retrieval fails.
        """
        raise NotImplementedError(
            f"Backend '{self.name}' does not support get_file_info for delta sync"
        )

    # === ReBAC Object Type Mapping ===

    def get_object_type(self, backend_path: str) -> str:
        """
        Map backend path to ReBAC object type.

        Override in subclasses for custom object type mapping.
        Called by ObjectTypeMapper as the virtual dispatch target.

        Used by the permission enforcer to determine what type of object
        is being accessed for ReBAC permission checks. This allows different
        backends to have different permission models.

        Args:
            _backend_path: Path relative to backend (no mount point prefix)

        Returns:
            ReBAC object type string

        Examples:
            CASLocalBackend: "file"
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

        Override in subclasses for custom object ID mapping.
        Called by ObjectTypeMapper as the virtual dispatch target.

        Used by the permission enforcer to identify the specific object
        being accessed in ReBAC permission checks.

        Args:
            backend_path: Path relative to backend

        Returns:
            Object identifier for ReBAC

        Examples:
            CASLocalBackend: backend_path (full relative path)
            PostgresBackend: "public/users" (schema/table)
            RedisBackend: "prod-cache" (instance name)

        Note:
            Default implementation returns the path as-is.
            Backends can override to return more appropriate identifiers.
        """
        return backend_path


@runtime_checkable
class AsyncBackend(Protocol):
    """Async variant of Backend -- ObjectStore ABC for async drivers.

    Mirrors the Backend interface with async method signatures.
    Required by the Four Pillars architecture: all ObjectStore drivers
    must implement the pillar ABC for interchangeability.
    """

    @property
    def name(self) -> str: ...

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult: ...

    async def read_content(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> bytes: ...

    async def delete_content(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> None: ...

    async def content_exists(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> bool: ...

    def stream_content(
        self,
        content_id: str,
        chunk_size: int = 65536,
        context: "OperationContext | None" = None,
    ) -> AsyncIterator[bytes]: ...
