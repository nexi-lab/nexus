"""Data models for .nexus bundle format and tenant portability.

This module defines the data structures for exporting and importing
tenant data as portable .nexus bundles. The bundle format supports:

- Complete tenant data portability (files, metadata, permissions, embeddings)
- Cross-tenant migration with ID remapping
- GDPR Article 20 compliance (right to data portability)
- Incremental and filtered exports

Bundle Format (tar.gz):
```
company-a-export-2025-01-31.nexus
├── manifest.json           # Bundle metadata and checksums
├── metadata/
│   ├── files.jsonl         # File path records (streaming)
│   ├── versions.jsonl      # Version history records
│   └── operations.jsonl    # Operation log (optional)
├── permissions/
│   ├── rebac_tuples.jsonl  # Permission relationships
│   └── api_keys.jsonl.enc  # Encrypted API keys
├── embeddings/
│   └── vectors.parquet     # Vector embeddings (optional)
└── content/
    └── cas/
        ├── ab/
        │   └── abcdef123...  # Content-addressable blobs
        └── ...
```

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Tenant Data Portability
"""

from __future__ import annotations

import builtins
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

# =============================================================================
# Constants
# =============================================================================

BUNDLE_FORMAT_VERSION = "1.0.0"
BUNDLE_EXTENSION = ".nexus"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_COMPRESSION_LEVEL = 6
DEFAULT_HASH_ALGORITHM = "sha256"
MANIFEST_SCHEMA_URL = "https://nexus.io/schemas/manifest-v1.json"
MANIFEST_SCHEMA_PATH = Path(__file__).parent / "schemas" / "manifest-v1.json"


class ConflictMode(StrEnum):
    """How to handle path collisions during import."""

    SKIP = "skip"  # Skip conflicting items, keep existing
    OVERWRITE = "overwrite"  # Always use imported data
    MERGE = "merge"  # Merge metadata, prefer newer content
    FAIL = "fail"  # Abort import on first conflict


class ContentMode(StrEnum):
    """How to handle content blobs during import."""

    INCLUDE = "include"  # Import content blobs
    REFERENCE = "reference"  # Only import references (content must exist)
    SKIP = "skip"  # Skip content entirely (metadata only)


# =============================================================================
# Checksum Models
# =============================================================================


