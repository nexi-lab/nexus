"""Multi-zone Raft manager for cross-zone federation.

Wraps PyO3 ZoneManager (Rust) to provide zone lifecycle management
and per-zone RaftMetadataStore instances.

Architecture:
    ZoneManager (Python)
    ├── PyZoneManager (Rust/PyO3) — owns Tokio runtime + gRPC server
    │   └── ZoneRaftRegistry (DashMap<zone_id, ZoneEntry>)
    ├── zone_id → RaftMetadataStore mapping (Python dict)
    └── create_zone() / get_store() / mount() / unmount()

Each zone is an independent Raft group with its own sled database.
All zones share one gRPC port (zone_id routing in transport layer).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.core._metadata_generated import DT_DIR, DT_MOUNT, FileMetadata

if TYPE_CHECKING:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

logger = logging.getLogger(__name__)


def _get_py_zone_manager():
    """Import PyO3 ZoneManager from _nexus_raft (avoid circular import with __init__)."""
    try:
        from _nexus_raft import ZoneManager as PyZoneManager
    except ImportError:
        PyZoneManager = None
    return PyZoneManager


class ZoneManager:
    """Manage multiple Raft zones and their metadata stores.

    Usage:
        mgr = ZoneManager(node_id=1, base_path="/var/lib/nexus/zones",
                          bind_addr="0.0.0.0:2126")
        store = mgr.create_zone("alpha", peers=["2@peer:2126"])
        store.put(metadata)

        # Mount zone-beta under /shared in zone-alpha
        mgr.mount("alpha", "/shared", "beta")
    """

    def __init__(
        self,
        node_id: int,
        base_path: str,
        bind_addr: str = "0.0.0.0:2126",
    ):
        PyZoneManager = _get_py_zone_manager()
        if PyZoneManager is None:
            raise RuntimeError(
                "ZoneManager requires PyO3 build with --features full. "
                "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
            )

        self._py_mgr = PyZoneManager(node_id, base_path, bind_addr)
        self._stores: dict[str, RaftMetadataStore] = {}
        self._node_id = node_id
        self._base_path = base_path

    def create_zone(
        self,
        zone_id: str,
        peers: list[str] | None = None,
    ) -> RaftMetadataStore:
        """Create a new zone and return its RaftMetadataStore.

        Only creates the Raft group + redb database. Does NOT create a
        root "/" entry — that's the responsibility of:
        - Node bootstrap (root zone, i_links_count=1)
        - mount() via _increment_links (lazy-creates "/" on first mount)

        Args:
            zone_id: Unique zone identifier.
            peers: Peer addresses in "id@host:port" format.

        Returns:
            RaftMetadataStore wrapping the zone's ZoneHandle.
        """
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        handle = self._py_mgr.create_zone(zone_id, peers or [])
        store = RaftMetadataStore(engine=handle, zone_id=zone_id)
        self._stores[zone_id] = store

        logger.info(
            "Zone '%s' created (peers=%d)",
            zone_id,
            len(peers or []),
        )
        return store

    def join_zone(
        self,
        zone_id: str,
        peers: list[str] | None = None,
    ) -> RaftMetadataStore:
        """Join an existing zone as a new Voter.

        Creates a local RaftNode without bootstrapping ConfState.
        After calling this, the leader must be notified via JoinZone RPC
        to propose ConfChange(AddNode) — the leader will auto-send a snapshot.

        Args:
            zone_id: Zone to join.
            peers: Existing peer addresses in "id@host:port" format.

        Returns:
            RaftMetadataStore wrapping the zone's ZoneHandle.
        """
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        handle = self._py_mgr.join_zone(zone_id, peers or [])
        store = RaftMetadataStore(engine=handle, zone_id=zone_id)
        self._stores[zone_id] = store

        logger.info(
            "Zone '%s' joined (peers=%d)",
            zone_id,
            len(peers or []),
        )
        return store

    def get_store(self, zone_id: str) -> RaftMetadataStore | None:
        """Get the RaftMetadataStore for a zone.

        Returns None if the zone doesn't exist.
        """
        if zone_id in self._stores:
            return self._stores[zone_id]

        # Zone might have been created by another process;
        # try to get a handle from Rust registry
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        handle = self._py_mgr.get_zone(zone_id)
        if handle is None:
            return None

        store = RaftMetadataStore(engine=handle, zone_id=zone_id)
        self._stores[zone_id] = store
        return store

    def remove_zone(self, zone_id: str, *, force: bool = False) -> None:
        """Remove a zone, shutting down its Raft group.

        Follows POSIX semantics: a zone can only be destroyed when
        i_links_count == 0 (no remaining references). Use force=True
        to bypass this check.

        Args:
            zone_id: Zone to remove.
            force: If True, skip i_links_count check.

        Raises:
            ValueError: If zone still has references (i_links_count > 0).
        """
        if not force:
            store = self.get_store(zone_id)
            if store is not None:
                root = store.get("/")
                if root is not None and root.i_links_count > 0:
                    raise ValueError(
                        f"Zone '{zone_id}' still has {root.i_links_count} reference(s) "
                        f"(i_links_count > 0). Unmount all references first, "
                        f"or use force=True."
                    )

        self._py_mgr.remove_zone(zone_id)
        self._stores.pop(zone_id, None)
        logger.info("Zone '%s' removed", zone_id)

    def list_zones(self) -> list[str]:
        """List all zone IDs."""
        return self._py_mgr.list_zones()

    @property
    def node_id(self) -> int:
        return self._node_id

    def mount(
        self,
        parent_zone_id: str,
        mount_path: str,
        target_zone_id: str,
    ) -> None:
        """Mount a zone at a path in another zone.

        Creates a DT_MOUNT entry in parent_zone's metadata and increments
        the target zone's i_links_count (POSIX: link() → nlink++).
        The mount path must not already exist (NFS-style, no shadow).

        Args:
            parent_zone_id: Zone containing the mount point.
            mount_path: Path in parent zone where target is mounted.
            target_zone_id: Zone to mount.

        Raises:
            ValueError: If mount_path already exists (no shadow).
            RuntimeError: If parent or target zone doesn't exist.
        """
        parent_store = self.get_store(parent_zone_id)
        if parent_store is None:
            raise RuntimeError(f"Parent zone '{parent_zone_id}' not found")

        target_store = self.get_store(target_zone_id)
        if target_store is None:
            raise RuntimeError(f"Target zone '{target_zone_id}' not found")

        # Reject if path already exists (NFS-style: no shadow)
        existing = parent_store.get(mount_path)
        if existing is not None:
            raise ValueError(
                f"Mount path '{mount_path}' already exists in zone '{parent_zone_id}'. "
                "Remove existing entry first (NFS-style: no shadow mount)."
            )

        # Create DT_MOUNT entry
        mount_entry = FileMetadata(
            path=mount_path,
            backend_name="mount",
            physical_path="",
            size=0,
            entry_type=DT_MOUNT,
            target_zone_id=target_zone_id,
            zone_id=parent_zone_id,
        )
        parent_store.put(mount_entry)

        # Ensure parent directory exists
        parent_dir = mount_path.rsplit("/", 1)[0] or "/"
        if parent_dir != "/" and parent_store.get(parent_dir) is None:
            dir_entry = FileMetadata(
                path=parent_dir,
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id=parent_zone_id,
            )
            parent_store.put(dir_entry)

        # Increment target zone's i_links_count (POSIX: link() → nlink++)
        self._increment_links(target_store, target_zone_id)

        logger.info(
            "Mounted zone '%s' at '%s' in zone '%s'",
            target_zone_id,
            mount_path,
            parent_zone_id,
        )

    def unmount(self, parent_zone_id: str, mount_path: str) -> None:
        """Remove a mount point.

        Deletes the DT_MOUNT entry and decrements the target zone's
        i_links_count (POSIX: unlink() → nlink--).

        Args:
            parent_zone_id: Zone containing the mount point.
            mount_path: Path to unmount.

        Raises:
            ValueError: If path is not a mount point.
        """
        parent_store = self.get_store(parent_zone_id)
        if parent_store is None:
            raise RuntimeError(f"Parent zone '{parent_zone_id}' not found")

        existing = parent_store.get(mount_path)
        if existing is None or not existing.is_mount:
            raise ValueError(f"'{mount_path}' is not a mount point in zone '{parent_zone_id}'")

        target_zone_id = existing.target_zone_id
        parent_store.delete(mount_path)

        # Decrement target zone's i_links_count (POSIX: unlink() → nlink--)
        if target_zone_id:
            target_store = self.get_store(target_zone_id)
            if target_store is not None:
                self._decrement_links(target_store)

        logger.info(
            "Unmounted '%s' from zone '%s' (target=%s)",
            mount_path,
            parent_zone_id,
            target_zone_id,
        )

    # =========================================================================
    # i_links_count helpers (POSIX i_nlink semantics)
    # =========================================================================

    @staticmethod
    def _increment_links(store: RaftMetadataStore, zone_id: str) -> int:
        """Increment a zone's i_links_count on its root "/" entry.

        Returns the new count.
        """
        from dataclasses import replace

        root = store.get("/")
        if root is None:
            # Zone has no root entry yet — create one
            root = FileMetadata(
                path="/",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id=zone_id,
                i_links_count=1,
            )
            store.put(root)
            return 1

        new_count = root.i_links_count + 1
        store.put(replace(root, i_links_count=new_count))
        return new_count

    @staticmethod
    def _decrement_links(store: RaftMetadataStore) -> int:
        """Decrement a zone's i_links_count on its root "/" entry.

        Returns the new count. Never goes below 0.
        """
        from dataclasses import replace

        root = store.get("/")
        if root is None:
            return 0

        new_count = max(0, root.i_links_count - 1)
        store.put(replace(root, i_links_count=new_count))
        return new_count

    def get_links_count(self, zone_id: str) -> int:
        """Get a zone's current i_links_count.

        Returns 0 if zone or root entry doesn't exist.
        """
        store = self.get_store(zone_id)
        if store is None:
            return 0
        root = store.get("/")
        if root is None:
            return 0
        return root.i_links_count

    def shutdown(self) -> None:
        """Shut down all zones and the gRPC server."""
        self._py_mgr.shutdown()
        self._stores.clear()
        logger.info("ZoneManager shut down")
