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

import hashlib
import json
import logging
import os
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.bricks.portability.models import (
    BUNDLE_PATHS,
    BundleChecksums,
    ExportManifest,
    FileRecord,
    PermissionRecord,
    ZoneExportOptions,
)
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes
from nexus.bricks.portability.strip import (
    DEFAULT_REDACT_PATTERNS,
    RegexStripper,
    SchemaStripper,
)

if TYPE_CHECKING:
    from nexus.contracts.portability_types import PortabilityFSProtocol

logger = logging.getLogger(__name__)

# Progress callback type: (current, total) -> None
ProgressCallback = Callable[[int, int], None]


def _sha256(data: bytes) -> str:
    """Return hex-encoded SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def _finalize_with_signature(
    bundle_dir: Path,
    manifest: ExportManifest,
    output_path: Path,
    *,
    signer: ArchiveSigner | None,
) -> None:
    """Write manifest.json (signed if signer is provided), then tar.gz the bundle.

    When `signer` is not None:
    - Embeds the signer's public key in the manifest dict.
    - Computes the canonical-JSON bytes of the manifest.
    - Signs ``manifest_bytes + merkle_root_b64`` with ed25519.
    - Writes ``signatures.json`` into the bundle directory.
    - The signature covers the canonical manifest bytes so that verifiers
      can reconstruct the payload without a separate merkle-root field.

    When `signer` is None the function is byte-identical to the legacy
    ``_create_bundle`` path — only the manifest is written and no
    ``signatures.json`` is emitted.
    """
    manifest_dict = manifest.to_dict()
    if signer is not None:
        manifest_dict["signer_pubkey_b64"] = signer.public_key_b64

    manifest_bytes = canonical_json_bytes(manifest_dict)
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)

    if signer is not None:
        merkle_root_b64 = (manifest_dict.get("checksums") or {}).get("merkle_root") or ""
        payload = manifest_bytes + merkle_root_b64.encode("utf-8")
        sig_b64, pub_b64 = signer.sign(payload)
        sig_doc = {
            "algorithm": "ed25519",
            "signer_pubkey_b64": pub_b64,
            "signature_b64": sig_b64,
            "manifest_sha256": _sha256(manifest_bytes),
        }
        (bundle_dir / "signatures.json").write_text(json.dumps(sig_doc, indent=2))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, mode="w:gz") as tar:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                arcname = str(path.relative_to(bundle_dir))
                tar.add(path, arcname=arcname)

    logger.info("Created bundle: %s (%d bytes)", output_path, output_path.stat().st_size)


class ZoneExportService:
    """Service for exporting zone data to .nexus bundles.

    Example usage:
        from nexus.bricks.portability import ZoneExportService, ZoneExportOptions

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
        nexus_fs: "PortabilityFSProtocol",
    ):
        """Initialize the export service.

        Args:
            nexus_fs: NexusFS-compatible instance with metadata store and backend access
        """
        self.nexus_fs = nexus_fs
        # ``nx.metadata`` was removed in W3b — reach the kernel directly so
        # ``list`` calls land on ``kernel.metastore_list``.
        self._kernel = nexus_fs._kernel
        # R20.18.x: `NexusFS.backend` is gone — the kernel owns mount
        # routing now. Content reads go through `nexus_fs.sys_read`;
        # see `_export_content_blobs`.

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

        logger.info("Starting export for zone %s to %s", zone_id, options.output_path)

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
            content_ids: set[str] = set()
            hash_to_path: dict[str, str] = {}
            files_path = temp_path / BUNDLE_PATHS["files"]

            file_count, total_size = self._export_metadata_to_jsonl(
                zone_id=zone_id,
                output_path=files_path,
                options=options,
                content_ids=content_ids,
                hash_to_path=hash_to_path,
                progress_callback=progress_callback,
            )

            manifest.file_count = file_count
            manifest.total_size_bytes = total_size

            # --- Credential stripping (v2+) ---
            # When strip_credentials=True, run schema+regex stripping over the
            # exported file records treated as a "documents" table, then update
            # files.jsonl in-place and stash any placeholders on the manifest.
            if options.strip_credentials and files_path.exists():
                raw_lines = files_path.read_text(encoding="utf-8").splitlines()
                file_rows = [json.loads(line) for line in raw_lines if line.strip()]
                stripped_tables, placeholders = _apply_credential_stripping(
                    {"documents": file_rows},
                    workspace_root=None,
                )
                stripped_rows = stripped_tables.get("documents", file_rows)
                files_path.write_text(
                    "\n".join(json.dumps(r) for r in stripped_rows)
                    + ("\n" if stripped_rows else ""),
                    encoding="utf-8",
                )
                if placeholders:
                    manifest.placeholders = list(placeholders)
                    logger.info(
                        "Credential stripping: %d placeholder(s) captured", len(placeholders)
                    )

            # Add checksum for files.jsonl
            if files_path.exists():
                checksums.add_file(BUNDLE_PATHS["files"], files_path.read_bytes())

            # Export content blobs if requested
            if options.include_content and content_ids:
                blob_count = self._export_content_blobs(
                    content_ids=content_ids,
                    hash_to_path=hash_to_path,
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

            # Instantiate signer if signing is enabled
            signer: ArchiveSigner | None = None
            if options.sign:
                key_path = (
                    options.signing_key_path or Path.home() / ".nexus" / "archive_signing_key"
                )
                signer = ArchiveSigner(key_path)

            # Write manifest (signed) and create tar.gz bundle
            _finalize_with_signature(
                bundle_dir=temp_path,
                manifest=manifest,
                output_path=options.output_path,
                signer=signer,
            )

            logger.info(
                "Export complete: %d files, %d blobs, %d bytes total",
                manifest.file_count,
                manifest.content_blob_count,
                manifest.total_size_bytes,
            )

            return manifest

    def _export_metadata_to_jsonl(
        self,
        zone_id: str,
        output_path: Path,
        options: ZoneExportOptions,
        content_ids: set[str],
        hash_to_path: dict[str, str],
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[int, int]:
        """Export file metadata to JSONL format.

        Args:
            zone_id: Zone to export
            output_path: Path for JSONL output
            options: Export options
            content_ids: Set to collect content hashes for blob export
            progress_callback: Optional progress callback

        Returns:
            Tuple of (file_count, total_size_bytes)
        """
        file_count = 0
        total_size = 0

        # Get all files from metadata store
        # Note: The actual implementation depends on the metadata store API
        prefix = options.path_prefix or ""
        all_files = list(self._kernel.metastore_list(prefix))

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
                # ``backend_id`` and ``physical_path`` were removed from
                # FileMetadata — the kernel now resolves the physical
                # location at read time via the mount/route layer, so
                # bundles only need to carry the virtual path. We keep
                # the FileRecord fields (the .nexus 1.0 schema requires
                # them) but emit empty strings.
                record = FileRecord(
                    path_id=getattr(file_meta, "path_id", str(idx)),
                    zone_id=zone_id,
                    virtual_path=file_meta.path,
                    backend_id="",
                    physical_path="",
                    file_type=file_meta.mime_type,
                    size_bytes=file_meta.size,
                    content_id=file_meta.content_id,
                    created_at=file_meta.created_at,
                    updated_at=file_meta.modified_at,
                    current_version=getattr(file_meta, "version", 1),
                    metadata=getattr(file_meta, "custom_metadata", None) or {},
                )

                # Write JSONL line
                f.write(record.to_jsonl() + "\n")

                # Collect content hash for blob export (CAS dedup): first
                # path wins. Later iterations that see the same hash
                # (identical content at a different path) are no-ops —
                # `_export_content_blobs` reads via sys_read(path) once
                # per unique hash.
                if file_meta.content_id and file_meta.content_id not in content_ids:
                    content_ids.add(file_meta.content_id)
                    hash_to_path[file_meta.content_id] = file_meta.path

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
        content_ids: set[str],
        hash_to_path: dict[str, str],
        output_dir: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> int:
        """Export content blobs to CAS directory structure.

        Reads each blob by path via `nexus_fs.sys_read` — the kernel
        resolves the correct mount + backend internally. The legacy
        `nexus_fs.backend.read_content(hash)` path was removed with
        `NexusFS.backend` in R20.18; post-migration the only way to
        reach a CAS blob is via a VFS path, so we carry one path per
        unique hash (first-seen wins — CAS dedup makes the choice
        arbitrary).
        """
        blob_count = 0
        total_hashes = len(content_ids)

        for idx, content_id in enumerate(content_ids):
            path = hash_to_path.get(content_id)
            if not path:
                logger.warning("No source path recorded for hash %s; skipping", content_id[:12])
                continue
            try:
                data = self.nexus_fs.sys_read(path)
                if data is None:
                    logger.warning(
                        "sys_read returned no data for %s (hash %s)", path, content_id[:12]
                    )
                    continue

                # Write to CAS structure (2-char prefix directories)
                if len(content_id) >= 2:
                    prefix = content_id[:2]
                    blob_dir = output_dir / prefix
                    blob_dir.mkdir(parents=True, exist_ok=True)
                    blob_path = blob_dir / content_id
                    blob_path.write_bytes(data if isinstance(data, bytes) else bytes(data))
                    blob_count += 1

                # Progress callback
                if progress_callback and idx % 50 == 0:
                    progress_callback(idx + 1, total_hashes)

            except Exception as e:
                logger.warning("Error exporting blob %s: %s", content_id, e)

        # Final progress update
        if progress_callback:
            progress_callback(total_hashes, total_hashes)

        return blob_count

    def _export_permissions(
        self,
        zone_id: str,
        output_path: Path,
    ) -> int:
        """Export ReBAC permission tuples to JSONL.

        Queries the ReBAC manager for all tuples in the zone and writes
        them as PermissionRecord JSONL lines.

        Args:
            zone_id: Zone ID to export permissions for
            output_path: Path for JSONL output

        Returns:
            Number of permissions exported
        """
        perm_count = 0

        # Get ReBAC manager if available
        rebac_manager = getattr(self.nexus_fs, "rebac_manager", None)
        if rebac_manager is None:
            logger.info("No ReBAC manager available, skipping permission export")
            return 0

        try:
            # Fetch all tuples for the zone from the database
            tuples = rebac_manager.get_zone_tuples(zone_id)

            with output_path.open("w", encoding="utf-8") as f:
                for t in tuples:
                    record = PermissionRecord(
                        object_type=t["object_type"],
                        object_id=t["object_id"],
                        relation=t["relation"],
                        subject_type=t["subject_type"],
                        subject_id=t["subject_id"],
                    )
                    f.write(record.to_jsonl() + "\n")
                    perm_count += 1

            logger.info("Exported %d permission tuples for zone %s", perm_count, zone_id)

        except Exception as e:
            logger.warning("Error exporting permissions: %s", e)

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

        # Honor the caller's chosen suffix. Bundle format is tar.gz
        # regardless of filename; `.nexus` is just the conventional
        # extension but not enforced. Previous version force-rewrote
        # the path to `.nexus`, which silently broke callers that
        # passed a different suffix (e.g., E2E tests using `.tar`).

        # Create tar.gz bundle
        # Note: tarfile "w:gz" uses default gzip compression level
        with tarfile.open(output_path, mode="w:gz") as tar:
            # Add all files from source directory
            for item in source_dir.rglob("*"):
                if item.is_file():
                    arcname = item.relative_to(source_dir)
                    tar.add(item, arcname=str(arcname))

        logger.info("Created bundle: %s (%d bytes)", output_path, output_path.stat().st_size)


def _apply_credential_stripping(
    rows_by_table: dict[str, list[dict[str, Any]]],
    *,
    workspace_root: str | None,
    extra_patterns: list[dict[str, str]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list]:
    """Run schema + regex strip across every row group.

    Layer 1 (SchemaStripper): nulls known sensitive columns by table + field
    name and replaces them with ``${PLACEHOLDER_NAME}`` strings.

    Layer 2 (RegexStripper): scans every string value for known credential
    patterns (API keys, PATs, etc.) as a backstop.

    Args:
        rows_by_table: Mapping of table name to list of row dicts.
        workspace_root: Workspace root path for path-normalisation rules in
            SchemaStripper (pass ``None`` to skip).
        extra_patterns: Additional regex patterns to append to
            ``DEFAULT_REDACT_PATTERNS`` (each dict has ``name`` + ``pattern``).

    Returns:
        A tuple of ``(stripped_rows_by_table, placeholders_list)`` where
        *placeholders_list* is a flat list of :class:`PlaceholderRef` objects
        collected across all tables.
    """
    schema = SchemaStripper(workspace_root=workspace_root)
    patterns = list(DEFAULT_REDACT_PATTERNS) + list(extra_patterns or [])
    regex = RegexStripper(patterns)

    out: dict[str, list[dict[str, Any]]] = {}
    placeholders: list = []

    for table, rows in rows_by_table.items():
        schema_result = schema.strip_table(table, rows)
        cleaned: list[dict[str, Any]] = []
        for i, row in enumerate(schema_result.rows):
            new_row = dict(row)
            for k, v in row.items():
                if isinstance(v, str):
                    scan = regex.scan(v, location=f"{table}:row={i}:field={k}")
                    new_row[k] = scan.text
            cleaned.append(new_row)
        out[table] = cleaned
        placeholders.extend(schema_result.placeholders)

    return out, placeholders


# Convenience function for CLI usage
def export_zone_bundle(
    nexus_fs: "PortabilityFSProtocol",
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
