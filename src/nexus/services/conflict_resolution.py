"""Conflict resolution for bidirectional sync (Issue #1129).

Pure functions for detecting and resolving conflicts between
Nexus and backend file states. No DB access, no side effects.

Conflict Detection:
- Compares Nexus state against backend state using the last
  synced checkpoint from ChangeLogStore.
- If both sides changed since last sync, it's a conflict.

Resolution Policies:
- LWW (Last Writer Wins): Compare mtimes, newer wins. Ties favor Nexus.
- Fork: Create a conflict copy, preserving both versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from nexus.backends.backend import FileInfo
    from nexus.services.change_log_store import ChangeLogEntry


@dataclass(frozen=True)
class ConflictRecord:
    """Immutable record of a detected conflict and its resolution."""

    path: str
    backend_name: str
    zone_id: str
    nexus_content_hash: str | None
    nexus_mtime: datetime | None
    backend_version: str | None
    backend_mtime: datetime | None
    resolution: Literal["lww_nexus_wins", "lww_backend_wins", "fork"]
    resolved_at: datetime


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


def resolve_conflict(
    nexus_mtime: datetime | None,
    backend_mtime: datetime | None,
    policy: Literal["lww", "fork"] = "lww",
) -> Literal["nexus_wins", "backend_wins", "fork"]:
    """Apply conflict resolution policy.

    Args:
        nexus_mtime: Nexus file modification time
        backend_mtime: Backend file modification time
        policy: Resolution policy — "lww" (last writer wins) or "fork"

    Returns:
        Resolution outcome
    """
    if policy == "fork":
        return "fork"

    # LWW: compare timestamps, Nexus wins on tie
    if nexus_mtime is None and backend_mtime is None:
        return "nexus_wins"
    if nexus_mtime is None:
        return "backend_wins"
    if backend_mtime is None:
        return "nexus_wins"

    if nexus_mtime >= backend_mtime:
        return "nexus_wins"
    return "backend_wins"


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
