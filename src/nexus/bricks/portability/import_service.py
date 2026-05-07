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

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.bricks.archive.errors import ArchiveEmbeddingDimMismatch, ArchiveTargetNotEmpty
from nexus.bricks.portability.bundle import BundleReader
from nexus.bricks.portability.models import (
    ConflictMode,
    ContentMode,
    FileRecord,
    ImportResult,
    PermissionRecord,
    ZoneImportOptions,
)
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.contracts.portability_types import PortabilityFSProtocol
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}", re.IGNORECASE)


def _scan_for_placeholders(rows: list[dict]) -> set[str]:
    """Return the set of ``${NAME}`` placeholder names found across all string values.

    Args:
        rows: List of row dicts to scan.

    Returns:
        Set of placeholder names (e.g. ``{"PROVIDER_KEY_anthropic"}``).
    """
    found: set[str] = set()
    for row in rows:
        for v in row.values():
            if isinstance(v, str):
                for m in _PLACEHOLDER_RE.finditer(v):
                    found.add(m.group(1))
    return found


def _apply_injections(rows: list[dict], injections: dict[str, str]) -> list[dict]:
    """Substitute every ``${NAME}`` placeholder for its injected value.

    Placeholders whose names are not present in *injections* are left as-is
    (so a subsequent :func:`_scan_for_placeholders` call will still find them).

    Args:
        rows: List of row dicts to process.
        injections: Mapping of placeholder name → replacement string.

    Returns:
        New list of row dicts with substitutions applied.
    """
    if not injections:
        return rows

    def _sub(m: re.Match[str]) -> str:
        return injections.get(m.group(1), m.group(0))

    out: list[dict] = []
    for row in rows:
        new_row = dict(row)
        for k, v in row.items():
            if isinstance(v, str):
                new_row[k] = _PLACEHOLDER_RE.sub(_sub, v)
        out.append(new_row)
    return out


def _check_target_empty(*, existing_zones: list[str], force: bool) -> None:
    """Raise ArchiveTargetNotEmpty if the restore target has existing zones.

    Args:
        existing_zones: Zone IDs already present in the target nexus instance.
        force: When ``True`` the operator has acknowledged the risk; the check
               is skipped and the restore proceeds (DESTRUCTIVE).

    Raises:
        ArchiveTargetNotEmpty: When *existing_zones* is non-empty and
            *force* is ``False``.
    """
    if existing_zones and not force:
        raise ArchiveTargetNotEmpty(existing_zones)


def _check_embedding_compat(
    *,
    archive_model: str | None,
    archive_dim: int | None,
    current_model: str,
    current_dim: int,
    rebuild_embeddings: bool,
) -> None:
    """Raise ArchiveEmbeddingDimMismatch if archive embeddings are incompatible.

    Returns None on compatibility (or if ``rebuild_embeddings`` overrides), or
    if the archive carries no embedding metadata (v1 bundles).

    Args:
        archive_model: Embedding model name stored in the bundle manifest.
                       ``None`` for v1 bundles that pre-date embedding metadata.
        archive_dim: Embedding vector dimension stored in the bundle manifest.
                     ``None`` for v1 bundles.
        current_model: Active embedding model in the running Nexus instance.
        current_dim: Active embedding dimension in the running Nexus instance.
        rebuild_embeddings: When ``True`` the caller intends to re-embed all
                            documents after restore, so a mismatch is safe and
                            the check is skipped.

    Raises:
        ArchiveEmbeddingDimMismatch: When the archive model or dimension differs
            from the current configuration and ``rebuild_embeddings`` is False.
    """
    if archive_model is None or archive_dim is None:
        # v1 bundle — no embedding metadata to check.
        return
    if rebuild_embeddings:
        return
    if archive_model == current_model and archive_dim == current_dim:
        return
    raise ArchiveEmbeddingDimMismatch(
        archive_model=archive_model,
        archive_dim=archive_dim,
        current_model=current_model,
        current_dim=current_dim,
    )


