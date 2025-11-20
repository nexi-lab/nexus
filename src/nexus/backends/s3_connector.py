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

import hashlib
import mimetypes
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from nexus.backends.backend import Backend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


class S3ConnectorBackend(Backend):
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

    Versioning Behavior:
    - If bucket has versioning enabled: Uses S3 version IDs for version tracking
    - If bucket has no versioning: Only current version retained (overwrites on update)

    Limitations:
    - No deduplication (same content stored multiple times)
    - Requires backend_path in OperationContext
    """

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
            # Priority: explicit credentials > credentials_path > default chain
            if access_key_id and secret_access_key:
                # Use explicit credentials
                self.client = boto3.client(
                    "s3",
                    region_name=region_name,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    aws_session_token=session_token,
                )
                self.resource = boto3.resource(
                    "s3",
                    region_name=region_name,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    aws_session_token=session_token,
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
                )
                self.resource = boto3.resource(
                    "s3",
                    region_name=region_name or creds.get("region_name"),
                    aws_access_key_id=creds.get("aws_access_key_id"),
                    aws_secret_access_key=creds.get("aws_secret_access_key"),
                    aws_session_token=creds.get("aws_session_token"),
                )
            else:
                # Use default credentials chain
                self.client = boto3.client("s3", region_name=region_name)
                self.resource = boto3.resource("s3", region_name=region_name)

            self.bucket = self.resource.Bucket(bucket_name)
            self.bucket_name = bucket_name
            self.prefix = prefix.rstrip("/")  # Remove trailing slash

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
            self.versioning_enabled = versioning.get("Status") == "Enabled"

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

    def _get_s3_path(self, backend_path: str) -> str:
        """
        Convert backend-relative path to S3 object path.

        Args:
            backend_path: Path relative to mount point (e.g., "file.txt")

        Returns:
            Full S3 object path including prefix (e.g., "data/file.txt")
        """
        backend_path = backend_path.lstrip("/")
        if self.prefix:
            if backend_path:
                return f"{self.prefix}/{backend_path}"
            else:
                # For empty backend_path, return prefix without trailing slash
                return self.prefix
        return backend_path

    def _compute_hash(self, content: bytes) -> str:
        """Compute SHA-256 hash of content for metadata compatibility."""
        return hashlib.sha256(content).hexdigest()

    def _detect_content_type(self, backend_path: str, content: bytes) -> str:
        """
        Detect appropriate Content-Type for file based on path and content.

        For text files, ensures charset=utf-8 is included for proper display.

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

    # === Content Operations (Path-based, not CAS) ===

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to S3 at actual path (not CAS path).

        Requires backend_path in context to know where to write.

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

        # Get actual S3 path from backend_path
        s3_path = self._get_s3_path(context.backend_path)

        try:
            # Detect appropriate Content-Type with charset for proper encoding
            content_type = self._detect_content_type(context.backend_path, content)

            # Write directly to actual path in S3 with proper Content-Type
            response = self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_path,
                Body=content,
                ContentType=content_type,
            )

            # If bucket has versioning enabled, return version ID
            # Otherwise, return content hash for metadata tracking
            if self.versioning_enabled and "VersionId" in response:
                return str(response["VersionId"])
            else:
                # No versioning - compute hash for metadata
                content_hash = self._compute_hash(content)
                return content_hash

        except Exception as e:
            raise BackendError(
                f"Failed to write content to {s3_path}: {e}",
                backend="s3_connector",
                path=s3_path,
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read content from S3 using backend_path.

        For connector backends with versioning enabled:
        - content_hash is the S3 version ID
        - Reads that specific version from S3

        For connector backends without versioning:
        - content_hash is ignored (just metadata hash)
        - Always reads current content from backend_path

        Args:
            content_hash: S3 version ID (if versioning) or hash (if not)
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
                "S3 connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes."
            )

        # Get actual S3 path from backend_path
        s3_path = self._get_s3_path(context.backend_path)

        try:
            # If versioning enabled and content_hash looks like a version ID,
            # retrieve that specific version
            get_params: dict = {"Bucket": self.bucket_name, "Key": s3_path}

            # S3 version IDs are typically alphanumeric strings
            # Check if this could be a version ID (versioning enabled and not a hex hash)
            if self.versioning_enabled and content_hash and not self._is_hex_hash(content_hash):
                get_params["VersionId"] = content_hash

            response = self.client.get_object(**get_params)
            content = response["Body"].read()
            return bytes(content)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise NexusFileNotFoundError(s3_path) from e
            raise BackendError(
                f"Failed to read content from {s3_path}: {e}",
                backend="s3_connector",
                path=s3_path,
            ) from e
        except Exception as e:
            raise BackendError(
                f"Failed to read content from {s3_path}: {e}",
                backend="s3_connector",
                path=s3_path,
            ) from e

    def _is_hex_hash(self, value: str) -> bool:
        """Check if value looks like a hex hash (64 chars, all hex digits)."""
        if len(value) != 64:
            return False
        try:
            int(value, 16)
            return True
        except ValueError:
            return False

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete content from S3 using backend_path.

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
            raise ValueError("S3 connector requires backend_path in OperationContext")

        s3_path = self._get_s3_path(context.backend_path)

        try:
            # Check if object exists first
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=s3_path)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "NoSuchKey"):
                    raise NexusFileNotFoundError(s3_path) from e
                raise

            # Delete the object
            self.client.delete_object(Bucket=self.bucket_name, Key=s3_path)

        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete content at {s3_path}: {e}",
                backend="s3_connector",
                path=s3_path,
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
            s3_path = self._get_s3_path(context.backend_path)
            self.client.head_object(Bucket=self.bucket_name, Key=s3_path)
            return True
        except ClientError:
            return False
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
            raise ValueError("S3 connector requires backend_path in OperationContext")

        s3_path = self._get_s3_path(context.backend_path)

        try:
            response = self.client.head_object(Bucket=self.bucket_name, Key=s3_path)
            return int(response["ContentLength"])

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise NexusFileNotFoundError(s3_path) from e
            raise BackendError(
                f"Failed to get content size for {s3_path}: {e}",
                backend="s3_connector",
                path=s3_path,
            ) from e
        except Exception as e:
            raise BackendError(
                f"Failed to get content size for {s3_path}: {e}",
                backend="s3_connector",
                path=s3_path,
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
        Create directory marker in S3.

        S3 doesn't have native directories, so we create marker objects
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

        # S3 directories are represented with trailing slash
        s3_path = self._get_s3_path(path) + "/"

        try:
            # Check if directory marker already exists
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=s3_path)
                if not exist_ok:
                    raise FileExistsError(f"Directory already exists: {path}")
                return
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code not in ("404", "NoSuchKey"):
                    raise

            if not parents:
                # Check if parent exists
                parent = "/".join(path.split("/")[:-1])
                if parent and not self.is_directory(parent):
                    raise FileNotFoundError(f"Parent directory not found: {parent}")

            # Create directory marker
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_path,
                Body=b"",
                ContentType="application/x-directory",
            )

        except (FileExistsError, FileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to create directory {path}: {e}",
                backend="s3_connector",
                path=path,
            ) from e

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove directory from S3.

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
            raise BackendError("Cannot remove root directory", backend="s3_connector", path=path)

        s3_path = self._get_s3_path(path) + "/"

        try:
            # Check if directory marker exists
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=s3_path)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "NoSuchKey"):
                    raise NexusFileNotFoundError(path) from e
                raise

            if not recursive:
                # Check if directory is empty
                response = self.client.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=s3_path, MaxKeys=2
                )
                contents = response.get("Contents", [])
                if len(contents) > 1:
                    raise OSError(f"Directory not empty: {path}")

            # Delete directory marker
            self.client.delete_object(Bucket=self.bucket_name, Key=s3_path)

            if recursive:
                # Delete all objects with this prefix
                paginator = self.client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=self.bucket_name, Prefix=s3_path):
                    for obj in page.get("Contents", []):
                        self.client.delete_object(Bucket=self.bucket_name, Key=obj["Key"])

        except (NexusFileNotFoundError, OSError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to remove directory {path}: {e}",
                backend="s3_connector",
                path=path,
            ) from e

    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.

        In S3, a "directory" is either:
        1. An explicit directory marker blob (path ending with "/")
        2. A virtual directory (any prefix that has blobs under it)

        This is important because S3 doesn't require explicit directory markers.
        For example, creating "folder/file.txt" doesn't require "folder/" to exist.

        Args:
            path: Path to check

        Returns:
            True if path is a directory (has marker or has children), False otherwise
        """
        try:
            path = path.strip("/")
            if not path:
                return True  # Root is always a directory

            s3_path = self._get_s3_path(path)

            # Check 1: Explicit directory marker blob
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=s3_path + "/")
                return True
            except ClientError:
                pass

            # Check 2: Virtual directory (has any blobs under this prefix)
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name, Prefix=s3_path + "/", MaxKeys=1
            )

            # If there's at least one object with this prefix, it's a directory
            return len(response.get("Contents", [])) > 0

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
            s3_base_path = self._get_s3_path(path)
            prefix = s3_base_path + "/" if s3_base_path else ""

            # List objects with this prefix and delimiter
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name, Prefix=prefix, Delimiter="/"
            )

            entries = set()

            # Add direct file objects
            for obj in response.get("Contents", []):
                name = obj["Key"][len(prefix) :]
                if name and name != "":
                    entries.add(name.rstrip("/"))

            # Add subdirectories (common prefixes)
            for prefix_obj in response.get("CommonPrefixes", []):
                name = prefix_obj["Prefix"][len(prefix) :].rstrip("/")
                if name:
                    entries.add(name + "/")

            return sorted(entries)

        except (FileNotFoundError, NotADirectoryError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="s3_connector",
                path=path,
            ) from e

    def rename_file(self, old_path: str, new_path: str) -> None:
        """
        Rename/move a file in S3.

        For path-based connector backends, we need to actually move
        the file in S3 (copy to new location and delete old).

        Args:
            old_path: Current backend-relative path
            new_path: New backend-relative path

        Raises:
            FileNotFoundError: If source file doesn't exist
            FileExistsError: If destination already exists
            BackendError: If operation fails
        """
        try:
            old_path = old_path.strip("/")
            new_path = new_path.strip("/")

            old_s3_path = self._get_s3_path(old_path)
            new_s3_path = self._get_s3_path(new_path)

            # Check source exists
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=old_s3_path)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "NoSuchKey"):
                    raise FileNotFoundError(f"Source file not found: {old_path}") from e
                raise

            # Check destination doesn't exist
            try:
                self.client.head_object(Bucket=self.bucket_name, Key=new_s3_path)
                raise FileExistsError(f"Destination already exists: {new_path}")
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code not in ("404", "NoSuchKey"):
                    raise

            # Copy to new location
            copy_source = {"Bucket": self.bucket_name, "Key": old_s3_path}
            self.client.copy_object(
                Bucket=self.bucket_name, Key=new_s3_path, CopySource=copy_source
            )

            # Delete old location
            self.client.delete_object(Bucket=self.bucket_name, Key=old_s3_path)

        except (FileNotFoundError, FileExistsError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to rename file {old_path} -> {new_path}: {e}",
                backend="s3_connector",
                path=old_path,
            ) from e
