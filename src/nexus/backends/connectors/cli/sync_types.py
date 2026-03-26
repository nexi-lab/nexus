"""Typed data structures for connector delta sync (Issue #3266).

Replaces the untyped dict contract for sync_delta() with explicit dataclasses.
Used by ConnectorSyncLoop to process delta sync results and write to the metastore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DeltaItem:
    """A single item added or modified during a delta sync.

    Attributes:
        id: Backend-specific identifier (e.g., Gmail message ID, event ID).
        path: Full display path relative to mount root (e.g., "INBOX/tid-mid.yaml").
        content_hash: Optional content hash for change detection.
        size: Content size in bytes (0 if unknown).
    """

    id: str
    path: str
    content_hash: str | None = None
    size: int = 0


@dataclass(frozen=True)
class DeltaSyncResult:
    """Result of a connector's sync_delta() call.

    Provides a typed contract for delta sync results, replacing the previous
    untyped dict. The sync loop uses this to write metadata and content to
    the metastore.

    Attributes:
        added: Items added or modified since the last sync.
        deleted: Paths of items deleted since the last sync.
        sync_token: Opaque token for the next delta sync (e.g., Gmail historyId,
            Calendar syncToken). Stored by the sync loop for the next cycle.
        full_sync_required: If True, the delta was too large or the token was
            invalid — caller should fall back to full BFS sync.
    """

    added: list[DeltaItem] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    sync_token: str | None = None
    full_sync_required: bool = False

    @property
    def has_changes(self) -> bool:
        """True if the delta contains any additions or deletions."""
        return bool(self.added or self.deleted)

    @property
    def total_changes(self) -> int:
        """Total number of changes (additions + deletions)."""
        return len(self.added) + len(self.deleted)


@dataclass
class MountSyncState:
    """Per-mount sync tracking for structured error handling (Issue #3266).

    Tracks sync health per mount point for observability and fallback decisions.
    """

    mount_point: str
    last_successful_sync: datetime | None = None
    last_sync_attempt: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    sync_token: str | None = None
    total_files_synced: int = 0
    sync_in_progress: bool = False

    def record_success(self, files_synced: int = 0, sync_token: str | None = None) -> None:
        """Record a successful sync cycle."""
        now = datetime.now()
        self.last_successful_sync = now
        self.last_sync_attempt = now
        self.consecutive_failures = 0
        self.last_error = None
        self.total_files_synced += files_synced
        if sync_token is not None:
            self.sync_token = sync_token

    def record_failure(self, error: str) -> None:
        """Record a failed sync cycle."""
        self.last_sync_attempt = datetime.now()
        self.consecutive_failures += 1
        self.last_error = error

    @property
    def is_healthy(self) -> bool:
        """True if the mount has synced successfully recently."""
        return self.consecutive_failures < 3

    def to_dict(self) -> dict:
        """Serialize to dict for health endpoint exposure."""
        return {
            "mount_point": self.mount_point,
            "last_successful_sync": (
                self.last_successful_sync.isoformat() if self.last_successful_sync else None
            ),
            "last_sync_attempt": (
                self.last_sync_attempt.isoformat() if self.last_sync_attempt else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "sync_token": self.sync_token,
            "total_files_synced": self.total_files_synced,
            "is_healthy": self.is_healthy,
            "sync_in_progress": self.sync_in_progress,
        }
