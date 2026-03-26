"""Federated metadata proxy for cross-zone DT_MOUNT traversal.

Implements MetastoreABC and routes each operation to the
correct zone's RaftMetadataStore via ZonePathResolver.

Usage:
    # Create from ZoneManager
    proxy = FederatedMetadataProxy.from_zone_manager(zone_manager, root_zone_id=ROOT_ZONE_ID)

    # Inject into NexusFS — no NexusFS changes needed
    fs = NexusFS(backend=backend, metadata_store=proxy)

    # All operations transparently cross zone boundaries:
    await fs.sys_write("/shared/file.txt", data)  # → resolves to zone-beta
    await fs.sys_read("/local/file.txt")          # → stays in root zone
"""

import builtins
import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC

if TYPE_CHECKING:
    from nexus.raft.zone_path_resolver import ResolvedPath, ZonePathResolver
    from nexus.storage.raft_metadata_store import RaftMetadataStore

logger = logging.getLogger(__name__)


class FederatedMetadataProxy(MetastoreABC):
    """Proxy that routes metadata operations across zones via DT_MOUNT.

    Transparent to callers — all paths are in the global namespace.
    The proxy resolves each path to the correct zone, delegates the
    operation, and remaps zone-relative paths back to global paths.
    """

    def __init__(
        self,
        resolver: "ZonePathResolver",
        root_store: "RaftMetadataStore",
        *,
        zone_manager: Any | None = None,
        self_address: str | None = None,
    ):
        """
        Args:
            resolver: ZonePathResolver for cross-zone path resolution.
            root_store: The root zone's store (used for close() and fallback).
            zone_manager: Optional ZoneManager ref for clean shutdown.
            self_address: This node's advertise address for backend_name enrichment.
        """
        self._resolver = resolver
        self._root_store = root_store
        self._zone_manager = zone_manager
        self._self_address = self_address
        # Map EC tokens to the store that issued them, so is_committed()
        # polls the correct zone's Raft log instead of always the root.
        self._token_stores: dict[int, "RaftMetadataStore"] = {}

    @classmethod
    def from_zone_manager(
        cls,
        zone_manager: Any,
        root_zone_id: str = ROOT_ZONE_ID,
    ) -> "FederatedMetadataProxy":
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
        # Use VFS gRPC port (default 2028) for content addressing, not Raft port (2126).
        # FederationContentResolver uses NexusVFSService (VFS gRPC), not Raft gRPC.
        import os as _os

        raft_addr: str | None = getattr(zone_manager, "advertise_addr", None)
        self_addr: str | None
        if raft_addr and ":" in raft_addr:
            hostname = raft_addr.rsplit(":", 1)[0]
            vfs_port = _os.environ.get("NEXUS_GRPC_PORT", "2028")
            self_addr = f"{hostname}:{vfs_port}"
        else:
            self_addr = raft_addr
        return cls(resolver, root_store, zone_manager=zone_manager, self_address=self_addr)

    # =========================================================================
    # Path remapping helpers
    # =========================================================================

    # Internal path prefixes that bypass zone resolution (stored in root zone only).
    _INTERNAL_PREFIXES = ("ns:", "mnt:", "/_internal/")

    def _resolve(self, path: str) -> "ResolvedPath":
        # Internal paths (namespace configs, mount configs) stay in root zone
        # without going through zone path resolution. (Issue #3192)
        if any(path.startswith(p) for p in self._INTERNAL_PREFIXES):
            from nexus.raft.zone_path_resolver import ResolvedPath

            return ResolvedPath(
                store=self._root_store,
                zone_id=getattr(self._root_store, "_zone_id", "root"),
                path=path,
                mount_chain=[],
            )
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
        resolved: "ResolvedPath",
    ) -> FileMetadata:
        """Remap global metadata path to zone-relative path for storage."""
        if not resolved.mount_chain:
            return metadata
        return replace(metadata, path=resolved.path)

    def _enrich_backend_name(self, metadata: FileMetadata) -> FileMetadata:
        """Enrich backend_name with node address for federation content targeting.

        Kernel writes ``backend_name="local"``; the proxy transparently
        enriches to ``"local@10.0.0.5:50051"`` so FederationContentResolver
        can locate which peer owns the content.

        For already-enriched entries (multi-origin), ensures self_address
        is present in the origins list (idempotent).
        """
        if not self._self_address or not metadata.backend_name:
            return metadata
        if "@" not in metadata.backend_name:
            # Fresh entry from kernel — add self as first origin
            return replace(
                metadata,
                backend_name=f"{metadata.backend_name}@{self._self_address}",
            )
        # Already enriched — ensure self_address is in origins (idempotent)
        from nexus.contracts.backend_address import BackendAddress

        addr = BackendAddress.parse(metadata.backend_name)
        updated = addr.with_origin(self._self_address)
        if updated is addr:
            return metadata  # already present
        return replace(metadata, backend_name=str(updated))

    # =========================================================================
    # MetastoreABC — abstract methods
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
        zone_meta = self._enrich_backend_name(zone_meta)
        token = resolved.store.put(zone_meta, consistency=consistency)
        if token is not None:
            self._token_stores[token] = resolved.store
        return token

    def is_committed(self, token: int) -> str | None:
        store = self._token_stores.get(token, self._root_store)
        return store.is_committed(token)

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        resolved = self._resolve(path)
        return resolved.store.delete(resolved.path, consistency=consistency)

    def exists(self, path: str) -> bool:
        resolved = self._resolve(path)
        return resolved.store.exists(resolved.path)

    def _walk_mount_tree(
        self,
        resolved: "ResolvedPath",
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
        # Deduplicate by (zone_id, mount_path) to allow the same zone
        # mounted at different paths while still preventing cycles.
        visited: set[tuple[str, str]] = {(resolved.zone_id, resolved.path)}
        queue: list[tuple[list[tuple[str, str]], str]] = []

        for meta in results:
            visit_key = (meta.target_zone_id or "", meta.path)
            if meta.is_mount and meta.target_zone_id and visit_key not in visited:
                queue.append(
                    (resolved.mount_chain + [(resolved.zone_id, meta.path)], meta.target_zone_id)
                )
                visited.add(visit_key)

        while queue:
            mount_chain, zone_id = queue.pop(0)
            store = self._resolver.get_store(zone_id)
            if store is None:
                logger.warning("Mount target zone '%s' not found, skipping", zone_id)
                continue
            child_results = store.list("/", recursive, **kwargs)
            for meta in child_results:
                remapped.append(self._remap_metadata(meta, mount_chain))
                visit_key = (meta.target_zone_id or "", meta.path)
                if meta.is_mount and meta.target_zone_id and visit_key not in visited:
                    queue.append((mount_chain + [(zone_id, meta.path)], meta.target_zone_id))
                    visited.add(visit_key)

        return remapped

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> list[FileMetadata]:
        resolve_path = prefix if prefix else "/"

        # Issue #3266: For synced connector mounts (/mnt/*), prefer the
        # Postgres file_paths table which has human-readable display names
        # written by the sync layer. Falls back to Raft on cache miss.
        if resolve_path.startswith("/mnt/"):
            fp_results = self._list_from_file_paths(resolve_path, recursive)
            if fp_results is not None:
                return fp_results

        resolved = self._resolve(resolve_path)
        raft_results = self._walk_mount_tree(resolved, recursive, **kwargs)

        # Issue #3266: Inject /mnt into root listing so TUI shows connector mounts.
        if resolve_path == "/" and not recursive:
            # Root non-recursive: just add /mnt as a directory, children load on expand
            existing = {m.path for m in raft_results}
            if "/mnt" not in existing:
                fp_check = self._list_from_file_paths("/mnt/", recursive=False)
                if fp_check:
                    from datetime import UTC, datetime

                    raft_results.append(
                        FileMetadata(
                            path="/mnt",
                            backend_name="__mount__",
                            physical_path="/mnt",
                            size=0,
                            etag=None,
                            created_at=datetime.now(UTC),
                            modified_at=datetime.now(UTC),
                            version=1,
                            zone_id=ROOT_ZONE_ID,
                        )
                    )
        elif resolve_path == "/" and recursive:
            # Root recursive: merge all connector files
            fp_results = self._list_from_file_paths("/mnt/", recursive=True)
            if fp_results:
                existing = {m.path for m in raft_results}
                for fp in fp_results:
                    if fp.path not in existing:
                        raft_results.append(fp)

        return raft_results

    _fp_engine: Any = None

    def _list_from_file_paths(
        self,
        prefix: str,
        recursive: bool,
    ) -> "builtins.list[FileMetadata] | None":
        """Query Postgres file_paths table for synced connector display names.

        Returns None on cache miss (no rows or DB error) so the caller
        falls back to the Raft store / live backend listing.
        """
        try:
            import os

            from sqlalchemy import text

            db_url = os.environ.get("NEXUS_DATABASE_URL", "")
            if not db_url:
                return None

            # Reuse cached engine to avoid creating a new pool per call.
            if not hasattr(self, "_fp_engine") or self._fp_engine is None:
                from sqlalchemy import create_engine

                self._fp_engine = create_engine(
                    db_url, pool_size=2, max_overflow=3, pool_pre_ping=True
                )
            engine = self._fp_engine
            norm = prefix.rstrip("/")

            with engine.connect() as conn:
                if recursive:
                    rows = conn.execute(
                        text(
                            "SELECT virtual_path, physical_path, size_bytes "
                            "FROM file_paths "
                            "WHERE virtual_path LIKE :pat "
                            "ORDER BY virtual_path"
                        ),
                        {"pat": f"{norm}/%"},
                    ).fetchall()
                else:
                    # Non-recursive: get direct children only.
                    # First try exact one-level children.
                    rows = conn.execute(
                        text(
                            "SELECT virtual_path, physical_path, size_bytes "
                            "FROM file_paths "
                            "WHERE virtual_path LIKE :pat "
                            "AND virtual_path NOT LIKE :deep "
                            "ORDER BY virtual_path"
                        ),
                        {"pat": f"{norm}/%", "deep": f"{norm}/%/%"},
                    ).fetchall()

                    # If no direct children found, synthesize directory entries
                    # from distinct next-level path components.
                    if not rows:
                        depth = norm.count("/") + 1
                        child_rows = conn.execute(
                            text(
                                "SELECT DISTINCT split_part(virtual_path, '/', :depth) "
                                "FROM file_paths "
                                "WHERE virtual_path LIKE :pat "
                                "AND split_part(virtual_path, '/', :depth) != ''"
                            ),
                            {"pat": f"{norm}/%", "depth": depth + 1},
                        ).fetchall()
                        if child_rows:
                            # Synthesize as directory entries
                            synth: list[Any] = [
                                (f"{norm}/{name}", f"{norm}/{name}", 0) for (name,) in child_rows
                            ]
                            rows = synth

            if not rows:
                return None

            from datetime import UTC, datetime

            now = datetime.now(UTC)
            results: list[FileMetadata] = []
            for vpath, ppath, size in rows:
                results.append(
                    FileMetadata(
                        path=vpath,
                        backend_name="__sync__",
                        physical_path=ppath or vpath,
                        size=size or 0,
                        etag=None,
                        created_at=now,
                        modified_at=now,
                        version=1,
                        zone_id=ROOT_ZONE_ID,
                    )
                )
            logger.debug(
                "[FILE_PATHS] %s returned %d entries (recursive=%s)",
                prefix,
                len(results),
                recursive,
            )
            return results
        except Exception:
            logger.debug("[FILE_PATHS] Query failed for %s", prefix, exc_info=True)
            return None

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        resolve_path = prefix if prefix else "/"

        if resolve_path.startswith("/mnt/"):
            fp_results = self._list_from_file_paths(resolve_path, recursive)
            if fp_results is not None:
                yield from fp_results
                return

        resolved = self._resolve(resolve_path)
        yield from self._walk_mount_tree(resolved, recursive, **kwargs)

        # Issue #3266: Inject /mnt into root listing
        if resolve_path == "/" and not recursive:
            fp_check = self._list_from_file_paths("/mnt/", recursive=False)
            if fp_check:
                from datetime import UTC, datetime

                yield FileMetadata(
                    path="/mnt",
                    backend_name="__mount__",
                    physical_path="/mnt",
                    size=0,
                    etag=None,
                    created_at=datetime.now(UTC),
                    modified_at=datetime.now(UTC),
                    version=1,
                    zone_id=ROOT_ZONE_ID,
                )
        elif resolve_path == "/" and recursive:
            fp_results = self._list_from_file_paths("/mnt/", recursive=True)
            if fp_results:
                yield from fp_results

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
            zone_meta = self._enrich_backend_name(zone_meta)
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
