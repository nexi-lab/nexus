"""Federated metadata proxy for cross-zone DT_MOUNT traversal.

Implements FileMetadataProtocol and routes each operation to the
correct zone's RaftMetadataStore via ZonePathResolver.

Usage:
    # Create from ZoneManager
    proxy = FederatedMetadataProxy.from_zone_manager(zone_manager, root_zone_id="default")

    # Inject into NexusFS — no NexusFS changes needed
    fs = NexusFS(backend=backend, metadata_store=proxy)

    # All operations transparently cross zone boundaries:
    fs.write("/shared/file.txt", data)  # → resolves to zone-beta
    fs.read("/local/file.txt")          # → stays in root zone
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol, PaginatedResult
from nexus.raft.zone_manager import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.raft.zone_path_resolver import ResolvedPath, ZonePathResolver
    from nexus.storage.raft_metadata_store import RaftMetadataStore

logger = logging.getLogger(__name__)


class FederatedMetadataProxy(FileMetadataProtocol):
    """Proxy that routes metadata operations across zones via DT_MOUNT.

    Transparent to callers — all paths are in the global namespace.
    The proxy resolves each path to the correct zone, delegates the
    operation, and remaps zone-relative paths back to global paths.
    """

    def __init__(
        self,
        resolver: ZonePathResolver,
        root_store: RaftMetadataStore,
        *,
        zone_manager: Any | None = None,
    ):
        """
        Args:
            resolver: ZonePathResolver for cross-zone path resolution.
            root_store: The root zone's store (used for close() and fallback).
            zone_manager: Optional ZoneManager ref for clean shutdown.
        """
        self._resolver = resolver
        self._root_store = root_store
        self._zone_manager = zone_manager

    @classmethod
    def from_zone_manager(
        cls,
        zone_manager: Any,
        root_zone_id: str = ROOT_ZONE_ID,
    ) -> FederatedMetadataProxy:
        """Create from a ZoneManager instance.

        Args:
            zone_manager: ZoneManager with create_zone/get_store.
            root_zone_id: ID of the root zone.

        Returns:
            FederatedMetadataProxy proxy.
        """
        from nexus.raft.zone_path_resolver import ZonePathResolver

        resolver = ZonePathResolver(zone_manager, root_zone_id=root_zone_id)
        root_store = zone_manager.get_store(root_zone_id)
        if root_store is None:
            raise RuntimeError(f"Root zone '{root_zone_id}' not found in ZoneManager")
        return cls(resolver, root_store, zone_manager=zone_manager)

    # =========================================================================
    # Path remapping helpers
    # =========================================================================

    def _resolve(self, path: str) -> ResolvedPath:
        return self._resolver.resolve(path)

    @staticmethod
    def _to_global_path(mount_chain: list[tuple[str, str]], zone_path: str) -> str:
        """Convert zone-relative path to global path.

        Example: mount_chain=[("root", "/shared")], zone_path="/file.txt"
                 → "/shared/file.txt"
        """
        if not mount_chain:
            return zone_path
        prefix = "".join(mp for _, mp in mount_chain)
        return prefix + zone_path

    def _remap_metadata(
        self,
        meta: FileMetadata,
        mount_chain: list[tuple[str, str]],
    ) -> FileMetadata:
        """Remap zone-relative metadata path back to global namespace."""
        if not mount_chain:
            return meta
        global_path = self._to_global_path(mount_chain, meta.path)
        return replace(meta, path=global_path)

    def _to_zone_metadata(
        self,
        metadata: FileMetadata,
        resolved: ResolvedPath,
    ) -> FileMetadata:
        """Remap global metadata path to zone-relative path for storage."""
        if not resolved.mount_chain:
            return metadata
        return replace(metadata, path=resolved.path)

    # =========================================================================
    # FileMetadataProtocol — abstract methods
    # =========================================================================

    def get(self, path: str) -> FileMetadata | None:
        resolved = self._resolve(path)
        meta = resolved.store.get(resolved.path)
        if meta is None:
            return None
        return self._remap_metadata(meta, resolved.mount_chain)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        resolved = self._resolve(metadata.path)
        zone_meta = self._to_zone_metadata(metadata, resolved)
        return resolved.store.put(zone_meta, consistency=consistency)

    def is_committed(self, token: int) -> str | None:
        return self._root_store.is_committed(token)

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        resolved = self._resolve(path)
        return resolved.store.delete(resolved.path, consistency=consistency)

    def exists(self, path: str) -> bool:
        resolved = self._resolve(path)
        return resolved.store.exists(resolved.path)

    def _walk_mount_tree(
        self,
        resolved: ResolvedPath,
        recursive: bool,
        **kwargs: Any,
    ) -> list[FileMetadata]:
        """List entries with iterative BFS into child DT_MOUNTs (stack-safe).

        When recursive=True, discovered DT_MOUNT entries are traversed to
        include files from child zones. A visited set prevents cycles
        (e.g. zone A mounts B, B mounts A).
        """
        results = resolved.store.list(resolved.path, recursive, **kwargs)
        remapped = [self._remap_metadata(m, resolved.mount_chain) for m in results]

        if not recursive:
            return remapped

        # BFS queue: (mount_chain, zone_id)
        visited: set[str] = {resolved.zone_id}
        queue: list[tuple[list[tuple[str, str]], str]] = []

        for meta in results:
            if meta.is_mount and meta.target_zone_id and meta.target_zone_id not in visited:
                queue.append(
                    (resolved.mount_chain + [(resolved.zone_id, meta.path)], meta.target_zone_id)
                )
                visited.add(meta.target_zone_id)

        while queue:
            mount_chain, zone_id = queue.pop(0)
            store = self._resolver.get_store(zone_id)
            if store is None:
                logger.warning("Mount target zone '%s' not found, skipping", zone_id)
                continue
            child_results = store.list("/", recursive, **kwargs)
            for meta in child_results:
                remapped.append(self._remap_metadata(meta, mount_chain))
                if meta.is_mount and meta.target_zone_id and meta.target_zone_id not in visited:
                    queue.append((mount_chain + [(zone_id, meta.path)], meta.target_zone_id))
                    visited.add(meta.target_zone_id)

        return remapped

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> list[FileMetadata]:
        resolve_path = prefix if prefix else "/"
        resolved = self._resolve(resolve_path)
        return self._walk_mount_tree(resolved, recursive, **kwargs)

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        resolve_path = prefix if prefix else "/"
        resolved = self._resolve(resolve_path)
        yield from self._walk_mount_tree(resolved, recursive, **kwargs)

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        resolve_path = prefix if prefix else "/"
        resolved = self._resolve(resolve_path)
        result = resolved.store.list_paginated(resolved.path, recursive, limit, cursor, zone_id)
        result.items = [self._remap_metadata(m, resolved.mount_chain) for m in result.items]
        return result

    def close(self) -> None:
        self._root_store.close()
        if self._zone_manager is not None:
            self._zone_manager.shutdown()

    # =========================================================================
    # Batch operations — group by zone for efficiency
    # =========================================================================

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        zone_groups: dict[str, list[tuple[str, str, list[tuple[str, str]]]]] = {}
        for path in paths:
            resolved = self._resolve(path)
            key = resolved.zone_id
            if key not in zone_groups:
                zone_groups[key] = []
            zone_groups[key].append((path, resolved.path, resolved.mount_chain))

        result: dict[str, FileMetadata | None] = {}
        for _zone_id, items in zone_groups.items():
            store = self._resolve(items[0][0]).store
            zone_paths = [zone_path for _, zone_path, _ in items]
            zone_results = store.get_batch(zone_paths)
            for global_path, zone_path, mount_chain in items:
                meta = zone_results.get(zone_path)
                if meta is not None:
                    meta = self._remap_metadata(meta, mount_chain)
                result[global_path] = meta
        return result

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        zone_groups: dict[str, list[FileMetadata]] = {}
        for metadata in metadata_list:
            resolved = self._resolve(metadata.path)
            zone_meta = self._to_zone_metadata(metadata, resolved)
            key = resolved.zone_id
            if key not in zone_groups:
                zone_groups[key] = []
            zone_groups[key].append(zone_meta)

        for zone_id, metas in zone_groups.items():
            store = self._resolver.get_store(zone_id)
            if store is not None:
                store.put_batch(metas)

    def delete_batch(self, paths: Sequence[str]) -> None:
        zone_groups: dict[str, list[str]] = {}
        for path in paths:
            resolved = self._resolve(path)
            key = resolved.zone_id
            if key not in zone_groups:
                zone_groups[key] = []
            zone_groups[key].append(resolved.path)

        for zone_id, zone_paths in zone_groups.items():
            store = self._resolver.get_store(zone_id)
            if store is not None:
                store.delete_batch(zone_paths)

    # =========================================================================
    # Store-specific methods (duck-typed by NexusFS)
    # =========================================================================

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename within the same zone. Cross-zone rename is not supported."""
        old_resolved = self._resolve(old_path)
        new_resolved = self._resolve(new_path)
        if old_resolved.zone_id != new_resolved.zone_id:
            raise ValueError(
                f"Cross-zone rename not supported: "
                f"'{old_path}' in zone '{old_resolved.zone_id}', "
                f"'{new_path}' in zone '{new_resolved.zone_id}'"
            )
        old_resolved.store.rename_path(old_resolved.path, new_resolved.path)

    def is_implicit_directory(self, path: str) -> bool:
        resolved = self._resolve(path)
        return resolved.store.is_implicit_directory(resolved.path)

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        resolved = self._resolve(path)
        resolved.store.set_file_metadata(resolved.path, key, value)

    def get_file_metadata(self, path: str, key: str) -> Any:
        resolved = self._resolve(path)
        return resolved.store.get_file_metadata(resolved.path, key)

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        return {path: self.get_file_metadata(path, key) for path in paths}

    def get_searchable_text(self, path: str) -> str | None:
        text: str | None = self.get_file_metadata(path, "parsed_text")
        return text

    def get_searchable_text_bulk(self, paths: Sequence[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in paths:
            text = self.get_searchable_text(path)
            if text is not None:
                result[path] = text
        return result

    def increment_revision(self, zone_id: str) -> int:
        store = self._resolver.get_store(zone_id)
        if store is None:
            return self._root_store.increment_revision(zone_id)
        return store.increment_revision(zone_id)

    def get_revision(self, zone_id: str) -> int:
        store = self._resolver.get_store(zone_id)
        if store is None:
            return self._root_store.get_revision(zone_id)
        return store.get_revision(zone_id)

    # =========================================================================
    # Fallback: forward unknown attributes to root store
    # =========================================================================

    def __getattr__(self, name: str) -> Any:
        return getattr(self._root_store, name)
