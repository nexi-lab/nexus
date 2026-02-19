"""Metadata Export/Import RPC Service — replaces NexusFS export/import facades.

Wraps MetastoreABC with @rpc_expose + standalone logic.
No dependency on NexusFS.

Issue #2033 — Phase 2.4 of LEGO microkernel decomposition.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.contracts.rpc import rpc_expose
from nexus.contracts.types import OperationContext
from nexus.core.export_import import (
    CollisionDetail,
    ExportFilter,
    ImportOptions,
    ImportResult,
)
from nexus.core.metadata import FileMetadata

logger = logging.getLogger(__name__)


class MetadataExportService:
    """RPC surface for metadata export/import operations.

    Replaces ~460 LOC of facades in NexusFS (export_metadata, import_metadata).
    """

    def __init__(
        self,
        *,
        metastore: Any,
        default_context: OperationContext,
    ) -> None:
        self._metastore = metastore
        self._default_context = default_context

    def _get_created_by(self, context: OperationContext | dict | None = None) -> str | None:
        """Get the created_by value for version history tracking."""
        user = None
        agent = None

        if context is None:
            user = getattr(self._default_context, "user_id", None)
            agent = self._default_context.agent_id
        elif hasattr(context, "agent_id"):
            user = getattr(context, "user_id", None)
            agent = context.agent_id
        elif isinstance(context, dict):
            user = context.get("user_id")
            agent = context.get("agent_id")
        else:
            user = getattr(self._default_context, "user_id", None)
            agent = self._default_context.agent_id

        parts = []
        if user:
            parts.append(f"user:{user}")
        if agent:
            parts.append(f"agent:{agent}")

        return ",".join(parts) if parts else None

    # ------------------------------------------------------------------
    # Public RPC Methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Export metadata to JSONL file")
    def export_metadata(
        self,
        output_path: str | Path,
        filter: ExportFilter | None = None,
        prefix: str = "",
    ) -> int:
        """Export metadata to JSONL file for backup and migration.

        Each line in the output file is a JSON object containing:
        - path: Virtual file path
        - backend_name: Backend identifier
        - physical_path: Physical storage path (content hash in CAS)
        - size: File size in bytes
        - etag: Content hash (SHA-256)
        - mime_type: MIME type (optional)
        - created_at: Creation timestamp (ISO format)
        - modified_at: Modification timestamp (ISO format)
        - version: Version number
        - custom_metadata: Dict of custom key-value metadata (optional)

        Output is sorted by path for clean git diffs.

        Args:
            output_path: Path to output JSONL file
            filter: Export filter options (zone_id, path_prefix, after_time, include_deleted)
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

        from nexus.core.nexus_fs_core import SYSTEM_PATH_PREFIX

        all_files = [
            m
            for m in self._metastore.list_iter(filter.path_prefix)
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
                metadata_dict: dict[str, Any] = {
                    "path": file_meta.path,
                    "backend_name": file_meta.backend_name,
                    "physical_path": file_meta.physical_path,
                    "size": file_meta.size,
                    "etag": file_meta.etag,
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

        IMPORTANT: This only imports metadata records, not the actual file content.
        The content must already exist in the CAS storage (matched by content hash).

        Args:
            input_path: Path to input JSONL file
            options: Import options (conflict mode, dry-run, preserve IDs)
            overwrite: (Deprecated) If True, overwrite existing (backward compat)
            skip_existing: (Deprecated) If True, skip existing (backward compat)

        Returns:
            ImportResult with counts and collision details

        Raises:
            ValueError: If JSONL format is invalid
            FileNotFoundError: If input file doesn't exist
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

                    existing = self._metastore.get(path)
                    imported_etag = metadata_dict.get("etag")

                    if existing:
                        self._handle_collision(
                            existing,
                            imported_etag,
                            metadata_dict,
                            path,
                            original_path,
                            created_at,
                            modified_at,
                            options,
                            result,
                        )
                    else:
                        self._handle_new_entry(
                            path,
                            metadata_dict,
                            imported_etag,
                            created_at,
                            modified_at,
                            options,
                            result,
                        )

                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON at line {line_num}: {e}") from e
                except Exception as e:
                    if isinstance(e, ValueError):
                        raise
                    raise ValueError(f"Error processing line {line_num}: {e}") from e

        return result

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _handle_collision(
        self,
        existing: Any,
        imported_etag: str | None,
        metadata_dict: dict[str, Any],
        path: str,
        original_path: str,
        created_at: datetime | None,
        modified_at: datetime | None,
        options: ImportOptions,
        result: ImportResult,
    ) -> None:
        """Handle import collision with existing file."""
        existing_etag = existing.etag
        is_same_content = existing_etag == imported_etag

        if is_same_content:
            if options.dry_run:
                result.updated += 1
                return
            file_meta = FileMetadata(
                path=path,
                backend_name=metadata_dict["backend_name"],
                physical_path=metadata_dict["physical_path"],
                size=metadata_dict["size"],
                etag=imported_etag,
                mime_type=metadata_dict.get("mime_type"),
                created_at=created_at or existing.created_at,
                modified_at=modified_at or existing.modified_at,
                version=metadata_dict.get("version", existing.version),
                created_by=self._get_created_by(),
            )
            self._metastore.put(file_meta)
            self._import_custom_metadata(path, metadata_dict)
            result.updated += 1
            return

        handler = {
            "skip": self._collision_skip,
            "overwrite": self._collision_overwrite,
            "remap": self._collision_remap,
            "auto": self._collision_auto,
        }.get(options.conflict_mode, self._collision_skip)

        handler(
            existing,
            existing_etag,
            imported_etag,
            metadata_dict,
            path,
            original_path,
            created_at,
            modified_at,
            options,
            result,
        )

    def _collision_skip(
        self,
        existing: Any,  # noqa: ARG002
        existing_etag: str | None,
        imported_etag: str | None,
        metadata_dict: dict[str, Any],  # noqa: ARG002
        path: str,
        original_path: str,  # noqa: ARG002
        created_at: datetime | None,  # noqa: ARG002
        modified_at: datetime | None,  # noqa: ARG002
        options: ImportOptions,  # noqa: ARG002
        result: ImportResult,
    ) -> None:
        result.skipped += 1
        result.collisions.append(
            CollisionDetail(
                path=path,
                existing_etag=existing_etag,
                imported_etag=imported_etag,
                resolution="skip",
                message="Skipped: existing file has different content",
            )
        )

    def _collision_overwrite(
        self,
        existing: Any,
        existing_etag: str | None,
        imported_etag: str | None,
        metadata_dict: dict[str, Any],
        path: str,
        original_path: str,  # noqa: ARG002
        created_at: datetime | None,
        modified_at: datetime | None,
        options: ImportOptions,
        result: ImportResult,
    ) -> None:
        if options.dry_run:
            result.updated += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_etag=existing_etag,
                    imported_etag=imported_etag,
                    resolution="overwrite",
                    message="Would overwrite with imported content",
                )
            )
            return

        file_meta = FileMetadata(
            path=path,
            backend_name=metadata_dict["backend_name"],
            physical_path=metadata_dict["physical_path"],
            size=metadata_dict["size"],
            etag=imported_etag,
            mime_type=metadata_dict.get("mime_type"),
            created_at=created_at or existing.created_at,
            modified_at=modified_at,
            version=metadata_dict.get("version", existing.version + 1),
            created_by=self._get_created_by(),
        )
        self._metastore.put(file_meta)
        self._import_custom_metadata(path, metadata_dict)
        result.updated += 1
        result.collisions.append(
            CollisionDetail(
                path=path,
                existing_etag=existing_etag,
                imported_etag=imported_etag,
                resolution="overwrite",
                message="Overwrote with imported content",
            )
        )

    def _collision_remap(
        self,
        existing: Any,  # noqa: ARG002
        existing_etag: str | None,
        imported_etag: str | None,
        metadata_dict: dict[str, Any],
        path: str,
        original_path: str,
        created_at: datetime | None,
        modified_at: datetime | None,
        options: ImportOptions,
        result: ImportResult,
    ) -> None:
        suffix = 1
        while self._metastore.exists(f"{path}_imported{suffix}"):
            suffix += 1
        remapped_path = f"{path}_imported{suffix}"

        if options.dry_run:
            result.remapped += 1
            result.collisions.append(
                CollisionDetail(
                    path=original_path,
                    existing_etag=existing_etag,
                    imported_etag=imported_etag,
                    resolution="remap",
                    message=f"Would remap to: {remapped_path}",
                )
            )
            return

        file_meta = FileMetadata(
            path=remapped_path,
            backend_name=metadata_dict["backend_name"],
            physical_path=metadata_dict["physical_path"],
            size=metadata_dict["size"],
            etag=imported_etag,
            mime_type=metadata_dict.get("mime_type"),
            created_at=created_at,
            modified_at=modified_at,
            version=metadata_dict.get("version", 1),
            created_by=self._get_created_by(),
        )
        self._metastore.put(file_meta)
        self._import_custom_metadata(remapped_path, metadata_dict)
        result.remapped += 1
        result.collisions.append(
            CollisionDetail(
                path=original_path,
                existing_etag=existing_etag,
                imported_etag=imported_etag,
                resolution="remap",
                message=f"Remapped to: {remapped_path}",
            )
        )

    def _collision_auto(
        self,
        existing: Any,
        existing_etag: str | None,
        imported_etag: str | None,
        metadata_dict: dict[str, Any],
        path: str,
        original_path: str,  # noqa: ARG002
        created_at: datetime | None,
        modified_at: datetime | None,
        options: ImportOptions,
        result: ImportResult,
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
                        existing_etag=existing_etag,
                        imported_etag=imported_etag,
                        resolution="auto_overwrite",
                        message=f"Would overwrite: imported is newer ({imported_time} > {existing_time})",
                    )
                )
                return

            file_meta = FileMetadata(
                path=path,
                backend_name=metadata_dict["backend_name"],
                physical_path=metadata_dict["physical_path"],
                size=metadata_dict["size"],
                etag=imported_etag,
                mime_type=metadata_dict.get("mime_type"),
                created_at=created_at or existing.created_at,
                modified_at=modified_at,
                version=metadata_dict.get("version", existing.version + 1),
                created_by=self._get_created_by(),
            )
            self._metastore.put(file_meta)
            self._import_custom_metadata(path, metadata_dict)
            result.updated += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_etag=existing_etag,
                    imported_etag=imported_etag,
                    resolution="auto_overwrite",
                    message=f"Overwrote: imported is newer ({imported_time} > {existing_time})",
                )
            )
        else:
            result.skipped += 1
            result.collisions.append(
                CollisionDetail(
                    path=path,
                    existing_etag=existing_etag,
                    imported_etag=imported_etag,
                    resolution="auto_skip",
                    message="Skipped: existing is newer or equal",
                )
            )

    def _handle_new_entry(
        self,
        path: str,
        metadata_dict: dict[str, Any],
        imported_etag: str | None,
        created_at: datetime | None,
        modified_at: datetime | None,
        options: ImportOptions,
        result: ImportResult,
    ) -> None:
        """Handle importing a new entry with no collision."""
        if options.dry_run:
            result.created += 1
            return

        file_meta = FileMetadata(
            path=path,
            backend_name=metadata_dict["backend_name"],
            physical_path=metadata_dict["physical_path"],
            size=metadata_dict["size"],
            etag=imported_etag,
            mime_type=metadata_dict.get("mime_type"),
            created_at=created_at,
            modified_at=modified_at,
            version=metadata_dict.get("version", 1),
            created_by=self._get_created_by(),
        )
        self._metastore.put(file_meta)
        self._import_custom_metadata(path, metadata_dict)
        result.created += 1

    def _import_custom_metadata(self, path: str, metadata_dict: dict[str, Any]) -> None:
        """Helper to import custom metadata for a file."""
        if "custom_metadata" in metadata_dict:
            custom_meta = metadata_dict["custom_metadata"]
            if isinstance(custom_meta, dict):
                for key, value in custom_meta.items():
                    try:
                        self._metastore.set_file_metadata(path, key, value)
                    except Exception as e:
                        logger.debug("Failed to set custom metadata %s for %s: %s", key, path, e)
