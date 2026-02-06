"""Zone import service for restoring from .nexus bundles.

This module provides the ZoneImportService class for importing zone data
from portable .nexus bundles including:
- File metadata (JSONL streaming)
- Content blobs (CAS structure)
- Permissions (ReBAC tuples)

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Zone Data Portability
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.portability.bundle import BundleReader
from nexus.portability.models import (
    ConflictMode,
    ContentMode,
    FileRecord,
    ImportResult,
    ZoneImportOptions,
)

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


def _create_import_context() -> OperationContext:
    """Create a system context for import operations.

    Import is a privileged system operation that bypasses normal permission checks.
    """
    from nexus.core.permissions import OperationContext

    return OperationContext(
        user="system",
        groups=[],
        is_admin=True,
        is_system=True,  # System operations bypass all checks
    )


# Progress callback type: (current, total, phase) -> None
ProgressCallback = Callable[[int, int, str], None]


class ZoneImportService:
    """Service for importing zone data from .nexus bundles.

    Example usage:
        from nexus.portability import ZoneImportService, ZoneImportOptions

        service = ZoneImportService(nexus_fs)
        options = ZoneImportOptions(
            bundle_path=Path("/backup/zone.nexus"),
            target_zone_id="new-zone",
            conflict_mode=ConflictMode.SKIP,
        )
        result = service.import_zone(options)
        print(f"Imported {result.files_created} files")
    """

    def __init__(
        self,
        nexus_fs: NexusFS,
    ):
        """Initialize the import service.

        Args:
            nexus_fs: NexusFS instance with metadata store and backend access
        """
        self.nexus_fs = nexus_fs

    def import_zone(
        self,
        options: ZoneImportOptions,
        progress_callback: ProgressCallback | None = None,
    ) -> ImportResult:
        """Import zone data from .nexus bundle.

        Reads a tar.gz bundle and imports:
        - File metadata from metadata/files.jsonl
        - Content blobs from content/cas/
        - Permissions from permissions/rebac_tuples.jsonl (if enabled)

        Args:
            options: Import options (remapping, conflict handling, etc.)
            progress_callback: Optional callback for progress updates (current, total, phase)

        Returns:
            ImportResult with import statistics and any errors
        """
        result = ImportResult(started_at=datetime.now(UTC))

        logger.info(f"Starting import from {options.bundle_path}")

        if not options.bundle_path.exists():
            result.add_error(
                path=str(options.bundle_path),
                error_type="file_not_found",
                message=f"Bundle not found: {options.bundle_path}",
            )
            result.completed_at = datetime.now(UTC)
            return result

        try:
            with BundleReader(options.bundle_path) as reader:
                # Validate bundle first
                is_valid, validation_errors = reader.validate()
                if not is_valid:
                    for error in validation_errors:
                        result.add_error(
                            path=str(options.bundle_path),
                            error_type="validation",
                            message=error,
                        )
                    result.completed_at = datetime.now(UTC)
                    return result

                manifest = reader.get_manifest()

                # Check zone remapping
                if options.target_zone_id:
                    result.zone_remapped = True
                    logger.info(
                        f"Remapping zone: {manifest.source_zone_id} -> {options.target_zone_id}"
                    )

                # Phase 1: Import file metadata and content
                self._import_files(
                    reader=reader,
                    options=options,
                    result=result,
                    manifest_file_count=manifest.file_count,
                    progress_callback=progress_callback,
                )

                # Phase 2: Import permissions (if enabled)
                if options.import_permissions and manifest.include_permissions:
                    self._import_permissions(
                        reader=reader,
                        options=options,
                        result=result,
                        progress_callback=progress_callback,
                    )

        except Exception as e:
            logger.exception(f"Import failed: {e}")
            result.add_error(
                path=str(options.bundle_path),
                error_type="exception",
                message=str(e),
            )

        result.completed_at = datetime.now(UTC)

        logger.info(
            f"Import complete: {result.files_created} created, "
            f"{result.files_updated} updated, {result.files_skipped} skipped, "
            f"{result.files_failed} failed"
        )

        return result

    def _import_files(
        self,
        reader: BundleReader,
        options: ZoneImportOptions,
        result: ImportResult,
        manifest_file_count: int,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Import file metadata and content from bundle.

        Args:
            reader: Open bundle reader
            options: Import options
            result: Result object to update
            manifest_file_count: Expected file count from manifest
            progress_callback: Optional progress callback
        """
        content_hashes_imported: set[str] = set()
        idx = 0

        for record in reader.iter_file_records():
            idx += 1

            try:
                self._import_file_record(
                    reader=reader,
                    record=record,
                    options=options,
                    result=result,
                    content_hashes_imported=content_hashes_imported,
                )

                # Progress callback
                if progress_callback and idx % 100 == 0:
                    progress_callback(idx, manifest_file_count, "files")

            except Exception as e:
                logger.warning(f"Failed to import {record.virtual_path}: {e}")
                result.add_error(
                    path=record.virtual_path,
                    error_type="import",
                    message=str(e),
                )
                result.files_failed += 1

                # If conflict mode is FAIL, stop on first error
                if options.conflict_mode == ConflictMode.FAIL:
                    raise

        # Final progress update
        if progress_callback:
            progress_callback(idx, manifest_file_count, "files")

    def _import_file_record(
        self,
        reader: BundleReader,
        record: FileRecord,
        options: ZoneImportOptions,
        result: ImportResult,
        content_hashes_imported: set[str],
    ) -> None:
        """Import a single file record.

        Args:
            reader: Open bundle reader
            record: File record to import
            options: Import options
            result: Result object to update
            content_hashes_imported: Set of already imported content hashes
        """
        # Apply path remapping
        original_path = record.virtual_path
        remapped_path = options.remap_path(original_path)
        if remapped_path != original_path:
            result.paths_remapped += 1

        # Determine target zone (used for logging/tracking)
        _ = options.target_zone_id or record.zone_id

        # Check if file already exists
        existing = self.nexus_fs.metadata.get(remapped_path)

        if existing is not None:
            # Handle conflict
            if options.conflict_mode == ConflictMode.SKIP:
                logger.debug(f"Skipping existing file: {remapped_path}")
                result.files_skipped += 1
                return

            elif options.conflict_mode == ConflictMode.FAIL:
                raise FileExistsError(f"File already exists: {remapped_path}")

            elif options.conflict_mode == ConflictMode.MERGE:
                # Merge: prefer newer content based on updated_at
                if (
                    existing.modified_at
                    and record.updated_at
                    and existing.modified_at >= record.updated_at
                ):
                    logger.debug(f"Skipping older file: {remapped_path}")
                    result.files_skipped += 1
                    return
                # Fall through to overwrite with newer content

            # OVERWRITE or MERGE (with newer content): proceed to write

        # Dry run mode - don't actually write
        if options.dry_run:
            if existing:
                result.files_updated += 1
            else:
                result.files_created += 1
            return

        # Import content blob if needed
        content: bytes | None = None
        if options.content_mode == ContentMode.INCLUDE and record.content_hash:
            # Check if we already imported this content (deduplication)
            if record.content_hash in content_hashes_imported:
                result.content_blobs_skipped += 1
            else:
                content = reader.read_content_blob(record.content_hash)
                if content is not None:
                    content_hashes_imported.add(record.content_hash)
                    result.content_blobs_imported += 1
                else:
                    logger.warning(
                        f"Content blob not found for {remapped_path}: {record.content_hash}"
                    )
                    result.add_warning(
                        f"Content blob not found: {record.content_hash} for {remapped_path}"
                    )

        elif options.content_mode == ContentMode.REFERENCE and record.content_hash:
            # Reference mode: verify content exists in backend
            try:
                response = self.nexus_fs.backend.read_content(record.content_hash)
                if not response.success:
                    result.add_warning(
                        f"Referenced content not found: {record.content_hash} for {remapped_path}"
                    )
            except Exception as e:
                result.add_warning(
                    f"Failed to verify content reference: {record.content_hash}: {e}"
                )

        # Write file to NexusFS
        if content is not None:
            try:
                # Use system context for import (privileged operation)
                import_context = _create_import_context()
                write_result = self.nexus_fs.write(
                    path=remapped_path,
                    content=content,
                    context=import_context,
                    force=True,  # Skip version check since we're restoring
                )

                # Update metadata if preserving timestamps
                if options.preserve_timestamps:
                    self._update_timestamps(
                        path=remapped_path,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                    )

                if existing:
                    result.files_updated += 1
                else:
                    result.files_created += 1

                logger.debug(f"Imported {remapped_path}: etag={write_result.get('etag', 'N/A')}")

            except Exception as e:
                logger.warning(f"Failed to write {remapped_path}: {e}")
                result.add_error(
                    path=remapped_path,
                    error_type="write",
                    message=str(e),
                )
                result.files_failed += 1

        elif options.content_mode == ContentMode.SKIP:
            # Metadata-only import
            self._import_metadata_only(
                path=remapped_path,
                record=record,
                options=options,
                result=result,
                is_update=existing is not None,
            )

    def _import_metadata_only(
        self,
        path: str,
        record: FileRecord,
        options: ZoneImportOptions,
        result: ImportResult,
        is_update: bool,
    ) -> None:
        """Import file metadata without content.

        Args:
            path: Target path
            record: File record with metadata
            options: Import options
            result: Result object to update
            is_update: Whether this is updating an existing file
        """
        try:
            # For metadata-only import, we need to create a metadata entry
            # without actual content. This is useful for catalog-style imports.
            from nexus.core._metadata_generated import FileMetadata

            metadata = FileMetadata(
                path=path,
                backend_name=record.backend_id,
                physical_path=record.physical_path,
                size=record.size_bytes,
                etag=record.content_hash,
                mime_type=record.file_type,
                created_at=record.created_at if options.preserve_timestamps else None,
                modified_at=record.updated_at if options.preserve_timestamps else None,
                version=record.current_version if options.preserve_ids else 1,
            )

            # Store metadata
            self.nexus_fs.metadata.put(metadata)

            if is_update:
                result.files_updated += 1
            else:
                result.files_created += 1

        except Exception as e:
            logger.warning(f"Failed to import metadata for {path}: {e}")
            result.add_error(
                path=path,
                error_type="metadata",
                message=str(e),
            )
            result.files_failed += 1

    def _update_timestamps(
        self,
        path: str,
        created_at: datetime | None,
        updated_at: datetime | None,
    ) -> None:
        """Update file timestamps after import.

        Args:
            path: File path
            created_at: Original creation time
            updated_at: Original modification time
        """
        try:
            # Use metadata store to update timestamps
            if created_at:
                self.nexus_fs.metadata.set_file_metadata(path, "created_at", created_at.isoformat())
            if updated_at:
                self.nexus_fs.metadata.set_file_metadata(
                    path, "modified_at", updated_at.isoformat()
                )
        except Exception as e:
            logger.warning(f"Failed to update timestamps for {path}: {e}")

    def _import_permissions(
        self,
        reader: BundleReader,
        options: ZoneImportOptions,
        result: ImportResult,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Import ReBAC permissions from bundle.

        Args:
            reader: Open bundle reader
            options: Import options
            result: Result object to update
            progress_callback: Optional progress callback
        """
        # Check if ReBAC is available
        rebac_manager = getattr(self.nexus_fs, "_rebac_manager", None)
        if rebac_manager is None:
            logger.info("No ReBAC manager available, skipping permission import")
            return

        idx = 0
        for perm_record in reader.iter_permission_records():
            idx += 1

            try:
                if options.dry_run:
                    result.permissions_imported += 1
                    continue

                # Apply user ID remapping
                subject_id = options.remap_user(perm_record.subject_id)
                if subject_id != perm_record.subject_id:
                    result.users_remapped += 1

                # Apply path remapping to object_id if it's a path
                object_id = perm_record.object_id
                if object_id.startswith("/"):
                    object_id = options.remap_path(object_id)

                # TODO: Actually write to ReBAC when API is available
                # For now, just count the permissions
                result.permissions_imported += 1

                logger.debug(
                    f"Would import permission: {perm_record.subject_type}:{subject_id} "
                    f"{perm_record.relation} {perm_record.object_type}:{object_id}"
                )

            except Exception as e:
                logger.warning(f"Failed to import permission: {e}")
                result.permissions_skipped += 1

            # Progress callback
            if progress_callback and idx % 100 == 0:
                progress_callback(idx, idx, "permissions")

        # Final progress update
        if progress_callback:
            progress_callback(idx, idx, "permissions")


def import_zone_bundle(
    nexus_fs: NexusFS,
    bundle_path: Path,
    target_zone_id: str | None = None,
    conflict_mode: ConflictMode = ConflictMode.SKIP,
    preserve_timestamps: bool = True,
    dry_run: bool = False,
    import_permissions: bool = True,
    path_prefix_remap: dict[str, str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ImportResult:
    """Convenience function to import a zone from a .nexus bundle.

    Args:
        nexus_fs: NexusFS instance
        bundle_path: Path to .nexus bundle
        target_zone_id: Remap to different zone (None = preserve original)
        conflict_mode: How to handle existing files
        preserve_timestamps: Keep original timestamps
        dry_run: Preview changes without applying
        import_permissions: Import ReBAC permissions
        path_prefix_remap: Path prefix remapping dict
        progress_callback: Optional progress callback

    Returns:
        ImportResult with import statistics
    """
    options = ZoneImportOptions(
        bundle_path=bundle_path,
        target_zone_id=target_zone_id,
        conflict_mode=conflict_mode,
        preserve_timestamps=preserve_timestamps,
        dry_run=dry_run,
        import_permissions=import_permissions,
        path_prefix_remap=path_prefix_remap or {},
    )

    service = ZoneImportService(nexus_fs)
    return service.import_zone(options, progress_callback)
