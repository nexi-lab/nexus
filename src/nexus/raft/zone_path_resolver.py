"""Cross-zone path resolver for DT_MOUNT traversal.

Resolves a virtual path to the correct (RaftMetadataStore, relative_path)
pair by walking path components and crossing DT_MOUNT boundaries.

Architecture:
    resolve("/shared/docs/file.txt")
    1. Look up "/" in root zone → DT_DIR → continue
    2. Look up "/shared" in root zone → DT_MOUNT(target=zone-beta) → switch zone
    3. Look up "/docs/file.txt" in zone-beta → DT_DIR + DT_REG → found

Because All-Voters model means every zone has a local sled replica,
all reads are local (~5μs). No network hop for path resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.zone_manager import ZoneManager
    from nexus.storage.raft_metadata_store import RaftMetadataStore

logger = logging.getLogger(__name__)

# Guard against infinite mount loops
MAX_MOUNT_DEPTH = 16


@dataclass
class ResolvedPath:
    """Result of cross-zone path resolution.

    Attributes:
        store: The RaftMetadataStore for the resolved zone.
        zone_id: The zone ID where the path lives.
        path: The path within the resolved zone.
        mount_chain: List of (zone_id, mount_path) pairs traversed.
    """

    store: RaftMetadataStore
    zone_id: str
    path: str
    mount_chain: list[tuple[str, str]]


class ZonePathResolver:
    """Resolve paths across zone boundaries via DT_MOUNT entries.

    Usage:
        resolver = ZonePathResolver(zone_manager, root_zone_id="default")
        resolved = resolver.resolve("/shared/docs/file.txt")
        metadata = resolved.store.get(resolved.path)
    """

    def __init__(self, zone_manager: ZoneManager, root_zone_id: str = "default"):
        self._zone_manager = zone_manager
        self._root_zone_id = root_zone_id

    @property
    def root_zone_id(self) -> str:
        return self._root_zone_id

    def resolve(self, path: str) -> ResolvedPath:
        """Resolve a path, crossing DT_MOUNT boundaries as needed.

        Args:
            path: Absolute path to resolve (must start with "/").

        Returns:
            ResolvedPath with the target store, zone_id, and relative path.

        Raises:
            FileNotFoundError: If a mount target zone doesn't exist.
            RuntimeError: If mount depth exceeds MAX_MOUNT_DEPTH (loop).
        """
        if not path.startswith("/"):
            raise ValueError(f"Path must be absolute, got: {path!r}")

        current_zone_id = self._root_zone_id
        current_store = self._zone_manager.get_store(current_zone_id)
        if current_store is None:
            raise FileNotFoundError(f"Root zone '{current_zone_id}' not found")

        mount_chain: list[tuple[str, str]] = []

        # Split path into components: "/a/b/c" → ["a", "b", "c"]
        parts = [p for p in path.split("/") if p]
        if not parts:
            # Root path "/" — stays in root zone
            return ResolvedPath(
                store=current_store,
                zone_id=current_zone_id,
                path="/",
                mount_chain=mount_chain,
            )

        # Walk path components, checking each prefix for DT_MOUNT
        for i in range(len(parts)):
            prefix = "/" + "/".join(parts[: i + 1])
            entry = current_store.get(prefix)

            if entry is not None and entry.is_mount:
                # Cross zone boundary
                target_zone_id = entry.target_zone_id
                if not target_zone_id:
                    raise FileNotFoundError(f"DT_MOUNT at '{prefix}' has no target_zone_id")

                mount_chain.append((current_zone_id, prefix))

                if len(mount_chain) > MAX_MOUNT_DEPTH:
                    raise RuntimeError(
                        f"Mount depth exceeded {MAX_MOUNT_DEPTH} — possible loop: {mount_chain}"
                    )

                # Switch to target zone
                target_store = self._zone_manager.get_store(target_zone_id)
                if target_store is None:
                    raise FileNotFoundError(
                        f"Mount target zone '{target_zone_id}' not found "
                        f"(mounted at '{prefix}' in zone '{current_zone_id}')"
                    )

                current_zone_id = target_zone_id
                current_store = target_store

                # Remaining path becomes root-relative in the new zone
                remaining = parts[i + 1 :]
                if not remaining:
                    return ResolvedPath(
                        store=current_store,
                        zone_id=current_zone_id,
                        path="/",
                        mount_chain=mount_chain,
                    )

                # Recurse: resolve remaining path in new zone
                remaining_path = "/" + "/".join(remaining)
                sub_resolved = self._resolve_in_zone(
                    current_store,
                    current_zone_id,
                    remaining_path,
                    mount_chain,
                )
                return sub_resolved

        # No mount points encountered — path is in current zone
        return ResolvedPath(
            store=current_store,
            zone_id=current_zone_id,
            path=path,
            mount_chain=mount_chain,
        )

    def _resolve_in_zone(
        self,
        store: RaftMetadataStore,
        zone_id: str,
        path: str,
        mount_chain: list[tuple[str, str]],
    ) -> ResolvedPath:
        """Resolve remaining path within a zone, checking for nested mounts."""
        parts = [p for p in path.split("/") if p]
        if not parts:
            return ResolvedPath(store=store, zone_id=zone_id, path="/", mount_chain=mount_chain)

        for i in range(len(parts)):
            prefix = "/" + "/".join(parts[: i + 1])
            entry = store.get(prefix)

            if entry is not None and entry.is_mount:
                target_zone_id = entry.target_zone_id
                if not target_zone_id:
                    raise FileNotFoundError(f"DT_MOUNT at '{prefix}' has no target_zone_id")

                mount_chain.append((zone_id, prefix))

                if len(mount_chain) > MAX_MOUNT_DEPTH:
                    raise RuntimeError(
                        f"Mount depth exceeded {MAX_MOUNT_DEPTH} — possible loop: {mount_chain}"
                    )

                target_store = self._zone_manager.get_store(target_zone_id)
                if target_store is None:
                    raise FileNotFoundError(f"Mount target zone '{target_zone_id}' not found")

                remaining = parts[i + 1 :]
                if not remaining:
                    return ResolvedPath(
                        store=target_store,
                        zone_id=target_zone_id,
                        path="/",
                        mount_chain=mount_chain,
                    )

                remaining_path = "/" + "/".join(remaining)
                return self._resolve_in_zone(
                    target_store, target_zone_id, remaining_path, mount_chain
                )

        return ResolvedPath(store=store, zone_id=zone_id, path=path, mount_chain=mount_chain)
