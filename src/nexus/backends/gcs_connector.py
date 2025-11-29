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

Caching:
    This connector supports the CacheConnectorMixin for caching content
    in the local database. Enable caching by passing a db_session when
    creating the connector.

Authentication (Recommended):
    Use service account credentials for production (no daily re-auth):
    - Set GOOGLE_APPLICATION_CREDENTIALS to service account JSON key path
    - Service accounts never expire and don't require daily re-authentication

    Alternative (Development Only):
    - gcloud auth application-default login (requires daily re-authentication)
    - Compute Engine/Cloud Run service account (auto-detected)
"""

import logging
from typing import TYPE_CHECKING

from google.api_core import retry
from google.cloud import storage
from google.cloud.exceptions import NotFound

from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class GCSConnectorBackend(BaseBlobStorageConnector, CacheConnectorMixin):
    """
    Google Cloud Storage connector backend with direct path mapping.

    This backend stores files at their actual paths in GCS, making the
    bucket browsable by external tools. Unlike GCSBackend (CAS-based),
    this connector does NOT transform paths to content hashes.

    Features:
    - Direct path mapping (file.txt → file.txt in GCS)
    - Optional local caching via CacheConnectorMixin
    - Full workspace compatibility
    - External tool compatibility (bucket remains browsable)
    - Native GCS versioning support (if bucket has versioning enabled)
    - Automatic retry for transient errors (503, network issues)
    - Optimistic locking via GCS generation numbers

    Versioning Behavior:
    - If bucket has versioning enabled: Uses GCS generation numbers for version tracking
    - If bucket has no versioning: Only current version retained (overwrites on update)

    Caching:
    - Pass db_session to enable local caching
    - Use read_content() with use_cache=True to read from cache first
    - Use sync() to bulk-sync files to cache

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
        # Database session for caching support (deprecated, use session_factory)
        db_session: "Session | None" = None,
        # Session factory for caching support (preferred)
        session_factory: "type[Session] | None" = None,
    ):
        """
        Initialize GCS connector backend.

        Args:
            bucket_name: GCS bucket name
            project_id: Optional GCP project ID (inferred from credentials if not provided)
            credentials_path: Optional path to service account credentials JSON file
            prefix: Optional prefix for all paths in bucket (e.g., "data/")
            access_token: OAuth access token (alternative to credentials_path)
            db_session: Optional SQLAlchemy session for caching (deprecated)
            session_factory: Optional session factory (e.g., metadata_store.SessionLocal)
                           for caching support. Preferred over db_session.
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

            # Store session info for caching support (CacheConnectorMixin)
            # Prefer session_factory (creates fresh sessions) over db_session
            self.session_factory = session_factory
            self.db_session = db_session  # Legacy support

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

    def _has_caching(self) -> bool:
        """Check if caching is enabled (session factory or db_session available)."""
        return self.session_factory is not None or self.db_session is not None

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

    # === Version Support for CacheConnectorMixin ===

    def get_version(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """
        Get GCS generation number for a file.

        The generation number changes on every write and is used for:
        - Optimistic locking (version checks before write)
        - Cache invalidation (detect stale cache entries)

        Args:
            path: Virtual file path (or backend_path from context)
            context: Operation context with optional backend_path

        Returns:
            GCS generation number as string, or None if file doesn't exist
        """
        try:
            # Get backend path
            if context and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            blob_path = self._get_blob_path(backend_path)
            blob = self.bucket.blob(blob_path)

            if not blob.exists():
                return None

            blob.reload()
            return str(blob.generation) if blob.generation else None

        except Exception:
            return None

    # === Override Content Operations with Caching ===

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """
        Read content from GCS with caching support.

        When caching is enabled (db_session provided):
        1. Check cache for non-stale entry with matching version
        2. If cache hit, return cached content
        3. If cache miss, read from GCS and cache result

        Args:
            content_hash: Version ID (if versioning) or hash (if not)
            context: Operation context with backend_path

        Returns:
            File content as bytes

        Raises:
            ValueError: If backend_path not provided
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "GCS connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes."
            )

        # Get virtual path for cache lookup
        virtual_path = context.backend_path
        if hasattr(context, "virtual_path") and context.virtual_path:
            virtual_path = context.virtual_path

        # Check cache first if enabled
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                cached = self._read_from_cache(virtual_path)
                if cached and not cached.stale and cached.content_binary:
                    # Verify version matches if we have version info
                    if cached.backend_version and content_hash:
                        if cached.backend_version == content_hash:
                            logger.info(f"[GCS] Cache hit for {virtual_path}")
                            return cached.content_binary
                        # Version mismatch - cache is stale, read from backend
                        logger.debug(f"[GCS] Cache version mismatch for {virtual_path}")
                    else:
                        # No version to compare, trust the cache
                        logger.info(f"[GCS] Cache hit (no version) for {virtual_path}")
                        return cached.content_binary

        # Read from GCS backend
        logger.info(f"[GCS] Cache miss, reading from backend: {virtual_path}")
        blob_path = self._get_blob_path(context.backend_path)

        # Determine if we should use version ID
        version_id = None
        if self.versioning_enabled and content_hash and self._is_version_id(content_hash):
            version_id = content_hash

        content = self._download_blob(blob_path, version_id)

        # Cache the result if caching is enabled
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                version = self.get_version(virtual_path, context)
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=virtual_path,
                    content=content,
                    backend_version=version,
                    tenant_id=tenant_id,
                )

        return content

    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> str:
        """
        Write content to GCS and update cache.

        Per design doc (cache-layer.md), after successful write:
        1. Write to GCS backend
        2. Update cache with new content and version

        Args:
            content: File content as bytes
            context: Operation context with backend_path

        Returns:
            If versioning enabled: GCS generation number
            If no versioning: Content hash (for metadata compatibility)

        Raises:
            ValueError: If backend_path is not provided in context
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "GCS connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes."
            )

        # Get virtual path for cache operations
        virtual_path = context.backend_path
        if hasattr(context, "virtual_path") and context.virtual_path:
            virtual_path = context.virtual_path

        # Get actual blob path from backend_path
        blob_path = self._get_blob_path(context.backend_path)

        # Detect appropriate Content-Type with charset for proper encoding
        content_type = self._detect_content_type(context.backend_path, content)

        # Upload blob
        new_version = self._upload_blob(blob_path, content, content_type)

        # Update cache after write if caching is enabled
        # Per design doc: both GCS and cache should be updated when write succeeds
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=virtual_path,
                    content=content,
                    backend_version=new_version,
                    tenant_id=tenant_id,
                )

        return new_version

    def write_content_with_version_check(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
        expected_version: str | None = None,
    ) -> str:
        """
        Write content with optimistic locking via version check.

        Args:
            content: File content as bytes
            context: Operation context with backend_path
            expected_version: Expected GCS generation for optimistic locking

        Returns:
            New GCS generation number (or content hash if no versioning)

        Raises:
            ValueError: If backend_path not provided
            ConflictError: If version check fails
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "GCS connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes."
            )

        # Get virtual path for version check
        virtual_path = context.backend_path
        if hasattr(context, "virtual_path") and context.virtual_path:
            virtual_path = context.virtual_path

        # Version check if requested
        if expected_version is not None:
            self._check_version(virtual_path, expected_version, context)

        # Perform the write
        return self.write_content(content, context)
