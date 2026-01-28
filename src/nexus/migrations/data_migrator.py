"""Data migrator for bulk imports from external sources.

This module provides utilities for importing data from:
- Amazon S3 buckets
- Google Cloud Storage buckets
- Local filesystem directories

Issue #165: Migration Tools & Upgrade Paths
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFilesystem


@dataclass
class ImportOptions:
    """Options for bulk import operations.

    Attributes:
        source_type: Type of source ('s3', 'gcs', 'local')
        recursive: Import subdirectories recursively
        overwrite: Overwrite existing files
        dry_run: Simulate without making changes
        checksum: Verify checksums after import
        batch_size: Number of files to process in each batch
        max_workers: Maximum parallel workers for import
        include_patterns: Glob patterns to include (empty = all)
        exclude_patterns: Glob patterns to exclude
    """

    source_type: Literal["s3", "gcs", "local"] = "local"
    recursive: bool = True
    overwrite: bool = False
    dry_run: bool = False
    checksum: bool = True
    batch_size: int = 100
    max_workers: int = 10
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """Result of an import operation.

    Attributes:
        files_imported: Number of files successfully imported
        files_skipped: Number of files skipped (already exist)
        files_failed: Number of files that failed to import
        bytes_transferred: Total bytes transferred
        errors: List of error messages
        duration_seconds: Total time taken
    """

    files_imported: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def total_processed(self) -> int:
        """Total number of files processed."""
        return self.files_imported + self.files_skipped + self.files_failed

    def __str__(self) -> str:
        """Human-readable summary."""
        return (
            f"ImportResult(imported={self.files_imported}, "
            f"skipped={self.files_skipped}, failed={self.files_failed}, "
            f"bytes={self.bytes_transferred:,}, duration={self.duration_seconds:.2f}s)"
        )


@dataclass
class FileInfo:
    """Information about a file to import.

    Attributes:
        source_path: Path in the source system
        target_path: Target path in Nexus
        size: File size in bytes
        checksum: Optional content checksum
    """

    source_path: str
    target_path: str
    size: int = 0
    checksum: str | None = None


class DataMigrator:
    """Handles bulk data migration from external sources.

    Supports importing from S3, GCS, and local filesystem into Nexus.
    Uses existing backend connectors for cloud storage access.

    Example:
        migrator = DataMigrator(nexus_fs)
        result = migrator.import_from_s3(
            bucket="my-bucket",
            prefix="/data/",
            target_path="/workspace/imported/",
            options=ImportOptions(dry_run=True),
        )
    """

    def __init__(self, nx: NexusFilesystem) -> None:
        """Initialize data migrator.

        Args:
            nx: Nexus filesystem instance to import into
        """
        self.nx = nx

    def import_from_s3(
        self,
        bucket: str,
        prefix: str,
        target_path: str,
        options: ImportOptions | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str | None = None,
    ) -> ImportResult:
        """Import files from an S3 bucket.

        Args:
            bucket: S3 bucket name
            prefix: Key prefix to import from
            target_path: Target path in Nexus
            options: Import options
            progress_callback: Optional callback for progress updates
            aws_access_key_id: AWS access key (uses env if not provided)
            aws_secret_access_key: AWS secret key (uses env if not provided)
            region_name: AWS region (uses env if not provided)

        Returns:
            ImportResult with details of the import
        """
        import time

        try:
            import boto3
        except ImportError:
            return ImportResult(
                errors=["boto3 is required for S3 imports. Install with: pip install boto3"]
            )

        options = options or ImportOptions(source_type="s3")
        start_time = time.time()
        result = ImportResult()

        # Create S3 client
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=aws_access_key_id or os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=aws_secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region_name=region_name or os.environ.get("AWS_DEFAULT_REGION"),
        )

        # List objects in bucket
        files_to_import = list(self._list_s3_objects(s3_client, bucket, prefix, options))
        total_files = len(files_to_import)

        for i, file_info in enumerate(files_to_import):
            if progress_callback:
                progress_callback(f"Importing {file_info.source_path}", i + 1, total_files)

            # Calculate target path
            relative_path = file_info.source_path
            if prefix and relative_path.startswith(prefix):
                relative_path = relative_path[len(prefix) :].lstrip("/")

            full_target = f"{target_path.rstrip('/')}/{relative_path}"

            # Check if exists
            if not options.overwrite and self.nx.exists(full_target):
                result.files_skipped += 1
                continue

            if options.dry_run:
                result.files_imported += 1
                result.bytes_transferred += file_info.size
                continue

            try:
                # Download and import
                response = s3_client.get_object(Bucket=bucket, Key=file_info.source_path)
                content = response["Body"].read()

                # Write to Nexus
                self.nx.write(full_target, content)

                result.files_imported += 1
                result.bytes_transferred += len(content)

            except Exception as e:
                result.files_failed += 1
                result.errors.append(f"Failed to import {file_info.source_path}: {e}")

        result.duration_seconds = time.time() - start_time
        return result

    def import_from_gcs(
        self,
        bucket: str,
        prefix: str,
        target_path: str,
        options: ImportOptions | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
        credentials_path: str | None = None,
    ) -> ImportResult:
        """Import files from a Google Cloud Storage bucket.

        Args:
            bucket: GCS bucket name
            prefix: Blob prefix to import from
            target_path: Target path in Nexus
            options: Import options
            progress_callback: Optional callback for progress updates
            credentials_path: Path to service account credentials JSON

        Returns:
            ImportResult with details of the import
        """
        import time

        try:
            from google.cloud import storage
        except ImportError:
            return ImportResult(
                errors=[
                    "google-cloud-storage is required for GCS imports. "
                    "Install with: pip install google-cloud-storage"
                ]
            )

        options = options or ImportOptions(source_type="gcs")
        start_time = time.time()
        result = ImportResult()

        # Create GCS client
        if credentials_path:
            client = storage.Client.from_service_account_json(credentials_path)
        else:
            client = storage.Client()

        gcs_bucket = client.bucket(bucket)

        # List blobs
        files_to_import = list(self._list_gcs_blobs(gcs_bucket, prefix, options))
        total_files = len(files_to_import)

        for i, file_info in enumerate(files_to_import):
            if progress_callback:
                progress_callback(f"Importing {file_info.source_path}", i + 1, total_files)

            # Calculate target path
            relative_path = file_info.source_path
            if prefix and relative_path.startswith(prefix):
                relative_path = relative_path[len(prefix) :].lstrip("/")

            full_target = f"{target_path.rstrip('/')}/{relative_path}"

            # Check if exists
            if not options.overwrite and self.nx.exists(full_target):
                result.files_skipped += 1
                continue

            if options.dry_run:
                result.files_imported += 1
                result.bytes_transferred += file_info.size
                continue

            try:
                # Download and import
                blob = gcs_bucket.blob(file_info.source_path)
                content = blob.download_as_bytes()

                # Write to Nexus
                self.nx.write(full_target, content)

                result.files_imported += 1
                result.bytes_transferred += len(content)

            except Exception as e:
                result.files_failed += 1
                result.errors.append(f"Failed to import {file_info.source_path}: {e}")

        result.duration_seconds = time.time() - start_time
        return result

    def import_from_local(
        self,
        source_path: str,
        target_path: str,
        options: ImportOptions | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> ImportResult:
        """Import files from local filesystem.

        Args:
            source_path: Local directory to import from
            target_path: Target path in Nexus
            options: Import options
            progress_callback: Optional callback for progress updates

        Returns:
            ImportResult with details of the import
        """
        import time

        options = options or ImportOptions(source_type="local")
        start_time = time.time()
        result = ImportResult()

        source_dir = Path(source_path)
        if not source_dir.exists():
            result.errors.append(f"Source path does not exist: {source_path}")
            return result

        if not source_dir.is_dir():
            result.errors.append(f"Source path is not a directory: {source_path}")
            return result

        # List files
        files_to_import = list(self._list_local_files(source_dir, options))
        total_files = len(files_to_import)

        for i, file_info in enumerate(files_to_import):
            if progress_callback:
                progress_callback(f"Importing {file_info.source_path}", i + 1, total_files)

            # Calculate target path
            relative_path = Path(file_info.source_path).relative_to(source_dir)
            full_target = f"{target_path.rstrip('/')}/{relative_path}"

            # Check if exists
            if not options.overwrite and self.nx.exists(full_target):
                result.files_skipped += 1
                continue

            if options.dry_run:
                result.files_imported += 1
                result.bytes_transferred += file_info.size
                continue

            try:
                # Read local file
                local_path = Path(file_info.source_path)
                content = local_path.read_bytes()

                # Write to Nexus
                self.nx.write(full_target, content)

                result.files_imported += 1
                result.bytes_transferred += len(content)

            except Exception as e:
                result.files_failed += 1
                result.errors.append(f"Failed to import {file_info.source_path}: {e}")

        result.duration_seconds = time.time() - start_time
        return result

    def _list_s3_objects(
        self, s3_client: Any, bucket: str, prefix: str, options: ImportOptions
    ) -> Iterator[FileInfo]:
        """List objects in S3 bucket matching criteria.

        Args:
            s3_client: Boto3 S3 client
            bucket: Bucket name
            prefix: Key prefix
            options: Import options

        Yields:
            FileInfo for each matching object
        """
        paginator = s3_client.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]

                # Skip directories
                if key.endswith("/"):
                    continue

                # Check patterns
                if not self._matches_patterns(key, options):
                    continue

                yield FileInfo(
                    source_path=key,
                    target_path="",  # Calculated later
                    size=obj["Size"],
                )

    def _list_gcs_blobs(
        self, bucket: Any, prefix: str, options: ImportOptions
    ) -> Iterator[FileInfo]:
        """List blobs in GCS bucket matching criteria.

        Args:
            bucket: GCS bucket object
            prefix: Blob prefix
            options: Import options

        Yields:
            FileInfo for each matching blob
        """
        blobs = bucket.list_blobs(prefix=prefix)

        for blob in blobs:
            # Skip directories
            if blob.name.endswith("/"):
                continue

            # Check patterns
            if not self._matches_patterns(blob.name, options):
                continue

            yield FileInfo(
                source_path=blob.name,
                target_path="",  # Calculated later
                size=blob.size or 0,
            )

    def _list_local_files(self, source_dir: Path, options: ImportOptions) -> Iterator[FileInfo]:
        """List files in local directory matching criteria.

        Args:
            source_dir: Source directory path
            options: Import options

        Yields:
            FileInfo for each matching file
        """
        files = source_dir.rglob("*") if options.recursive else source_dir.glob("*")

        for file_path in files:
            # Skip directories
            if file_path.is_dir():
                continue

            # Check patterns
            if not self._matches_patterns(str(file_path), options):
                continue

            yield FileInfo(
                source_path=str(file_path),
                target_path="",  # Calculated later
                size=file_path.stat().st_size,
            )

    def _matches_patterns(self, path: str, options: ImportOptions) -> bool:
        """Check if path matches include/exclude patterns.

        Args:
            path: Path to check
            options: Import options with patterns

        Returns:
            True if path should be included
        """
        import fnmatch

        # If include patterns specified, must match at least one
        if options.include_patterns and not any(
            fnmatch.fnmatch(path, p) for p in options.include_patterns
        ):
            return False

        # If exclude patterns specified, must not match any
        return not (
            options.exclude_patterns
            and any(fnmatch.fnmatch(path, p) for p in options.exclude_patterns)
        )

    def _compute_checksum(self, content: bytes) -> str:
        """Compute SHA-256 checksum of content.

        Args:
            content: Bytes to hash

        Returns:
            Hex-encoded SHA-256 hash
        """
        return hashlib.sha256(content).hexdigest()
