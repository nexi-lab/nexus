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
        lazy: bool = False,
    ) -> RaftMetadataStore:
        """Create a new zone and return its RaftMetadataStore.

        Args:
            zone_id: Unique zone identifier.
            peers: Peer addresses in "id@host:port" format.
            lazy: If True, use EC mode (lazy consensus).

        Returns:
            RaftMetadataStore wrapping the zone's ZoneHandle.
        """
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        handle = self._py_mgr.create_zone(zone_id, peers or [], lazy)
        store = RaftMetadataStore(engine=handle, zone_id=zone_id)
        self._stores[zone_id] = store

        mode = "EC" if lazy else "SC"
        logger.info(
            "Zone '%s' created (mode=%s, peers=%d)",
            zone_id,
            mode,
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

    def remove_zone(self, zone_id: str) -> None:
        """Remove a zone, shutting down its Raft group."""
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

        Creates a DT_MOUNT entry in parent_zone's metadata.
        The mount path must not already exist (NFS-style, no shadow).

        Args:
            parent_zone_id: Zone containing the mount point.
            mount_path: Path in parent zone where target is mounted.
            target_zone_id: Zone to mount.

        Raises:
            ValueError: If mount_path already exists (no shadow).
            RuntimeError: If parent zone doesn't exist.
        """
        parent_store = self.get_store(parent_zone_id)
        if parent_store is None:
            raise RuntimeError(f"Parent zone '{parent_zone_id}' not found")

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

        logger.info(
            "Mounted zone '%s' at '%s' in zone '%s'",
            target_zone_id,
            mount_path,
            parent_zone_id,
        )

    def unmount(self, parent_zone_id: str, mount_path: str) -> None:
        """Remove a mount point.

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

        parent_store.delete(mount_path)
        logger.info(
            "Unmounted '%s' from zone '%s'",
            mount_path,
            parent_zone_id,
        )

    def shutdown(self) -> None:
        """Shut down all zones and the gRPC server."""
        self._py_mgr.shutdown()
        self._stores.clear()
        logger.info("ZoneManager shut down")