def _create_import_context(zone_id: str | None = None) -> "OperationContext":
    """Create a system context for import operations.

    Import is a privileged system operation that bypasses normal permission checks.
    """
    from nexus.contracts.types import OperationContext

    return OperationContext(
        user_id="system",
        groups=[],
        is_admin=True,
        is_system=True,  # System operations bypass all checks
        zone_id=zone_id,
    )


# Progress callback type: (current, total, phase) -> None
ProgressCallback = Callable[[int, int, str], None]


class ZoneImportService:
    """Service for importing zone data from .nexus bundles.

    Example usage:
        from nexus.bricks.portability import ZoneImportService, ZoneImportOptions

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
        nexus_fs: "PortabilityFSProtocol",
        *,
        file_metadata_class: type[Any] | None = None,
    ):
        """Initialize the import service.

        Args:
            nexus_fs: NexusFS-compatible instance with metadata store and backend access
            file_metadata_class: FileMetadata class for metadata-only imports (DI).
                                 If None, metadata-only imports are skipped.
        """
        self.nexus_fs = nexus_fs
        self._file_metadata_class = file_metadata_class

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

        logger.info("Starting import from %s", options.bundle_path)

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

                # --- Pre-flight guards (v2+) ---

                # Guard 1: target-not-empty check.
                # Discover zones already present in the target nexus instance.
                # ``list_zones`` was a Raft-only method that commit V dropped;
                # the Python boundary returns the empty list now, which means
                # the target-empty check passes unconditionally. Will need to
                # be reinstated once the kernel exposes the zone registry.
                existing_zones: list[str] = []
                _check_target_empty(existing_zones=existing_zones, force=options.force)

                # Guard 2: embedding compatibility.
                current_model = getattr(
                    getattr(self.nexus_fs, "config", None), "embedding_model", "unknown"
                )
                current_dim = getattr(getattr(self.nexus_fs, "config", None), "embedding_dim", 0)
                _check_embedding_compat(
                    archive_model=manifest.embedding_model,
                    archive_dim=manifest.embedding_dim,
                    current_model=current_model,
                    current_dim=current_dim,
                    rebuild_embeddings=options.rebuild_embeddings,
                )

                # Guard 3: placeholder injection.
                # If the bundle's manifest declares placeholders (strip_credentials=True
                # was used during export), load ALL file records, apply any caller-
                # supplied injections, then check that no placeholders remain.
                if manifest.placeholders and options.require_no_placeholders:
                    all_rows = [
                        dict(rec.__dict__) if hasattr(rec, "__dict__") else vars(rec)
                        for rec in reader.iter_file_records()
                    ]
                    all_rows = _apply_injections(all_rows, options.injections)
                    remaining = _scan_for_placeholders(all_rows)
                    if remaining:
                        from nexus.bricks.archive.errors import ArchivePlaceholderNotInjected

                        raise ArchivePlaceholderNotInjected(sorted(remaining))

                # Check zone remapping
                if options.target_zone_id:
                    result.zone_remapped = True
                    logger.info(
                        "Remapping zone: %s -> %s",
                        manifest.source_zone_id,
                        options.target_zone_id,
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
            logger.exception("Import failed: %s", e)
            result.add_error(
                path=str(options.bundle_path),
                error_type="exception",
                message=str(e),
            )

        result.completed_at = datetime.now(UTC)

        logger.info(
            "Import complete: %d created, %d updated, %d skipped, %d failed",
            result.files_created,
            result.files_updated,
            result.files_skipped,
            result.files_failed,
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
                logger.warning("Failed to import %s: %s", record.virtual_path, e)
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

        # Determine target zone for write context
        target_zone_id = options.target_zone_id or record.zone_id

        # Check if file already exists
        existing = self.nexus_fs._kernel.sys_stat(remapped_path, ROOT_ZONE_ID)

        if existing is not None:
            # Handle conflict
            if options.conflict_mode == ConflictMode.SKIP:
                logger.debug("Skipping existing file: %s", remapped_path)
                result.files_skipped += 1
                return

            elif options.conflict_mode == ConflictMode.FAIL:
                raise FileExistsError(f"File already exists: {remapped_path}")

            elif options.conflict_mode == ConflictMode.MERGE:
                # Merge: prefer newer content based on modified_at
                existing_modified = existing.get("modified_at")
                if (
                    existing_modified
                    and record.updated_at
                    and existing_modified >= record.updated_at.isoformat()
                ):
                    logger.debug("Skipping older file: %s", remapped_path)
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
        content_ref_valid = False  # True when content_id exists in CAS/backend
        if options.content_mode == ContentMode.INCLUDE and record.content_id:
            # Check if we already imported this content (deduplication)
            if record.content_id in content_hashes_imported:
                result.content_blobs_skipped += 1
                content_ref_valid = True  # Already in CAS from a prior file
            else:
                content = reader.read_content_blob(record.content_id)
                if content is not None:
                    content_hashes_imported.add(record.content_id)
                    result.content_blobs_imported += 1
                else:
                    logger.warning(
                        "Content blob not found for %s: %s", remapped_path, record.content_id
                    )
                    result.add_warning(
                        f"Content blob not found: {record.content_id} for {remapped_path}"
                    )

        elif options.content_mode == ContentMode.REFERENCE and record.content_id:
            # Reference mode: verify content exists by reading through the
            # VFS path. R20.18.x removed direct backend access on NexusFS;
            # the kernel owns routing, so we re-read via sys_read and
            # compare hashes if available. A sys_read success is proof
            # enough that the content exists in *some* local backend.
            try:
                data = self.nexus_fs.sys_read(remapped_path)
                if data is None:
                    result.add_warning(
                        f"Referenced content not found: {record.content_id} for {remapped_path}"
                    )
                else:
                    content_ref_valid = True
            except Exception as e:
                result.add_warning(f"Failed to verify content reference: {record.content_id}: {e}")

        # Write file to NexusFS
        if content is not None:
            try:
                # Use system context for import (privileged operation)
                import_context = _create_import_context(zone_id=target_zone_id)
                # force=True (skip OCC) → just call write() directly.
                # Issue #1323: OCC extracted to lib/occ.py, write() is now OCC-free.
                write_result = self.nexus_fs.write(
                    path=remapped_path,
                    buf=content,
                    context=import_context,
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

                logger.debug(
                    "Imported %s: content_id=%s",
                    remapped_path,
                    write_result.get("content_id", "N/A"),
                )

            except Exception as e:
                logger.warning("Failed to write %s: %s", remapped_path, e)
                result.add_error(
                    path=remapped_path,
                    error_type="write",
                    message=str(e),
                )
                result.files_failed += 1

        elif content_ref_valid or options.content_mode == ContentMode.SKIP:
            # Content already in CAS (dedup) / verified in backend (reference) /
            # metadata-only import (SKIP) — create metadata entry pointing to
            # the existing content_id.
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
            if self._file_metadata_class is None:
                logger.warning("No file_metadata_class injected, skipping metadata-only import")
                result.files_skipped += 1
                return

            _created_at = record.created_at if options.preserve_timestamps else None
            _modified_at = record.updated_at if options.preserve_timestamps else None

            # Store metadata via sys_setattr DT_REG upsert
            self.nexus_fs._kernel.sys_setattr(
                path,
                0,  # DT_REG upsert
                content_id=record.content_id,
                size=record.size_bytes,
                mime_type=record.file_type,
                version=record.current_version if options.preserve_ids else 1,
                zone_id=ROOT_ZONE_ID,
                created_at_ms=int(_created_at.timestamp() * 1000) if _created_at else None,
                modified_at_ms=int(_modified_at.timestamp() * 1000) if _modified_at else None,
            )

            if is_update:
                result.files_updated += 1
            else:
                result.files_created += 1

        except Exception as e:
            logger.warning("Failed to import metadata for %s: %s", path, e)
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
        # created_at / modified_at live on the inode (FileMetadata.created_at_ms /
        # modified_at_ms) — no xattr write needed. sys_write already sets
        # modified_at_ms; callers that need to override timestamps should use
        # sys_setattr(modified_at_ms=...) instead.
        pass

    @staticmethod
    def validate_permission_graph(
        records: list[PermissionRecord],
    ) -> list[str]:
        """Validate permission graph integrity before import.

        Checks for:
        - Empty or missing required fields
        - Invalid object/subject types
        - Self-referential tuples (subject == object with same relation)

        Args:
            records: List of permission records to validate

        Returns:
            List of validation error messages (empty if valid)
        """
        errors: list[str] = []
        valid_types = {"file", "directory", "user", "group", "zone", "agent", "memory"}
        seen: set[tuple[str, str, str, str, str]] = set()

        for i, rec in enumerate(records):
            # Check required fields
            if not rec.subject_type or not rec.subject_id:
                errors.append(f"Record {i}: missing subject_type or subject_id")
            if not rec.object_type or not rec.object_id:
                errors.append(f"Record {i}: missing object_type or object_id")
            if not rec.relation:
                errors.append(f"Record {i}: missing relation")

            # Check known types (warn, don't fail — extensible)
            if rec.subject_type and rec.subject_type not in valid_types:
                errors.append(f"Record {i}: unknown subject_type '{rec.subject_type}'")
            if rec.object_type and rec.object_type not in valid_types:
                errors.append(f"Record {i}: unknown object_type '{rec.object_type}'")

            # Check for self-referential tuples
            if rec.subject_type == rec.object_type and rec.subject_id == rec.object_id:
                errors.append(
                    f"Record {i}: self-referential tuple "
                    f"({rec.subject_type}:{rec.subject_id} {rec.relation})"
                )

            # Check for duplicates
            key = (
                rec.subject_type,
                rec.subject_id,
                rec.relation,
                rec.object_type,
                rec.object_id,
            )
            if key in seen:
                errors.append(
                    f"Record {i}: duplicate tuple "
                    f"({rec.subject_type}:{rec.subject_id} "
                    f"{rec.relation} {rec.object_type}:{rec.object_id})"
                )
            seen.add(key)

        return errors

    def _import_permissions(
        self,
        reader: BundleReader,
        options: ZoneImportOptions,
        result: ImportResult,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Import ReBAC permissions from bundle.

        Validates graph integrity first, then writes tuples to ReBAC.

        Args:
            reader: Open bundle reader
            options: Import options
            result: Result object to update
            progress_callback: Optional progress callback
        """
        # Check if ReBAC is available
        rebac_manager = getattr(self.nexus_fs, "rebac_manager", None)
        if rebac_manager is None:
            logger.info("No ReBAC manager available, skipping permission import")
            return

        # Collect all records for validation
        records = list(reader.iter_permission_records())

        # Validate graph integrity
        validation_errors = self.validate_permission_graph(records)
        if validation_errors:
            for err in validation_errors:
                result.add_warning(f"Permission validation: {err}")
            logger.warning("Permission graph validation found %d issues", len(validation_errors))

        idx = 0
        for perm_record in records:
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

                # Determine target zone
                target_zone = options.target_zone_id or ROOT_ZONE_ID

                # Write tuple to ReBAC
                rebac_manager.rebac_write(
                    subject=(perm_record.subject_type, subject_id),
                    relation=perm_record.relation,
                    object=(perm_record.object_type, object_id),
                    zone_id=target_zone,
                )
                result.permissions_imported += 1

                logger.debug(
                    "Imported permission: %s:%s %s %s:%s",
                    perm_record.subject_type,
                    subject_id,
                    perm_record.relation,
                    perm_record.object_type,
                    object_id,
                )

            except Exception as e:
                logger.warning("Failed to import permission: %s", e)
                result.permissions_skipped += 1

            # Progress callback
            if progress_callback and idx % 100 == 0:
                progress_callback(idx, idx, "permissions")

        # Final progress update
        if progress_callback:
            progress_callback(idx, idx, "permissions")


def import_zone_bundle(
    nexus_fs: "PortabilityFSProtocol",
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
