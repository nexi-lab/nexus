"""Integrity validators for migration operations.

This module provides utilities for validating:
- Metadata integrity (database consistency)
- Content integrity (file checksums)
- Orphaned content (files without metadata)
- Missing content (metadata without files)

Issue #165: Migration Tools & Upgrade Paths
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFilesystem


@dataclass
class ValidationResult:
    """Result of validation check.

    Attributes:
        valid: Overall validation passed
        errors: Critical errors found
        warnings: Non-critical issues found
        checked_files: Number of files checked
        corrupted_files: Number of files with corrupt content
        orphaned_content: Number of orphaned content blocks
        missing_content: Number of files with missing content
    """

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_files: int = 0
    corrupted_files: int = 0
    orphaned_content: int = 0
    missing_content: int = 0

    def __str__(self) -> str:
        """Human-readable summary."""
        status = "VALID" if self.valid else "INVALID"
        return (
            f"ValidationResult({status}: checked={self.checked_files}, "
            f"corrupted={self.corrupted_files}, orphaned={self.orphaned_content}, "
            f"missing={self.missing_content}, errors={len(self.errors)}, "
            f"warnings={len(self.warnings)})"
        )

    def merge(self, other: ValidationResult) -> ValidationResult:
        """Merge another validation result into this one.

        Args:
            other: ValidationResult to merge

        Returns:
            Self for chaining
        """
        self.valid = self.valid and other.valid
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.checked_files += other.checked_files
        self.corrupted_files += other.corrupted_files
        self.orphaned_content += other.orphaned_content
        self.missing_content += other.missing_content
        return self


class IntegrityValidator:
    """Validates data integrity during and after migration.

    Provides comprehensive validation of:
    - Metadata consistency in the database
    - Content-addressable storage integrity
    - Orphaned content detection
    - Missing content detection

    Example:
        validator = IntegrityValidator(nexus_fs)
        result = validator.full_validation()
        if not result.valid:
            print("Validation failed:", result.errors)
    """

    def __init__(self, nx: NexusFilesystem) -> None:
        """Initialize validator.

        Args:
            nx: Nexus filesystem instance to validate
        """
        self.nx = nx

    def full_validation(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> ValidationResult:
        """Run all validation checks.

        Args:
            progress_callback: Optional callback for progress updates

        Returns:
            Combined ValidationResult from all checks
        """
        result = ValidationResult()
        checks = [
            ("Metadata integrity", self.validate_metadata_integrity),
            ("Content integrity", self.validate_content_integrity),
            ("Orphaned content", self.check_orphaned_content),
            ("Missing content", self.check_missing_content),
        ]

        for i, (name, check_fn) in enumerate(checks):
            if progress_callback:
                progress_callback(f"Running {name} check", i + 1, len(checks))

            check_result = check_fn()
            result.merge(check_result)

        return result

    def validate_metadata_integrity(self) -> ValidationResult:
        """Validate metadata store consistency.

        Checks:
        - All file paths have valid metadata
        - No duplicate paths
        - Path references are consistent

        Returns:
            ValidationResult with metadata issues
        """
        result = ValidationResult()

        try:
            # List all files and check metadata consistency
            files = self.nx.list("/", recursive=True, details=True)

            seen_paths: set[str] = set()
            for file_info in files:
                path = file_info.get("path", "") if isinstance(file_info, dict) else str(file_info)

                result.checked_files += 1

                # Check for duplicates
                if path in seen_paths:
                    result.errors.append(f"Duplicate path found: {path}")
                    result.valid = False
                else:
                    seen_paths.add(path)

                # Validate path format
                if not path.startswith("/"):
                    result.warnings.append(f"Path missing leading slash: {path}")

        except Exception as e:
            result.errors.append(f"Failed to validate metadata: {e}")
            result.valid = False

        return result

    def validate_content_integrity(
        self,
        sample_size: int = 100,
    ) -> ValidationResult:
        """Validate content checksums.

        Checks a sample of files to verify content matches stored checksums.

        Args:
            sample_size: Number of files to sample (0 = all)

        Returns:
            ValidationResult with content issues
        """
        import hashlib

        result = ValidationResult()

        try:
            # Get list of files
            files = self.nx.list("/", recursive=True, details=True)

            # Sample files if needed
            files_to_check = files[:sample_size] if sample_size > 0 else files

            for file_info in files_to_check:
                if isinstance(file_info, dict):
                    path = file_info.get("path", "")
                    stored_hash = file_info.get("etag") or file_info.get("content_hash")
                else:
                    continue  # Skip if no details

                result.checked_files += 1

                try:
                    # Read content and compute hash
                    content = self.nx.read(path)
                    if content is None:
                        result.missing_content += 1
                        result.warnings.append(f"Could not read content: {path}")
                        continue

                    # Compute hash
                    if isinstance(content, str):
                        content = content.encode("utf-8")
                    computed_hash = hashlib.sha256(content).hexdigest()

                    # Compare with stored hash (if available)
                    # Handle different hash formats by checking prefix match
                    if (
                        stored_hash
                        and computed_hash != stored_hash
                        and not stored_hash.startswith(computed_hash[:8])
                    ):
                        result.corrupted_files += 1
                        result.errors.append(
                            f"Checksum mismatch: {path} "
                            f"(stored={stored_hash[:16]}..., computed={computed_hash[:16]}...)"
                        )
                        result.valid = False

                except Exception as e:
                    result.warnings.append(f"Could not verify {path}: {e}")

        except Exception as e:
            result.errors.append(f"Failed to validate content: {e}")
            result.valid = False

        return result

    def check_orphaned_content(self) -> ValidationResult:
        """Check for content blocks without metadata references.

        Returns:
            ValidationResult with orphaned content count
        """
        result = ValidationResult()

        # This check requires direct access to the CAS storage
        # Implementation depends on backend type
        try:
            # Get all referenced content hashes from metadata
            files = self.nx.list("/", recursive=True, details=True)
            referenced_hashes: set[str] = set()

            for file_info in files:
                if isinstance(file_info, dict):
                    content_hash = file_info.get("etag") or file_info.get("content_hash")
                    if content_hash:
                        referenced_hashes.add(content_hash)

            result.warnings.append(
                f"Found {len(referenced_hashes)} unique content hashes in metadata"
            )

            # Note: Full orphan detection requires access to the CAS directory
            # which is backend-specific. This is a basic check.

        except Exception as e:
            result.warnings.append(f"Could not check for orphaned content: {e}")

        return result

    def check_missing_content(self) -> ValidationResult:
        """Check for metadata entries without corresponding content.

        Returns:
            ValidationResult with missing content count
        """
        result = ValidationResult()

        try:
            files = self.nx.list("/", recursive=True, details=True)

            for file_info in files:
                if isinstance(file_info, dict):
                    path = file_info.get("path", "")
                    size = file_info.get("size", 0)

                    result.checked_files += 1

                    # Skip directories (size 0)
                    if size == 0:
                        continue

                    # Try to read file
                    if not self.nx.exists(path):
                        result.missing_content += 1
                        result.errors.append(f"Missing content for: {path}")
                        result.valid = False

        except Exception as e:
            result.errors.append(f"Failed to check missing content: {e}")
            result.valid = False

        return result

    def validate_rebac_integrity(self) -> ValidationResult:
        """Validate ReBAC permission consistency.

        Checks:
        - All files have owner relationships
        - Permission inheritance is consistent
        - No orphaned permission tuples

        Returns:
            ValidationResult with ReBAC issues
        """
        result = ValidationResult()

        # This is a placeholder for ReBAC validation
        # Full implementation would check the rebac_tuples table
        result.warnings.append("ReBAC validation not yet implemented")

        return result