@dataclass
class FileChecksum:
    """Checksum information for a single file in the bundle.

    Attributes:
        path: Relative path within the bundle
        algorithm: Hash algorithm used (e.g., "sha256")
        hash: Hex-encoded hash value
        size_bytes: File size in bytes
    """

    path: str
    algorithm: str
    hash: str
    size_bytes: int

    def verify(self, data: bytes) -> bool:
        """Verify that data matches this checksum.

        Args:
            data: Raw bytes to verify

        Returns:
            True if checksum matches, False otherwise
        """
        if self.algorithm == "sha256":
            computed = hashlib.sha256(data).hexdigest()
        elif self.algorithm == "sha512":
            computed = hashlib.sha512(data).hexdigest()
        elif self.algorithm == "md5":
            computed = hashlib.md5(data).hexdigest()  # noqa: S324
        else:
            raise ValueError(f"Unsupported hash algorithm: {self.algorithm}")

        return computed == self.hash

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "path": self.path,
            "algorithm": self.algorithm,
            "hash": self.hash,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create from dictionary."""
        return cls(
            path=data["path"],
            algorithm=data["algorithm"],
            hash=data["hash"],
            size_bytes=data["size_bytes"],
        )


@dataclass
class BundleChecksums:
    """Checksum information for bundle integrity verification.

    Supports both individual file checksums and Merkle tree root hash
    for efficient partial verification.

    Attributes:
        algorithm: Default hash algorithm for all checksums
        files: Individual file checksums
        merkle_root: Optional Merkle tree root hash for efficient verification
    """

    algorithm: str = DEFAULT_HASH_ALGORITHM
    files: dict[str, FileChecksum] = field(default_factory=dict)
    merkle_root: str | None = None

    def add_file(self, path: str, data: bytes) -> FileChecksum:
        """Add a file and compute its checksum.

        Args:
            path: Relative path within the bundle
            data: Raw file content

        Returns:
            The computed FileChecksum
        """
        if self.algorithm == "sha256":
            hash_value = hashlib.sha256(data).hexdigest()
        elif self.algorithm == "sha512":
            hash_value = hashlib.sha512(data).hexdigest()
        else:
            raise ValueError(f"Unsupported algorithm: {self.algorithm}")

        checksum = FileChecksum(
            path=path,
            algorithm=self.algorithm,
            hash=hash_value,
            size_bytes=len(data),
        )
        self.files[path] = checksum
        return checksum

    def verify_file(self, path: str, data: bytes) -> bool:
        """Verify a file against its stored checksum.

        Args:
            path: Relative path within the bundle
            data: Raw file content

        Returns:
            True if checksum matches, False if not found or mismatch
        """
        checksum = self.files.get(path)
        if checksum is None:
            return False
        return checksum.verify(data)

    def compute_merkle_root(self) -> str:
        """Compute Merkle tree root hash for efficient verification.

        The Merkle root enables:
        - Quick integrity check without reading all files
        - Efficient partial verification
        - Parallel verification of chunks

        Returns:
            Hex-encoded Merkle root hash

        Example:
            >>> checksums = BundleChecksums()
            >>> checksums.add_file("a.txt", b"content a")
            >>> checksums.add_file("b.txt", b"content b")
            >>> root = checksums.compute_merkle_root()
            >>> len(root)
            64
        """
        if not self.files:
            self.merkle_root = hashlib.sha256(b"").hexdigest()
            return self.merkle_root

        # Get sorted hashes for deterministic ordering
        hashes = sorted([cs.hash for cs in self.files.values()])

        # Build Merkle tree bottom-up
        while len(hashes) > 1:
            # Pad to even length by duplicating last hash
            if len(hashes) % 2 == 1:
                hashes.append(hashes[-1])

            # Combine pairs
            hashes = [
                hashlib.sha256((h1 + h2).encode()).hexdigest()
                for h1, h2 in zip(hashes[::2], hashes[1::2], strict=False)
            ]

        self.merkle_root = hashes[0]
        return self.merkle_root

    def verify_merkle_root(self) -> bool:
        """Verify the stored Merkle root matches computed value.

        Returns:
            True if Merkle root is valid, False otherwise
        """
        if self.merkle_root is None:
            return False

        # Compute fresh and compare
        stored = self.merkle_root
        computed = self.compute_merkle_root()
        self.merkle_root = stored  # Restore original

        return stored == computed

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "algorithm": self.algorithm,
            "files": {path: cs.to_dict() for path, cs in self.files.items()},
            "merkle_root": self.merkle_root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create from dictionary."""
        files = {
            path: FileChecksum.from_dict(cs_data) for path, cs_data in data.get("files", {}).items()
        }
        return cls(
            algorithm=data.get("algorithm", DEFAULT_HASH_ALGORITHM),
            files=files,
            merkle_root=data.get("merkle_root"),
        )


# =============================================================================
# Export Options
# =============================================================================


