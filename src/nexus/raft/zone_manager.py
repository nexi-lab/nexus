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

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_DIR, DT_MOUNT, FileMetadata

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig
    from nexus.storage.raft_metadata_store import RaftMetadataStore

logger = logging.getLogger(__name__)


def _get_py_zone_manager() -> type | None:
    """Import PyO3 ZoneManager from ``nexus_kernel`` (F2 C8 merged the raft
    PyO3 classes into the kernel cdylib). Uses ``getattr`` so local stale
    stubs don't trip mypy while a freshly-built wheel is pending.
    """
    try:
        import nexus_kernel as _nk
    except ImportError:
        return None
    py_zm = getattr(_nk, "ZoneManager", None)
    return py_zm if isinstance(py_zm, type) else None


def _make_py_zone_manager(
    py_zone_manager: type,
    *,
    hostname: str,
    base_path: str,
    peers: list[str],
    bind_addr: str,
    tls_cert_path: str | None,
    tls_key_path: str | None,
    tls_ca_path: str | None,
    ca_key_path: str | None,
    join_token_hash: str | None,
) -> Any:
    """Construct the PyO3 ZoneManager.

    `peers` is forwarded to the Rust side so the registry can enumerate
    pre-existing zones from disk at construction time (R15.e). The
    enumeration runs before the gRPC server starts accepting traffic,
    eliminating the race where an inbound vote/append arrives for a
    zone that has on-disk state but has not yet been reloaded into
    memory.
    """
    return py_zone_manager(
        hostname,
        base_path,
        peers,
        bind_addr=bind_addr,
        tls_cert_path=tls_cert_path,
        tls_key_path=tls_key_path,
        tls_ca_path=tls_ca_path,
        ca_key_path=ca_key_path,
        join_token_hash=join_token_hash,
    )


