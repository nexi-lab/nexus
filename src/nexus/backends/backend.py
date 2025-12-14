"""Unified backend interface for Nexus storage.

This module provides a single, unified interface for all storage backends,
combining content-addressable storage (CAS) with directory operations.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


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

    # === Content Operations (CAS) ===

    @abstractmethod
    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to storage and return its content hash.

        If content already exists (same hash), increments reference count
        instead of writing duplicate data.

        Args:
            content: File content as bytes
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            Content hash (SHA-256 as hex string)

        Raises:
            BackendError: If write operation fails

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read content by its hash.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If read operation fails

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
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            Dictionary mapping content_hash -> content bytes
            Returns None for hashes that don't exist (instead of raising)

        Note:
            Unlike read_content(), this does NOT raise on missing content.
            Missing content is indicated by None values in the result dict.
        """
        result: dict[str, bytes | None] = {}
        for content_hash in content_hashes:
            try:
                result[content_hash] = self.read_content(content_hash, context=context)
            except Exception:
                # Return None for missing/errored content
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
            context: Operation context with user/tenant info (optional, for user-scoped backends)

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
        content = self.read_content(content_hash, context=context)
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def write_stream(
        self,
        chunks: Iterator[bytes],
        context: "OperationContext | None" = None,
    ) -> str:
        """
        Write content from an iterator of chunks and return its content hash.

        This is a memory-efficient alternative to write_content() for large files.
        Instead of requiring entire content in memory, accepts chunks as an iterator.
        Computes hash incrementally while streaming.

        Args:
            chunks: Iterator yielding byte chunks
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            Content hash (SHA-256 as hex string)

        Raises:
            BackendError: If write operation fails

        Example:
            >>> # Stream large file without loading into memory
            >>> def file_chunks(path, chunk_size=8192):
            ...     with open(path, 'rb') as f:
            ...         while chunk := f.read(chunk_size):
            ...             yield chunk
            >>> content_hash = backend.write_stream(file_chunks('/large/file.bin'))

        Note:
            Default implementation collects all chunks and calls write_content().
            Backends should override for true streaming with incremental hashing.
        """
        # Default implementation: collect chunks and call write_content()
        # Backends can override for true streaming with incremental hashing
        content = b"".join(chunks)
        return self.write_content(content, context=context)

    @abstractmethod
    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete content by hash.

        Decrements reference count. Only deletes actual file when
        reference count reaches zero.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If delete operation fails

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if content exists.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            True if content exists, False otherwise

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """
        Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If operation fails

        Note:
            For user_scoped backends, context.user_id determines which user's
            credentials to use. Non-user-scoped backends ignore this parameter.
        """
        pass

    @abstractmethod
    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """
        Get reference count for content.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Returns:
            Number of references to this content

        Raises:
            NexusFileNotFoundError: If content doesn't exist

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
    ) -> None:
        """
        Create a directory.

        For backends without native directory support (e.g., S3),
        this may be a no-op or create marker objects.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Operation context with user/tenant info (optional, for user-scoped backends)

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            BackendError: If operation fails

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
    ) -> None:
        """
        Remove a directory.

        Args:
            path: Directory path
            recursive: Remove non-empty directory (like rm -rf)
            context: Operation context for authentication (optional)

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        pass

    @abstractmethod
    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Path to check
            context: Operation context for authentication (optional)

        Returns:
            True if path is a directory, False otherwise
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
