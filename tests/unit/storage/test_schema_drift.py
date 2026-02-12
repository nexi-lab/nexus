"""Schema drift detection tests.

Verifies that FileMetadata (proto) and FilePathModel (SQLAlchemy) stay in sync.
Catches field additions/removals that aren't reflected in both representations.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from sqlalchemy import inspect as sa_inspect

from nexus.core._metadata_generated import FileMetadata
from nexus.storage.models import FilePathModel

# ---------------------------------------------------------------------------
# Known field mapping: proto name -> SQLAlchemy column name
# This is the canonical mapping that the MetadataMapper (Phase 1) will codify.
# If you add a field to FileMetadata, add it here too!
# ---------------------------------------------------------------------------
PROTO_TO_SQL_FIELD_MAP: dict[str, str | None] = {
    # Proto field -> FilePathModel column (None = not in SQL, by design)
    "path": "virtual_path",
    "backend_name": "backend_id",
    "physical_path": "physical_path",
    "size": "size_bytes",
    "etag": "content_hash",
    "mime_type": "file_type",
    "created_at": "created_at",
    "modified_at": "updated_at",
    "version": "current_version",
    "zone_id": "zone_id",
    "created_by": None,  # TODO(#1246): Add to FilePathModel
    "entry_type": None,  # TODO(#1246): Add to FilePathModel
    "target_zone_id": None,  # DT_MOUNT target, not in SQL
    "owner_id": "posix_uid",
    "i_links_count": None,  # Metastore-only (mount ref count), not in SQL
}

# Fields that exist in FilePathModel but NOT in FileMetadata (PG-only concerns)
SQL_ONLY_FIELDS: set[str] = {
    "path_id",
    "accessed_at",
    "deleted_at",
    "indexed_content_hash",
    "last_indexed_at",
    "locked_by",
}


class TestProtoFieldsCovered:
    """Every FileMetadata field must have an entry in the field map."""

    def test_all_proto_fields_have_mapping_entry(self) -> None:
        """If you add a field to FileMetadata, you MUST add it to PROTO_TO_SQL_FIELD_MAP."""
        proto_fields = {f.name for f in dataclasses.fields(FileMetadata)}
        mapped_fields = set(PROTO_TO_SQL_FIELD_MAP.keys())

        missing = proto_fields - mapped_fields
        assert missing == set(), (
            f"FileMetadata has fields not in PROTO_TO_SQL_FIELD_MAP: {missing}. "
            f"Add them to the map in test_schema_drift.py (use None if intentionally SQL-excluded)."
        )

    def test_no_stale_mapping_entries(self) -> None:
        """Field map should not reference proto fields that no longer exist."""
        proto_fields = {f.name for f in dataclasses.fields(FileMetadata)}
        mapped_fields = set(PROTO_TO_SQL_FIELD_MAP.keys())

        stale = mapped_fields - proto_fields
        assert stale == set(), (
            f"PROTO_TO_SQL_FIELD_MAP references fields not in FileMetadata: {stale}. "
            f"Remove them from the map."
        )


class TestSqlColumnsCovered:
    """Every mapped SQL column must actually exist in FilePathModel."""

    def test_mapped_columns_exist_in_model(self) -> None:
        """SQL column names in the map must be real FilePathModel columns."""
        mapper = sa_inspect(FilePathModel)
        sql_columns = {col.key for col in mapper.columns}

        mapped_sql_names = {v for v in PROTO_TO_SQL_FIELD_MAP.values() if v is not None}

        missing = mapped_sql_names - sql_columns
        assert missing == set(), (
            f"PROTO_TO_SQL_FIELD_MAP references columns not in FilePathModel: {missing}. "
            f"Check for typos or add the missing columns."
        )

    def test_sql_only_fields_exist(self) -> None:
        """SQL-only fields (not in proto) should still exist in the model."""
        mapper = sa_inspect(FilePathModel)
        sql_columns = {col.key for col in mapper.columns}

        missing = SQL_ONLY_FIELDS - sql_columns
        assert missing == set(), (
            f"SQL_ONLY_FIELDS references columns not in FilePathModel: {missing}."
        )

    def test_all_sql_columns_accounted_for(self) -> None:
        """Every FilePathModel column should be in either the field map or SQL_ONLY_FIELDS."""
        mapper = sa_inspect(FilePathModel)
        sql_columns = {col.key for col in mapper.columns}

        mapped_sql_names = {v for v in PROTO_TO_SQL_FIELD_MAP.values() if v is not None}
        accounted = mapped_sql_names | SQL_ONLY_FIELDS

        unaccounted = sql_columns - accounted
        assert unaccounted == set(), (
            f"FilePathModel has columns not in field map or SQL_ONLY_FIELDS: {unaccounted}. "
            f"Add them to the appropriate set in test_schema_drift.py."
        )


class TestRoundtripConsistency:
    """FileMetadata -> FilePathModel values -> verify no data loss."""

    def _metadata_to_file_path_values(self, metadata: FileMetadata) -> dict:
        """Map FileMetadata to FilePathModel column values via MetadataMapper."""
        from nexus.storage._metadata_mapper_generated import MetadataMapper

        return MetadataMapper.to_file_path_values(metadata)

    def test_roundtrip_preserves_core_fields(self) -> None:
        """Core fields should survive the FileMetadata -> FilePathModel mapping."""
        now = datetime(2026, 2, 10, 12, 0, 0)
        metadata = FileMetadata(
            path="/zone1/docs/readme.md",
            backend_name="s3",
            physical_path="/bucket/abc123",
            size=2048,
            etag="sha256-xyz789",
            mime_type="text/markdown",
            created_at=now,
            modified_at=now,
            version=3,
            zone_id="zone1",
            created_by="agent-1",
            owner_id="user-42",
        )

        values = self._metadata_to_file_path_values(metadata)

        assert values["virtual_path"] == "/zone1/docs/readme.md"
        assert values["backend_id"] == "s3"
        assert values["physical_path"] == "/bucket/abc123"
        assert values["size_bytes"] == 2048
        assert values["content_hash"] == "sha256-xyz789"
        assert values["file_type"] == "text/markdown"
        assert values["zone_id"] == "zone1"
        assert values["posix_uid"] == "user-42"

    def test_roundtrip_with_none_optionals(self) -> None:
        """None optional fields should map to sensible defaults."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="",
            physical_path="",
            size=0,
            etag=None,
            mime_type=None,
            created_at=None,
            modified_at=None,
            zone_id=None,
            owner_id=None,
        )

        values = self._metadata_to_file_path_values(metadata)

        assert values["backend_id"] == "local"  # default
        assert values["physical_path"] == "/test/file.txt"  # fallback to path
        assert values["content_hash"] is None
        assert values["file_type"] is None
        assert values["zone_id"] == "default"  # default
        assert values["posix_uid"] is None

    def test_fields_not_yet_in_sql_are_documented(self) -> None:
        """created_by and entry_type are in proto but not yet in SQL.

        This test documents the gap and will fail when we add the columns,
        reminding us to update the mapping.
        """
        none_mapped = {k for k, v in PROTO_TO_SQL_FIELD_MAP.items() if v is None}
        # These are the expected gaps — update this when columns are added
        assert none_mapped == {"created_by", "entry_type", "target_zone_id", "i_links_count"}, (
            f"Expected only created_by, entry_type, target_zone_id, and i_links_count to be unmapped, "
            f"but got: {none_mapped}. "
            f"Did you add a column to FilePathModel? Update PROTO_TO_SQL_FIELD_MAP."
        )