class ZoneManager:
    """Manage multiple Raft zones and their metadata stores.

    Usage:
        mgr = ZoneManager(hostname="nexus-1", base_path="/var/lib/nexus/zones",
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
        hostname: str,
        base_path: str,
        peers: list[str],
        bind_addr: str = "0.0.0.0:2126",
        *,
        advertise_addr: str | None = None,
        tls_cert_path: str | None = None,
        tls_key_path: str | None = None,
        tls_ca_path: str | None = None,
    ):
        PyZoneManager = _get_py_zone_manager()
        if PyZoneManager is None:
            raise RuntimeError(
                "ZoneManager requires PyO3 build with --features full. "
                "Build with: maturin develop -m rust/raft/Cargo.toml --features full"
            )

        self._hostname = hostname

        from nexus.security.tls.config import ZoneTlsConfig

        # SSOT: NEXUS_RAFT_TLS controls ALL TLS behavior.
        # When false, skip cert generation/detection AND tell Rust to use plaintext.
        raft_tls = os.environ.get("NEXUS_RAFT_TLS", "true").lower()
        self._use_tls = raft_tls not in ("false", "0", "no")

        self._tls_config: ZoneTlsConfig | None = None
        ca_key_path: str | None = None
        join_token_hash: str | None = None

        if not self._use_tls:
            logger.info("NEXUS_RAFT_TLS=false — Raft transport running without TLS")
        elif tls_cert_path is None and tls_key_path is None and tls_ca_path is None:
            existing = ZoneTlsConfig.from_data_dir(base_path)
            if existing is not None:
                # Certs exist (from auto-generate, previous run, or pre-provisioned join)
                tls_cert_path = str(existing.node_cert_path)
                tls_key_path = str(existing.node_key_path)
                tls_ca_path = str(existing.ca_cert_path)
                self._tls_config = existing
                hash_path = Path(base_path) / "tls" / "join-token-hash"
                if hash_path.exists():
                    join_token_hash = hash_path.read_text().strip()
                    ca_key_path = str(Path(base_path) / "tls" / "ca-key.pem")
                logger.debug("Auto-detected existing TLS certs in %s/tls/", base_path)
            else:
                # No certs → auto-generate (first node bootstrap)
                auto = self._auto_generate_tls(base_path, hostname)
                if auto is not None:
                    tls_cert_path = str(auto.node_cert_path)
                    tls_key_path = str(auto.node_key_path)
                    tls_ca_path = str(auto.ca_cert_path)
                    self._tls_config = auto
                    hash_path = Path(base_path) / "tls" / "join-token-hash"
                    if hash_path.exists():
                        join_token_hash = hash_path.read_text().strip()
                        ca_key_path = str(Path(base_path) / "tls" / "ca-key.pem")

        self._py_mgr = _make_py_zone_manager(
            PyZoneManager,
            hostname=hostname,
            base_path=base_path,
            peers=peers,
            bind_addr=bind_addr,
            tls_cert_path=tls_cert_path,
            tls_key_path=tls_key_path,
            tls_ca_path=tls_ca_path,
            ca_key_path=ca_key_path,
            join_token_hash=join_token_hash,
        )
        self._stores: dict[str, RaftMetadataStore] = {}
        self._node_id = self._py_mgr.node_id
        self._base_path = base_path
        self._advertise_addr = advertise_addr or bind_addr
        self._bind_addr = bind_addr
        self._root_zone_id: str | None = None
        self._tls_cert_path = tls_cert_path
        self._tls_key_path = tls_key_path
        self._tls_ca_path = tls_ca_path
        self._pending_mounts: dict[str, str] | None = None
        self._topology_initialized = False
        # Receives dcache invalidation on mount/unmount (dcache entries
        # under a changed mount point become stale).
        self._dcache_proxy: Any | None = None
        self._coordinator: Any | None = None  # late-bound: DriverLifecycleCoordinator
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()

    @property
    def tls_config(self) -> "ZoneTlsConfig | None":
        """Resolved TLS config (auto-generated or from explicit paths)."""
        if self._tls_config is not None:
            return self._tls_config
        # Build from explicit paths if all three were provided
        if self._tls_cert_path and self._tls_key_path and self._tls_ca_path:
            from nexus.security.tls.config import ZoneTlsConfig

            return ZoneTlsConfig(
                ca_cert_path=Path(self._tls_ca_path),
                node_cert_path=Path(self._tls_cert_path),
                node_key_path=Path(self._tls_key_path),
                known_zones_path=Path(self._base_path) / "tls" / "known_zones",
            )
        return None

    @property
    def advertise_addr(self) -> str:
        """Routable address for peers to connect to this node."""
        return self._advertise_addr

    @staticmethod
    def _auto_generate_tls(base_path: str, hostname: str) -> "ZoneTlsConfig | None":
        """Auto-generate TLS certs on first startup; reuse on subsequent starts."""
        from nexus.security.tls.config import ZoneTlsConfig

        existing = ZoneTlsConfig.from_data_dir(base_path)
        if existing is not None:
            logger.debug("Auto-detected existing TLS certs in %s/tls/", base_path)
            return existing

        # Generate new CA + node cert + join token
        try:
            from nexus.raft.peer_address import hostname_to_node_id
            from nexus.security.tls.certgen import (
                cert_fingerprint,
                generate_node_cert,
                generate_zone_ca,
                save_pem,
            )
            from nexus.security.tls.join_token import generate_join_token

            node_id = hostname_to_node_id(hostname)
            tls_dir = Path(base_path) / "tls"
            zone_id = ROOT_ZONE_ID
            ca_cert, ca_key = generate_zone_ca(zone_id)
            save_pem(tls_dir / "ca.pem", ca_cert)
            save_pem(tls_dir / "ca-key.pem", ca_key, is_private=True)

            node_cert, node_key = generate_node_cert(
                node_id, zone_id, ca_cert, ca_key, hostname=hostname
            )
            save_pem(tls_dir / "node.pem", node_cert)
            save_pem(tls_dir / "node-key.pem", node_key, is_private=True)

            # Generate join token for K3s-style cluster bootstrap (#2694)
            token, pw_hash = generate_join_token(ca_cert)
            (tls_dir / "join-token").write_text(token)
            (tls_dir / "join-token-hash").write_text(pw_hash)

            fp = cert_fingerprint(ca_cert)
            logger.info("Auto-generated TLS certs (CA fingerprint: %s)", fp)
            logger.info("Join token: %s", token)
            return ZoneTlsConfig.from_data_dir(base_path)
        except Exception:
            logger.debug(
                "TLS auto-generation skipped (cryptography not available or error)", exc_info=True
            )
            return None

    def bootstrap(
        self,
        root_zone_id: str = ROOT_ZONE_ID,
        peers: list[str] | None = None,
    ) -> "RaftMetadataStore":
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
    ) -> "RaftMetadataStore":
        """Create a new zone and return its RaftMetadataStore.

        Only creates the Raft group + redb database. Does NOT create a
        root "/" entry — that's the responsibility of:
        - Node bootstrap (root zone)
        - share_subtree() / _apply_topology() for non-root zones

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
    ) -> "RaftMetadataStore":
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

    def get_store(self, zone_id: str) -> "RaftMetadataStore | None":
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
                count = store.get_zone_links_count()
                if count > 0:
                    raise ValueError(
                        f"Zone '{zone_id}' still has {count} reference(s) "
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

    def reconcile_mounts_from_raft(self) -> int:
        """Sync DLC mount entries from each local zone's replicated state.

        federation_mount only runs on the node that received the RPC. The
        resulting DT_MOUNT metadata replicates via Raft to follower nodes,
        but without a local ``DriverLifecycleCoordinator.mount`` call the
        kernel's mount table stays unaware of the federation mount and
        sys_* cold paths fall through to the global metastore.

        This method scans every local zone's state machine for DT_MOUNT
        entries and, for each one whose target zone is present on this
        node, calls ``self.mount(..., idempotent)``. The updated mount
        code already skips the put-to-raft side when the DT_MOUNT is
        already present, but still wires DLC — so it's safe to call
        repeatedly.

        Returns the number of DLC entries ensured in this pass.
        """
        if self._coordinator is None:
            return 0

        ensured = 0
        # Iterate a snapshot since zone_manager.mount() may mutate _stores.
        zones_snapshot = list(self._stores.items())
        for zone_id, store in zones_snapshot:
            try:
                entries = list(store.list_iter(prefix="/", recursive=True))
            except Exception as exc:
                logger.debug("reconcile_mounts: list_iter(%s) failed: %s", zone_id, exc)
                continue
            for meta in entries:
                if not getattr(meta, "is_mount", False):
                    continue
                target_zone_id = getattr(meta, "target_zone_id", None) or ""
                if not target_zone_id:
                    continue
                if target_zone_id not in self._stores:
                    # Target zone hasn't been created on this node yet.
                    continue
                # Compute the global path that matches how
                # zone_manager.mount() keyed DLC on the source node.
                local_path = meta.path
                global_path = local_path
                # For non-root parent zones the zone_manager.mount source
                # side passes a global_path. Replicated DT_MOUNT entries
                # store only the zone-relative local_path, so prepend the
                # parent zone id (e.g. zone='corp', local='/eng' →
                # global='/corp/eng') to match the original DLC key.
                if (
                    zone_id != self._root_zone_id
                    and zone_id
                    and not local_path.startswith(f"/{zone_id}")
                ):
                    global_path = f"/{zone_id}{local_path}"
                try:
                    self.mount(
                        zone_id,
                        local_path,
                        target_zone_id,
                        global_path=global_path,
                        increment_links=False,
                    )
                    ensured += 1
                except Exception as exc:
                    logger.debug(
                        "reconcile_mounts: mount(%s %s -> %s) skipped: %s",
                        zone_id,
                        local_path,
                        target_zone_id,
                        exc,
                    )
        if ensured:
            logger.info(
                "[MOUNT-RECONCILE] ensured %d DLC mount entries from raft state",
                ensured,
            )
        return ensured

    def start_mount_reconciler(self, interval_seconds: float = 1.0) -> None:
        """Start a background thread that reconciles DLC mounts from raft.

        Federation_mount runs on whichever node the client hit, so the
        resulting DT_MOUNT metadata only propagates to peer nodes via
        Raft replication. Without a listener, the peer's kernel mount
        table stays empty and cross-node reads fall back to the global
        metastore. This thread polls every ``interval_seconds`` and
        idempotently applies any DT_MOUNT entries whose target zones
        are local to this node.

        Called from the service-link phase once the DLC is wired in.
        """
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            return
        self._reconcile_stop.clear()

        def _loop() -> None:
            while not self._reconcile_stop.wait(interval_seconds):
                try:
                    self.reconcile_mounts_from_raft()
                except Exception as exc:
                    logger.debug("[MOUNT-RECONCILE] iteration failed: %s", exc)

        t = threading.Thread(
            target=_loop,
            name="nexus-mount-reconcile",
            daemon=True,
        )
        t.start()
        self._reconcile_thread = t
        logger.info(
            "[MOUNT-RECONCILE] background thread started (interval=%.1fs)",
            interval_seconds,
        )

    def stop_mount_reconciler(self) -> None:
        """Stop the background mount reconciler (for shutdown)."""
        self._reconcile_stop.set()
        thread = self._reconcile_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._reconcile_thread = None

    @property
    def node_id(self) -> int:
        return int(self._node_id)

    def _mount_via_kernel(
        self,
        mount_point: str,
        backend: Any,
        metastore: "RaftMetadataStore",
    ) -> None:
        """Register a mount with the kernel directly (sys_setattr + DLC bookkeeping).

        Replaces the legacy ``DLC.mount()`` shim — calls the Rust kernel
        ``sys_setattr(DT_MOUNT)`` for routing/metastore/dcache wiring,
        then ``_store_mount_info()`` for Python-side bookkeeping.
        """
        import contextlib

        from nexus.core.path_utils import normalize_path

        normalized = normalize_path(mount_point)
        backend_name = backend.name if isinstance(backend.name, str) else str(backend.name)
        zone_handle = getattr(metastore, "_engine", None)

        coordinator = self._coordinator
        if coordinator is None:
            return

        kernel = coordinator._kernel
        if kernel is not None:
            with contextlib.suppress(Exception):
                kernel.sys_setattr(
                    normalized,
                    DT_MOUNT,
                    backend_name,
                    py_backend=backend,
                    py_zone_handle=zone_handle,
                )

        coordinator._store_mount_info(normalized, backend)

    def mount(
        self,
        parent_zone_id: str,
        mount_path: str,
        target_zone_id: str,
        *,
        increment_links: bool = True,
        global_path: str | None = None,
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
            increment_links: If True (default), increment i_links_count on
                target zone. Set to False when the caller (e.g. JoinZone RPC
                handler) has already incremented on the leader side.

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

        # NFS compliance: mount point must exist as a directory.
        # Auto-create DT_DIR if missing (mkdir -p semantics, matches
        # ensure_topology() behavior for static mounts).
        existing = parent_store.get(mount_path)
        if existing is None:
            dir_entry = FileMetadata(
                path=mount_path,
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id=parent_zone_id,
            )
            parent_store.put(dir_entry)
            existing = dir_entry
            logger.info(
                "Auto-created mount point directory '%s' in zone '%s'",
                mount_path,
                parent_zone_id,
            )
        if existing.is_mount:
            if existing.target_zone_id == target_zone_id:
                # Idempotent raft replication: another node already committed
                # the DT_MOUNT into this zone's state machine. We still need
                # to register the mount with THIS node's DLC so kernel syscall
                # routing picks it up — otherwise sys_read on /corp/eng/foo
                # on a follower node falls through to the global metastore
                # and federation replication is invisible.
                if self._coordinator:
                    dlc_path = global_path or mount_path
                    root_backend = self._coordinator.get_root_backend()
                    if root_backend is not None:
                        self._mount_via_kernel(dlc_path, root_backend, target_store)
                logger.debug(
                    "Mount '%s' → '%s' already committed to raft; ensured DLC entry",
                    mount_path,
                    target_zone_id,
                )
                return
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
        # Raft propose() transparently forwards to leader if this node
        # is a follower for the target zone.
        if increment_links:
            self._increment_links(target_store)

        # Register in local mount map via DLC (runtime routing)
        if self._coordinator:
            dlc_path = global_path or mount_path
            root_backend = self._coordinator.get_root_backend()
            if root_backend is not None:
                self._mount_via_kernel(dlc_path, root_backend, target_store)

        # Invalidate proxy dcache — entries resolved through this mount point
        # are now stale (the path prefix routes to a different zone).
        # Clear entire dcache because mount_path is zone-relative but dcache
        # keys are global paths; mounts are rare so full clear is fine.
        if self._dcache_proxy is not None:
            self._dcache_proxy._dcache.clear()

        logger.info(
            "Mounted zone '%s' at '%s' in zone '%s'",
            target_zone_id,
            mount_path,
            parent_zone_id,
        )

    def unmount(
        self, parent_zone_id: str, mount_path: str, *, global_path: str | None = None
    ) -> None:
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

        # Remove from local MountTable via DLC (runtime routing)
        if self._coordinator:
            self._coordinator.unmount(global_path or mount_path)

        # Invalidate proxy dcache — entries cached through this mount point
        # would still resolve into the now-unmounted zone.
        # Clear entire dcache because mount_path is zone-relative but dcache
        # keys are global paths; unmounts are rare so full clear is fine.
        if self._dcache_proxy is not None:
            self._dcache_proxy._dcache.clear()

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
    def _increment_links(store: "RaftMetadataStore") -> int:
        """Increment a zone's i_links_count via atomic Raft command.

        Returns the new count.
        """
        return store.adjust_zone_links_count(1)

    @staticmethod
    def _decrement_links(store: "RaftMetadataStore") -> int:
        """Decrement a zone's i_links_count via atomic Raft command.

        Returns the new count. Never goes below 0 (clamped in state machine).
        """
        return store.adjust_zone_links_count(-1)

    def get_links_count(self, zone_id: str) -> int:
        """Get a zone's current i_links_count.

        Returns 0 if zone or store doesn't exist.
        """
        store = self.get_store(zone_id)
        if store is None:
            return 0
        return store.get_zone_links_count()

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
        entries = [
            e
            for e in parent_store.list_iter(prefix=prefix, recursive=True)
            if e.path == prefix or e.path.startswith(prefix + "/")
        ]

        # Step 3: Copy entries to new zone with path rebasing
        # Track nested DT_MOUNT targets so we can increment their link counts
        from dataclasses import replace

        nested_mount_targets: list[str] = []

        for entry in entries:
            if entry.path == prefix:
                # The root dir becomes "/" in the new zone
                rebased = replace(
                    entry,
                    path="/",
                    zone_id=new_zone_id,
                    entry_type=DT_DIR,
                )
            else:
                # Rebase: /usr/alice/projectA/foo → /foo
                relative = entry.path[len(prefix) :]
                if not relative.startswith("/"):
                    relative = "/" + relative
                rebased = replace(entry, path=relative, zone_id=new_zone_id)
                # Track nested mounts for link count updates
                if entry.is_mount and entry.target_zone_id:
                    nested_mount_targets.append(entry.target_zone_id)
            new_store.put(rebased)

        # Increment link counts for nested mount targets (Finding #4)
        for nested_target_id in nested_mount_targets:
            nested_store = self.get_store(nested_target_id)
            if nested_store is not None:
                self._increment_links(nested_store)

        # Ensure new zone has a root "/" even if no entries existed
        if new_store.get("/") is None:
            root_entry = FileMetadata(
                path="/",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id=new_zone_id,
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

    # -------------------------------------------------------------------------
    # Topology management
    # -------------------------------------------------------------------------

    def ensure_topology(self) -> bool:
        """Ensure root "/" and mount topology exist via standard Raft proposals.

        Shared readiness gate called by health check (every 10s on each node).

        Each zone has an **independent Raft group with independent leadership**.
        A single mount requires writes to TWO zones (parent + target), which
        may have different leaders on different nodes. This method handles
        each write independently:

        - **Phase A**: DT_MOUNT in parent zone (needs parent zone leader)
        - **Phase B**: i_links_count in target zone (needs target zone leader)

        Each node applies what it can (zones where it's leader). Other nodes
        handle their zones. Raft replication propagates results to followers.
        All nodes converge within 1-2 health check intervals (~10-20s).

        Idempotent — safe to call repeatedly on any node.

        Returns:
            True if topology is fully ready on this node, False if still
            waiting for leader writes or Raft replication.
        """
        if self._topology_initialized:
            return True

        if not self._root_zone_id:
            return False

        root_store = self.get_store(self._root_zone_id)
        if root_store is None:
            return False

        # Fast path: check if everything is already replicated to this node
        root = root_store.get("/")
        if root is not None and (not self._pending_mounts or self._all_mounts_ready()):
            self._pending_mounts = None
            self._topology_initialized = True
            return True

        # Not ready — apply what we can (zones where this node is leader)
        return self._apply_topology()

    def _all_mounts_ready(self) -> bool:
        """Check if all pending mounts are fully applied on this node.

        Uses target zone's i_links_count as mount-complete indicator:
        Phase B (_increment_links) sets i_links_count on the target zone.
        The expected count equals the number of mounts referencing that zone.

        Works on both leader and follower nodes (reads replicated state).
        """
        if not self._pending_mounts:
            return True
        # Count expected links per target zone
        from collections import Counter

        expected_counts = Counter(self._pending_mounts.values())
        for target_zone_id, expected in expected_counts.items():
            target_store = self.get_store(target_zone_id)
            if target_store is None:
                return False
            if target_store.get_zone_links_count() < expected:
                return False
        return True

    @staticmethod
    def _is_zone_leader(store: "RaftMetadataStore") -> bool:
        """Check if this node is the Raft leader for the given zone's store."""
        try:
            engine = store._engine  # noqa: SLF001
            return engine is not None and hasattr(engine, "is_leader") and engine.is_leader()
        except Exception:
            return False

    def _apply_topology(self) -> bool:
        """Apply pending topology entries with per-zone fault tolerance.

        Each zone has independent Raft leadership. A single mount requires
        writes to TWO zones (parent for DT_MOUNT, target for i_links_count),
        which may have different leaders on different nodes. This method
        handles each write independently — no cross-zone atomicity needed.

        Per-mount phases:
          Phase A: Create DT_DIR + DT_MOUNT in parent zone
          Phase B: Increment i_links_count in target zone

        Each phase has its own leadership check and error handling.
        Failed phases stay pending for the next ensure_topology() call.

        Returns:
            True if all topology is fully applied, False if still converging.
        """
        assert self._root_zone_id is not None
        root_store = self.get_store(self._root_zone_id)
        if root_store is None:
            logger.debug("Root zone store not ready yet (zone=%s)", self._root_zone_id)
            return False

        # --- Root "/" creation (needs root zone leader) ---
        root = root_store.get("/")
        if root is None:
            if not self._is_zone_leader(root_store):
                return False
            try:
                root_entry = FileMetadata(
                    path="/",
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id=self._root_zone_id,
                )
                root_store.put(root_entry)
                logger.info(
                    "Leader: created root '/' in zone '%s'",
                    self._root_zone_id,
                )
            except RuntimeError as e:
                logger.debug("Root creation deferred: %s", e)
                return False

        # --- Mount topology (per-mount, per-zone fault tolerance) ---
        if not self._pending_mounts:
            self._topology_initialized = True
            return True

        # Process mounts in path-depth order (parents before children)
        sorted_mounts = sorted(self._pending_mounts.items(), key=lambda x: x[0].count("/"))

        # Track active mounts for nested path resolution
        active_mounts: dict[str, str] = {}  # global_path → zone_id
        remaining: dict[str, str] = {}  # mounts not yet fully applied

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
            target_store = self.get_store(target_zone_id)
            if parent_store is None or target_store is None:
                logger.warning(
                    "Zone store missing for mount '%s' (parent=%s, target=%s)",
                    global_path,
                    parent_zone,
                    target_zone_id,
                )
                remaining[global_path] = target_zone_id
                continue

            # --- Phase A: DT_MOUNT in parent zone ---
            existing = parent_store.get(local_path)
            if existing is None or not existing.is_mount:
                if not self._is_zone_leader(parent_store):
                    remaining[global_path] = target_zone_id
                    active_mounts[global_path] = target_zone_id
                    continue
                try:
                    # Create directory if needed
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

                    # Replace DT_DIR with DT_MOUNT
                    mount_entry = FileMetadata(
                        path=local_path,
                        backend_name="mount",
                        physical_path="",
                        size=0,
                        entry_type=DT_MOUNT,
                        target_zone_id=target_zone_id,
                        zone_id=parent_zone,
                    )
                    parent_store.put(mount_entry)

                    # Register in local mount map via DLC (runtime routing)
                    if self._coordinator:
                        _target_store = self.get_store(target_zone_id)
                        root_backend = self._coordinator.get_root_backend()
                        if root_backend is not None and _target_store is not None:
                            self._mount_via_kernel(global_path, root_backend, _target_store)

                    logger.debug(
                        "Phase A done: DT_MOUNT '%s' in zone '%s'", global_path, parent_zone
                    )
                except RuntimeError:
                    remaining[global_path] = target_zone_id
                    active_mounts[global_path] = target_zone_id
                    continue

            # --- Phase B: i_links_count in target zone ---
            # Count how many pending mounts reference this target zone.
            # i_links_count must reflect ALL mount references, not just the first.
            expected_links = sum(1 for _, tz in sorted_mounts if tz == target_zone_id)
            if target_store.get_zone_links_count() < expected_links:
                if not self._is_zone_leader(target_store):
                    remaining[global_path] = target_zone_id
                    active_mounts[global_path] = target_zone_id
                    continue
                try:
                    self._increment_links(target_store)
                    logger.debug("Phase B done: i_links_count for zone '%s'", target_zone_id)
                except RuntimeError:
                    remaining[global_path] = target_zone_id
                    active_mounts[global_path] = target_zone_id
                    continue

            active_mounts[global_path] = target_zone_id

        applied = len(self._pending_mounts) - len(remaining)
        if remaining:
            logger.info(
                "Topology progress: %d/%d mounts applied, %d pending",
                applied,
                len(self._pending_mounts),
                len(remaining),
            )
            self._pending_mounts = remaining
            return False

        logger.info(
            "Static topology applied: %d mounts via Raft consensus",
            len(self._pending_mounts),
        )
        self._pending_mounts = None
        self._topology_initialized = True
        return True

    def shutdown(self) -> None:
        """Shut down all zones and the gRPC server."""
        self._py_mgr.shutdown()
        self._stores.clear()
        logger.info("ZoneManager shut down")
