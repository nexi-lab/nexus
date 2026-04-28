"""Metadata export/import service (Issue #841).

Extracted from NexusFS kernel — provides JSONL export/import of file metadata
for backup, migration, and disaster recovery.

Wired into ServiceRegistry via factory function.
Receives MetastoreABC via dependency injection.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.lib.export_import import (
    CollisionDetail,
    ExportFilter,
    ImportOptions,
    ImportResult,
)
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)


class MetadataExportService:
    """Handles JSONL metadata export/import operations.

    Dependencies (injected via constructor):
        metadata: MetastoreABC for reading/writing file metadata.
        created_by: Static string for tracking who performed imports.
    """

    def __init__(
        self,
        metadata: Any,
        created_by: str | None = None,
    ) -> None:
        self._metadata: Any = metadata
        self._created_by = created_by

    @rpc_expose(description="Export metadata to JSONL file")
    def export_metadata(
        self,
        output_path: str | Path,
        filter: ExportFilter | None = None,
        prefix: str = "",
    ) -> int:
        """Export metadata to JSONL file for backup and migration.

        Args:
            output_path: Path to output JSONL file
            filter: Export filter options
            prefix: (Deprecated) Path prefix filter for backward compatibility

        Returns:
            Number of files exported
        """
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        if filter is None:
            filter = ExportFilter(path_prefix=prefix)
        elif prefix:
            filter.path_prefix = prefix

        from nexus.contracts.constants import SYSTEM_PATH_PREFIX

        all_files = [
            m
            for m in self._metadata.list_iter(filter.path_prefix)
            if not m.path.startswith(SYSTEM_PATH_PREFIX)
        ]

        filtered_files = []
        for file_meta in all_files:
            if filter.after_time and file_meta.modified_at:
                file_time = file_meta.modified_at
                filter_time = filter.after_time
                if file_time.tzinfo is None:
                    file_time = file_time.replace(tzinfo=UTC)
                if filter_time.tzinfo is None:
                    filter_time = filter_time.replace(tzinfo=UTC)

                if file_time < filter_time:
                    continue

            filtered_files.append(file_meta)

        filtered_files.sort(key=lambda m: m.path)

        count = 0
        with output_file.open("w", encoding="utf-8") as f:
            for file_meta in filtered_files:
                # ``backend_name``/``physical_path`` were removed from
                # FileMetadata — the kernel resolves the physical
                # location at read time via the mount/route layer, so
                # exported metadata no longer surfaces them.
                metadata_dict: dict[str, Any] = {
                    "path": file_meta.path,
                    "size": file_meta.size,
                    "content_id": file_meta.content_id,
                    "mime_type": file_meta.mime_type,
                    "created_at": (
                        file_meta.created_at.isoformat() if file_meta.created_at else None
                    ),
                    "modified_at": (
                        file_meta.modified_at.isoformat() if file_meta.modified_at else None
                    ),
                    "version": file_meta.version,
                }

                try:
                    if file_meta.custom_metadata:
                        metadata_dict["custom_metadata"] = dict(file_meta.custom_metadata)
                except (AttributeError, TypeError):
                    pass

                f.write(json.dumps(metadata_dict) + "\n")
                count += 1

        return count

    @rpc_expose(description="Import metadata from JSONL file")
    def import_metadata(
        self,
        input_path: str | Path,
        options: ImportOptions | None = None,
        overwrite: bool = False,
        skip_existing: bool = True,
    ) -> ImportResult:
        """Import metadata from JSONL file.

        Args:
            input_path: Path to input JSONL file
            options: Import options (conflict mode, dry-run, preserve IDs)
            overwrite: (Deprecated) If True, overwrite existing (backward compat)
            skip_existing: (Deprecated) If True, skip existing (backward compat)

        Returns:
            ImportResult with counts and collision details
        """
        input_file = Path(input_path)
        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        if options is None:
            if overwrite:
                options = ImportOptions(conflict_mode="overwrite")
            elif skip_existing:
                options = ImportOptions(conflict_mode="skip")
            else:
                options = ImportOptions(conflict_mode="skip")

        result = ImportResult()

        with input_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    metadata_dict = json.loads(line)

                    required_fields = ["path", "backend_name", "physical_path", "size"]
                    for field in required_fields:
                        if field not in metadata_dict:
                            raise ValueError(f"Missing required field: {field}")

                    original_path = metadata_dict["path"]
                    path = original_path

                    created_at = None
                    if metadata_dict.get("created_at"):
                        created_at = datetime.fromisoformat(metadata_dict["created_at"])

                    modified_at = None
                    if metadata_dict.get("modified_at"):
                        modified_at = datetime.fromisoformat(metadata_dict["modified_at"])

                    existing = self._metadata.get(path)
                    imported_content_id = metadata_dict.get("content_id")

                    if existing:
                        self._handle_collision(
                            result,
                            options,
                            existing,
                            metadata_dict,
                            path,
                            original_path,
                            imported_content_id,
                            created_at,
                            modified_at,
                        )
                    else:
                        self._handle_new_file(
                            result,
                            options,
                            metadata_dict,
                            path,
                            imported_content_id,
                            created_at,
                            modified_at,
                        )

                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON at line {line_num}: {e}") from e
                except Exception as e:
                    raise ValueError(f"Error processing line {line_num}: {e}") from e

        return result

    def _handle_collision(
        self,
        result: ImportResult,
        options: ImportOptions,
        existing: Any,
        metadata_dict: dict[str, Any],
        path: str,
        original_path: str,
        imported_content_id: str | None,
        created_at: datetime | None,
        modified_at: datetime | None,
    ) -> None:
        """Handle import collision with existing file."""
        existing_content_id = existing.content_id
        is_same_content = existing_content_id == imported_content_id

        if is_same_content:
            if options.dry_run:
                result.updated += 1
                return

            file_meta = FileMetadata(
                path=path,
                size=metadata_dict["size"],
                content_id=imported_content_id,
                mime_type=metadata_dict.get("mime_type"),
                created_at=created_at or existing.created_at,
                modified_at=modified_at or existing.modified_at,
                version=metadata_dict.get("version", existing.version),
            )
            self._metadata.put(file_meta)
            self._import_custom_metadata(path, metadata_dict)
            result.updated += 1
            return

        if options.conflict_mode == "skip":
            result.skipped += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_content_id=existing_content_id,
                    imported_content_id=imported_content_id,
                    resolution="skip",
                    message="Skipped: existing file has different content",
                )
            )
        elif options.conflict_mode == "overwrite":
            self._handle_overwrite(
                result,
                options,
                existing,
                metadata_dict,
                path,
                existing_content_id,
                imported_content_id,
                created_at,
                modified_at,
            )
        elif options.conflict_mode == "remap":
            self._handle_remap(
                result,
                options,
                metadata_dict,
                path,
                original_path,
                existing_content_id,
                imported_content_id,
                created_at,
                modified_at,
            )
        elif options.conflict_mode == "auto":
            self._handle_auto(
                result,
                options,
                existing,
                metadata_dict,
                path,
                existing_content_id,
                imported_content_id,
                created_at,
                modified_at,
            )

    def _handle_overwrite(
        self,
        result: ImportResult,
        options: ImportOptions,
        existing: Any,
        metadata_dict: dict[str, Any],
        path: str,
        existing_content_id: str | None,
        imported_content_id: str | None,
        created_at: datetime | None,
        modified_at: datetime | None,
    ) -> None:
        if options.dry_run:
            result.updated += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_content_id=existing_content_id,
                    imported_content_id=imported_content_id,
                    resolution="overwrite",
                    message="Would overwrite with imported content",
                )
            )
            return

        file_meta = FileMetadata(
            path=path,
            size=metadata_dict["size"],
            content_id=imported_content_id,
            mime_type=metadata_dict.get("mime_type"),
            created_at=created_at or existing.created_at,
            modified_at=modified_at,
            version=metadata_dict.get("version", existing.version + 1),
        )
        self._metadata.put(file_meta)
        self._import_custom_metadata(path, metadata_dict)
        result.updated += 1
        result.collisions.append(
            CollisionDetail(
                path=path,
                existing_content_id=existing_content_id,
                imported_content_id=imported_content_id,
                resolution="overwrite",
                message="Overwrote with imported content",
            )
        )

    def _handle_remap(
        self,
        result: ImportResult,
        options: ImportOptions,
        metadata_dict: dict[str, Any],
        path: str,
        original_path: str,
        existing_content_id: str | None,
        imported_content_id: str | None,
        created_at: datetime | None,
        modified_at: datetime | None,
    ) -> None:
        import uuid as _uuid

        remapped_path = f"{path}_imported{_uuid.uuid4().hex[:8]}"

        if options.dry_run:
            result.remapped += 1
            result.collisions.append(
                CollisionDetail(
                    path=original_path,
                    existing_content_id=existing_content_id,
                    imported_content_id=imported_content_id,
                    resolution="remap",
                    message=f"Would remap to: {remapped_path}",
                )
            )
            return

        file_meta = FileMetadata(
            path=remapped_path,
            size=metadata_dict["size"],
            content_id=imported_content_id,
            mime_type=metadata_dict.get("mime_type"),
            created_at=created_at,
            modified_at=modified_at,
            version=metadata_dict.get("version", 1),
        )
        self._metadata.put(file_meta)
        self._import_custom_metadata(remapped_path, metadata_dict)
        result.remapped += 1
        result.collisions.append(
            CollisionDetail(
                path=original_path,
                existing_content_id=existing_content_id,
                imported_content_id=imported_content_id,
                resolution="remap",
                message=f"Remapped to: {remapped_path}",
            )
        )

    def _handle_auto(
        self,
        result: ImportResult,
        options: ImportOptions,
        existing: Any,
        metadata_dict: dict[str, Any],
        path: str,
        existing_content_id: str | None,
        imported_content_id: str | None,
        created_at: datetime | None,
        modified_at: datetime | None,
    ) -> None:
        existing_time = existing.modified_at or existing.created_at
        imported_time = modified_at or created_at

        if existing_time and existing_time.tzinfo is None:
            existing_time = existing_time.replace(tzinfo=UTC)
        if imported_time and imported_time.tzinfo is None:
            imported_time = imported_time.replace(tzinfo=UTC)

        if imported_time and existing_time and imported_time > existing_time:
            if options.dry_run:
                result.updated += 1
                result.collisions.append(
                    CollisionDetail(
                        path=path,
                        existing_content_id=existing_content_id,
                        imported_content_id=imported_content_id,
                        resolution="auto_overwrite",
                        message=f"Would overwrite: imported is newer ({imported_time} > {existing_time})",
                    )
                )
                return

            file_meta = FileMetadata(
                path=path,
                size=metadata_dict["size"],
                content_id=imported_content_id,
                mime_type=metadata_dict.get("mime_type"),
                created_at=created_at or existing.created_at,
                modified_at=modified_at,
                version=metadata_dict.get("version", existing.version + 1),
            )
            self._metadata.put(file_meta)
            self._import_custom_metadata(path, metadata_dict)
            result.updated += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_content_id=existing_content_id,
                    imported_content_id=imported_content_id,
                    resolution="auto_overwrite",
                    message=f"Overwrote: imported is newer ({imported_time} > {existing_time})",
                )
            )
        else:
            result.skipped += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_content_id=existing_content_id,
                    imported_content_id=imported_content_id,
                    resolution="auto_skip",
                    message="Skipped: existing is newer or equal",
                )
            )

    def _handle_new_file(
        self,
        result: ImportResult,
        options: ImportOptions,
        metadata_dict: dict[str, Any],
        path: str,
        imported_content_id: str | None,
        created_at: datetime | None,
        modified_at: datetime | None,
    ) -> None:
        """Handle importing a file that doesn't exist yet."""
        if options.dry_run:
            result.created += 1
            return

        file_meta = FileMetadata(
            path=path,
            size=metadata_dict["size"],
            content_id=imported_content_id,
            mime_type=metadata_dict.get("mime_type"),
            created_at=created_at,
            modified_at=modified_at,
            version=metadata_dict.get("version", 1),
        )

        self._metadata.put(file_meta)
        self._import_custom_metadata(path, metadata_dict)
        result.created += 1

    def _import_custom_metadata(self, path: str, metadata_dict: dict[str, Any]) -> None:
        """Helper to import custom metadata for a file."""
        if "custom_metadata" in metadata_dict:
            custom_meta = metadata_dict["custom_metadata"]
            if isinstance(custom_meta, dict):
                for key, value in custom_meta.items():
                    try:
                        self._metadata.set_file_metadata(path, key, value)
                    except Exception as e:
                        logger.debug("Failed to set custom metadata %s for %s: %s", key, path, e)
