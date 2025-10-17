"""Export/import dataclasses and types for metadata operations.

This module defines the types used for JSONL export/import operations,
following the pattern from the Beads project for git-friendly backups
and zero-downtime migrations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class ExportFilter:
    """Filter options for metadata export.

    Attributes:
        tenant_id: Filter by tenant ID (None = all tenants)
        path_prefix: Only export paths starting with this prefix (default: "")
        after_time: Only export files modified after this time (None = all)
        include_deleted: Include soft-deleted files in export (default: False)
    """

    tenant_id: str | None = None
    path_prefix: str = ""
    after_time: datetime | None = None
    include_deleted: bool = False


@dataclass
class CollisionDetail:
    """Details about a collision during import.

    Attributes:
        path: The virtual path that collided
        existing_etag: Content hash of existing file
        imported_etag: Content hash of imported file
        resolution: How the collision was resolved
        message: Human-readable description
    """

    path: str
    existing_etag: str | None
    imported_etag: str | None
    resolution: str
    message: str


ConflictMode = Literal["skip", "overwrite", "remap", "auto"]


@dataclass
class ImportOptions:
    """Options for metadata import.

    Attributes:
        dry_run: If True, simulate import without making changes
        conflict_mode: How to handle path collisions:
            - "skip": Skip conflicting items, keep existing
            - "overwrite": Always use imported data
            - "remap": Rename imported items to avoid collisions
            - "auto": Smart resolution based on timestamps (newer wins)
        preserve_ids: Preserve original UUIDs from export (default: True)
    """

    dry_run: bool = False
    conflict_mode: ConflictMode = "skip"
    preserve_ids: bool = True


@dataclass
class ImportResult:
    """Result of metadata import operation.

    Attributes:
        created: Number of new files created
        updated: Number of existing files updated
        skipped: Number of files skipped due to conflicts
        remapped: Number of files remapped to new paths
        collisions: Detailed list of all collisions encountered
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    remapped: int = 0
    collisions: list[CollisionDetail] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        """Total number of items processed."""
        return self.created + self.updated + self.skipped + self.remapped

    def __str__(self) -> str:
        """Human-readable summary."""
        return (
            f"ImportResult(created={self.created}, updated={self.updated}, "
            f"skipped={self.skipped}, remapped={self.remapped}, "
            f"collisions={len(self.collisions)})"
        )