@dataclass
class TenantExportOptions:
    """Options for tenant data export to .nexus bundle.

    Attributes:
        output_path: Path where the .nexus bundle will be written
        include_content: Include actual file content (CAS blobs)
        include_permissions: Include ReBAC permission tuples
        include_embeddings: Include vector embeddings (parquet format)
        include_api_keys: Include API keys (encrypted)
        include_deleted: Include soft-deleted files
        include_versions: Include version history
        path_prefix: Only export paths starting with this prefix
        after_time: Only export files modified after this time
        before_time: Only export files modified before this time
        compression_level: Gzip compression level (1-9, higher = smaller)
        max_concurrent_reads: Maximum concurrent file reads
        encryption_key: Key for encrypting sensitive data (API keys)
    """

    output_path: Path

    # Content selection
    include_content: bool = True
    include_permissions: bool = True
    include_embeddings: bool = False
    include_api_keys: bool = False
    include_deleted: bool = False
    include_versions: bool = True

    # Filtering
    path_prefix: str | None = None
    after_time: datetime | None = None
    before_time: datetime | None = None

    # Performance
    compression_level: int = DEFAULT_COMPRESSION_LEVEL
    max_concurrent_reads: int = 10

    # Security
    encryption_key: bytes | None = None

    def __post_init__(self) -> None:
        """Validate options after initialization."""
        if isinstance(self.output_path, str):
            self.output_path = Path(self.output_path)

        if not 1 <= self.compression_level <= 9:
            raise ValueError(f"compression_level must be 1-9, got {self.compression_level}")

        if self.max_concurrent_reads < 1:
            raise ValueError(f"max_concurrent_reads must be >= 1, got {self.max_concurrent_reads}")

        if self.include_api_keys and self.encryption_key is None:
            raise ValueError("encryption_key required when include_api_keys is True")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization (excludes sensitive data)."""
        return {
            "output_path": str(self.output_path),
            "include_content": self.include_content,
            "include_permissions": self.include_permissions,
            "include_embeddings": self.include_embeddings,
            "include_api_keys": self.include_api_keys,
            "include_deleted": self.include_deleted,
            "include_versions": self.include_versions,
            "path_prefix": self.path_prefix,
            "after_time": self.after_time.isoformat() if self.after_time else None,
            "before_time": self.before_time.isoformat() if self.before_time else None,
            "compression_level": self.compression_level,
            "max_concurrent_reads": self.max_concurrent_reads,
            # Note: encryption_key intentionally excluded for security
        }


# =============================================================================
# Import Options
# =============================================================================


@dataclass
class TenantImportOptions:
    """Options for importing tenant data from .nexus bundle.

    Attributes:
        bundle_path: Path to the .nexus bundle file
        target_tenant_id: Remap to different tenant (None = preserve original)
        path_prefix_remap: Path prefix remapping (e.g., {"/old/": "/new/"})
        user_id_remap: User ID remapping (e.g., {"old-user": "new-user"})
        conflict_mode: How to handle path collisions
        preserve_timestamps: Keep original created/modified timestamps
        preserve_ids: Preserve original UUIDs (False = generate new)
        dry_run: Preview changes without applying
        content_mode: How to handle content blobs
        import_permissions: Import ReBAC permissions
        import_api_keys: Import API keys (requires decryption_key)
        decryption_key: Key for decrypting sensitive data
        batch_size: Number of records to process per batch
        max_concurrent_writes: Maximum concurrent write operations
    """

    bundle_path: Path

    # Remapping
    target_tenant_id: str | None = None
    path_prefix_remap: dict[str, str] = field(default_factory=dict)
    user_id_remap: dict[str, str] = field(default_factory=dict)

    # Conflict resolution
    conflict_mode: ConflictMode = ConflictMode.SKIP

    # Preservation options
    preserve_timestamps: bool = True
    preserve_ids: bool = False  # Generate new UUIDs by default

    # Mode
    dry_run: bool = False

    # Content handling
    content_mode: ContentMode = ContentMode.INCLUDE

    # Permissions
    import_permissions: bool = True
    import_api_keys: bool = False

    # Security
    decryption_key: bytes | None = None

    # Performance
    batch_size: int = 1000
    max_concurrent_writes: int = 10

    def __post_init__(self) -> None:
        """Validate options after initialization."""
        if isinstance(self.bundle_path, str):
            self.bundle_path = Path(self.bundle_path)

        if isinstance(self.conflict_mode, str):
            self.conflict_mode = ConflictMode(self.conflict_mode)

        if isinstance(self.content_mode, str):
            self.content_mode = ContentMode(self.content_mode)

        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {self.batch_size}")

        if self.max_concurrent_writes < 1:
            raise ValueError(
                f"max_concurrent_writes must be >= 1, got {self.max_concurrent_writes}"
            )

        if self.import_api_keys and self.decryption_key is None:
            raise ValueError("decryption_key required when import_api_keys is True")

    def remap_path(self, path: str) -> str:
        """Apply path prefix remapping to a path.

        Args:
            path: Original path

        Returns:
            Remapped path (or original if no mapping applies)
        """
        for old_prefix, new_prefix in self.path_prefix_remap.items():
            if path.startswith(old_prefix):
                return new_prefix + path[len(old_prefix) :]
        return path

    def remap_user(self, user_id: str) -> str:
        """Apply user ID remapping.

        Args:
            user_id: Original user ID

        Returns:
            Remapped user ID (or original if no mapping exists)
        """
        return self.user_id_remap.get(user_id, user_id)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization (excludes sensitive data)."""
        return {
            "bundle_path": str(self.bundle_path),
            "target_tenant_id": self.target_tenant_id,
            "path_prefix_remap": self.path_prefix_remap,
            "user_id_remap": self.user_id_remap,
            "conflict_mode": self.conflict_mode.value,
            "preserve_timestamps": self.preserve_timestamps,
            "preserve_ids": self.preserve_ids,
            "dry_run": self.dry_run,
            "content_mode": self.content_mode.value,
            "import_permissions": self.import_permissions,
            "import_api_keys": self.import_api_keys,
            "batch_size": self.batch_size,
            "max_concurrent_writes": self.max_concurrent_writes,
            # Note: decryption_key intentionally excluded for security
        }


