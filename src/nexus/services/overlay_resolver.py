"""Overlay resolution service for ComposeFS-style agent workspace overlays.

Resolves file lookups through overlay layers (base + upper) where:
- Base layer: Immutable workspace snapshot (WorkspaceManifest in CAS)
- Upper layer: Per-agent metadata entries (in the metadata store)

Unmodified files are resolved from the base manifest with zero storage overhead.
Modified files in the upper layer take precedence. Whiteout markers represent
deletions of base-layer files.

Issue #1264: CAS dedup at VFS level.
Pattern follows: services/search_service.py (independent service, injected into NexusFS)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.workspace_manifest import ManifestEntry, WorkspaceManifest

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol

logger = logging.getLogger(__name__)

# Sentinel hash value for whiteout markers (deletions in overlay)
WHITEOUT_HASH = "whiteout:deleted"


@dataclass(slots=True)
class OverlayConfig:
    """Configuration for an overlay workspace.

    Attributes:
        enabled: Whether overlay resolution is active
        base_manifest_hash: CAS hash of the base workspace snapshot manifest
        workspace_path: Root path of the workspace (e.g., "/my-workspace")
        agent_id: Agent ID owning this overlay (for multi-agent isolation)
    """

    enabled: bool = False
    base_manifest_hash: str | None = None
    workspace_path: str = ""
    agent_id: str | None = None


@dataclass(slots=True)
class OverlayStats:
    """Storage statistics for an overlay workspace.

    Attributes:
        total_files: Total number of files visible through the overlay
        base_files: Number of files from the base layer
        upper_files: Number of files in the upper layer (modifications)
        whiteout_count: Number of whiteout markers (base files deleted)
        shared_ratio: Fraction of files resolved from base (0.0 - 1.0)
        estimated_savings_bytes: Estimated bytes saved by sharing base layer
    """

    total_files: int = 0
    base_files: int = 0
    upper_files: int = 0
    whiteout_count: int = 0
    shared_ratio: float = 0.0
    estimated_savings_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "total_files": self.total_files,
            "base_files": self.base_files,
            "upper_files": self.upper_files,
            "whiteout_count": self.whiteout_count,
            "shared_ratio": self.shared_ratio,
            "estimated_savings_bytes": self.estimated_savings_bytes,
        }


class OverlayResolver:
    """Resolves file lookups through overlay layers (base + upper).

    Uses a two-layer resolution strategy:
    1. Check upper layer (metadata store) first — agent's local modifications
    2. Fall back to base layer (immutable snapshot manifest) for unmodified files
    3. Whiteout markers (etag == WHITEOUT_HASH) hide base-layer files

    Base manifests are cached in memory since they are immutable once created.

    Args:
        metadata: Metadata store (acts as the upper layer)
        backend: CAS backend for reading base manifests
    """

    def __init__(
        self,
        metadata: FileMetadataProtocol,
        backend: Backend,
    ) -> None:
        self._metadata = metadata
        self._backend = backend
        self._manifest_cache: dict[str, WorkspaceManifest] = {}

    def get_base_manifest(self, base_hash: str) -> WorkspaceManifest:
        """Load and cache an immutable base manifest from CAS.

        Manifests are immutable once created, so caching is safe and effective.
        Multiple overlay workspaces sharing the same base hash will share
        the cached manifest object.

        Args:
            base_hash: CAS hash of the manifest JSON

        Returns:
            Parsed WorkspaceManifest

        Raises:
            BackendError: If manifest cannot be read from CAS
        """
        if base_hash in self._manifest_cache:
            return self._manifest_cache[base_hash]

        manifest_bytes = self._backend.read_content(base_hash, context=None).unwrap()
        manifest = WorkspaceManifest.from_json(manifest_bytes)
        self._manifest_cache[base_hash] = manifest
        return manifest

    def resolve_read(
        self,
        path: str,
        overlay_config: OverlayConfig,
    ) -> FileMetadata | None:
        """Resolve a file read through the overlay layers.

        Resolution order:
        1. Check upper layer (metadata store) for the full path
        2. If not in upper layer, check base manifest
        3. If found in base, synthesize a FileMetadata from the ManifestEntry
        4. If found nowhere, return None

        Args:
            path: Full absolute path (e.g., "/my-workspace/src/main.py")
            overlay_config: Overlay configuration for this workspace

        Returns:
            FileMetadata if found (may be a whiteout), None if not found
        """
        if not overlay_config.enabled or not overlay_config.base_manifest_hash:
            return None

        # Step 1: Check upper layer
        upper_meta = self._metadata.get(path)
        if upper_meta is not None:
            return upper_meta  # Upper layer takes precedence (may be whiteout)

        # Step 2: Check base manifest
        workspace_prefix = overlay_config.workspace_path
        if not workspace_prefix.endswith("/"):
            workspace_prefix += "/"

        # Convert absolute path to relative path within workspace
        if not path.startswith(workspace_prefix):
            return None

        rel_path = path[len(workspace_prefix) :]
        manifest = self.get_base_manifest(overlay_config.base_manifest_hash)
        entry = manifest.get(rel_path)

        if entry is None:
            return None

        # Synthesize FileMetadata from base manifest entry
        return self._manifest_entry_to_metadata(entry, path)

    def is_whiteout(self, meta: FileMetadata) -> bool:
        """Check if a metadata entry is a whiteout marker.

        Whiteout markers represent files that exist in the base layer
        but have been deleted in the upper layer.

        Args:
            meta: File metadata to check

        Returns:
            True if this is a whiteout marker
        """
        return meta.etag == WHITEOUT_HASH

    def create_whiteout(
        self,
        path: str,
        overlay_config: OverlayConfig,
    ) -> None:
        """Create a whiteout marker in the upper layer.

        Used when deleting a file that exists only in the base layer.
        Instead of actually deleting (which would be a no-op since it's not
        in the upper layer), we create a sentinel entry that hides the base
        layer file.

        Args:
            path: Full absolute path to the file being deleted
            overlay_config: Overlay configuration for this workspace
        """
        from nexus.core._metadata_generated import FileMetadata

        whiteout_meta = FileMetadata(
            path=path,
            backend_name="overlay",
            physical_path=WHITEOUT_HASH,
            size=0,
            etag=WHITEOUT_HASH,
            mime_type=None,
            modified_at=datetime.now(UTC),
            version=1,
            created_by=overlay_config.agent_id,
        )
        self._metadata.put(whiteout_meta)

    def list_overlay(
        self,
        prefix: str,
        overlay_config: OverlayConfig,
    ) -> list[FileMetadata]:
        """List files by merging upper and base layers.

        Two-pass set merge:
        1. Collect all upper-layer entries (modifications + whiteouts)
        2. Add base-layer entries not overridden by upper layer
        3. Exclude whiteout markers from final result

        Args:
            prefix: Path prefix to list (e.g., "/my-workspace/src/")
            overlay_config: Overlay configuration for this workspace

        Returns:
            Merged list of FileMetadata, excluding whiteouts
        """
        if not overlay_config.enabled or not overlay_config.base_manifest_hash:
            return self._metadata.list(prefix=prefix)

        # Pass 1: Collect upper-layer entries
        upper_entries = self._metadata.list(prefix=prefix)
        upper_paths: set[str] = set()
        result: list[FileMetadata] = []

        for meta in upper_entries:
            upper_paths.add(meta.path)
            if not self.is_whiteout(meta):
                result.append(meta)

        # Pass 2: Add base-layer entries not in upper layer
        workspace_prefix = overlay_config.workspace_path
        if not workspace_prefix.endswith("/"):
            workspace_prefix += "/"

        manifest = self.get_base_manifest(overlay_config.base_manifest_hash)

        for rel_path in manifest.paths():
            full_path = workspace_prefix + rel_path
            # Only include if matching prefix and not overridden by upper
            if full_path.startswith(prefix) and full_path not in upper_paths:
                entry = manifest.get(rel_path)
                if entry is not None:
                    result.append(self._manifest_entry_to_metadata(entry, full_path))

        return result

    def flatten(
        self,
        overlay_config: OverlayConfig,
    ) -> WorkspaceManifest:
        """Merge upper layer into a new snapshot manifest.

        Creates a new WorkspaceManifest by:
        1. Starting with all base-layer entries
        2. Overriding with upper-layer modifications
        3. Removing entries hidden by whiteouts
        4. Clearing upper-layer entries after merge

        Args:
            overlay_config: Overlay configuration for this workspace

        Returns:
            New WorkspaceManifest representing the flattened state

        Raises:
            ValueError: If overlay is not enabled or has no base manifest
        """
        if not overlay_config.enabled or not overlay_config.base_manifest_hash:
            raise ValueError("Cannot flatten: overlay not enabled or no base manifest")

        workspace_prefix = overlay_config.workspace_path
        if not workspace_prefix.endswith("/"):
            workspace_prefix += "/"

        # Start with base entries
        base_manifest = self.get_base_manifest(overlay_config.base_manifest_hash)
        merged_entries: dict[str, ManifestEntry] = dict(base_manifest.entries)

        # Apply upper-layer changes
        upper_entries = self._metadata.list(prefix=workspace_prefix)
        upper_paths: list[str] = []

        for meta in upper_entries:
            rel_path = meta.path[len(workspace_prefix) :]
            upper_paths.append(meta.path)

            if self.is_whiteout(meta):
                # Remove from merged (whiteout hides base entry)
                merged_entries.pop(rel_path, None)
            elif meta.etag:
                # Override base with upper-layer modification
                merged_entries[rel_path] = ManifestEntry(
                    content_hash=meta.etag,
                    size=meta.size,
                    mime_type=meta.mime_type,
                )

        # Clean up upper-layer entries
        if upper_paths:
            self._metadata.delete_batch(upper_paths)

        return WorkspaceManifest(entries=merged_entries)

    def overlay_stats(
        self,
        overlay_config: OverlayConfig,
    ) -> OverlayStats:
        """Compute storage statistics for an overlay workspace.

        Args:
            overlay_config: Overlay configuration for this workspace

        Returns:
            OverlayStats with storage savings information
        """
        if not overlay_config.enabled or not overlay_config.base_manifest_hash:
            return OverlayStats()

        workspace_prefix = overlay_config.workspace_path
        if not workspace_prefix.endswith("/"):
            workspace_prefix += "/"

        base_manifest = self.get_base_manifest(overlay_config.base_manifest_hash)
        upper_entries = self._metadata.list(prefix=workspace_prefix)

        # Categorize upper-layer entries
        upper_paths: set[str] = set()
        whiteout_count = 0
        upper_file_count = 0

        for meta in upper_entries:
            rel_path = meta.path[len(workspace_prefix) :]
            upper_paths.add(rel_path)
            if self.is_whiteout(meta):
                whiteout_count += 1
            else:
                upper_file_count += 1

        # Count base files not overridden or deleted
        base_only_count = 0
        base_only_size = 0
        for rel_path in base_manifest.paths():
            if rel_path not in upper_paths:
                base_only_count += 1
                entry = base_manifest.get(rel_path)
                if entry:
                    base_only_size += entry.size

        total_files = base_only_count + upper_file_count
        shared_ratio = base_only_count / total_files if total_files > 0 else 0.0

        return OverlayStats(
            total_files=total_files,
            base_files=base_only_count,
            upper_files=upper_file_count,
            whiteout_count=whiteout_count,
            shared_ratio=shared_ratio,
            estimated_savings_bytes=base_only_size,
        )

    def clear_cache(self, base_hash: str | None = None) -> None:
        """Clear cached manifests.

        Args:
            base_hash: If provided, clear only this hash. If None, clear all.
        """
        if base_hash is not None:
            self._manifest_cache.pop(base_hash, None)
        else:
            self._manifest_cache.clear()

    def _manifest_entry_to_metadata(
        self,
        entry: ManifestEntry,
        full_path: str,
    ) -> FileMetadata:
        """Synthesize a FileMetadata from a base-layer ManifestEntry.

        Args:
            entry: ManifestEntry from base manifest
            full_path: Full absolute path for the file

        Returns:
            FileMetadata pointing to the CAS content
        """
        from nexus.core._metadata_generated import FileMetadata

        return FileMetadata(
            path=full_path,
            backend_name="local",
            physical_path=entry.content_hash,
            size=entry.size,
            etag=entry.content_hash,
            mime_type=entry.mime_type,
        )
