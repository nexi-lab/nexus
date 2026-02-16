"""Multi-zone Raft manager for cross-zone federation.

Wraps PyO3 ZoneManager (Rust) to provide zone lifecycle management
and per-zone RaftMetadataStore instances.

Architecture:
    ZoneManager (Python)
    ├── PyZoneManager (Rust/PyO3) — owns Tokio runtime + gRPC server
    │   └── ZoneRaftRegistry (DashMap<zone_id, ZoneEntry>)
    ├── zone_id → RaftMetadataStore mapping (Python dict)
    └── create_zone() / get_store() / mount() / unmount()

Each zone is an independent Raft group with its own redb database.
All zones share one gRPC port (zone_id routing in transport layer).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.core._metadata_generated import DT_DIR, DT_MOUNT, FileMetadata

if TYPE_CHECKING:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

logger = logging.getLogger(__name__)

# SSOT for default root zone ID — used by bootstrap() and from_zone_manager()
ROOT_ZONE_ID = "root"


def _get_py_zone_manager() -> type | None:
    """Import PyO3 ZoneManager from _nexus_raft (avoid circular import with __init__)."""
    try:
        from _nexus_raft import ZoneManager as PyZoneManager
    except ImportError:
        return None
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

    Lifecycle:
        bootstrap() → bootstrap_static() → ensure_topology()

        ensure_topology() is the shared readiness gate for both:
        - Static (Day 1): called by health check after initial leader election
        - Dynamic (Recovery): called after campaign() + wait_for_leader()

        All state changes use standard Raft operations (is_leader + propose).
    """

    def __init__(
        self,
        node_id: int,
        base_path: str,
        bind_addr: str = "0.0.0.0:2126",
        *,
        tls_cert_path: str | None = None,
        tls_key_path: str | None = None,
        tls_ca_path: str | None = None,
    ):
        PyZoneManager = _get_py_zone_manager()
        if PyZoneManager is None:
            raise RuntimeError(
                "ZoneManager requires PyO3 build with --features full. "
                "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
            )

        self._py_mgr = PyZoneManager(
            node_id,
            base_path,
            bind_addr,
            tls_cert_path=tls_cert_path,
            tls_key_path=tls_key_path,
            tls_ca_path=tls_ca_path,
        )
        self._stores: dict[str, RaftMetadataStore] = {}
        self._node_id = node_id
        self._base_path = base_path
        self._root_zone_id: str | None = None
        self._tls_cert_path = tls_cert_path
        self._tls_key_path = tls_key_path
        self._tls_ca_path = tls_ca_path
        self._pending_mounts: dict[str, str] | None = None
        self._topology_initialized = False

    def bootstrap(
        self,
        root_zone_id: str = ROOT_ZONE_ID,
        peers: list[str] | None = None,
    ) -> RaftMetadataStore:
        """Bootstrap this node's root zone Raft group.

        Creates the Raft group with ConfState (standard raft-rs bootstrap).
        Does NOT write any data — the root "/" entry and mount topology
        are created via normal Raft proposals after leader election.
        This follows the standard Raft contract: all state changes go
        through committed log entries.

        Idempotent — safe to call on every startup.

        Args:
            root_zone_id: Zone ID for this node's root zone.
            peers: Peer addresses for the root zone (multi-node).

        Returns:
            RaftMetadataStore for the root zone.
        """
        self._root_zone_id = root_zone_id

        # Check if root zone already exists
        store = self.get_store(root_zone_id)
        if store is not None:
            logger.info("Node bootstrap: root zone '%s' already exists", root_zone_id)
            return store

        # Create root zone Raft group (ConfState bootstrap only, no data writes)
        store = self.create_zone(root_zone_id, peers=peers)

        logger.info(
            "Node bootstrap: root zone '%s' Raft group created (awaiting leader election)",
            root_zone_id,
        )
        return store

    @property
    def root_zone_id(self) -> str | None:
        """The root zone ID set during bootstrap, or None if not bootstrapped."""
        return self._root_zone_id

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

        Creates a local ZoneConsensus node without bootstrapping ConfState.
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
        result: list[str] = self._py_mgr.list_zones()
        return result

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def advertise_addr(self) -> str:
        """Public address this node advertises to peers."""
        addr: str = self._py_mgr.advertise_addr()
        return addr

    @property
    def tls_cert_path(self) -> str | None:
        """TLS certificate path (if configured)."""
        return self._tls_cert_path

    @property
    def tls_key_path(self) -> str | None:
        """TLS key path (if configured)."""
        return self._tls_key_path

    @property
    def tls_ca_path(self) -> str | None:
        """TLS CA certificate path (if configured)."""
        return self._tls_ca_path

    def mount(
        self,
        parent_zone_id: str,
        mount_path: str,
        target_zone_id: str,
    ) -> None:
        """Mount a zone at a path in another zone (NFS-style, strict).

        The mount point must already exist as a DT_DIR entry in the parent
        zone — matching Linux NFS behavior where the mount point directory
        must be created beforehand (``mkdir -p /mnt/nfs && mount ...``).

        The existing DT_DIR is replaced with a DT_MOUNT entry that routes
        all child path access to the target zone.

        No implicit directory creation: callers must ensure the mount point
        and all its parents exist first. See Task #125 for future ``-p``
        auto-create option.

        Args:
            parent_zone_id: Zone containing the mount point.
            mount_path: Path in parent zone where target is mounted.
                Must already exist as DT_DIR.
            target_zone_id: Zone to mount.

        Raises:
            ValueError: If mount_path doesn't exist, is not DT_DIR, or
                is already a DT_MOUNT.
            RuntimeError: If parent or target zone doesn't exist.
        """
        parent_store = self.get_store(parent_zone_id)
        if parent_store is None:
            raise RuntimeError(f"Parent zone '{parent_zone_id}' not found")

        target_store = self.get_store(target_zone_id)
        if target_store is None:
            raise RuntimeError(f"Target zone '{target_zone_id}' not found")

        # NFS compliance: mount point must exist as a directory
        existing = parent_store.get(mount_path)
        if existing is None:
            raise ValueError(
                f"Mount point '{mount_path}' does not exist in zone "
                f"'{parent_zone_id}'. Create the directory first (mkdir -p)."
            )
        if existing.is_mount:
            raise ValueError(
                f"Mount point '{mount_path}' is already a DT_MOUNT in zone "
                f"'{parent_zone_id}'. Unmount first."
            )
        if existing.entry_type != DT_DIR:
            raise ValueError(
                f"Mount point '{mount_path}' is not a directory "
                f"(type={existing.entry_type}) in zone '{parent_zone_id}'. "
                f"Mount points must be directories."
            )

        # Replace DT_DIR with DT_MOUNT (shadows original directory contents)
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

        # Increment target zone's i_links_count (POSIX: link() → nlink++)
        self._increment_links(target_store, target_zone_id)

        logger.info(
            "Mounted zone '%s' at '%s' in zone '%s'",
            target_zone_id,
            mount_path,
            parent_zone_id,
        )

    def unmount(self, parent_zone_id: str, mount_path: str) -> None:
        """Remove a mount point, restoring the original DT_DIR.

        Replaces the DT_MOUNT entry with a DT_DIR (NFS behavior: ``umount``
        reveals the original mount point directory) and decrements the target
        zone's i_links_count (POSIX: unlink() → nlink--).

        Any entries that were shadowed by the DT_MOUNT become visible again
        (stale entries from share_subtree are harmless per federation-memo §6).

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

        # Restore DT_DIR at mount point (NFS: umount reveals original directory)
        restored_dir = FileMetadata(
            path=mount_path,
            backend_name="virtual",
            physical_path="",
            size=0,
            entry_type=DT_DIR,
            zone_id=parent_zone_id,
        )
        parent_store.put(restored_dir)

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

    def share_subtree(
        self,
        parent_zone_id: str,
        path: str,
        peers: list[str] | None = None,
        zone_id: str | None = None,
    ) -> str:
        """Share a subtree by creating a new zone and copying metadata into it.

        Steps:
        1. Create a new zone (auto UUID if zone_id not provided)
        2. List all entries under path in parent zone
        3. Copy each entry to new zone (rebase paths: /a/b/foo → /foo)
        4. Replace path with DT_MOUNT in parent zone (shadows old entries)
        NO deletion — old entries are harmless, shadowed by DT_MOUNT.

        Args:
            parent_zone_id: Zone containing the subtree to share.
            path: Path prefix to share (e.g., "/usr/alice/projectA").
            peers: Peer addresses for the new zone.
            zone_id: Explicit zone ID (auto-generated UUID if None).

        Returns:
            The new zone's ID.

        Raises:
            RuntimeError: If parent zone not found.
            ValueError: If path is already a DT_MOUNT.
        """
        import uuid

        parent_store = self.get_store(parent_zone_id)
        if parent_store is None:
            raise RuntimeError(f"Parent zone '{parent_zone_id}' not found")

        # Check path isn't already a mount
        existing = parent_store.get(path)
        if existing is not None and existing.is_mount:
            raise ValueError(f"'{path}' is already a DT_MOUNT in zone '{parent_zone_id}'")

        # Generate zone ID
        new_zone_id = zone_id or str(uuid.uuid4())

        # Step 1: Create new zone
        new_store = self.create_zone(new_zone_id, peers=peers)

        # Step 2: List all entries under path (including path itself if it's a dir)
        # Normalize: ensure path ends without trailing slash for prefix matching
        prefix = path.rstrip("/")
        entries = list(parent_store.list_iter(prefix=prefix, recursive=True))

        # Step 3: Copy entries to new zone with path rebasing
        from dataclasses import replace

        for entry in entries:
            if entry.path == prefix:
                # The root dir becomes "/" in the new zone
                rebased = replace(
                    entry,
                    path="/",
                    zone_id=new_zone_id,
                    entry_type=DT_DIR,
                    i_links_count=1,
                )
            else:
                # Rebase: /usr/alice/projectA/foo → /foo
                relative = entry.path[len(prefix) :]
                if not relative.startswith("/"):
                    relative = "/" + relative
                rebased = replace(entry, path=relative, zone_id=new_zone_id)
            new_store.put(rebased)

        # Ensure new zone has a root "/" even if no entries existed
        if new_store.get("/") is None:
            root_entry = FileMetadata(
                path="/",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id=new_zone_id,
                i_links_count=1,
            )
            new_store.put(root_entry)

        # Step 4: Ensure path exists as DT_DIR (may be implicit directory)
        # mount() requires an explicit DT_DIR entry (NFS compliance).
        if parent_store.get(path) is None:
            parent_store.put(
                FileMetadata(
                    path=path,
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id=parent_zone_id,
                )
            )

        # Step 5: Replace DT_DIR with DT_MOUNT (shadows old entries)
        self.mount(parent_zone_id, path, new_zone_id)

        logger.info(
            "Shared subtree '%s' from zone '%s' → new zone '%s' (%d entries copied)",
            path,
            parent_zone_id,
            new_zone_id,
            len(entries),
        )
        return new_zone_id

    # =========================================================================
    # Static Bootstrap (Day 1 cluster formation)
    # =========================================================================

    def bootstrap_static(
        self,
        zones: list[str],
        peers: list[str],
        mounts: dict[str, str] | None = None,
    ) -> None:
        """Static Day-1 cluster formation: create Raft groups for all zones.

        All nodes in the cluster call this with identical parameters during
        startup. Each node creates Raft groups locally (ConfState bootstrap)
        for every zone with the full peer list.

        This is Phase 1 only — Raft group creation is local, no consensus.
        Mount topology (Phase 2) is deferred to ensure_topology(), which
        runs after leader election via the server health check lifecycle.

        Idempotent — safe to call on every startup. Skips existing zones.

        For Day 2+ dynamic membership changes (adding/removing nodes at
        runtime), see expand_zone() [tracked, not yet implemented].

        Args:
            zones: Non-root zone IDs to create (e.g., ["corp", "corp-eng"]).
                Root zone must already exist via bootstrap().
            peers: Peer addresses shared by all zones ("id@host:port" format).
                Every zone uses the same Raft group membership.
            mounts: Stored for deferred application by ensure_topology().
                Global path → target zone mapping
                (e.g., {"/corp": "corp", "/corp/engineering": "corp-eng"}).

        Raises:
            RuntimeError: If bootstrap() has not been called first.
        """
        if not self._root_zone_id:
            raise RuntimeError("Must call bootstrap() before bootstrap_static()")

        # Phase 1: Create Raft groups for all zones with peers
        # This is a local operation — each node initializes its own Raft
        # state machine. No consensus needed; raft-rs handles election.
        for zone_id in zones:
            if self.get_store(zone_id) is not None:
                logger.debug("Zone '%s' already exists, skipping", zone_id)
                continue
            self.create_zone(zone_id, peers=peers)

        # Store pending mounts for deferred application after leader election
        if mounts:
            self._pending_mounts = mounts

    def ensure_topology(self) -> bool:
        """Ensure root "/" and mount topology exist via standard Raft proposals.

        Shared readiness gate for both static and dynamic paths:

        - **Static (Day 1)**: Called by server health check after initial
          leader election. Leader creates root "/" + mounts via normal Raft
          proposals; followers receive them via log replication.

        - **Dynamic (Recovery)**: Called after campaign() + wait_for_leader()
          re-elects a leader. Validates persisted topology (idempotent no-op
          when data survived in Raft snapshots/redb).

        All operations use standard Raft: is_leader() + propose(). No custom
        algorithms. Idempotent — safe to call repeatedly on any node.

        Returns:
            True if topology is fully ready (root "/" + all mounts exist on
            this node), False if still waiting for leader election or
            log replication.
        """
        if self._topology_initialized:
            return True

        if not self._root_zone_id:
            return False

        root_store = self.get_store(self._root_zone_id)
        if root_store is None:
            return False

        # Check if root "/" exists on this node (replicated or persisted)
        root = root_store.get("/")
        if root is None:
            # Root missing — leader needs to create it
            return self._try_apply_topology(root_store)

        # Root exists — verify pending mounts if any
        if self._pending_mounts and not self._all_mounts_ready():
            # Some mounts missing — leader needs to apply them.
            # Followers wait for replication.
            return self._try_apply_topology(root_store)

        # All topology in place — works for both:
        # - Static: leader created, followers received via replication
        # - Dynamic: data persisted in Raft snapshots across restart
        self._pending_mounts = None
        self._topology_initialized = True
        return True

    def _all_mounts_ready(self) -> bool:
        """Check if all pending mounts have been applied on this node.

        Uses target zone's i_links_count as mount indicator: mount() always
        calls _increment_links() on the target zone. If i_links_count >= 1,
        the mount was applied and replicated to this node.

        This avoids complex zone-aware path resolution — works correctly
        for both top-level (/corp) and nested (/corp/engineering) mounts,
        and on both leader and follower nodes.
        """
        if not self._pending_mounts:
            return True
        for target_zone_id in self._pending_mounts.values():
            target_store = self.get_store(target_zone_id)
            if target_store is None:
                return False
            target_root = target_store.get("/")
            if target_root is None or target_root.i_links_count < 1:
                return False
        return True

    def _try_apply_topology(self, root_store: RaftMetadataStore) -> bool:
        """Create root "/" and apply mount topology via standard Raft proposals.

        Only succeeds if this node is the Raft leader for the root zone.
        Followers return False and wait for log replication.

        Idempotent — skips existing root and mounts.

        Returns:
            True if topology was created/exists, False if not leader or
            leadership changed during application.
        """
        # Standard Raft: only leader can propose
        try:
            engine = root_store._engine  # noqa: SLF001
            if engine is None or not hasattr(engine, "is_leader") or not engine.is_leader():
                return False
        except Exception:
            return False

        try:
            # Create root "/" via normal Raft proposal (idempotent)
            root = root_store.get("/")
            if root is None:
                root_entry = FileMetadata(
                    path="/",
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id=self._root_zone_id,
                    i_links_count=1,
                )
                root_store.put(root_entry)
                logger.info(
                    "Leader: created root '/' in zone '%s' via Raft consensus",
                    self._root_zone_id,
                )

            # Apply mount topology via normal Raft proposals (idempotent)
            if self._pending_mounts:
                self._apply_mounts(self._pending_mounts)
                self._pending_mounts = None

            self._topology_initialized = True
            return True

        except RuntimeError as e:
            # "not leader" — leadership may have changed during writes
            logger.debug("Topology creation deferred: %s", e)
            return False

    def _apply_mounts(self, mounts: dict[str, str]) -> None:
        """Apply mount topology via normal Raft proposals (leader only).

        Called by ensure_topology() after leader election. Writes go through
        Raft consensus — only the leader proposes; followers receive via
        log replication. This follows the standard Raft contract.

        Mounts are specified as global paths (e.g., "/corp/engineering")
        and resolved to the correct parent zone via longest-prefix matching
        against already-active mounts.

        Args:
            mounts: Global path → target zone mapping.
        """
        assert self._root_zone_id is not None

        # Process mounts in path-depth order (parents before children)
        sorted_mounts = sorted(mounts.items(), key=lambda x: x[0].count("/"))

        # Track active mounts for nested path resolution
        active_mounts: dict[str, str] = {}  # global_path → zone_id

        for global_path, target_zone_id in sorted_mounts:
            # Resolve which zone owns this mount point
            parent_zone = self._root_zone_id
            local_path = global_path

            # Find longest-prefix active mount (nested mount resolution)
            for mount_path in sorted(active_mounts, key=len, reverse=True):
                if global_path.startswith(mount_path + "/"):
                    parent_zone = active_mounts[mount_path]
                    local_path = global_path[len(mount_path) :]
                    break

            parent_store = self.get_store(parent_zone)
            if parent_store is None:
                logger.warning(
                    "Parent zone '%s' not found for mount '%s', skipping",
                    parent_zone,
                    global_path,
                )
                continue

            # Check if already mounted (idempotent)
            existing = parent_store.get(local_path)
            if existing is not None and existing.is_mount:
                logger.debug("Mount '%s' already exists, skipping", global_path)
                active_mounts[global_path] = target_zone_id
                continue

            # Create directory at mount point if it doesn't exist
            if existing is None:
                dir_entry = FileMetadata(
                    path=local_path,
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id=parent_zone,
                )
                parent_store.put(dir_entry)

            # Mount (standard Raft proposal — DT_MOUNT + i_links_count)
            self.mount(parent_zone, local_path, target_zone_id)
            active_mounts[global_path] = target_zone_id

        logger.info(
            "Static topology applied: %d mounts via Raft consensus",
            len(active_mounts),
        )

    def shutdown(self) -> None:
        """Shut down all zones and the gRPC server."""
        self._py_mgr.shutdown()
        self._stores.clear()
        logger.info("ZoneManager shut down")