# =============================================================================
# Export Manifest
# =============================================================================


@dataclass
class ExportManifest:
    """Manifest for .nexus export bundle.

    The manifest is the first file read from a bundle and contains
    all metadata needed to validate and import the bundle.

    Attributes:
        format_version: Bundle format version (semantic versioning)
        nexus_version: Version of Nexus that created this bundle
        bundle_id: Unique identifier for this bundle (UUIDv4)
        source_instance: URL or identifier of source Nexus instance
        source_tenant_id: Original tenant ID
        export_timestamp: When the export was created
        file_count: Number of file records in metadata
        total_size_bytes: Total size of all content
        content_blob_count: Number of unique content blobs
        permission_count: Number of permission tuples
        embedding_count: Number of embedding vectors
        checksums: Integrity verification data
        include_content: Whether content blobs are included
        include_permissions: Whether permissions are included
        include_embeddings: Whether embeddings are included
        include_deleted: Whether deleted files are included
        include_versions: Whether version history is included
        path_prefix_filter: Path prefix filter used (if any)
        after_time_filter: Time filter used (if any)
        encryption_method: Encryption method for sensitive data
        metadata: Additional extensible metadata
    """

    # Format versioning - MUST be first for compatibility checks
    format_version: str = BUNDLE_FORMAT_VERSION
    nexus_version: str = ""

    # Bundle identification
    bundle_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_instance: str = ""
    source_tenant_id: str = ""
    export_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Content statistics
    file_count: int = 0
    total_size_bytes: int = 0
    content_blob_count: int = 0
    permission_count: int = 0
    embedding_count: int = 0

    # Integrity verification
    checksums: BundleChecksums = field(default_factory=BundleChecksums)

    # Export options used
    include_content: bool = True
    include_permissions: bool = True
    include_embeddings: bool = False
    include_deleted: bool = False
    include_versions: bool = True
    path_prefix_filter: str | None = None
    after_time_filter: datetime | None = None

    # Encryption
    encryption_method: str | None = None  # e.g., "age-v1", "aes-256-gcm"
    encrypted_dek: str | None = None  # Base64-encoded encrypted data encryption key

    # Extensible metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Returns:
            Dictionary representation suitable for JSON encoding
        """
        return {
            # Schema reference for validation
            "$schema": "https://nexus.io/schemas/manifest-v1.json",
            # Format versioning
            "format_version": self.format_version,
            "nexus_version": self.nexus_version,
            # Bundle identification
            "bundle_id": self.bundle_id,
            "source_instance": self.source_instance,
            "source_tenant_id": self.source_tenant_id,
            "export_timestamp": self.export_timestamp.isoformat(),
            # Statistics
            "statistics": {
                "file_count": self.file_count,
                "total_size_bytes": self.total_size_bytes,
                "content_blob_count": self.content_blob_count,
                "permission_count": self.permission_count,
                "embedding_count": self.embedding_count,
            },
            # Options used
            "options": {
                "include_content": self.include_content,
                "include_permissions": self.include_permissions,
                "include_embeddings": self.include_embeddings,
                "include_deleted": self.include_deleted,
                "include_versions": self.include_versions,
                "path_prefix_filter": self.path_prefix_filter,
                "after_time_filter": (
                    self.after_time_filter.isoformat() if self.after_time_filter else None
                ),
            },
            # Integrity
            "checksums": self.checksums.to_dict(),
            # Encryption
            "encryption": {
                "method": self.encryption_method,
                "encrypted_dek": self.encrypted_dek,
            }
            if self.encryption_method
            else None,
            # Extensible metadata
            "metadata": self.metadata,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string.

        Args:
            indent: JSON indentation level

        Returns:
            JSON string representation
        """
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create ExportManifest from dictionary.

        Args:
            data: Dictionary (e.g., from JSON parsing)

        Returns:
            ExportManifest instance

        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Parse statistics
        stats = data.get("statistics", {})

        # Parse options
        options = data.get("options", {})

        # Parse encryption
        encryption = data.get("encryption") or {}

        # Parse timestamps
        export_ts = data.get("export_timestamp")
        if isinstance(export_ts, str):
            export_ts = datetime.fromisoformat(export_ts)
        elif export_ts is None:
            export_ts = datetime.now(UTC)

        after_time = options.get("after_time_filter")
        if isinstance(after_time, str):
            after_time = datetime.fromisoformat(after_time)

        # Parse checksums
        checksums_data = data.get("checksums", {})
        checksums = BundleChecksums.from_dict(checksums_data)

        return cls(
            format_version=data.get("format_version", BUNDLE_FORMAT_VERSION),
            nexus_version=data.get("nexus_version", ""),
            bundle_id=data.get("bundle_id", str(uuid.uuid4())),
            source_instance=data.get("source_instance", ""),
            source_tenant_id=data.get("source_tenant_id", ""),
            export_timestamp=export_ts,
            file_count=stats.get("file_count", 0),
            total_size_bytes=stats.get("total_size_bytes", 0),
            content_blob_count=stats.get("content_blob_count", 0),
            permission_count=stats.get("permission_count", 0),
            embedding_count=stats.get("embedding_count", 0),
            checksums=checksums,
            include_content=options.get("include_content", True),
            include_permissions=options.get("include_permissions", True),
            include_embeddings=options.get("include_embeddings", False),
            include_deleted=options.get("include_deleted", False),
            include_versions=options.get("include_versions", True),
            path_prefix_filter=options.get("path_prefix_filter"),
            after_time_filter=after_time,
            encryption_method=encryption.get("method"),
            encrypted_dek=encryption.get("encrypted_dek"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> Self:
        """Create ExportManifest from JSON string.

        Args:
            json_str: JSON string

        Returns:
            ExportManifest instance
        """
        return cls.from_dict(json.loads(json_str))

    def validate(self) -> list[str]:
        """Validate manifest integrity.

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[str] = []

        if not self.format_version:
            errors.append("format_version is required")

        if not self.bundle_id:
            errors.append("bundle_id is required")

        if not self.source_tenant_id:
            errors.append("source_tenant_id is required")

        if self.file_count < 0:
            errors.append("file_count cannot be negative")

        if self.total_size_bytes < 0:
            errors.append("total_size_bytes cannot be negative")

        return errors

    def validate_against_schema(self) -> list[str]:
        """Validate manifest against JSON Schema.

        Requires jsonschema package to be installed.

        Returns:
            List of validation errors (empty if valid)

        Raises:
            ImportError: If jsonschema package is not available
        """
        try:
            import jsonschema
        except builtins.ImportError as e:
            raise builtins.ImportError(
                "jsonschema package required for schema validation. "
                "Install with: pip install jsonschema"
            ) from e

        errors: list[str] = []

        # Load schema
        if not MANIFEST_SCHEMA_PATH.exists():
            errors.append(f"Schema file not found: {MANIFEST_SCHEMA_PATH}")
            return errors

        schema = json.loads(MANIFEST_SCHEMA_PATH.read_text())
        manifest_data = self.to_dict()

        # Validate against schema
        validator = jsonschema.Draft202012Validator(schema)
        for error in validator.iter_errors(manifest_data):
            path = ".".join(str(p) for p in error.absolute_path)
            if path:
                errors.append(f"{path}: {error.message}")
            else:
                errors.append(error.message)

        return errors

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """Get the JSON Schema for manifest validation.

        Returns:
            JSON Schema as dictionary

        Raises:
            FileNotFoundError: If schema file doesn't exist
        """
        if not MANIFEST_SCHEMA_PATH.exists():
            raise FileNotFoundError(f"Schema file not found: {MANIFEST_SCHEMA_PATH}")
        schema: dict[str, Any] = json.loads(MANIFEST_SCHEMA_PATH.read_text())
        return schema


# =============================================================================
# Import Result
# =============================================================================


@dataclass
class ImportError:
    """Details about a single import error.

    Attributes:
        path: File path that caused the error
        error_type: Category of error (e.g., "validation", "conflict", "io")
        message: Human-readable error description
        details: Additional error context
    """

    path: str
    error_type: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "path": self.path,
            "error_type": self.error_type,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class ImportResult:
    """Result of tenant import operation.

    Tracks all operations performed during import and provides
    summary statistics.

    Attributes:
        files_created: Number of new files created
        files_updated: Number of existing files updated
        files_skipped: Number of files skipped due to conflicts
        files_failed: Number of files that failed to import
        permissions_imported: Number of permissions imported
        permissions_skipped: Number of permissions skipped
        content_blobs_imported: Number of content blobs imported
        content_blobs_skipped: Number of content blobs already existing
        embeddings_imported: Number of embeddings imported
        tenant_remapped: Whether tenant ID was remapped
        paths_remapped: Number of paths remapped
        users_remapped: Number of user IDs remapped
        errors: List of errors encountered
        warnings: List of warnings
        started_at: Import start time
        completed_at: Import completion time
    """

    # File statistics
    files_created: int = 0
    files_updated: int = 0
    files_skipped: int = 0
    files_failed: int = 0

    # Permission statistics
    permissions_imported: int = 0
    permissions_skipped: int = 0

    # Content statistics
    content_blobs_imported: int = 0
    content_blobs_skipped: int = 0  # Already existed (deduplication)

    # Embedding statistics
    embeddings_imported: int = 0

    # Remapping statistics
    tenant_remapped: bool = False
    paths_remapped: int = 0
    users_remapped: int = 0

    # Errors and warnings
    errors: list[ImportError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Timing
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def total_files_processed(self) -> int:
        """Total number of files processed."""
        return self.files_created + self.files_updated + self.files_skipped + self.files_failed

    @property
    def duration_seconds(self) -> float:
        """Duration of import in seconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0

    @property
    def success(self) -> bool:
        """Whether import completed without fatal errors."""
        return len(self.errors) == 0

    def add_error(
        self,
        path: str,
        error_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Add an error to the result.

        Args:
            path: File path that caused the error
            error_type: Category of error
            message: Human-readable description
            details: Additional context
        """
        self.errors.append(
            ImportError(
                path=path,
                error_type=error_type,
                message=message,
                details=details or {},
            )
        )

    def add_warning(self, message: str) -> None:
        """Add a warning to the result.

        Args:
            message: Warning message
        """
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "files": {
                "created": self.files_created,
                "updated": self.files_updated,
                "skipped": self.files_skipped,
                "failed": self.files_failed,
                "total_processed": self.total_files_processed,
            },
            "permissions": {
                "imported": self.permissions_imported,
                "skipped": self.permissions_skipped,
            },
            "content": {
                "blobs_imported": self.content_blobs_imported,
                "blobs_skipped": self.content_blobs_skipped,
            },
            "embeddings": {
                "imported": self.embeddings_imported,
            },
            "remapping": {
                "tenant_remapped": self.tenant_remapped,
                "paths_remapped": self.paths_remapped,
                "users_remapped": self.users_remapped,
            },
            "errors": [e.to_dict() for e in self.errors],
            "warnings": self.warnings,
            "timing": {
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "completed_at": self.completed_at.isoformat() if self.completed_at else None,
                "duration_seconds": self.duration_seconds,
            },
            "success": self.success,
        }

    def __str__(self) -> str:
        """Human-readable summary."""
        status = "SUCCESS" if self.success else f"FAILED ({len(self.errors)} errors)"
        return (
            f"ImportResult({status}): "
            f"created={self.files_created}, updated={self.files_updated}, "
            f"skipped={self.files_skipped}, failed={self.files_failed}, "
            f"duration={self.duration_seconds:.2f}s"
        )


# =============================================================================
# File Record Models (for JSONL streaming)
# =============================================================================


@dataclass
class FileRecord:
    """File metadata record for JSONL export.

    Represents a single file's metadata in the metadata/files.jsonl stream.

    Attributes:
        path_id: Unique identifier (UUID)
        tenant_id: Tenant ID
        virtual_path: Virtual file path
        backend_id: Backend storage identifier
        physical_path: Physical storage path
        file_type: MIME type or file extension
        size_bytes: File size
        content_hash: SHA-256 hash of content
        created_at: Creation timestamp
        updated_at: Last modification timestamp
        deleted_at: Soft deletion timestamp (if deleted)
        current_version: Version number
        posix_uid: POSIX owner ID
        metadata: Additional file metadata
    """

    path_id: str
    tenant_id: str
    virtual_path: str
    backend_id: str
    physical_path: str
    file_type: str | None = None
    size_bytes: int = 0
    content_hash: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    current_version: int = 1
    posix_uid: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSONL serialization."""
        return {
            "path_id": self.path_id,
            "tenant_id": self.tenant_id,
            "virtual_path": self.virtual_path,
            "backend_id": self.backend_id,
            "physical_path": self.physical_path,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "current_version": self.current_version,
            "posix_uid": self.posix_uid,
            "metadata": self.metadata,
        }

    def to_jsonl(self) -> str:
        """Convert to JSONL line (no trailing newline)."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create from dictionary."""

        def parse_dt(val: str | None) -> datetime | None:
            if val is None:
                return None
            return datetime.fromisoformat(val)

        return cls(
            path_id=data["path_id"],
            tenant_id=data["tenant_id"],
            virtual_path=data["virtual_path"],
            backend_id=data["backend_id"],
            physical_path=data["physical_path"],
            file_type=data.get("file_type"),
            size_bytes=data.get("size_bytes", 0),
            content_hash=data.get("content_hash"),
            created_at=parse_dt(data.get("created_at")),
            updated_at=parse_dt(data.get("updated_at")),
            deleted_at=parse_dt(data.get("deleted_at")),
            current_version=data.get("current_version", 1),
            posix_uid=data.get("posix_uid"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_jsonl(cls, line: str) -> Self:
        """Create from JSONL line."""
        return cls.from_dict(json.loads(line))


@dataclass
class PermissionRecord:
    """Permission tuple record for JSONL export.

    Represents a single ReBAC permission tuple.

    Attributes:
        object_type: Type of object (e.g., "file", "directory")
        object_id: Object identifier
        relation: Permission relation (e.g., "owner", "viewer", "editor")
        subject_type: Type of subject (e.g., "user", "group")
        subject_id: Subject identifier
        created_at: When permission was granted
    """

    object_type: str
    object_id: str
    relation: str
    subject_type: str
    subject_id: str
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSONL serialization."""
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "relation": self.relation,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_jsonl(self) -> str:
        """Convert to JSONL line."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create from dictionary."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        return cls(
            object_type=data["object_type"],
            object_id=data["object_id"],
            relation=data["relation"],
            subject_type=data["subject_type"],
            subject_id=data["subject_id"],
            created_at=created_at,
        )

    @classmethod
    def from_jsonl(cls, line: str) -> Self:
        """Create from JSONL line."""
        return cls.from_dict(json.loads(line))


# =============================================================================
# Bundle Path Constants
# =============================================================================

BUNDLE_PATHS = {
    "manifest": "manifest.json",
    "files": "metadata/files.jsonl",
    "versions": "metadata/versions.jsonl",
    "operations": "metadata/operations.jsonl",
    "permissions": "permissions/rebac_tuples.jsonl",
    "api_keys": "permissions/api_keys.jsonl.enc",
    "embeddings": "embeddings/vectors.parquet",
    "content": "content/cas",
}
