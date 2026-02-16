"""AWS S3 connector backend with direct path mapping.

This is a connector backend that maps files directly to S3 bucket paths,
unlike a CAS-based S3Backend which would store files by content hash.

Use case: Mount external S3 buckets where files should remain at their
original paths, browsable by external tools.

Storage structure:
    bucket/
    ├── prefix/
    │   ├── workspace/
    │   │   ├── file.txt          # Stored at actual path
    │   │   └── data/
    │   │       └── output.json

Key differences from CAS backends:
- No CAS transformation (files stored at actual paths)
- No deduplication (same content = multiple files)
- No reference counting
- External tools can browse bucket normally
- Requires backend_path in OperationContext

Authentication:
    Uses AWS credentials in priority order:
    - Explicit credentials (access_key_id + secret_access_key)
    - Credentials file path (AWS credentials JSON/INI)
    - AWS default credentials chain (~/.aws/credentials, environment variables, IAM roles)
"""

import logging
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from nexus.backends.backend import HandlerStatusResponse
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.backends.multipart_upload_mixin import MultipartUploadMixin
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


@register_connector(
    "s3_connector",
    description="AWS S3 with direct path mapping",
    category="storage",
    requires=["boto3"],
    service_name="s3",
)
class S3ConnectorBackend(BaseBlobStorageConnector, CacheConnectorMixin, MultipartUploadMixin):
    """
    AWS S3 connector backend with direct path mapping.

    This backend stores files at their actual paths in S3, making the
    bucket browsable by external tools. Unlike a CAS-based backend,
    this connector does NOT transform paths to content hashes.

    Features:
    - Direct path mapping (file.txt → file.txt in S3)
    - Write-through storage (no local caching)
    - Full workspace compatibility
    - External tool compatibility (bucket remains browsable)
    - S3 versioning support (if bucket has versioning enabled)
    - Automatic retry for transient errors (503, throttling)

    Versioning Behavior:
    - If bucket has versioning enabled: Uses S3 version IDs for version tracking
    - If bucket has no versioning: Only current version retained (overwrites on update)

    Limitations:
    - No deduplication (same content stored multiple times)
    - Requires backend_path in OperationContext
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "bucket_name": ConnectionArg(
            type=ArgType.STRING,
            description="S3 bucket name",
            required=True,
            config_key="bucket",
        ),
        "region_name": ConnectionArg(
            type=ArgType.STRING,
            description="AWS region (e.g., 'us-east-1')",
            required=False,
            env_var="AWS_REGION",
        ),
        "credentials_path": ConnectionArg(
            type=ArgType.PATH,
            description="Path to AWS credentials file (JSON format)",
            required=False,
            secret=True,
        ),
        "prefix": ConnectionArg(
            type=ArgType.STRING,
            description="Path prefix for all files in bucket",
            required=False,
            default="",
        ),
        "access_key_id": ConnectionArg(
            type=ArgType.SECRET,
            description="AWS access key ID",
            required=False,
            secret=True,
            env_var="AWS_ACCESS_KEY_ID",
        ),
        "secret_access_key": ConnectionArg(
            type=ArgType.PASSWORD,
            description="AWS secret access key",
            required=False,
            secret=True,
            env_var="AWS_SECRET_ACCESS_KEY",
        ),
        "session_token": ConnectionArg(
            type=ArgType.SECRET,
            description="AWS session token (for temporary credentials)",
            required=False,
            secret=True,
            env_var="AWS_SESSION_TOKEN",
        ),
    }

    def __init__(
        self,
        bucket_name: str,
        region_name: str | None = None,
        credentials_path: str | None = None,
        prefix: str = "",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        # Database session for caching support (deprecated, use session_factory)
        db_session: "Session | None" = None,
        # Session factory for caching support (preferred)
        session_factory: "type[Session] | None" = None,
    ):
        """
        Initialize S3 connector backend.

        Args:
            bucket_name: S3 bucket name
            region_name: AWS region (e.g., 'us-east-1')
            credentials_path: Optional path to AWS credentials file (JSON format)
            prefix: Optional prefix for all paths in bucket (e.g., "data/")
            access_key_id: AWS access key (alternative to credentials_path)
            secret_access_key: AWS secret key (alternative to credentials_path)
            session_token: AWS session token (for temporary credentials)
            db_session: Optional SQLAlchemy session for caching (deprecated)
            session_factory: Optional session factory (e.g., metadata_store.SessionLocal)
                           for caching support. Preferred over db_session.
        """
        try:
            # Configure retry behavior for transient errors
            boto_config = Config(
                retries={
                    "max_attempts": 3,
                    "mode": "adaptive",  # Adaptive retry mode for better handling
                }
            )

            # Priority: explicit credentials > credentials_path > default chain
            if access_key_id and secret_access_key:
                # Use explicit credentials
                self.client = boto3.client(
                    "s3",
                    region_name=region_name,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    aws_session_token=session_token,
                    config=boto_config,
                )
                self.resource = boto3.resource(
                    "s3",
                    region_name=region_name,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    aws_session_token=session_token,
                    config=boto_config,
                )
            elif credentials_path:
                # Load credentials from file (JSON format)
                import json

                with open(credentials_path) as f:
                    creds = json.load(f)
                self.client = boto3.client(
                    "s3",
                    region_name=region_name or creds.get("region_name"),
                    aws_access_key_id=creds.get("aws_access_key_id"),
                    aws_secret_access_key=creds.get("aws_secret_access_key"),
                    aws_session_token=creds.get("aws_session_token"),
                    config=boto_config,
                )
                self.resource = boto3.resource(
                    "s3",
                    region_name=region_name or creds.get("region_name"),
                    aws_access_key_id=creds.get("aws_access_key_id"),
                    aws_secret_access_key=creds.get("aws_secret_access_key"),
                    aws_session_token=creds.get("aws_session_token"),
                    config=boto_config,
                )
            else:
                # Use default credentials chain
                self.client = boto3.client("s3", region_name=region_name, config=boto_config)
                self.resource = boto3.resource("s3", region_name=region_name, config=boto_config)

            self.bucket = self.resource.Bucket(bucket_name)

            # Verify bucket exists
            try:
                self.client.head_bucket(Bucket=bucket_name)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "404" or error_code == "NoSuchBucket":
                    raise BackendError(
                        f"Bucket '{bucket_name}' does not exist",
                        backend="s3_connector",
                        path=bucket_name,
                    ) from e
                raise

            # Check if bucket has versioning enabled
            versioning = self.client.get_bucket_versioning(Bucket=bucket_name)
            versioning_enabled = versioning.get("Status") == "Enabled"

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
                f"Failed to initialize S3 connector backend: {e}",
                backend="s3_connector",
                path=bucket_name,
            ) from e

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "s3_connector"

    def check_connection(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        """
        Verify S3 connection is healthy.

        Performs a lightweight head_bucket call to verify:
        - AWS credentials are valid
        - Bucket is accessible
        - Network connectivity is working

        Args:
            context: Operation context (unused, S3 uses shared credentials)

        Returns:
            HandlerStatusResponse with health status and latency
        """
        import time

        start = time.perf_counter()

        try:
            # Lightweight check - head_bucket is fast and verifies access
            self.client.head_bucket(Bucket=self.bucket_name)

            latency_ms = (time.perf_counter() - start) * 1000
            return HandlerStatusResponse(
                success=True,
                latency_ms=latency_ms,
                details={
                    "backend": self.name,
                    "bucket": self.bucket_name,
                    "prefix": self.prefix,
                    "versioning_enabled": self.versioning_enabled,
                },
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            error_message = e.response.get("Error", {}).get("Message", str(e))

            return HandlerStatusResponse(
                success=False,
                error_message=f"S3 access failed ({error_code}): {error_message}",
                latency_ms=(time.perf_counter() - start) * 1000,
                details={
                    "backend": self.name,
                    "bucket": self.bucket_name,
                    "error_code": error_code,
                },
            )

        except Exception as e:
            return HandlerStatusResponse(
                success=False,
                error_message=str(e),
                latency_ms=(time.perf_counter() - start) * 1000,
                details={"backend": self.name, "bucket": self.bucket_name},
            )

    # _has_caching() inherited from CacheConnectorMixin

    def _is_version_id(self, value: str) -> bool:
        """
        Check if value looks like an S3 version ID.

        S3 version IDs are URL-safe base64-encoded strings (e.g., "null" for no versioning,
        or random strings like "3HL4kqtJvjVBH40Nrjfkd" when versioning is enabled).

        Args:
            value: String to check

        Returns:
            True if likely a version ID, False if likely a content hash
        """
        # S3 version IDs are not hex (unlike content hashes)
        # Content hashes are 64-char hex strings (SHA-256)
        if len(value) == 64:
            try:
                int(value, 16)
                return False  # It's a hex hash
            except ValueError:
                return True  # Not hex, probably version ID
        return True  # Not 64 chars, probably version ID

    # === Version Support for CacheConnectorMixin ===

    def get_version(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """
        Get S3 version ID for a file.

        The version ID changes on every write (if versioning enabled) and is used for:
        - Optimistic locking (version checks before write)
        - Cache invalidation (detect stale cache entries)

        Args:
            path: Virtual file path (or backend_path from context)
            context: Operation context with optional backend_path

        Returns:
            S3 version ID as string, or None if file doesn't exist or no versioning
        """
        try:
            # Get backend path
            if context and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            blob_path = self._get_blob_path(backend_path)

            # Get object metadata
            response = self.client.head_object(Bucket=self.bucket_name, Key=blob_path)

            # Return version ID if versioning is enabled
            version_id = response.get("VersionId")
            if version_id and version_id != "null":
                return str(version_id)
            return None

        except ClientError:
            return None
        except Exception:
            return None

    def get_file_info(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse:
        """
        Get file metadata for delta sync change detection (Issue #1127).

        Returns S3 object metadata including size, mtime, and version ID
        for efficient change detection during incremental sync.

        Args:
            path: Virtual file path (or backend_path from context)
            context: Operation context with optional backend_path

        Returns:
            HandlerResponse with FileInfo containing:
            - size: Object size in bytes
            - mtime: Last modified time
            - backend_version: S3 version ID (if versioning enabled)
        """
        from nexus.backends.backend import FileInfo

        try:
            # Get backend path
            if context and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            blob_path = self._get_blob_path(backend_path)

            # Get object metadata via head_object (no content download)
            response = self.client.head_object(Bucket=self.bucket_name, Key=blob_path)

            # Extract metadata
            size = response.get("ContentLength", 0)
            mtime = response.get("LastModified")  # datetime object
            version_id = response.get("VersionId")

            # Build backend_version: prefer version ID, fallback to ETag
            backend_version: str | None = None
            if version_id and version_id != "null":
                backend_version = str(version_id)
            else:
                # Use ETag as fallback (changes on content change)
                etag = response.get("ETag", "").strip('"')
                backend_version = f"etag:{etag}" if etag else None

            file_info = FileInfo(
                size=size,
                mtime=mtime,
                backend_version=backend_version,
                content_hash=None,  # Not computed to avoid content download
            )

            return HandlerResponse.ok(file_info, backend_name=self.name, path=path)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404" or error_code == "NoSuchKey":
                return HandlerResponse.not_found(path, backend_name=self.name)
            return HandlerResponse.error(f"S3 error: {error_code}", backend_name=self.name)
        except Exception as e:
            return HandlerResponse.error(f"Failed to get file info: {e}", backend_name=self.name)

    def generate_presigned_url(
        self,
        path: str,
        expires_in: int = 3600,
        context: "OperationContext | None" = None,
    ) -> dict[str, str | int]:
        """
        Generate a presigned URL for direct download from S3.

        This allows clients to download files directly from S3, bypassing the
        Nexus server. The URL is time-limited and includes a signature.

        Args:
            path: Virtual file path (or backend_path from context)
            expires_in: URL expiration time in seconds (default: 1 hour, max: 7 days)
            context: Operation context with optional backend_path

        Returns:
            Dict with:
            - url: Presigned download URL
            - expires_in: Expiration time in seconds
            - method: HTTP method ("GET")

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If URL generation fails
        """
        try:
            # Get backend path
            if context and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            blob_path = self._get_blob_path(backend_path)

            # Verify file exists
            if not self._blob_exists(blob_path):
                raise NexusFileNotFoundError(path)

            # Clamp expires_in to S3 max (7 days = 604800 seconds)
            expires_in = min(expires_in, 604800)

            # Generate presigned URL
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": blob_path},
                ExpiresIn=expires_in,
                HttpMethod="GET",
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
                f"Failed to generate presigned URL for {path}: {e}",
                backend="s3_connector",
                path=path,
            ) from e

    # === S3-Specific Blob Operations ===

    def _upload_blob(
        self,
        blob_path: str,
        content: bytes,
        content_type: str,
    ) -> str:
        """
        Upload blob to S3.

        Args:
            blob_path: Full S3 object key
            content: File content bytes
            content_type: MIME type with optional charset

        Returns:
            Version ID if versioning enabled, else content hash

        Raises:
            BackendError: If upload fails
        """
        try:
            # Write directly to actual path in S3 with proper Content-Type
            response = self.client.put_object(
                Bucket=self.bucket_name,
                Key=blob_path,
                Body=content,
                ContentType=content_type,
            )

            # If bucket has versioning enabled, return version ID
            # Otherwise, return content hash for metadata tracking
            if self.versioning_enabled and "VersionId" in response:
                return str(response["VersionId"])
            else:
                # No versioning - compute hash for metadata
                return self._compute_hash(content)

        except Exception as e:
            raise BackendError(
                f"Failed to upload blob to {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e

    def _download_blob(
        self,
        blob_path: str,
        version_id: str | None = None,
    ) -> tuple[bytes, str | None]:
        """
        Download blob from S3.

        Args:
            blob_path: Full S3 object key
            version_id: Optional S3 version ID

        Returns:
            Tuple of (content, version_id)
            - content: File content as bytes
            - version_id: S3 version ID as string, or None if not available

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If download fails
        """
        try:
            # Build get parameters
            get_params: dict = {"Bucket": self.bucket_name, "Key": blob_path}

            # Add version ID if provided
            if version_id:
                get_params["VersionId"] = version_id

            response = self.client.get_object(**get_params)
            content = response["Body"].read()

            # Extract version ID from response metadata
            response_version_id = response.get("VersionId")

            return bytes(content), response_version_id

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise NexusFileNotFoundError(blob_path) from e
            raise BackendError(
                f"Failed to download blob from {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e
        except Exception as e:
            raise BackendError(
                f"Failed to download blob from {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e

    def _stream_blob(
        self,
        blob_path: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """
        Stream blob content from S3 in chunks.

        Uses S3's StreamingBody for true streaming without loading entire file.

        Args:
            blob_path: Full S3 object key
            chunk_size: Size of each chunk in bytes
            version_id: Optional S3 version ID

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If stream operation fails
        """
        try:
            # Build get parameters
            get_params: dict = {"Bucket": self.bucket_name, "Key": blob_path}

            # Add version ID if provided
            if version_id:
                get_params["VersionId"] = version_id

            response = self.client.get_object(**get_params)

            # S3's StreamingBody supports chunked iteration
            streaming_body = response["Body"]

            # Use iter_chunks for efficient streaming
            yield from streaming_body.iter_chunks(chunk_size=chunk_size)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise NexusFileNotFoundError(blob_path) from e
            raise BackendError(
                f"Failed to stream blob from {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream blob from {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e

    def _batch_get_versions(
        self,
        backend_paths: list[str],
        contexts: dict[str, "OperationContext"] | None = None,
    ) -> dict[str, str | None]:
        """
        Get S3 version IDs for multiple files using parallel head_object calls.

        Uses ThreadPoolExecutor to parallelize head_object() calls since S3
        doesn't have a single batch API like GCS.

        Args:
            backend_paths: List of backend-relative paths
            contexts: Optional dict mapping path -> OperationContext (unused for S3)

        Returns:
            Dict mapping backend_path -> version ID string (or None)

        Performance:
            - Parallel head_object() calls with threading
            - ~500ms for 100 files with 20 workers
            - 5-10x speedup over sequential get_version() calls
        """
        if not backend_paths:
            return {}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        logger.info(
            f"[S3] Batch fetching versions for {len(backend_paths)} files via parallel head_object()"
        )

        versions: dict[str, str | None] = {}

        def get_one_version(backend_path: str) -> tuple[str, str | None]:
            """Get version for single file."""
            try:
                blob_path = self._get_blob_path(backend_path)
                response = self.client.head_object(Bucket=self.bucket_name, Key=blob_path)

                # Return version ID if versioning is enabled
                version_id = response.get("VersionId")
                if version_id and version_id != "null":
                    return (backend_path, str(version_id))
                return (backend_path, None)

            except ClientError:
                return (backend_path, None)
            except Exception:
                return (backend_path, None)

        # Parallel head_object calls
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(get_one_version, path): path for path in backend_paths}

            for future in as_completed(futures):
                backend_path, version = future.result()
                versions[backend_path] = version

        logger.info(
            f"[S3] Batch version fetch complete: {len([v for v in versions.values() if v])}/{len(backend_paths)} with versions"
        )
        return versions

    def _bulk_download_blobs(
        self,
        blob_paths: list[str],
        version_ids: dict[str, str] | None = None,
        max_workers: int = 20,
    ) -> dict[str, bytes]:
        """
        Download multiple blobs in parallel with S3-optimized settings.

        Leverages boto3 client's built-in connection pooling and thread-safety
        for efficient parallel downloads.

        Args:
            blob_paths: List of S3 object keys to download
            version_ids: Optional dict mapping blob_path -> version ID
            max_workers: Number of concurrent downloads (default: 20, moderate)

        Returns:
            Dict mapping blob_path -> content bytes (only successful downloads)

        Performance:
            - 20 workers (recommended): Good balance of throughput and reliability
            - S3 generally tolerates higher concurrency than GCS
            - boto3 handles automatic retries and connection pooling

        Note:
            boto3 client is thread-safe and has built-in connection pooling,
            making it ideal for parallel operations. The default of 20 provides
            a good balance between performance and API rate limits.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not blob_paths:
            return {}

        results: dict[str, bytes] = {}

        def download_one(blob_path: str) -> tuple[str, bytes | None]:
            """Download single blob, delegating to _download_blob() to avoid duplication."""
            try:
                # Get version ID if provided
                version_id = version_ids.get(blob_path) if version_ids else None
                # Call existing _download_blob() to reuse error handling and boto3 logic
                content, _version_id = self._download_blob(blob_path, version_id)
                return (blob_path, content)

            except Exception as e:
                logger.warning(f"[S3] Failed to download {blob_path}: {e}")
                return (blob_path, None)

        # Parallel downloads with thread pool
        logger.info(
            f"[S3] Starting bulk download of {len(blob_paths)} objects with {max_workers} workers"
        )

        # boto3 client is thread-safe, so we can share it across threads
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all download tasks
            futures = {executor.submit(download_one, path): path for path in blob_paths}

            # Collect results as they complete
            for future in as_completed(futures):
                blob_path, content = future.result()
                if content is not None:
                    results[blob_path] = content

        logger.info(
            f"[S3] Bulk download complete: {len(results)}/{len(blob_paths)} successful "
            f"({len(blob_paths) - len(results)} failed)"
        )

        return results

    def _delete_blob(self, blob_path: str) -> None:
        """
        Delete blob from S3.

        Args:
            blob_path: Full S3 object key

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If delete fails
        """
        try:
            # Check if object exists first
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=blob_path)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "NoSuchKey"):
                    raise NexusFileNotFoundError(blob_path) from e
                raise

            # Delete the object
            self.client.delete_object(Bucket=self.bucket_name, Key=blob_path)

        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete blob at {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e

    def _blob_exists(self, blob_path: str) -> bool:
        """
        Check if blob exists in S3.

        Args:
            blob_path: Full S3 object key

        Returns:
            True if blob exists, False otherwise
        """
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=blob_path)
            return True
        except ClientError:
            return False
        except Exception:
            return False

    def _get_blob_size(self, blob_path: str) -> int:
        """
        Get blob size from S3.

        Args:
            blob_path: Full S3 object key

        Returns:
            Blob size in bytes

        Raises:
            NexusFileNotFoundError: If blob doesn't exist
            BackendError: If operation fails
        """
        try:
            response = self.client.head_object(Bucket=self.bucket_name, Key=blob_path)
            return int(response["ContentLength"])

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise NexusFileNotFoundError(blob_path) from e
            raise BackendError(
                f"Failed to get blob size for {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e
        except Exception as e:
            raise BackendError(
                f"Failed to get blob size for {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e

    def _list_blobs(
        self,
        prefix: str,
        delimiter: str = "/",
    ) -> tuple[list[str], list[str]]:
        """
        List blobs in S3 with given prefix.

        Args:
            prefix: Prefix to filter blobs
            delimiter: Delimiter for virtual directories

        Returns:
            Tuple of (blob_keys, common_prefixes)

        Raises:
            BackendError: If list operation fails
        """
        try:
            # List objects with this prefix and delimiter
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name, Prefix=prefix, Delimiter=delimiter
            )

            blob_keys = [obj["Key"] for obj in response.get("Contents", [])]
            common_prefixes = [p["Prefix"] for p in response.get("CommonPrefixes", [])]

            return blob_keys, common_prefixes

        except Exception as e:
            raise BackendError(
                f"Failed to list blobs with prefix {prefix}: {e}",
                backend="s3_connector",
                path=prefix,
            ) from e

    def _create_directory_marker(self, blob_path: str) -> None:
        """
        Create directory marker in S3.

        Args:
            blob_path: Directory path (should end with '/')

        Raises:
            BackendError: If creation fails
        """
        try:
            # Create directory marker
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=blob_path,
                Body=b"",
                ContentType="application/x-directory",
            )

        except Exception as e:
            raise BackendError(
                f"Failed to create directory marker at {blob_path}: {e}",
                backend="s3_connector",
                path=blob_path,
            ) from e

    def _copy_blob(self, source_path: str, dest_path: str) -> None:
        """
        Copy blob to new location in S3.

        Args:
            source_path: Source S3 object key
            dest_path: Destination S3 object key

        Raises:
            NexusFileNotFoundError: If source doesn't exist
            BackendError: If copy fails
        """
        try:
            # Copy to new location
            copy_source = {"Bucket": self.bucket_name, "Key": source_path}
            self.client.copy_object(Bucket=self.bucket_name, Key=dest_path, CopySource=copy_source)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise NexusFileNotFoundError(source_path) from e
            raise BackendError(
                f"Failed to copy blob from {source_path} to {dest_path}: {e}",
                backend="s3_connector",
                path=source_path,
            ) from e
        except Exception as e:
            raise BackendError(
                f"Failed to copy blob from {source_path} to {dest_path}: {e}",
                backend="s3_connector",
                path=source_path,
            ) from e

    # === Override Content Operations with Caching ===

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[bytes]:
        """
        Read content from S3 with caching support.

        When caching is enabled (db_session provided):
        1. Check cache for non-stale entry with matching version
        2. If cache hit, return cached content
        3. If cache miss, read from S3 and cache result

        Args:
            content_hash: Version ID (if versioning) or hash (if not)
            context: Operation context with backend_path

        Returns:
            HandlerResponse with file content as bytes in data field
        """
        start_time = time.perf_counter()

        if not context or not context.backend_path:
            return HandlerResponse.error(
                message="S3 connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes.",
                code=400,
                is_expected=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        # Get cache path (prefers virtual_path over backend_path)
        cache_path = self._get_cache_path(context) or context.backend_path
        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Check cache first if enabled
            # PERF FIX (Issue #847): Trust L1 cache TTL instead of making API call on every hit.
            # Previously, get_version() was called on every cache hit (50-200ms API call),
            # which negated the benefit of L1 caching (<1ms). Now we rely on TTL-based
            # expiration (default 5 min) which is sufficient for most use cases.
            # Users needing real-time consistency can use --no-cache.
            if self._has_caching():
                import contextlib

                with contextlib.suppress(Exception):
                    cached = self._read_from_cache(cache_path, original=True)
                    if cached and not cached.stale and cached.content_binary:
                        logger.info(f"[S3] Cache hit (TTL-based) for {cache_path}")
                        return HandlerResponse.ok(
                            data=cached.content_binary,
                            execution_time_ms=(time.perf_counter() - start_time) * 1000,
                            backend_name=self.name,
                            path=blob_path,
                        )

            # Read from S3 backend
            logger.info(f"[S3] Cache miss, reading from backend: {cache_path}")

            # Determine if we should use version ID
            version_id = None
            if self.versioning_enabled and content_hash and self._is_version_id(content_hash):
                version_id = content_hash

            content, response_version_id = self._download_blob(blob_path, version_id)

            # Cache the result if caching is enabled
            if self._has_caching():
                import contextlib

                with contextlib.suppress(Exception):
                    # Use version ID from download instead of making extra API call
                    zone_id = getattr(context, "zone_id", None)
                    self._write_to_cache(
                        path=cache_path,
                        content=content,
                        backend_version=response_version_id,
                        zone_id=zone_id,
                    )

            return HandlerResponse.ok(
                data=content,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=blob_path,
            )

        except Exception as e:
            return HandlerResponse.from_exception(
                e,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=blob_path,
            )

    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[str]:
        """
        Write content to S3 and update cache.

        Per design doc (cache-layer.md), after successful write:
        1. Write to S3 backend
        2. Update cache with new content and version

        Args:
            content: File content as bytes
            context: Operation context with backend_path

        Returns:
            HandlerResponse with version ID (S3 version or content hash) in data field
        """
        start_time = time.perf_counter()

        if not context or not context.backend_path:
            return HandlerResponse.error(
                message="S3 connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes.",
                code=400,
                is_expected=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        # Get cache path (prefers virtual_path over backend_path)
        cache_path = self._get_cache_path(context) or context.backend_path

        # Get actual blob path from backend_path
        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Detect appropriate Content-Type with charset for proper encoding
            content_type = self._detect_content_type(context.backend_path, content)

            # Upload blob
            new_version = self._upload_blob(blob_path, content, content_type)

            # Update cache after write if caching is enabled
            # Per design doc: both S3 and cache should be updated when write succeeds
            if self._has_caching():
                import contextlib

                with contextlib.suppress(Exception):
                    zone_id = getattr(context, "zone_id", None)
                    self._write_to_cache(
                        path=cache_path,
                        content=content,
                        backend_version=new_version,
                        zone_id=zone_id,
                    )

            return HandlerResponse.ok(
                data=new_version,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=blob_path,
            )

        except Exception as e:
            return HandlerResponse.from_exception(
                e,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=blob_path,
            )

    def write_content_with_version_check(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
        expected_version: str | None = None,
    ) -> HandlerResponse[str]:
        """
        Write content with optimistic locking via version check.

        Args:
            content: File content as bytes
            context: Operation context with backend_path
            expected_version: Expected S3 version for optimistic locking

        Returns:
            HandlerResponse with new version ID (S3 version or content hash) in data field
        """
        start_time = time.perf_counter()

        if not context or not context.backend_path:
            return HandlerResponse.error(
                message="S3 connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes.",
                code=400,
                is_expected=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        blob_path = self._get_blob_path(context.backend_path)

        try:
            # Get cache path (prefers virtual_path over backend_path)
            cache_path = self._get_cache_path(context) or context.backend_path

            # Version check if requested
            if expected_version is not None:
                self._check_version(cache_path, expected_version, context)

            # Perform the write (returns HandlerResponse)
            return self.write_content(content, context)

        except Exception as e:
            return HandlerResponse.from_exception(
                e,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=blob_path,
            )

    # === Multipart Upload Operations (Issue #788) ===

    def init_multipart(
        self,
        backend_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Initialize an S3 multipart upload.

        Args:
            backend_path: S3 key for the upload target.
            content_type: MIME type of the content.
            metadata: Optional key-value metadata for the upload.

        Returns:
            S3 UploadId string.
        """
        blob_path = self._get_blob_path(backend_path)
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name,
            "Key": blob_path,
            "ContentType": content_type,
        }
        if metadata:
            kwargs["Metadata"] = metadata

        response = self.client.create_multipart_upload(**kwargs)
        upload_id: str = response["UploadId"]
        logger.debug(f"S3 multipart upload initiated: {upload_id} for {blob_path}")
        return upload_id

    def upload_part(
        self,
        backend_path: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> dict[str, Any]:
        """Upload a single part to S3 multipart upload.

        Args:
            backend_path: S3 key for the upload target.
            upload_id: S3 UploadId from init_multipart().
            part_number: 1-based part number.
            data: Raw bytes for this chunk.

        Returns:
            Dict with "etag" and "part_number" keys.
        """
        blob_path = self._get_blob_path(backend_path)
        response = self.client.upload_part(
            Bucket=self.bucket_name,
            Key=blob_path,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )
        return {"etag": response["ETag"], "part_number": part_number}

    def complete_multipart(
        self,
        backend_path: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> str:
        """Complete an S3 multipart upload.

        Args:
            backend_path: S3 key for the upload target.
            upload_id: S3 UploadId from init_multipart().
            parts: Ordered list of part dicts with "etag" and "part_number".

        Returns:
            S3 ETag of the completed object.
        """
        blob_path = self._get_blob_path(backend_path)
        multipart_upload = {
            "Parts": [
                {"ETag": p["etag"], "PartNumber": p["part_number"]}
                for p in sorted(parts, key=lambda x: x["part_number"])
            ]
        }

        response = self.client.complete_multipart_upload(
            Bucket=self.bucket_name,
            Key=blob_path,
            UploadId=upload_id,
            MultipartUpload=multipart_upload,
        )
        etag: str = response.get("ETag", "")
        logger.debug(f"S3 multipart upload completed: {upload_id} -> {etag}")
        return etag

    def abort_multipart(
        self,
        backend_path: str,
        upload_id: str,
    ) -> None:
        """Abort an S3 multipart upload and clean up parts.

        Args:
            backend_path: S3 key for the upload target.
            upload_id: S3 UploadId from init_multipart().
        """
        blob_path = self._get_blob_path(backend_path)
        try:
            self.client.abort_multipart_upload(
                Bucket=self.bucket_name,
                Key=blob_path,
                UploadId=upload_id,
            )
            logger.debug(f"S3 multipart upload aborted: {upload_id}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code != "NoSuchUpload":
                raise BackendError(
                    f"Failed to abort multipart upload: {e}",
                    backend="s3_connector",
                    path=blob_path,
                ) from e
