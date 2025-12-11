"""Google Cloud Storage backend with CAS and directory support.

Authentication (Recommended):
    Use service account credentials for production (no daily re-auth):
    1. Create service account: gcloud iam service-accounts create nexus-storage-sa
    2. Grant permissions: gcloud projects add-iam-policy-binding PROJECT_ID
    3. Download key: gcloud iam service-accounts keys create gcs-credentials.json
    4. Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcs-credentials.json

    Alternative (Development Only):
    - gcloud auth application-default login (requires daily re-authentication)
    - Compute Engine/Cloud Run service account (auto-detected)
"""

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from google.cloud import storage
from google.cloud.exceptions import NotFound

from nexus.backends.backend import Backend
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


@register_connector(
    "gcs",
    description="Google Cloud Storage with CAS deduplication",
    category="storage",
    requires=["google-cloud-storage"],
)
class GCSBackend(Backend):
    """
    Google Cloud Storage backend.

    Combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Directory operations for filesystem compatibility

    Storage structure:
        bucket/
        ├── cas/              # Content storage (by hash)
        │   ├── ab/
        │   │   └── cd/
        │   │       ├── abcd1234...ef56        # Content file
        │   │       └── abcd1234...ef56.meta   # Metadata (ref count)

    Features:
    - Content deduplication (same content stored once)
    - Reference counting for safe deletion
    - Directory marker support for compatibility
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "bucket_name": ConnectionArg(
            type=ArgType.STRING,
            description="GCS bucket name",
            required=True,
        ),
        "project_id": ConnectionArg(
            type=ArgType.STRING,
            description="GCP project ID (inferred from credentials if not provided)",
            required=False,
            env_var="GCP_PROJECT_ID",
        ),
        "credentials_path": ConnectionArg(
            type=ArgType.PATH,
            description="Path to service account credentials JSON file",
            required=False,
            secret=True,
            env_var="GOOGLE_APPLICATION_CREDENTIALS",
        ),
    }

    def __init__(
        self,
        bucket_name: str,
        project_id: str | None = None,
        credentials_path: str | None = None,
    ):
        """
        Initialize GCS backend.

        Authentication priority (Recommended for Production):
        1. Service account credentials (credentials_path parameter or GOOGLE_APPLICATION_CREDENTIALS)
           - Long-lived credentials, no expiration
           - No daily re-authentication needed
        2. Application Default Credentials (ADC) - Development only
           - gcloud auth application-default login (expires daily, requires re-auth)
           - Compute Engine/Cloud Run service account (auto-detected)

        Args:
            bucket_name: GCS bucket name
            project_id: Optional GCP project ID (inferred from credentials if not provided)
            credentials_path: Optional path to service account credentials JSON file
                            If not provided, uses Application Default Credentials (gcloud auth)
        """
        try:
            if credentials_path:
                # Explicit service account credentials file
                self.client = storage.Client.from_service_account_json(
                    credentials_path, project=project_id
                )
            else:
                # Use Application Default Credentials (gcloud auth application-default login)
                # This automatically uses:
                # 1. GOOGLE_APPLICATION_CREDENTIALS env var
                # 2. gcloud auth application-default credentials
                # 3. GCE/Cloud Run service account
                self.client = storage.Client(project=project_id)

            self.bucket = self.client.bucket(bucket_name)
            self.bucket_name = bucket_name

            # Verify bucket exists
            if not self.bucket.exists():
                raise BackendError(
                    f"Bucket '{bucket_name}' does not exist",
                    backend="gcs",
                    path=bucket_name,
                )

        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to initialize GCS backend: {e}", backend="gcs", path=bucket_name
            ) from e

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "gcs"

    # === Content Operations (CAS) ===

    def _compute_hash(self, content: bytes) -> str:
        """Compute BLAKE3 hash of content (Rust-accelerated)."""
        return hash_content(content)

    def _hash_to_path(self, content_hash: str) -> str:
        """
        Convert content hash to GCS object path.

        Uses two-level directory structure:
        cas/ab/cd/abcd1234...ef56

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            GCS object path string
        """
        if len(content_hash) < 4:
            raise ValueError(f"Invalid hash length: {content_hash}")

        dir1 = content_hash[:2]
        dir2 = content_hash[2:4]

        return f"cas/{dir1}/{dir2}/{content_hash}"

    def _get_meta_path(self, content_hash: str) -> str:
        """Get path to metadata object for content."""
        content_path = self._hash_to_path(content_hash)
        return f"{content_path}.meta"

    def _read_metadata(self, content_hash: str) -> dict[str, Any]:
        """Read metadata for content."""
        meta_path = self._get_meta_path(content_hash)

        try:
            blob = self.bucket.blob(meta_path)
            if not blob.exists():
                return {"ref_count": 0, "size": 0}

            content = blob.download_as_text(encoding="utf-8")
            result: dict[str, Any] = json.loads(content)
            return result

        except NotFound:
            return {"ref_count": 0, "size": 0}
        except (json.JSONDecodeError, Exception) as e:
            raise BackendError(
                f"Failed to read metadata: {e}", backend="gcs", path=content_hash
            ) from e

    def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content."""
        meta_path = self._get_meta_path(content_hash)

        try:
            blob = self.bucket.blob(meta_path)
            blob.upload_from_string(
                json.dumps(metadata), content_type="application/json", timeout=60
            )
        except Exception as e:
            raise BackendError(
                f"Failed to write metadata: {e}", backend="gcs", path=content_hash
            ) from e

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to CAS storage and return its hash.

        If content already exists, increments reference count.

        Args:
            content: File content as bytes
            _context: Operation context (ignored for GCS backend)
        """
        content_hash = self._compute_hash(content)
        content_path = self._hash_to_path(content_hash)

        try:
            blob = self.bucket.blob(content_path)

            # Check if content already exists
            if blob.exists():
                metadata = self._read_metadata(content_hash)
                metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                self._write_metadata(content_hash, metadata)
                return content_hash

            # Content doesn't exist - write it
            blob.upload_from_string(content, timeout=60)

            # Create metadata
            metadata = {"ref_count": 1, "size": len(content)}
            self._write_metadata(content_hash, metadata)

            return content_hash

        except Exception as e:
            raise BackendError(
                f"Failed to write content: {e}", backend="gcs", path=content_hash
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """Read content by its hash.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for GCS backend)
        """
        content_path = self._hash_to_path(content_hash)

        try:
            blob = self.bucket.blob(content_path)

            if not blob.exists():
                raise NexusFileNotFoundError(content_hash)

            content = blob.download_as_bytes(timeout=60)

            # Verify hash
            actual_hash = self._compute_hash(content)
            if actual_hash != content_hash:
                raise BackendError(
                    f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                    backend="gcs",
                    path=content_hash,
                )

            return bytes(content)

        except NotFound as e:
            raise NexusFileNotFoundError(content_hash) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to read content: {e}", backend="gcs", path=content_hash
            ) from e

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Any:
        """
        Stream content from GCS in chunks without loading entire file into memory.

        Uses GCS's streaming download to yield chunks progressively.

        Args:
            content_hash: SHA-256 hash as hex string
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Operation context (ignored for GCS backend)

        Yields:
            bytes: Chunks of file content
        """
        import io

        content_path = self._hash_to_path(content_hash)

        try:
            blob = self.bucket.blob(content_path)

            if not blob.exists():
                raise NexusFileNotFoundError(content_hash)

            # Use streaming download with BytesIO buffer
            buffer = io.BytesIO()
            blob.download_to_file(buffer)
            buffer.seek(0)

            while True:
                chunk = buffer.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        except NotFound as e:
            raise NexusFileNotFoundError(content_hash) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream content: {e}", backend="gcs", path=content_hash
            ) from e

    def write_stream(
        self,
        chunks: Iterator[bytes],
        context: "OperationContext | None" = None,
    ) -> str:
        """
        Write content from an iterator of chunks.

        Streams chunks to temp file, then uploads to GCS.
        Uses same hash algorithm as write_content() for consistency.

        Args:
            chunks: Iterator yielding byte chunks
            context: Operation context (ignored for GCS backend)

        Returns:
            Content hash (BLAKE3 or SHA-256 as hex string)
        """
        import tempfile

        try:
            # Write chunks to temp file while collecting for hashing
            # Note: We collect for hashing to match hash_content() algorithm
            collected_chunks: list[bytes] = []

            with tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024) as tmp:
                for chunk in chunks:
                    tmp.write(chunk)
                    collected_chunks.append(chunk)

                # Compute hash using same algorithm as write_content
                content = b"".join(collected_chunks)
                content_hash = self._compute_hash(content)
                total_size = len(content)

                content_path = self._hash_to_path(content_hash)
                blob = self.bucket.blob(content_path)

                # Check if content already exists
                if blob.exists():
                    # Increment ref_count
                    metadata = self._read_metadata(content_hash)
                    metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                    self._write_metadata(content_hash, metadata)
                    return content_hash

                # Upload from temp file
                tmp.seek(0)
                blob.upload_from_file(tmp, timeout=300)

                # Create metadata
                metadata = {"ref_count": 1, "size": total_size}
                self._write_metadata(content_hash, metadata)

                return content_hash

        except Exception as e:
            raise BackendError(f"Failed to write stream: {e}", backend="gcs", path="stream") from e

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """Delete content by hash with reference counting.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for GCS backend)
        """
        content_path = self._hash_to_path(content_hash)

        try:
            blob = self.bucket.blob(content_path)

            if not blob.exists():
                raise NexusFileNotFoundError(content_hash)

            metadata = self._read_metadata(content_hash)
            ref_count = metadata.get("ref_count", 1)

            if ref_count <= 1:
                # Last reference - delete file and metadata
                blob.delete(timeout=60)

                meta_blob = self.bucket.blob(self._get_meta_path(content_hash))
                if meta_blob.exists():
                    meta_blob.delete(timeout=60)
            else:
                # Decrement reference count
                metadata["ref_count"] = ref_count - 1
                self._write_metadata(content_hash, metadata)

        except NotFound as e:
            raise NexusFileNotFoundError(content_hash) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete content: {e}", backend="gcs", path=content_hash
            ) from e

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """Check if content exists.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for GCS backend)
        """
        try:
            content_path = self._hash_to_path(content_hash)
            blob = self.bucket.blob(content_path)
            return bool(blob.exists())
        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string
            _context: Operation context (ignored for GCS backend)
        """
        content_path = self._hash_to_path(content_hash)

        try:
            blob = self.bucket.blob(content_path)

            if not blob.exists():
                raise NexusFileNotFoundError(content_hash)

            # Reload to get metadata including size
            blob.reload()
            size = blob.size
            if size is None:
                raise BackendError(
                    "Failed to get content size: size is None",
                    backend="gcs",
                    path=content_hash,
                )
            return int(size)

        except NotFound as e:
            raise NexusFileNotFoundError(content_hash) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get content size: {e}", backend="gcs", path=content_hash
            ) from e

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get reference count for content.

        Args:
            content_hash: SHA-256 hash as hex string
            context: Operation context (ignored for GCS backend)
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
        """
        Create directory marker in GCS.

        GCS doesn't have native directories, so we create marker objects
        with trailing slashes to represent directories.
        """
        # Normalize path
        path = path.strip("/")
        if not path:
            return  # Root always exists

        # GCS directories are represented with trailing slash
        dir_path = f"dirs/{path}/"

        try:
            blob = self.bucket.blob(dir_path)

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
            raise BackendError(f"Failed to create directory: {e}", backend="gcs", path=path) from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | EnhancedOperationContext | None" = None,
    ) -> None:
        """Remove directory from GCS."""
        # Normalize path
        path = path.strip("/")
        if not path:
            raise BackendError("Cannot remove root directory", backend="gcs", path=path)

        dir_path = f"dirs/{path}/"

        try:
            blob = self.bucket.blob(dir_path)

            if not blob.exists():
                raise NexusFileNotFoundError(path)

            if not recursive:
                # Check if directory is empty
                # List objects with this prefix (excluding the marker itself)
                blobs = list(
                    self.client.list_blobs(
                        self.bucket_name, prefix=dir_path, max_results=2, timeout=60
                    )
                )
                # If there's more than just the marker, directory is not empty
                if len(blobs) > 1:
                    raise OSError(f"Directory not empty: {path}")

            # Delete directory marker
            blob.delete(timeout=60)

            if recursive:
                # Delete all objects with this prefix
                blobs = self.client.list_blobs(self.bucket_name, prefix=dir_path, timeout=60)
                for blob in blobs:
                    blob.delete(timeout=60)

        except NotFound as e:
            raise NexusFileNotFoundError(path) from e
        except (NexusFileNotFoundError, OSError):
            raise
        except Exception as e:
            raise BackendError(f"Failed to remove directory: {e}", backend="gcs", path=path) from e

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory."""
        try:
            # Normalize path
            path = path.strip("/")
            if not path:
                return True  # Root is always a directory

            dir_path = f"dirs/{path}/"
            blob = self.bucket.blob(dir_path)
            return bool(blob.exists())

        except Exception:
            return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """List directory contents using GCS list_blobs with delimiter."""
        try:
            # Normalize path
            path = path.strip("/")

            # Check if directory exists (except root)
            if path and not self.is_directory(path):
                raise FileNotFoundError(f"Directory not found: {path}")

            # Build prefix for this directory
            prefix = f"dirs/{path}/" if path else "dirs/"

            # List blobs with this prefix
            blobs = self.bucket.list_blobs(prefix=prefix, delimiter="/")

            entries = set()

            # Add direct file blobs (excluding the directory marker itself)
            for blob in blobs:
                # Get relative name from the prefix
                name = blob.name[len(prefix) :]
                if name and name != "":  # Skip the directory marker itself
                    entries.add(name.rstrip("/"))

            # Add subdirectories (from prefixes returned by delimiter)
            for prefix_path in blobs.prefixes:
                # Extract just the directory name
                name = prefix_path[len(prefix) :].rstrip("/")
                if name:
                    entries.add(name + "/")

            return sorted(entries)

        except (FileNotFoundError, NotADirectoryError):
            raise
        except Exception as e:
            raise BackendError(f"Failed to list directory: {e}", backend="gcs", path=path) from e
