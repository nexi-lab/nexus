"""Google Cloud Storage connector backend with direct path mapping.

This is a connector backend that maps files directly to GCS bucket paths,
unlike the CAS-based GCSBackend which stores files by content hash.

Use case: Mount external GCS buckets where files should remain at their
original paths, browsable by external tools.

Storage structure:
    bucket/
    ├── workspace/
    │   ├── file.txt          # Stored at actual path
    │   └── data/
    │       └── output.json

Key differences from GCSBackend:
- No CAS transformation (files stored at actual paths)
- No deduplication (same content = multiple files)
- No reference counting
- External tools can browse bucket normally
- Requires backend_path in OperationContext

Authentication:
    Same as GCSBackend - uses Application Default Credentials (ADC):
    - gcloud auth application-default login
    - GOOGLE_APPLICATION_CREDENTIALS environment variable
    - Compute Engine/Cloud Run service account
"""

import hashlib
from typing import TYPE_CHECKING

from google.cloud import storage
from google.cloud.exceptions import NotFound

from nexus.backends.backend import Backend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


class GCSConnectorBackend(Backend):
    """
    Google Cloud Storage connector backend with direct path mapping.

    This backend stores files at their actual paths in GCS, making the
    bucket browsable by external tools. Unlike GCSBackend (CAS-based),
    this connector does NOT transform paths to content hashes.

    Features:
    - Direct path mapping (file.txt → file.txt in GCS)
    - Write-through storage (no local caching)
    - Full workspace compatibility
    - External tool compatibility (bucket remains browsable)
    - Native GCS versioning support (if bucket has versioning enabled)

    Versioning Behavior:
    - If bucket has versioning enabled: Uses GCS generation numbers for version tracking
    - If bucket has no versioning: Only current version retained (overwrites on update)

    Limitations:
    - No deduplication (same content stored multiple times)
    - Requires backend_path in OperationContext
    """

    def __init__(
        self,
        bucket_name: str,
        project_id: str | None = None,
        credentials_path: str | None = None,
        prefix: str = "",
    ):
        """
        Initialize GCS connector backend.

        Args:
            bucket_name: GCS bucket name
            project_id: Optional GCP project ID (inferred from credentials if not provided)
            credentials_path: Optional path to service account credentials JSON file
            prefix: Optional prefix for all paths in bucket (e.g., "data/")
        """
        try:
            if credentials_path:
                self.client = storage.Client.from_service_account_json(
                    credentials_path, project=project_id
                )
            else:
                # Use Application Default Credentials
                self.client = storage.Client(project=project_id)

            self.bucket = self.client.bucket(bucket_name)
            self.bucket_name = bucket_name
            self.prefix = prefix.rstrip("/")  # Remove trailing slash

            # Verify bucket exists and check versioning status
            if not self.bucket.exists():
                raise BackendError(
                    f"Bucket '{bucket_name}' does not exist",
                    backend="gcs_connector",
                    path=bucket_name,
                )

            # Check if bucket has versioning enabled
            self.bucket.reload()  # Load bucket metadata
            self.versioning_enabled = self.bucket.versioning_enabled or False

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to initialize GCS connector backend: {e}",
                backend="gcs_connector",
                path=bucket_name,
            ) from e

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "gcs_connector"

    def _get_gcs_path(self, backend_path: str) -> str:
        """
        Convert backend-relative path to GCS object path.

        Args:
            backend_path: Path relative to mount point (e.g., "file.txt")

        Returns:
            Full GCS object path including prefix (e.g., "data/file.txt")
        """
        backend_path = backend_path.lstrip("/")
        if self.prefix:
            return f"{self.prefix}/{backend_path}"
        return backend_path

    def _compute_hash(self, content: bytes) -> str:
        """Compute SHA-256 hash of content for metadata compatibility."""
        return hashlib.sha256(content).hexdigest()

    # === Content Operations (Path-based, not CAS) ===

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to GCS at actual path (not CAS path).

        Requires backend_path in context to know where to write.

        Args:
            content: File content as bytes
            context: Operation context with backend_path

        Returns:
            Content hash (for metadata compatibility, not used for storage path)

        Raises:
            ValueError: If backend_path is not provided in context
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "GCS connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes."
            )

        # Get actual GCS path from backend_path
        gcs_path = self._get_gcs_path(context.backend_path)

        try:
            # Write directly to actual path in GCS
            blob = self.bucket.blob(gcs_path)
            blob.upload_from_string(content, timeout=60)

            # If bucket has versioning enabled, return generation number
            # Otherwise, return content hash for metadata tracking
            if self.versioning_enabled:
                # Reload blob to get the generation number assigned by GCS
                blob.reload()
                return str(blob.generation)
            else:
                # No versioning - compute hash for metadata
                content_hash = self._compute_hash(content)
                return content_hash

        except Exception as e:
            raise BackendError(
                f"Failed to write content to {gcs_path}: {e}",
                backend="gcs_connector",
                path=gcs_path,
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read content from GCS using backend_path.

        For connector backends with versioning enabled:
        - content_hash is the GCS generation number
        - Reads that specific version from GCS

        For connector backends without versioning:
        - content_hash is ignored (just metadata hash)
        - Always reads current content from backend_path

        Args:
            content_hash: GCS generation number (if versioning) or hash (if not)
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
                "GCS connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes."
            )

        # Get actual GCS path from backend_path
        gcs_path = self._get_gcs_path(context.backend_path)

        try:
            # If versioning enabled and content_hash looks like a generation number,
            # retrieve that specific version
            if self.versioning_enabled and content_hash.isdigit():
                generation = int(content_hash)
                blob = self.bucket.blob(gcs_path, generation=generation)
            else:
                # No versioning or hash-based identifier - read current version
                blob = self.bucket.blob(gcs_path)

            if not blob.exists():
                raise NexusFileNotFoundError(gcs_path)

            content = blob.download_as_bytes(timeout=60)
            return bytes(content)

        except NotFound as e:
            raise NexusFileNotFoundError(gcs_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to read content from {gcs_path}: {e}",
                backend="gcs_connector",
                path=gcs_path,
            ) from e

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete content from GCS using backend_path.

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
            raise ValueError("GCS connector requires backend_path in OperationContext")

        gcs_path = self._get_gcs_path(context.backend_path)

        try:
            blob = self.bucket.blob(gcs_path)

            if not blob.exists():
                raise NexusFileNotFoundError(gcs_path)

            blob.delete(timeout=60)

        except NotFound as e:
            raise NexusFileNotFoundError(gcs_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete content at {gcs_path}: {e}",
                backend="gcs_connector",
                path=gcs_path,
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
            gcs_path = self._get_gcs_path(context.backend_path)
            blob = self.bucket.blob(gcs_path)
            return bool(blob.exists())
        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """
        Get content size using backend_path.

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
            raise ValueError("GCS connector requires backend_path in OperationContext")

        gcs_path = self._get_gcs_path(context.backend_path)

        try:
            blob = self.bucket.blob(gcs_path)

            if not blob.exists():
                raise NexusFileNotFoundError(gcs_path)

            blob.reload()
            size = blob.size
            if size is None:
                raise BackendError(
                    "Failed to get content size: size is None",
                    backend="gcs_connector",
                    path=gcs_path,
                )
            return int(size)

        except NotFound as e:
            raise NexusFileNotFoundError(gcs_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get content size for {gcs_path}: {e}",
                backend="gcs_connector",
                path=gcs_path,
            ) from e

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

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> None:
        """
        Create directory marker in GCS.

        GCS doesn't have native directories, so we create marker objects
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

        # GCS directories are represented with trailing slash
        gcs_path = self._get_gcs_path(path) + "/"

        try:
            blob = self.bucket.blob(gcs_path)

            if blob.exists():
                if not exist_ok:
                    raise FileExistsError(f"Directory already exists: {path}")
                return

            if not parents:
                # Check if parent exists
                parent = "/".join(path.split("/")[:-1])
                if parent and not self.is_directory(parent):
                    raise FileNotFoundError(f"Parent directory not found: {parent}")

            # Create directory marker
            blob.upload_from_string("", content_type="application/x-directory", timeout=60)

        except (FileExistsError, FileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to create directory {path}: {e}",
                backend="gcs_connector",
                path=path,
            ) from e

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove directory from GCS.

        Args:
            path: Directory path
            recursive: Remove non-empty directory

        Raises:
            BackendError: If trying to remove root
            NexusFileNotFoundError: If directory doesn't exist
            OSError: If directory not empty and recursive=False
            BackendError: If operation fails
        """
        path = path.strip("/")
        if not path:
            raise BackendError("Cannot remove root directory", backend="gcs_connector", path=path)

        gcs_path = self._get_gcs_path(path) + "/"

        try:
            blob = self.bucket.blob(gcs_path)

            if not blob.exists():
                raise NexusFileNotFoundError(path)

            if not recursive:
                # Check if directory is empty
                blobs = list(
                    self.client.list_blobs(
                        self.bucket_name, prefix=gcs_path, max_results=2, timeout=60
                    )
                )
                if len(blobs) > 1:
                    raise OSError(f"Directory not empty: {path}")

            # Delete directory marker
            blob.delete(timeout=60)

            if recursive:
                # Delete all objects with this prefix
                blobs = self.client.list_blobs(self.bucket_name, prefix=gcs_path, timeout=60)
                for blob in blobs:
                    blob.delete(timeout=60)

        except NotFound as e:
            raise NexusFileNotFoundError(path) from e
        except (NexusFileNotFoundError, OSError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to remove directory {path}: {e}",
                backend="gcs_connector",
                path=path,
            ) from e

    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Path to check

        Returns:
            True if path is a directory, False otherwise
        """
        try:
            path = path.strip("/")
            if not path:
                return True  # Root is always a directory

            gcs_path = self._get_gcs_path(path) + "/"
            blob = self.bucket.blob(gcs_path)
            return bool(blob.exists())

        except Exception:
            return False

    def list_dir(self, path: str) -> list[str]:
        """
        List directory contents.

        Args:
            path: Directory path to list

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
            gcs_base_path = self._get_gcs_path(path)
            prefix = gcs_base_path + "/" if gcs_base_path else ""

            # List blobs with this prefix and delimiter
            blobs = self.bucket.list_blobs(prefix=prefix, delimiter="/")

            entries = set()

            # Add direct file blobs
            for blob in blobs:
                name = blob.name[len(prefix) :]
                if name and name != "":
                    entries.add(name.rstrip("/"))

            # Add subdirectories
            for prefix_path in blobs.prefixes:
                name = prefix_path[len(prefix) :].rstrip("/")
                if name:
                    entries.add(name + "/")

            return sorted(entries)

        except (FileNotFoundError, NotADirectoryError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="gcs_connector",
                path=path,
            ) from e
