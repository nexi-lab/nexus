"""Zone export service for creating .nexus bundles.

This module provides the ZoneExportService class for exporting zone data
to portable .nexus bundles including:
- File metadata (JSONL streaming)
- Content blobs (CAS structure)
- Permissions (ReBAC tuples)
- Embeddings (Parquet format)

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Zone Data Portability
"""

from __future__ import annotations

import logging
import os
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.portability.models import (
    BUNDLE_PATHS,
    BundleChecksums,
    ExportManifest,
    FileRecord,
    ZoneExportOptions,
)

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core._metadata_generated import MetadataStore
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# Progress callback type: (current, total) -> None
ProgressCallback = Callable[[int, int], None]


class ZoneExportService:
    """Service for exporting zone data to .nexus bundles.

    Example usage:
        from nexus.portability import ZoneExportService, ZoneExportOptions

        service = ZoneExportService(nexus_fs)
        options = ZoneExportOptions(
            output_path=Path("/backup/zone.nexus"),
            include_content=True,
            include_permissions=True,
        )
        manifest = await service.export_zone("zone-123", options)
        print(f"Exported {manifest.file_count} files")
    """

    def __init__(
        self,
        nexus_fs: NexusFS,
    ):
        """Initialize the export service.

        Args:
            nexus_fs: NexusFS instance with metadata store and backend access
        """
        self.nexus_fs = nexus_fs
        self.metadata_store: MetadataStore = nexus_fs.metadata
        self.backend: Backend = nexus_fs.backend

    def export_zone(
        self,
        zone_id: str,
        options: ZoneExportOptions,
        progress_callback: ProgressCallback | None = None,
    ) -> ExportManifest:
        """Export zone data to .nexus bundle.

        Creates a tar.gz bundle with the following structure:
        - manifest.json: Bundle metadata and checksums
        - metadata/files.jsonl: File metadata records
        - metadata/versions.jsonl: Version history (if include_versions)
        - permissions/rebac_tuples.jsonl: Permission relationships
        - content/cas/: Content-addressable blobs

        Args:
            zone_id: Zone ID to export
            options: Export options (filters, content selection, etc.)
            progress_callback: Optional callback for progress updates (current, total)

        Returns:
            ExportManifest with export statistics and checksums
        """
        import nexus

        logger.info(f"Starting export for zone {zone_id} to {options.output_path}")

        # Create temporary directory for building bundle
        with tempfile.TemporaryDirectory(prefix="nexus_export_") as temp_dir:
            temp_path = Path(temp_dir)

            # Initialize manifest
            manifest = ExportManifest(
                nexus_version=nexus.__version__,
                source_instance=os.environ.get("NEXUS_INSTANCE_ID", "local"),
                source_zone_id=zone_id,
                export_timestamp=datetime.now(UTC),
                include_content=options.include_content,
                include_permissions=options.include_permissions,
                include_embeddings=options.include_embeddings,
                include_deleted=options.include_deleted,
                include_versions=options.include_versions,
                path_prefix_filter=options.path_prefix,
                after_time_filter=options.after_time,
            )

            checksums = BundleChecksums()

            # Create directory structure
            (temp_path / "metadata").mkdir(parents=True)
            (temp_path / "permissions").mkdir(parents=True)
            if options.include_content:
                (temp_path / "content" / "cas").mkdir(parents=True)
            if options.include_embeddings:
                (temp_path / "embeddings").mkdir(parents=True)

            # Export metadata to JSONL
            content_hashes: set[str] = set()
            files_path = temp_path / BUNDLE_PATHS["files"]

            file_count, total_size = self._export_metadata_to_jsonl(
                zone_id=zone_id,
                output_path=files_path,
                options=options,
                content_hashes=content_hashes,
                progress_callback=progress_callback,
            )

            manifest.file_count = file_count
            manifest.total_size_bytes = total_size

            # Add checksum for files.jsonl
            if files_path.exists():
                checksums.add_file(BUNDLE_PATHS["files"], files_path.read_bytes())

            # Export content blobs if requested
            if options.include_content and content_hashes:
                blob_count = self._export_content_blobs(
                    content_hashes=content_hashes,
                    output_dir=temp_path / "content" / "cas",
                    progress_callback=progress_callback,
                )
                manifest.content_blob_count = blob_count

            # Export permissions if requested
            if options.include_permissions:
                perms_path = temp_path / BUNDLE_PATHS["permissions"]
                perm_count = self._export_permissions(
                    zone_id=zone_id,
                    output_path=perms_path,
                )
                manifest.permission_count = perm_count
                if perms_path.exists():
                    checksums.add_file(BUNDLE_PATHS["permissions"], perms_path.read_bytes())

            # Finalize manifest
            manifest.checksums = checksums

            # Write manifest
            manifest_path = temp_path / BUNDLE_PATHS["manifest"]
            manifest_path.write_text(manifest.to_json())

            # Create tar.gz bundle
            self._create_bundle(
                source_dir=temp_path,
                output_path=options.output_path,
                compression_level=options.compression_level,
            )

            logger.info(
                f"Export complete: {manifest.file_count} files, "
                f"{manifest.content_blob_count} blobs, "
                f"{manifest.total_size_bytes} bytes total"
            )

            return manifest

    def _export_metadata_to_jsonl(
        self,
        zone_id: str,
        output_path: Path,
        options: ZoneExportOptions,
        content_hashes: set[str],
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[int, int]:
        """Export file metadata to JSONL format.

        Args:
            zone_id: Zone to export
            output_path: Path for JSONL output
            options: Export options
            content_hashes: Set to collect content hashes for blob export
            progress_callback: Optional progress callback

        Returns:
            Tuple of (file_count, total_size_bytes)
        """
        file_count = 0
        total_size = 0

        # Get all files from metadata store
        # Note: The actual implementation depends on the metadata store API
        prefix = options.path_prefix or ""
        all_files = list(self.metadata_store.list(prefix))

        # Apply zone filter if metadata store doesn't do it
        # (In a real implementation, this would be done at the database level)

        total_files = len(all_files)

        with output_path.open("w", encoding="utf-8") as f:
            for idx, file_meta in enumerate(all_files):
                # Apply time filters
                if (
                    options.after_time
                    and file_meta.modified_at
                    and file_meta.modified_at < options.after_time
                ):
                    continue

                if (
                    options.before_time
                    and file_meta.modified_at
                    and file_meta.modified_at > options.before_time
                ):
                    continue

                # Build FileRecord
                record = FileRecord(
                    path_id=getattr(file_meta, "path_id", str(idx)),
                    zone_id=zone_id,
                    virtual_path=file_meta.path,
                    backend_id=file_meta.backend_name,
                    physical_path=file_meta.physical_path,
                    file_type=file_meta.mime_type,
                    size_bytes=file_meta.size,
                    content_hash=file_meta.etag,
                    created_at=file_meta.created_at,
                    updated_at=file_meta.modified_at,
                    current_version=getattr(file_meta, "version", 1),
                    metadata=getattr(file_meta, "custom_metadata", None) or {},
                )

                # Write JSONL line
                f.write(record.to_jsonl() + "\n")

                # Collect content hash for blob export
                if file_meta.etag:
                    content_hashes.add(file_meta.etag)

                file_count += 1
                total_size += file_meta.size

                # Progress callback
                if progress_callback and idx % 100 == 0:
                    progress_callback(idx + 1, total_files)

        # Final progress update
        if progress_callback:
            progress_callback(total_files, total_files)

        return file_count, total_size

    def _export_content_blobs(
        self,
        content_hashes: set[str],
        output_dir: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> int:
        """Export content blobs to CAS directory structure.

        Args:
            content_hashes: Set of content hashes to export
            output_dir: Output directory for CAS structure
            progress_callback: Optional progress callback

        Returns:
            Number of blobs exported
        """
        blob_count = 0
        total_hashes = len(content_hashes)

        for idx, content_hash in enumerate(content_hashes):
            try:
                # Read content from backend
                response = self.backend.read_content(content_hash)
                if not response.success or response.data is None:
                    logger.warning(f"Failed to read content {content_hash}: {response.error}")
                    continue

                # Write to CAS structure (2-char prefix directories)
                if len(content_hash) >= 2:
                    prefix = content_hash[:2]
                    blob_dir = output_dir / prefix
                    blob_dir.mkdir(parents=True, exist_ok=True)
                    blob_path = blob_dir / content_hash
                    blob_path.write_bytes(response.data)
                    blob_count += 1

                # Progress callback
                if progress_callback and idx % 50 == 0:
                    progress_callback(idx + 1, total_hashes)

            except Exception as e:
                logger.warning(f"Error exporting blob {content_hash}: {e}")

        # Final progress update
        if progress_callback:
            progress_callback(total_hashes, total_hashes)

        return blob_count

    def _export_permissions(
        self,
        zone_id: str,  # noqa: ARG002
        output_path: Path,
    ) -> int:
        """Export ReBAC permission tuples to JSONL.

        Args:
            zone_id: Zone ID to export permissions for
            output_path: Path for JSONL output

        Returns:
            Number of permissions exported
        """
        perm_count = 0

        # Get ReBAC manager if available
        rebac_manager = getattr(self.nexus_fs, "_rebac_manager", None)
        if rebac_manager is None:
            logger.info("No ReBAC manager available, skipping permission export")
            return 0

        try:
            # Export permissions - implementation depends on ReBAC manager API
            # TODO: Query ReBAC for all tuples related to this zone
            # For now, create an empty permissions file
            with output_path.open("w", encoding="utf-8"):
                # Placeholder - real implementation would iterate ReBAC tuples
                logger.info(
                    f"Permission export: ReBAC tuple export not yet implemented "
                    f"(rebac_manager={type(rebac_manager).__name__})"
                )

        except Exception as e:
            logger.warning(f"Error exporting permissions: {e}")

        return perm_count

    def _create_bundle(
        self,
        source_dir: Path,
        output_path: Path,
        compression_level: int = 6,  # noqa: ARG002
    ) -> None:
        """Create tar.gz bundle from source directory.

        Args:
            source_dir: Directory containing bundle contents
            output_path: Output path for .nexus bundle
            compression_level: Gzip compression level (1-9) - reserved for future use
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Ensure .nexus extension
        if not str(output_path).endswith(".nexus"):
            output_path = output_path.with_suffix(".nexus")

        # Create tar.gz bundle
        # Note: tarfile "w:gz" uses default compression; custom level support TODO
        with tarfile.open(output_path, mode="w:gz") as tar:
            # Add all files from source directory
            for item in source_dir.rglob("*"):
                if item.is_file():
                    arcname = item.relative_to(source_dir)
                    tar.add(item, arcname=str(arcname))

        logger.info(f"Created bundle: {output_path} ({output_path.stat().st_size} bytes)")


# Convenience function for CLI usage
def export_zone_bundle(
    nexus_fs: NexusFS,
    zone_id: str,
    output_path: Path,
    include_content: bool = True,
    include_permissions: bool = True,
    include_embeddings: bool = False,
    path_prefix: str | None = None,
    compression_level: int = 6,
    progress_callback: ProgressCallback | None = None,
) -> ExportManifest:
    """Convenience function to export a zone to a .nexus bundle.

    Args:
        nexus_fs: NexusFS instance
        zone_id: Zone ID to export
        output_path: Output path for bundle
        include_content: Include content blobs
        include_permissions: Include permissions
        include_embeddings: Include embeddings
        path_prefix: Optional path prefix filter
        compression_level: Compression level (1-9)
        progress_callback: Optional progress callback

    Returns:
        ExportManifest with export statistics
    """
    options = ZoneExportOptions(
        output_path=output_path,
        include_content=include_content,
        include_permissions=include_permissions,
        include_embeddings=include_embeddings,
        path_prefix=path_prefix,
        compression_level=compression_level,
    )

    service = ZoneExportService(nexus_fs)
    return service.export_zone(zone_id, options, progress_callback)
