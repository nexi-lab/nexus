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

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    pass


@register_connector(
    "s3_connector",
    description="AWS S3 with direct path mapping",
    category="storage",
    requires=["boto3"],
)
class S3ConnectorBackend(BaseBlobStorageConnector):
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
