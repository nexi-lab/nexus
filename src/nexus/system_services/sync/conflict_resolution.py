"""Conflict resolution for bidirectional sync (Issue #1129, #1130).

Pure functions for detecting and resolving conflicts between
Nexus and backend file states. No DB access, no side effects.

Conflict Detection:
- Compares Nexus state against backend state using the last
  synced checkpoint from ChangeLogStore.
- If both sides changed since last sync, it's a conflict.

Resolution Strategies (Issue #1130):
- ABORT: Raise error on conflict
- KEEP_REMOTE: Backend always wins
- KEEP_LOCAL: Nexus always wins
- KEEP_NEWER: Newer mtime wins (ties favor Nexus) — replaces old LWW
- KEEP_LARGER: Larger file wins (ties favor Nexus)
- RENAME_CONFLICT: Create .sync-conflict copy preserving both versions
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.backend import FileInfo
    from nexus.services.change_log_store import ChangeLogEntry


class ConflictStrategy(StrEnum):
    """Configurable conflict resolution strategy."""

    ABORT = "abort"
    KEEP_REMOTE = "keep_remote"
    KEEP_LOCAL = "keep_local"
    KEEP_NEWER = "keep_newer"
    KEEP_LARGER = "keep_larger"
    RENAME_CONFLICT = "rename_conflict"


class ResolutionOutcome(StrEnum):
    """Result of applying a conflict resolution strategy."""

    NEXUS_WINS = "nexus_wins"
    BACKEND_WINS = "backend_wins"
    ABORT = "abort"
    RENAME_CONFLICT = "rename_conflict"


class ConflictStatus(StrEnum):
    """Status of a conflict record in the audit log."""

    AUTO_RESOLVED = "auto_resolved"
    MANUAL_PENDING = "manual_pending"
    MANUALLY_RESOLVED = "manually_resolved"


@dataclass(frozen=True)
class ConflictContext:
    """All metadata needed for conflict resolution — replaces positional args."""

    nexus_mtime: datetime | None
    nexus_size: int | None
    nexus_content_hash: str | None
    backend_mtime: datetime | None
    backend_size: int | None
    backend_content_hash: str | None
    path: str
    backend_name: str
    zone_id: str


@dataclass(frozen=True)
class ConflictRecord:
    """Immutable record of a detected conflict and its resolution."""

    id: str
    path: str
    backend_name: str
    zone_id: str
    strategy: ConflictStrategy
    outcome: ResolutionOutcome
    nexus_content_hash: str | None
    nexus_mtime: datetime | None
    nexus_size: int | None
    backend_content_hash: str | None
    backend_mtime: datetime | None
    backend_size: int | None
    conflict_copy_path: str | None  # For RENAME_CONFLICT
    status: ConflictStatus
    resolved_at: datetime


class ConflictAbortError(Exception):
    """Raised when ABORT strategy is applied to a conflict."""


def resolve_conflict(
    ctx: ConflictContext,
    strategy: ConflictStrategy = ConflictStrategy.KEEP_NEWER,
) -> ResolutionOutcome:
    """Apply conflict resolution strategy — pure function, no side effects.

    Args:
        ctx: ConflictContext with all metadata for both sides
        strategy: Which resolution strategy to apply

    Returns:
        ResolutionOutcome indicating the winner or action
    """
    match strategy:
        case ConflictStrategy.ABORT:
            return ResolutionOutcome.ABORT
        case ConflictStrategy.KEEP_REMOTE:
            return ResolutionOutcome.BACKEND_WINS
        case ConflictStrategy.KEEP_LOCAL:
            return ResolutionOutcome.NEXUS_WINS
        case ConflictStrategy.KEEP_NEWER:
            return _resolve_by_mtime(ctx.nexus_mtime, ctx.backend_mtime)
        case ConflictStrategy.KEEP_LARGER:
            return _resolve_by_size(ctx.nexus_size, ctx.backend_size)
        case ConflictStrategy.RENAME_CONFLICT:
            return ResolutionOutcome.RENAME_CONFLICT


def detect_conflict(
    nexus_mtime: datetime | None,
    nexus_content_hash: str | None,
    backend_file_info: FileInfo,
    last_synced: ChangeLogEntry | None,
) -> bool:
    """Determine if both Nexus and backend changed since last sync.

    Args:
        nexus_mtime: Current Nexus file modification time
        nexus_content_hash: Current Nexus file content hash
        backend_file_info: Current backend file info
        last_synced: Last synced state from ChangeLogStore (None = first sync)

    Returns:
        True if both sides changed (conflict), False otherwise
    """
    # No previous sync record means first sync — no conflict possible
    if last_synced is None:
        return False

    nexus_changed = _nexus_changed(nexus_mtime, nexus_content_hash, last_synced)
    backend_changed = _backend_changed(backend_file_info, last_synced)

    return nexus_changed and backend_changed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_by_mtime(
    nexus_mtime: datetime | None,
    backend_mtime: datetime | None,
) -> ResolutionOutcome:
    """Compare timestamps — Nexus wins on tie or when backend mtime is None."""
    if nexus_mtime is None and backend_mtime is None:
        return ResolutionOutcome.NEXUS_WINS
    if nexus_mtime is None:
        return ResolutionOutcome.BACKEND_WINS
    if backend_mtime is None:
        return ResolutionOutcome.NEXUS_WINS
    if nexus_mtime >= backend_mtime:
        return ResolutionOutcome.NEXUS_WINS
    return ResolutionOutcome.BACKEND_WINS


def _resolve_by_size(
    nexus_size: int | None,
    backend_size: int | None,
) -> ResolutionOutcome:
    """Compare file sizes — Nexus wins on tie. None treated as 0."""
    n = nexus_size if nexus_size is not None else 0
    b = backend_size if backend_size is not None else 0
    if n >= b:
        return ResolutionOutcome.NEXUS_WINS
    return ResolutionOutcome.BACKEND_WINS


def _nexus_changed(
    nexus_mtime: datetime | None,
    nexus_content_hash: str | None,
    last_synced: ChangeLogEntry,
) -> bool:
    """Check if Nexus side changed since last sync."""
    # Compare content hash first (most reliable)
    if nexus_content_hash is not None and last_synced.content_hash is not None:
        return nexus_content_hash != last_synced.content_hash

    # Fall back to mtime comparison
    if nexus_mtime is not None and last_synced.mtime is not None:
        return nexus_mtime != last_synced.mtime

    # Cannot determine — assume changed to be safe
    return True


def _backend_changed(
    backend_file_info: FileInfo,
    last_synced: ChangeLogEntry,
) -> bool:
    """Check if backend side changed since last sync."""
    # Compare backend version first (most reliable for cloud backends)
    if backend_file_info.backend_version and last_synced.backend_version:
        return backend_file_info.backend_version != last_synced.backend_version

    # Compare content hash
    if backend_file_info.content_hash and last_synced.content_hash:
        return backend_file_info.content_hash != last_synced.content_hash

    # Compare size + mtime
    if (
        backend_file_info.size is not None
        and last_synced.size_bytes is not None
        and backend_file_info.size != last_synced.size_bytes
    ):
        return True

    if backend_file_info.mtime is not None and last_synced.mtime is not None:
        return backend_file_info.mtime != last_synced.mtime

    # Cannot determine — assume changed to be safe
    return True
