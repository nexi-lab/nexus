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

Authentication (Recommended):
    Use service account credentials for production (no daily re-auth):
    - Set GOOGLE_APPLICATION_CREDENTIALS to service account JSON key path
    - Service accounts never expire and don't require daily re-authentication

    Alternative (Development Only):
    - gcloud auth application-default login (requires daily re-authentication)
    - Compute Engine/Cloud Run service account (auto-detected)
"""

from typing import TYPE_CHECKING

from google.api_core import retry
from google.cloud import storage
from google.cloud.exceptions import NotFound

from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    pass


class GCSConnectorBackend(BaseBlobStorageConnector):
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
    - Automatic retry for transient errors (503, network issues)

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
        # OAuth access token (alternative to credentials_path)
        access_token: str | None = None,
    ):
        """
        Initialize GCS connector backend.

        Args:
            bucket_name: GCS bucket name
            project_id: Optional GCP project ID (inferred from credentials if not provided)
            credentials_path: Optional path to service account credentials JSON file
            prefix: Optional prefix for all paths in bucket (e.g., "data/")
            access_token: OAuth access token (alternative to credentials_path)
        """
        try:
            # Priority: access_token > credentials_path > ADC
            if access_token:
                # Use access token directly (no refresh capability)
                from google.oauth2 import credentials as oauth2_credentials

                creds = oauth2_credentials.Credentials(token=access_token)
                self.client = storage.Client(project=project_id, credentials=creds)
            elif credentials_path:
                self.client = storage.Client.from_service_account_json(
                    credentials_path, project=project_id
                )
            else:
                # Use Application Default Credentials
                self.client = storage.Client(project=project_id)

            self.bucket = self.client.bucket(bucket_name)

            # Verify bucket exists and check versioning status
            if not self.bucket.exists():
                raise BackendError(
                    f"Bucket '{bucket_name}' does not exist",
                    backend="gcs_connector",
                    path=bucket_name,
                )

            # Check if bucket has versioning enabled
            self.bucket.reload()  # Load bucket metadata
            versioning_enabled = self.bucket.versioning_enabled or False

            # Initialize base class
            super().__init__(
                bucket_name=bucket_name,
                prefix=prefix,
                versioning_enabled=versioning_enabled,
            )

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

    def _is_version_id(self, value: str) -> bool:
        """
        Check if value looks like a GCS generation number.

        GCS generation numbers are numeric strings.

        Args:
            value: String to check

        Returns:
            True if likely a generation number, False if likely a hash
        """
        # If it's all digits, it's a generation number
        return value.isdigit()

    # === GCS-Specific Blob Operations ===

    def _upload_blob(
        self,
        blob_path: str,
        content: bytes,
        content_type: str,
    ) -> str:
        """
        Upload blob to GCS.

        Args:
            blob_path: Full GCS object path
            content: File content bytes
            content_type: MIME type with optional charset

        Returns:
            Generation number if versioning enabled, else content hash

        Raises:
            BackendError: If upload fails
        """
        try:
            # Write directly to actual path in GCS with proper Content-Type
            blob = self.bucket.blob(blob_path)
            blob.upload_from_string(
                content,
                content_type=content_type,
                timeout=60,
                retry=retry.Retry(deadline=120),  # Retry for up to 2 minutes
            )

            # If bucket has versioning enabled, return generation number
            # Otherwise, return content hash for metadata tracking
            if self.versioning_enabled:
                # Reload blob to get the generation number assigned by GCS
                blob.reload()
                return str(blob.generation)
            else:
                # No versioning - compute hash for metadata
                return self._compute_hash(content)

        except Exception as e:
            raise BackendError(
                f"Failed to upload blob to {blob_path}: {e}",
                backend="gcs_connector",
                path=blob_path,
            ) from e

    def _download_blob(
        self,
        blob_path: str,
        version_id: str | None = None,
    ) -> bytes:
        """
        Download blob from GCS.

        Args:
            blob_path: Full GCS object path
            version_id: Optional GCS generation number

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If download fails
        """
        try:
            # If versioning enabled and version_id looks like a generation number,
            # retrieve that specific version
            if version_id and version_id.isdigit():
                generation = int(version_id)
                blob = self.bucket.blob(blob_path, generation=generation)
            else:
                # No versioning or hash-based identifier - read current version
                blob = self.bucket.blob(blob_path)

            if not blob.exists():
                raise NexusFileNotFoundError(blob_path)

            content = blob.download_as_bytes(
                timeout=60,
                retry=retry.Retry(deadline=120),  # Retry for up to 2 minutes
            )
            return bytes(content)

        except NotFound as e:
            raise NexusFileNotFoundError(blob_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to download blob from {blob_path}: {e}",
                backend="gcs_connector",
                path=blob_path,
            ) from e

    def _delete_blob(self, blob_path: str) -> None:
        """
        Delete blob from GCS.

        Args:
            blob_path: Full GCS object path

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If delete fails
        """
        try:
            blob = self.bucket.blob(blob_path)

            if not blob.exists():
                raise NexusFileNotFoundError(blob_path)

            blob.delete(timeout=60, retry=retry.Retry(deadline=120))

        except NotFound as e:
            raise NexusFileNotFoundError(blob_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete blob at {blob_path}: {e}",
                backend="gcs_connector",
                path=blob_path,
            ) from e

    def _blob_exists(self, blob_path: str) -> bool:
        """
        Check if blob exists in GCS.

        Args:
            blob_path: Full GCS object path

        Returns:
            True if blob exists, False otherwise
        """
        try:
            blob = self.bucket.blob(blob_path)
            return bool(blob.exists())
        except Exception:
            return False

    def _get_blob_size(self, blob_path: str) -> int:
        """
        Get blob size from GCS.

        Args:
            blob_path: Full GCS object path

        Returns:
            Blob size in bytes

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If operation fails
        """
        try:
            blob = self.bucket.blob(blob_path)

            if not blob.exists():
                raise NexusFileNotFoundError(blob_path)

            blob.reload()
            size = blob.size
            if size is None:
                raise BackendError(
                    "Failed to get content size: size is None",
                    backend="gcs_connector",
                    path=blob_path,
                )
            return int(size)

        except NotFound as e:
            raise NexusFileNotFoundError(blob_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get blob size for {blob_path}: {e}",
                backend="gcs_connector",
                path=blob_path,
            ) from e

    def _list_blobs(
        self,
        prefix: str,
        delimiter: str = "/",
    ) -> tuple[list[str], list[str]]:
        """
        List blobs in GCS with given prefix.

        Args:
            prefix: Prefix to filter blobs
            delimiter: Delimiter for virtual directories

        Returns:
            Tuple of (blob_keys, common_prefixes)

        Raises:
            BackendError: If list operation fails
        """
        try:
            # List blobs with this prefix and delimiter
            blobs = self.bucket.list_blobs(prefix=prefix, delimiter=delimiter)

            blob_keys = [blob.name for blob in blobs]
            common_prefixes = list(blobs.prefixes) if blobs.prefixes else []

            return blob_keys, common_prefixes

        except Exception as e:
            raise BackendError(
                f"Failed to list blobs with prefix {prefix}: {e}",
                backend="gcs_connector",
                path=prefix,
            ) from e

    def _create_directory_marker(self, blob_path: str) -> None:
        """
        Create directory marker in GCS.

        Args:
            blob_path: Directory path (should end with '/')

        Raises:
            BackendError: If creation fails
        """
        try:
            blob = self.bucket.blob(blob_path)
            blob.upload_from_string(
                "",
                content_type="application/x-directory",
                timeout=60,
                retry=retry.Retry(deadline=120),
            )

        except Exception as e:
            raise BackendError(
                f"Failed to create directory marker at {blob_path}: {e}",
                backend="gcs_connector",
                path=blob_path,
            ) from e

    def _copy_blob(self, source_path: str, dest_path: str) -> None:
        """
        Copy blob to new location in GCS.

        Args:
            source_path: Source GCS object path
            dest_path: Destination GCS object path

        Raises:
            NexusFileNotFoundError: If source doesn't exist
            BackendError: If copy fails
        """
        try:
            # Get source blob
            source_blob = self.bucket.blob(source_path)
            if not source_blob.exists():
                raise NexusFileNotFoundError(source_path)

            # Copy to new location
            self.bucket.copy_blob(source_blob, self.bucket, dest_path)

        except NotFound as e:
            raise NexusFileNotFoundError(source_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to copy blob from {source_path} to {dest_path}: {e}",
                backend="gcs_connector",
                path=source_path,
            ) from e
