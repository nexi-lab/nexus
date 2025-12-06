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
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


@register_connector(
    "s3_connector",
    description="AWS S3 with direct path mapping",
    category="storage",
    requires=["boto3"],
)
class S3ConnectorBackend(BaseBlobStorageConnector, CacheConnectorMixin):
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
    ) -> bytes:
        """
        Download blob from S3.

        Args:
            blob_path: Full S3 object key
            version_id: Optional S3 version ID

        Returns:
            File content as bytes

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
            return bytes(content)

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
    ) -> bytes:
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
            File content as bytes

        Raises:
            ValueError: If backend_path not provided
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "S3 connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes."
            )

        # Get cache path (prefers virtual_path over backend_path)
        cache_path = self._get_cache_path(context) or context.backend_path

        # Check cache first if enabled
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                cached = self._read_from_cache(cache_path, original=True)
                if cached and not cached.stale and cached.content_binary:
                    # Verify version matches if we have version info
                    if cached.backend_version and content_hash:
                        if cached.backend_version == content_hash:
                            logger.info(f"[S3] Cache hit for {cache_path}")
                            return cached.content_binary
                        # Version mismatch - cache is stale, read from backend
                        logger.debug(f"[S3] Cache version mismatch for {cache_path}")
                    else:
                        # No version to compare, trust the cache
                        logger.info(f"[S3] Cache hit (no version) for {cache_path}")
                        return cached.content_binary

        # Read from S3 backend
        logger.info(f"[S3] Cache miss, reading from backend: {cache_path}")
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
                version = self.get_version(context.backend_path, context)
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=cache_path,
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
        Write content to S3 and update cache.

        Per design doc (cache-layer.md), after successful write:
        1. Write to S3 backend
        2. Update cache with new content and version

        Args:
            content: File content as bytes
            context: Operation context with backend_path

        Returns:
            If versioning enabled: S3 version ID
            If no versioning: Content hash (for metadata compatibility)

        Raises:
            ValueError: If backend_path is not provided in context
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "S3 connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes."
            )

        # Get cache path (prefers virtual_path over backend_path)
        cache_path = self._get_cache_path(context) or context.backend_path

        # Get actual blob path from backend_path
        blob_path = self._get_blob_path(context.backend_path)

        # Detect appropriate Content-Type with charset for proper encoding
        content_type = self._detect_content_type(context.backend_path, content)

        # Upload blob
        new_version = self._upload_blob(blob_path, content, content_type)

        # Update cache after write if caching is enabled
        # Per design doc: both S3 and cache should be updated when write succeeds
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=cache_path,
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
            expected_version: Expected S3 version for optimistic locking

        Returns:
            New S3 version ID (or content hash if no versioning)

        Raises:
            ValueError: If backend_path not provided
            ConflictError: If version check fails
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise ValueError(
                "S3 connector requires backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes."
            )

        # Get cache path (prefers virtual_path over backend_path)
        cache_path = self._get_cache_path(context) or context.backend_path

        # Version check if requested
        if expected_version is not None:
            self._check_version(cache_path, expected_version, context)

        # Perform the write
        return self.write_content(content, context)
