"""Workspace manifest for snapshot and overlay operations.

Provides a shared dataclass for workspace snapshot manifests, used by both
WorkspaceManager (snapshot/restore/diff) and OverlayResolver (base layer resolution).

The manifest maps relative file paths to their content hashes and metadata.
It is stored as JSON in CAS, with entries sorted by path for deterministic hashing.

JSON format (backward-compatible with existing snapshots):
    {
        "rel/path/file.txt": {"hash": "abc123...", "size": 1024, "mime_type": "text/plain"},
        "rel/path/other.py": {"hash": "def456...", "size": 512, "mime_type": "text/x-python"}
    }

Issue #1264: Extracted from WorkspaceManager to enable DRY sharing with OverlayResolver.
Pattern follows: chunked_storage.py (ChunkInfo + ChunkedReference)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ManifestEntry:
    """A single file entry in a workspace manifest.

    Attributes:
        content_hash: BLAKE3/SHA-256 hash of the file content (CAS key)
        size: File size in bytes
        mime_type: MIME type of the file (optional)
    """

    content_hash: str
    size: int
    mime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        result: dict[str, Any] = {"hash": self.content_hash, "size": self.size}
        # Always include mime_type for backward compatibility with existing format
        result["mime_type"] = self.mime_type
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestEntry:
        """Deserialize from JSON dict."""
        return cls(
            content_hash=data["hash"],
            size=data["size"],
            mime_type=data.get("mime_type"),
        )


@dataclass
class WorkspaceManifest:
    """Manifest of all files in a workspace snapshot.

    Maps relative file paths to their content metadata. Used as:
    - Snapshot format for WorkspaceManager (create/restore/diff)
    - Base layer for OverlayResolver (Issue #1264)

    Entries are sorted by path for deterministic JSON serialization,
    ensuring the same workspace state produces the same manifest hash.

    Attributes:
        entries: Mapping of relative path -> ManifestEntry
    """

    entries: dict[str, ManifestEntry] = field(default_factory=dict)

    def get(self, path: str) -> ManifestEntry | None:
        """Get entry for a relative path.

        Args:
            path: Relative file path within the workspace

        Returns:
            ManifestEntry if found, None otherwise
        """
        return self.entries.get(path)

    def paths(self) -> set[str]:
        """Get all file paths in this manifest.

        Returns:
            Set of relative file paths
        """
        return set(self.entries.keys())

    @property
    def file_count(self) -> int:
        """Number of files in this manifest."""
        return len(self.entries)

    @property
    def total_size(self) -> int:
        """Total size of all files in bytes."""
        return sum(e.size for e in self.entries.values())

    def to_json(self) -> bytes:
        """Serialize to JSON bytes, sorted by path for deterministic hashing.

        Produces the same JSON format used by existing workspace snapshots,
        ensuring backward compatibility.

        Returns:
            JSON bytes ready for CAS storage
        """
        sorted_entries = sorted(self.entries.items(), key=lambda kv: kv[0])
        manifest_dict = {path: entry.to_dict() for path, entry in sorted_entries}
        return json.dumps(manifest_dict, separators=(",", ": ")).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> WorkspaceManifest:
        """Deserialize from JSON bytes.

        Handles the existing snapshot JSON format for backward compatibility.

        Args:
            data: JSON bytes from CAS

        Returns:
            WorkspaceManifest instance

        Raises:
            json.JSONDecodeError: If data is not valid JSON
            KeyError: If required fields are missing
        """
        parsed = json.loads(data)
        entries = {path: ManifestEntry.from_dict(entry_data) for path, entry_data in parsed.items()}
        return cls(entries=entries)

    @classmethod
    def from_file_list(
        cls,
        file_entries: list[tuple[str, str, int, str | None]],
    ) -> WorkspaceManifest:
        """Create manifest from a list of file entries.

        This is the primary constructor used by WorkspaceManager.create_snapshot().

        Args:
            file_entries: List of (rel_path, content_hash, size, mime_type) tuples.
                         Must already be filtered (no directories, no missing etags).

        Returns:
            WorkspaceManifest with entries sorted by path
        """
        entries = {
            rel_path: ManifestEntry(
                content_hash=content_hash,
                size=size,
                mime_type=mime_type,
            )
            for rel_path, content_hash, size, mime_type in file_entries
        }
        return cls(entries=entries)
