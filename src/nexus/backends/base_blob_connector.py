"""Base class for blob storage connector backends (S3, GCS, Azure, MinIO, etc.).

This abstract base class provides shared functionality for cloud blob storage
connectors that support direct path mapping (not CAS-based).

Shared features:
- Content hash computation (BLAKE3, Rust-accelerated)
- Content-Type detection with UTF-8 charset handling
- Reference counting (always 1 - no deduplication)
- Common path mapping patterns
- Directory operations with marker objects
- Versioning support patterns

Backend-specific implementations:
- Actual blob upload/download/delete operations
- Authentication and client initialization
- Cloud provider-specific API calls
"""

import mimetypes
from abc import abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

from nexus.backends.backend import Backend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


class BaseBlobStorageConnector(Backend):
    """
    Abstract base class for blob storage connector backends.

    This class provides common functionality for cloud storage services
    that use blob/object storage paradigms (S3, GCS, Azure Blob, MinIO, etc.).

    Subclasses must implement:
    - Cloud-specific client initialization
    - Blob upload/download/delete operations
    - Blob metadata operations (exists, size, list)
    - Cloud-specific path handling

    Shared implementations:
    - Content hash computation
    - Content-Type detection with UTF-8 support
    - Reference counting (no deduplication)
    - Common directory operation patterns
    """

    def __init__(
        self,
        bucket_name: str,
        prefix: str = "",
        versioning_enabled: bool = False,
    ):
        """
        Initialize base blob storage connector.

        Args:
            bucket_name: Storage bucket/container name
            prefix: Optional prefix for all paths (e.g., "data/")
            versioning_enabled: Whether versioning is enabled on the bucket
        """
        self.bucket_name = bucket_name
        self.prefix = prefix.rstrip("/")  # Remove trailing slash
        self.versioning_enabled = versioning_enabled

    # === Helper Methods (Shared Implementation) ===

    def _compute_hash(self, content: bytes) -> str:
        """
        Compute BLAKE3 hash of content (Rust-accelerated).

        Uses BLAKE3 for ~3x faster hashing than SHA-256.
        Falls back to SHA-256 if Rust extension is not available.

        Args:
            content: File content bytes

        Returns:
            Hash as hexadecimal string (64 characters)
        """
        return hash_content(content)

    def _detect_content_type(self, backend_path: str, content: bytes) -> str:
        """
        Detect appropriate Content-Type for file based on path and content.

        For text files, ensures charset=utf-8 is included for proper display
        in cloud consoles and other tools.

        Args:
            backend_path: File path (used for extension-based detection)
            content: File content bytes

        Returns:
            Content-Type string (e.g., "text/plain; charset=utf-8")
        """
        # Try to guess from file extension
        content_type, _ = mimetypes.guess_type(backend_path)

        # If couldn't guess or got text type, try to detect if it's UTF-8 text
        if not content_type or content_type.startswith("text/"):
            try:
                # Try decoding as UTF-8
                content.decode("utf-8")
                # Success! It's UTF-8 text
                if content_type and content_type.startswith("text/"):
                    # Use guessed text type with charset
                    return f"{content_type}; charset=utf-8"
                else:
                    # Default to text/plain with UTF-8
                    return "text/plain; charset=utf-8"
            except UnicodeDecodeError:
                # Not UTF-8 text, use guessed type or default to binary
                return content_type or "application/octet-stream"

        # Use guessed type for non-text files
        return content_type

    def _get_blob_path(self, backend_path: str) -> str:
        """
        Convert backend-relative path to full blob path.

        Args:
            backend_path: Path relative to mount point (e.g., "file.txt")

        Returns:
            Full blob path including prefix (e.g., "data/file.txt")
        """
        backend_path = backend_path.lstrip("/")
        if self.prefix:
            if backend_path:
                return f"{self.prefix}/{backend_path}"
            else:
                # For empty backend_path, return prefix without trailing slash
                return self.prefix
        return backend_path

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """
        Get reference count (always 1 for connector backends).

        Connector backends don't do deduplication, so each file
        has exactly one reference.

        Args:
            content_hash: Content hash
            context: Operation context

        Returns:
            Always 1 (no reference counting)
        """
        # No deduplication - each file is unique
        return 1

    # === Abstract Methods (Must Implement in Subclasses) ===

    @abstractmethod
    def _upload_blob(
        self,
        blob_path: str,
        content: bytes,
        content_type: str,
    ) -> str:
        """
        Upload blob to cloud storage.

        Args:
            blob_path: Full blob path (including prefix)
            content: File content bytes
            content_type: MIME type with optional charset

        Returns:
            Version ID if versioning enabled, else content hash

        Raises:
            BackendError: If upload fails
        """
        pass

    @abstractmethod
    def _download_blob(
        self,
        blob_path: str,
        version_id: str | None = None,
    ) -> tuple[bytes, str | None]:
        """
        Download blob from cloud storage.

        Args:
            blob_path: Full blob path (including prefix)
            version_id: Optional version ID for versioned reads

        Returns:
            Tuple of (content, version_id)
            - content: File content as bytes
            - version_id: Backend version ID as string, or None if not available

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If download fails
        """
        pass

    def _batch_get_versions(
        self,
        backend_paths: list[str],
        contexts: dict[str, "OperationContext"] | None = None,
    ) -> dict[str, str | None]:
        """
        Get versions for multiple files in batch (optimized for bulk operations).

        This method should be overridden by subclasses for cloud-specific optimizations.
        Default implementation calls get_version() for each path (no batching).

        Args:
            backend_paths: List of backend-relative paths
            contexts: Optional dict mapping path -> OperationContext

        Returns:
            Dict mapping backend_path -> version string (or None if no version/doesn't exist)

        Performance:
            - GCS: Single list_blobs() API call (~200ms for 1000s of files)
            - S3: Batch head_object() calls (20-30ms per batch of 1000)
            - Default: Sequential get_version() calls (slow, ~10ms per file)

        Note:
            Subclasses should override for cloud-specific batch operations:
            - GCS: Use list_blobs() with prefix to get all generations at once
            - S3: Use batch head_object() calls with threading
        """
        if not hasattr(self, "get_version"):
            # No version support - return empty dict
            return dict.fromkeys(backend_paths)

        # Default implementation: sequential calls (slow, but works)
        results: dict[str, str | None] = {}
        for path in backend_paths:
            context = contexts.get(path) if contexts else None
            try:
                version = self.get_version(path, context)
                results[path] = version
            except Exception:
                results[path] = None
        return results

    def _bulk_download_blobs(
        self,
        blob_paths: list[str],
        version_ids: dict[str, str] | None = None,
        max_workers: int = 20,
    ) -> dict[str, bytes]:
        """
        Download multiple blobs in parallel using ThreadPoolExecutor.

        This method should be overridden by subclasses for cloud-specific optimizations.
        Default implementation uses ThreadPoolExecutor for parallel downloads.

        Args:
            blob_paths: List of blob paths to download
            version_ids: Optional dict mapping blob_path -> version_id
            max_workers: Number of concurrent downloads (default: 20, conservative)

        Returns:
            Dict mapping blob_path -> content bytes (only successful downloads)

        Performance:
            - Typical speedup: 10-20x over sequential
            - Optimal worker count varies by backend (GCS: 10, S3: 20-30)
            - Graceful degradation: Failed downloads are logged but don't stop others

        Note:
            Subclasses should override with cloud-specific defaults:
            - GCS: 10 workers (optimal, avoids rate limiting)
            - S3: 20-30 workers (higher throughput tolerance)
            - Azure: 15-20 workers (moderate)
        """
        import logging
        from concurrent.futures import ThreadPoolExecutor, as_completed

        logger = logging.getLogger(__name__)

        if not blob_paths:
            return {}

        results: dict[str, bytes] = {}

        def download_one(blob_path: str) -> tuple[str, bytes | None]:
            """Download single blob, return (path, content) or (path, None) on error."""
            try:
                version_id = version_ids.get(blob_path) if version_ids else None
                # Call existing _download_blob() to avoid code duplication
                content, _version_id = self._download_blob(blob_path, version_id)
                return (blob_path, content)
            except Exception as e:
                logger.warning(f"Failed to download {blob_path}: {e}")
                return (blob_path, None)

        # Parallel downloads with thread pool
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all download tasks
            futures = {executor.submit(download_one, path): path for path in blob_paths}

            # Collect results as they complete
            for future in as_completed(futures):
                blob_path, content = future.result()
                if content is not None:
                    results[blob_path] = content

        return results

    @abstractmethod
    def _delete_blob(self, blob_path: str) -> None:
        """
        Delete blob from cloud storage.

        Args:
            blob_path: Full blob path (including prefix)

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If delete fails
        """
        pass

    @abstractmethod
    def _blob_exists(self, blob_path: str) -> bool:
        """
        Check if blob exists in cloud storage.

        Args:
            blob_path: Full blob path (including prefix)

        Returns:
            True if blob exists, False otherwise
        """
        pass

    @abstractmethod
    def _get_blob_size(self, blob_path: str) -> int:
        """
        Get blob size in bytes.

        Args:
            blob_path: Full blob path (including prefix)

        Returns:
            Blob size in bytes

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If operation fails
        """
        pass

    @abstractmethod
    def _list_blobs(
        self,
        prefix: str,
        delimiter: str = "/",
    ) -> tuple[list[str], list[str]]:
        """
        List blobs with given prefix.

        Args:
            prefix: Prefix to filter blobs
            delimiter: Delimiter for virtual directories

        Returns:
            Tuple of (blob_keys, common_prefixes)
            - blob_keys: List of blob object keys
            - common_prefixes: List of virtual directory prefixes

        Raises:
            BackendError: If list operation fails
        """
        pass

    @abstractmethod
    def _create_directory_marker(self, blob_path: str) -> None:
        """
        Create directory marker object.

        Args:
            blob_path: Directory path (should end with '/')

        Raises:
            BackendError: If creation fails
        """
        pass

    @abstractmethod
    def _copy_blob(self, source_path: str, dest_path: str) -> None:
        """
        Copy blob to new location.

        Args:
            source_path: Source blob path
            dest_path: Destination blob path

        Raises:
            NexusFileNotFoundError: If source doesn't exist
            BackendError: If copy fails
        """
        pass

    # === Content Operations (Shared Implementation) ===

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to blob storage at actual path (not CAS path).

        Requires backend_path in context to know where to write.

        Args:
            content: File content as bytes
            context: Operation context with backend_path

        Returns:
            If versioning enabled: Version ID (cloud-specific)
            If no versioning: Content hash (for metadata compatibility)

        Raises:
            ValueError: If backend_path is not provided in context
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                f"{self.name} connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes."
            )

        # Get actual blob path from backend_path
        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Detect appropriate Content-Type with charset for proper encoding
            content_type = self._detect_content_type(context.backend_path, content)

            # Upload blob (subclass implements cloud-specific upload)
            return self._upload_blob(blob_path, content, content_type)

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to write content to {blob_path}: {e}",
                backend=self.name,
                path=blob_path,
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read content from blob storage using backend_path.

        For connector backends with versioning enabled:
        - content_hash is the version ID (cloud-specific)
        - Reads that specific version

        For connector backends without versioning:
        - content_hash is ignored (just metadata hash)
        - Always reads current content from backend_path

        Args:
            content_hash: Version ID (if versioning) or hash (if not)
            context: Operation context with backend_path

        Returns:
            File content as bytes

        Raises:
            ValueError: If backend_path is not provided in context
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                f"{self.name} connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes."
            )

        # Get actual blob path from backend_path
        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Determine if we should use version ID
            version_id = None
            if self.versioning_enabled and content_hash and self._is_version_id(content_hash):
                version_id = content_hash

            # Download blob (subclass implements cloud-specific download)
            content, _version_id = self._download_blob(blob_path, version_id)
            return content

        except (NexusFileNotFoundError, BackendError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to read content from {blob_path}: {e}",
                backend=self.name,
                path=blob_path,
            ) from e

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Iterator[bytes]:
        """
        Stream content from blob storage in chunks.

        This is a memory-efficient alternative to read_content() for large files.
        Yields chunks as an iterator, allowing processing of files larger than RAM.

        For connector backends with versioning enabled:
        - content_hash is the version ID (cloud-specific)
        - Streams that specific version

        For connector backends without versioning:
        - content_hash is ignored (just metadata hash)
        - Always streams current content from backend_path

        Args:
            content_hash: Version ID (if versioning) or hash (if not)
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context with backend_path

        Yields:
            bytes: Chunks of file content

        Raises:
            ValueError: If backend_path is not provided in context
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If stream operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                f"{self.name} connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes."
            )

        # Get actual blob path from backend_path
        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Determine if we should use version ID
            version_id = None
            if self.versioning_enabled and content_hash and self._is_version_id(content_hash):
                version_id = content_hash

            # Stream blob (subclass can override _stream_blob for optimized streaming)
            yield from self._stream_blob(blob_path, chunk_size, version_id)

        except (NexusFileNotFoundError, BackendError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream content from {blob_path}: {e}",
                backend=self.name,
                path=blob_path,
            ) from e

    def _stream_blob(
        self,
        blob_path: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """
        Stream blob content in chunks.

        Default implementation downloads entire blob and yields in chunks.
        Subclasses should override for true streaming from cloud storage.

        Args:
            blob_path: Full blob path (including prefix)
            chunk_size: Size of each chunk in bytes
            version_id: Optional version ID for versioned reads

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If stream operation fails
        """
        # Default implementation: download and chunk
        # Subclasses can override for true streaming
        content, _ = self._download_blob(blob_path, version_id)
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def _is_version_id(self, value: str) -> bool:
        """
        Check if value looks like a version ID (not a hex hash).

        Default implementation: version IDs are not 64-char hex strings.
        Subclasses can override for cloud-specific logic.

        Args:
            value: String to check

        Returns:
            True if likely a version ID, False if likely a hash
        """
        # If it's a 64-character hex string, it's probably a hash
        if len(value) == 64:
            try:
                int(value, 16)
                return False  # It's a hex hash
            except ValueError:
                pass
        return True  # Likely a version ID

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete content from blob storage using backend_path.

        No reference counting - deletes immediately.

        Args:
            content_hash: Content hash (ignored, kept for interface compatibility)
            context: Operation context with backend_path

        Raises:
            ValueError: If backend_path is not provided in context
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If delete operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(f"{self.name} connector requires backend_path in OperationContext")

        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Delete blob (subclass implements cloud-specific delete)
            self._delete_blob(blob_path)

        except (NexusFileNotFoundError, BackendError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete content at {blob_path}: {e}",
                backend=self.name,
                path=blob_path,
            ) from e

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if content exists at backend_path.

        Args:
            content_hash: Content hash (ignored)
            context: Operation context with backend_path

        Returns:
            True if file exists, False otherwise
        """
        if not context or not context.backend_path:
            return False

        try:
            blob_path = self._get_blob_path(context.backend_path)
            return self._blob_exists(blob_path)
        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """
        Get content size using backend_path (cache-first, efficient).

        Performance optimization: Checks cache first to avoid API calls during
        ls -la operations. Only hits cloud storage API if not cached.

        Args:
            content_hash: Content hash (ignored)
            context: Operation context with backend_path

        Returns:
            Content size in bytes

        Raises:
            ValueError: If backend_path is not provided
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(f"{self.name} connector requires backend_path in OperationContext")

        # OPTIMIZATION: Check cache first (efficient - no API call)
        # This is crucial for ls -la performance with many files
        # Only available if connector uses CacheConnectorMixin
        if (
            hasattr(self, "_get_size_from_cache")
            and hasattr(context, "virtual_path")
            and context.virtual_path
        ):
            cached_size = self._get_size_from_cache(context.virtual_path)
            if cached_size is not None:
                assert isinstance(cached_size, int), "Cache size must be int"
                return cached_size

        # Fallback: Get size from cloud storage API
        # This only happens when file is not cached
        blob_path = self._get_blob_path(context.backend_path)

        try:
            return self._get_blob_size(blob_path)

        except (NexusFileNotFoundError, BackendError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get content size for {blob_path}: {e}",
                backend=self.name,
                path=blob_path,
            ) from e

    # === Directory Operations (Shared Implementation) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> None:
        """
        Create directory marker in blob storage.

        Blob storage doesn't have native directories, so we create marker objects
        with trailing slashes.

        Args:
            path: Directory path relative to backend root
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context (not used for directory creation)

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            BackendError: If operation fails
        """
        # Normalize path
        path = path.strip("/")
        if not path:
            return  # Root always exists

        # Directory paths end with trailing slash
        blob_path = self._get_blob_path(path) + "/"

        try:
            # Check if directory marker already exists
            if self._blob_exists(blob_path):
                if not exist_ok:
                    raise FileExistsError(f"Directory already exists: {path}")
                return

            if not parents:
                # Check if parent exists
                parent = "/".join(path.split("/")[:-1])
                if parent and not self.is_directory(parent):
                    raise FileNotFoundError(f"Parent directory not found: {parent}")

            # Create directory marker (subclass implements cloud-specific creation)
            self._create_directory_marker(blob_path)

        except (FileExistsError, FileNotFoundError):
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to create directory {path}: {e}",
                backend=self.name,
                path=path,
            ) from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> None:
        """
        Remove directory from blob storage.

        Args:
            path: Directory path
            recursive: Remove non-empty directory
            context: Operation context (not used for directory removal)

        Raises:
            BackendError: If trying to remove root
            NexusFileNotFoundError: If directory doesn't exist
            OSError: If directory not empty and recursive=False
            BackendError: If operation fails
        """
        path = path.strip("/")
        if not path:
            raise BackendError("Cannot remove root directory", backend=self.name, path=path)

        blob_path = self._get_blob_path(path) + "/"

        try:
            # Check if directory marker exists
            if not self._blob_exists(blob_path):
                raise NexusFileNotFoundError(path)

            if not recursive:
                # Check if directory is empty
                blobs, prefixes = self._list_blobs(prefix=blob_path, delimiter="/")
                # Directory marker itself will be in the list, so check for more than 1
                if len(blobs) > 1 or prefixes:
                    raise OSError(f"Directory not empty: {path}")

            # Delete directory marker
            self._delete_blob(blob_path)

            if recursive:
                # Delete all objects with this prefix
                import contextlib

                blobs, _ = self._list_blobs(prefix=blob_path, delimiter="")
                for blob_key in blobs:
                    if blob_key != blob_path:  # Don't delete marker twice
                        with contextlib.suppress(Exception):
                            # Continue deleting other blobs even if one fails
                            self._delete_blob(blob_key)

        except (NexusFileNotFoundError, OSError):
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to remove directory {path}: {e}",
                backend=self.name,
                path=path,
            ) from e

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if path is a directory.

        In blob storage, a "directory" is either:
        1. An explicit directory marker blob (path ending with "/")
        2. A virtual directory (any prefix that has blobs under it)

        Args:
            path: Path to check
            context: Operation context (not used for directory check)

        Returns:
            True if path is a directory (has marker or has children), False otherwise
        """
        try:
            path = path.strip("/")
            if not path:
                return True  # Root is always a directory

            blob_path = self._get_blob_path(path)

            # Check 1: Explicit directory marker blob
            if self._blob_exists(blob_path + "/"):
                return True

            # Check 2: Virtual directory (has any blobs under this prefix)
            blobs, prefixes = self._list_blobs(prefix=blob_path + "/", delimiter="/")
            return len(blobs) > 0 or len(prefixes) > 0

        except Exception:
            return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents.

        Args:
            path: Directory path to list
            context: Operation context (not used for directory listing)

        Returns:
            List of entry names (directories have trailing '/')

        Raises:
            FileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        try:
            path = path.strip("/")

            # Check if directory exists (except root)
            if path and not self.is_directory(path):
                raise FileNotFoundError(f"Directory not found: {path}")

            # Build prefix for this directory
            blob_base_path = self._get_blob_path(path)
            prefix = blob_base_path + "/" if blob_base_path else ""

            # List blobs with this prefix and delimiter
            blobs, prefixes = self._list_blobs(prefix=prefix, delimiter="/")

            entries = set()

            # Add direct file blobs
            for blob_key in blobs:
                name = blob_key[len(prefix) :]
                if name and name != "":
                    entries.add(name.rstrip("/"))

            # Add subdirectories (common prefixes)
            for prefix_path in prefixes:
                name = prefix_path[len(prefix) :].rstrip("/")
                if name:
                    entries.add(name + "/")

            return sorted(entries)

        except (FileNotFoundError, NotADirectoryError):
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend=self.name,
                path=path,
            ) from e

    def rename_file(
        self,
        old_path: str,
        new_path: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Rename/move a file in blob storage.

        For path-based connector backends, we need to actually move
        the file (copy to new location and delete old).

        Args:
            old_path: Current backend-relative path
            new_path: New backend-relative path
            context: Operation context (not used for file rename)

        Raises:
            FileNotFoundError: If source file doesn't exist
            FileExistsError: If destination already exists
            BackendError: If operation fails
        """
        try:
            old_path = old_path.strip("/")
            new_path = new_path.strip("/")

            old_blob_path = self._get_blob_path(old_path)
            new_blob_path = self._get_blob_path(new_path)

            # Check source exists
            if not self._blob_exists(old_blob_path):
                raise FileNotFoundError(f"Source file not found: {old_path}")

            # Check destination doesn't exist
            if self._blob_exists(new_blob_path):
                raise FileExistsError(f"Destination already exists: {new_path}")

            # Copy to new location (subclass implements cloud-specific copy)
            self._copy_blob(old_blob_path, new_blob_path)

            # Delete old location
            self._delete_blob(old_blob_path)

        except (FileNotFoundError, FileExistsError):
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to rename file {old_path} -> {new_path}: {e}",
                backend=self.name,
                path=old_path,
            ) from e
