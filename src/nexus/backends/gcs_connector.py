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
from collections.abc import Iterator
from typing import TYPE_CHECKING

from google.api_core import retry
from google.cloud import storage
from google.cloud.exceptions import NotFound

from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


@register_connector(
    "gcs_connector",
    description="Google Cloud Storage with direct path mapping",
    category="storage",
    requires=["google-cloud-storage"],
)
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
        "prefix": ConnectionArg(
            type=ArgType.STRING,
            description="Path prefix for all files in bucket",
            required=False,
            default="",
        ),
        "access_token": ConnectionArg(
            type=ArgType.SECRET,
            description="OAuth access token (alternative to credentials_path)",
            required=False,
            secret=True,
        ),
    }

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

    # _has_caching() inherited from CacheConnectorMixin

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
    ) -> tuple[bytes, str | None]:
        """
        Download blob from GCS.

        Args:
            blob_path: Full GCS object path
            version_id: Optional GCS generation number

        Returns:
            Tuple of (content, generation_number)
            - content: File content as bytes
            - generation_number: GCS generation as string, or None if not available

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

            # Reload blob metadata to get generation after download
            blob.reload()
            generation_str = str(blob.generation) if blob.generation else None

            return bytes(content), generation_str

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

    def _batch_get_versions(
        self,
        backend_paths: list[str],
        contexts: dict[str, "OperationContext"] | None = None,
    ) -> dict[str, str | None]:
        """
        Get GCS generation numbers for multiple files in a single API call.

        This is highly optimized for GCS - uses list_blobs() which returns
        generation numbers for all files in one request.

        Args:
            backend_paths: List of backend-relative paths
            contexts: Optional dict mapping path -> OperationContext (unused for GCS)

        Returns:
            Dict mapping backend_path -> generation number string

        Performance:
            - Single list_blobs() API call regardless of file count
            - ~200ms for 1000s of files (vs ~2-5s for sequential checks)
            - 10-25x speedup over sequential get_version() calls
        """
        if not backend_paths:
            return {}

        # Convert backend paths to blob paths
        blob_paths_map = {self._get_blob_path(path): path for path in backend_paths}

        # Single API call to list all blobs under prefix with metadata
        # This returns generation numbers for all blobs at once
        logger.info(
            f"[GCS] Batch fetching versions for {len(backend_paths)} files via list_blobs()"
        )

        try:
            # List blobs under the prefix (returns ALL metadata including generation)
            blobs = self.bucket.list_blobs(prefix=self.prefix if self.prefix else None)

            # Build version map
            versions: dict[str, str | None] = {}
            blob_generations: dict[str, int] = {}

            for blob in blobs:
                if blob.name in blob_paths_map:
                    blob_generations[blob.name] = blob.generation

            # Map back to backend paths
            for blob_path, backend_path in blob_paths_map.items():
                generation = blob_generations.get(blob_path)
                versions[backend_path] = str(generation) if generation else None

            logger.info(
                f"[GCS] Batch version fetch complete: {len(versions)}/{len(backend_paths)} found"
            )
            return versions

        except Exception as e:
            logger.warning(f"[GCS] Batch version fetch failed: {e}, falling back to sequential")
            # Fallback to sequential
            return super()._batch_get_versions(backend_paths, contexts)

    def _bulk_download_blobs(
        self,
        blob_paths: list[str],
        version_ids: dict[str, str] | None = None,
        max_workers: int = 10,
    ) -> dict[str, bytes]:
        """
        Download multiple blobs in parallel with GCS-optimized settings.

        Overrides base implementation with GCS-specific retry policies
        optimized for high-throughput batch operations.

        Args:
            blob_paths: List of GCS object paths to download
            version_ids: Optional dict mapping blob_path -> generation number
            max_workers: Number of concurrent downloads (default: 10, optimal for GCS)

        Returns:
            Dict mapping blob_path -> content bytes (only successful downloads)

        Performance:
            - 10 workers (optimal): ~15x speedup, avoids GCS rate limiting
            - 50 workers: ~8x speedup (rate limited)
            - 100 workers: ~1.5x speedup (severe rate limiting)

        Note:
            Based on performance testing with 267 files, 10 workers provides
            the best performance for GCS (2.84s vs 43.5s sequential = 15.3x faster).
            Higher worker counts trigger GCS rate limiting and connection pool saturation.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not blob_paths:
            return {}

        results: dict[str, bytes] = {}

        def download_one(blob_path: str) -> tuple[str, bytes | None]:
            """Download single blob, delegating to _download_blob() to avoid duplication."""
            try:
                # Get version ID (generation number) if provided
                version_id = version_ids.get(blob_path) if version_ids else None
                # Call existing _download_blob() to reuse error handling and retry logic
                content, _generation = self._download_blob(blob_path, version_id)
                return (blob_path, content)

            except Exception as e:
                logger.warning(f"[GCS] Failed to download {blob_path}: {e}")
                return (blob_path, None)

        # Parallel downloads with thread pool
        logger.info(
            f"[GCS] Starting bulk download of {len(blob_paths)} blobs with {max_workers} workers"
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all download tasks
            futures = {executor.submit(download_one, path): path for path in blob_paths}

            # Collect results as they complete
            for future in as_completed(futures):
                blob_path, content = future.result()
                if content is not None:
                    results[blob_path] = content

        logger.info(
            f"[GCS] Bulk download complete: {len(results)}/{len(blob_paths)} successful "
            f"({len(blob_paths) - len(results)} failed)"
        )

        return results

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

    def _stream_blob(
        self,
        blob_path: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """
        Stream blob content from GCS in chunks.

        Uses GCS's download_to_file with a streaming buffer for memory efficiency.

        Args:
            blob_path: Full GCS object path
            chunk_size: Size of each chunk in bytes
            version_id: Optional GCS generation number

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If stream operation fails
        """
        import io

        try:
            # If version_id looks like a generation number, use it
            if version_id and version_id.isdigit():
                generation = int(version_id)
                blob = self.bucket.blob(blob_path, generation=generation)
            else:
                blob = self.bucket.blob(blob_path)

            if not blob.exists():
                raise NexusFileNotFoundError(blob_path)

            # Use streaming download with BytesIO buffer
            # Note: GCS client doesn't support true streaming, so we download to buffer
            # This is still more memory-efficient than loading into a single bytes object
            # when yielding chunks progressively
            buffer = io.BytesIO()
            blob.download_to_file(
                buffer,
                timeout=60,
                retry=retry.Retry(deadline=120),
            )
            buffer.seek(0)

            while True:
                chunk = buffer.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        except NotFound as e:
            raise NexusFileNotFoundError(blob_path) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream blob from {blob_path}: {e}",
                backend="gcs_connector",
                path=blob_path,
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

    def generate_signed_url(
        self,
        path: str,
        expires_in: int = 3600,
        context: "OperationContext | None" = None,
    ) -> dict[str, str | int]:
        """
        Generate a signed URL for direct download from GCS.

        This allows clients to download files directly from GCS, bypassing the
        Nexus server. The URL is time-limited and includes a signature.

        Args:
            path: Virtual file path (or backend_path from context)
            expires_in: URL expiration time in seconds (default: 1 hour, max: 7 days)
            context: Operation context with optional backend_path

        Returns:
            Dict with:
            - url: Signed download URL
            - expires_in: Expiration time in seconds
            - method: HTTP method ("GET")

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If URL generation fails
        """
        from datetime import timedelta

        try:
            # Get backend path
            if context and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            blob_path = self._get_blob_path(backend_path)
            blob = self.bucket.blob(blob_path)

            # Verify file exists
            if not blob.exists():
                raise NexusFileNotFoundError(path)

            # Clamp expires_in to GCS max (7 days = 604800 seconds)
            expires_in = min(expires_in, 604800)

            # Generate signed URL using V4 signing
            url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_in),
                method="GET",
            )

            return {
                "url": url,
                "expires_in": expires_in,
                "method": "GET",
            }

        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to generate signed URL for {path}: {e}",
                backend="gcs_connector",
                path=path,
            ) from e

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

        # Get cache path (prefers virtual_path over backend_path)
        cache_path = self._get_cache_path(context) or context.backend_path

        # Check cache first if enabled
        cache_rejected_reason = None
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                cached = self._read_from_cache(cache_path, original=True)
                if cached and not cached.stale and cached.content_binary:
                    # For GCS versioned storage, always check current backend version
                    # Don't compare content_hash (which may be SHA256 from metadata)
                    # to backend_version (which is GCS generation number) - they're different types
                    if cached.backend_version:
                        # Get current backend version to verify cache freshness
                        current_version = self.get_version(context.backend_path, context)
                        if current_version and cached.backend_version == current_version:
                            logger.info(
                                f"[GCS] Cache hit (version match) for {cache_path} "
                                f"(version={current_version})"
                            )
                            return cached.content_binary
                        elif current_version:
                            # Version mismatch - cache entry exists but version is stale
                            cache_rejected_reason = "version mismatch"
                            logger.info(
                                f"[GCS] Cache version mismatch for {cache_path} "
                                f"(cached={cached.backend_version}, current={current_version})"
                            )
                        else:
                            # Can't get current version, trust cache
                            logger.info(f"[GCS] Cache hit (no current version) for {cache_path}")
                            return cached.content_binary
                    else:
                        # No version in cache, trust the cache
                        logger.info(f"[GCS] Cache hit (no cached version) for {cache_path}")
                        return cached.content_binary

        # Read from GCS backend
        if cache_rejected_reason:
            logger.info(f"[GCS] Reading from backend due to {cache_rejected_reason}: {cache_path}")
        else:
            logger.info(f"[GCS] Cache miss, reading from backend: {cache_path}")
        blob_path = self._get_blob_path(context.backend_path)

        # Determine if we should use version ID
        version_id = None
        if self.versioning_enabled and content_hash and self._is_version_id(content_hash):
            version_id = content_hash

        content, generation = self._download_blob(blob_path, version_id)

        # Cache the result if caching is enabled
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                # Use generation from download instead of making extra API call
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=generation,
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
