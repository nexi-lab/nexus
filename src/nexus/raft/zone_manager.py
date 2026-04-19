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
    """Construct the PyO3 ZoneManager across hostname/node_id API variants.

    Current Rust builds take ``hostname`` as the first positional arg and
    derive the node ID internally. Older extension builds exposed
    ``node_id`` directly, so we inspect the signature and pass whichever
    shape the loaded binding expects — lets the same Python deploy against
    mixed wheel vintages.

    ``peers`` is forwarded so the Rust registry can enumerate pre-existing
    zones from disk at construction time (R15.e) before the gRPC server
    accepts traffic.
    """
    from inspect import signature

    from nexus.raft.peer_address import hostname_to_node_id

    try:
        first_param = next(iter(signature(py_zone_manager).parameters.values())).name
    except (TypeError, ValueError, StopIteration):
        first_param = "hostname"

    first_arg: str | int = hostname_to_node_id(hostname) if first_param == "node_id" else hostname

    return py_zone_manager(
        first_arg,
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
        # R20.5: target_zone_id -> set of (parent_zone_id, mount_path, global_path)
        # tuples so ``remove_zone(force=True)`` can cascade-unmount every
        # mount pointing at the departing zone. Populated by ``mount()``
        # + ``_on_mount_event`` (catch-up replays), cleared by ``unmount()``.
        self._mounts_by_target: dict[str, set[tuple[str, str, str]]] = {}

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

        R20.5: when ``force=True`` is used to tear down a zone that still
        has active mounts, cascade-unmount every mount pointing at this
        zone FIRST — otherwise the DLC keeps dangling mount entries
        whose ZoneMetastore wraps a destroyed Raft group, and subsequent
        reads at those paths observe corruption instead of a clean
        "zone gone" error. Snapshot of mounts taken before the loop so
        ``unmount()``'s own bookkeeping update doesn't mutate under us.

        Args:
            zone_id: Zone to remove.
            force: If True, skip i_links_count check and cascade-unmount.

        Raises:
            ValueError: If zone still has references (i_links_count > 0)
                and ``force`` is False.
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
        else:
            # R20.5 cascade: copy the set so unmount() can delete from it.
            for parent_zone_id, mount_path, dlc_global in list(
                self._mounts_by_target.get(zone_id, set())
            ):
                try:
                    self.unmount(parent_zone_id, mount_path, global_path=dlc_global)
                except Exception as exc:
                    # Best-effort cascade: log and keep going so one stuck
                    # mount doesn't block the others from being cleaned up.
                    logger.warning(
                        "cascade unmount of %s in %s (target=%s) failed: %s",
                        mount_path,
                        parent_zone_id,
                        zone_id,
                        exc,
                    )
            self._mounts_by_target.pop(zone_id, None)

        self._py_mgr.remove_zone(zone_id)
        self._stores.pop(zone_id, None)
        logger.info("Zone '%s' removed", zone_id)

    def list_zones(self) -> list[str]:
        """List all zone IDs."""
        result: list[str] = self._py_mgr.list_zones()
        return result

    def _on_mount_event(self, parent_zone_id: str, mount_path: str, target_zone_id: str) -> None:
        """DT_MOUNT apply-event callback (R16.2).

        Invoked by the Rust ``PyZoneManager`` consumer task under the
        GIL once per committed DT_MOUNT entry — both for fresh raft
        applies and for entries surfaced by the catch-up scan on
        ``set_mount_hook`` / ``create_zone``. Delegates to the
        idempotent ``mount()`` shim, which no-ops when the entry is
        already wired into DLC.

        Must not raise: the consumer logs exceptions at ``error!`` with
        the full event payload, but this hook should return cleanly
        even for skippable cases (target zone not local yet, DLC not
        wired). The narrow ``logger.debug`` swallows those explicitly.
        """
        if self._coordinator is None:
            # DLC not wired in on this node yet — defer to the catch-up
            # scan that fires again after set_mount_hook is called.
            return
        # After a process restart Rust re-opens persisted zones directly
        # from disk without going through Python's create_zone / join_zone,
        # so self._stores starts empty. get_store() lazily populates it
        # from the Rust registry, so fall through to that check rather
        # than reading _stores raw.
        if self.get_store(target_zone_id) is None:
            # Target zone not local yet. create_zone() re-emits events
            # via the Rust catch-up scan once the local zone appears.
            return

        # Reconstruct the global DLC path: replicated DT_MOUNT entries
        # only carry the zone-relative local_path, so for non-root
        # parent zones we prepend the parent zone id to match the
        # originating node's DLC key (e.g. zone='corp', local='/eng' →
        # global='/corp/eng').
        global_path = mount_path
        if (
            parent_zone_id != self._root_zone_id
            and parent_zone_id
            and not mount_path.startswith(f"/{parent_zone_id}")
        ):
            global_path = f"/{parent_zone_id}{mount_path}"

        try:
            self.mount(
                parent_zone_id,
                mount_path,
                target_zone_id,
                global_path=global_path,
                increment_links=False,
            )
        except Exception as exc:
            # Idempotency + target-not-local already filtered above,
            # so real failures here are worth seeing at debug (full
            # trace is surfaced by the Rust consumer at error!).
            logger.debug(
                "mount-event: mount(%s %s -> %s) skipped: %s",
                parent_zone_id,
                mount_path,
                target_zone_id,
                exc,
            )

    def install_mount_hook(self) -> None:
        """Register the DT_MOUNT apply-event callback with Rust (R16.2).

        Replaces the legacy ``start_mount_reconciler`` polling thread.
        Called from the service-link phase once the DLC is wired in;
        the Rust side runs a catch-up scan of every existing DT_MOUNT
        entry at registration time, so any historic mounts replayed
        before this call still get wired to DLC.
        """
        set_hook = getattr(self._py_mgr, "set_mount_hook", None)
        if set_hook is None:
            logger.warning(
                "PyZoneManager lacks set_mount_hook (stale build?); "
                "DT_MOUNT apply events will not fire — federation mounts "
                "replicated from peers will not appear in this node's DLC"
            )
            return
        set_hook(self._on_mount_event)
        logger.info("DT_MOUNT apply-event hook registered (event-driven reconciler)")

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

        R16.1b: raft-replicated state (DT_MOUNT entry + i_links_count
        bump + DT_DIR auto-create + idempotency check) is handled by
        the Rust ``PyZoneManager.mount`` call. This shim adds the
        Python-only pieces Rust cannot own directly:
        ``DriverLifecycleCoordinator`` bookkeeping + dcache invalidation.

        Args:
            parent_zone_id: Zone containing the mount point.
            mount_path: Path in parent zone where target is mounted.
                Auto-created as DT_DIR if absent (mkdir -p semantics).
            target_zone_id: Zone to mount.
            increment_links: If True (default), increment i_links_count on
                target zone. Set to False when the caller (e.g. JoinZone RPC
                handler) has already incremented on the leader side.
            global_path: Global path used for DLC registration when the
                federation mount is surfaced under a different mount point
                than ``mount_path`` (share_subtree). Defaults to ``mount_path``.

        Raises:
            ValueError: If mount_path is occupied by a non-directory entry
                or by a DT_MOUNT to a different target.
            RuntimeError: If parent or target zone doesn't exist.
        """
        # Raft-replicated state (DT_DIR auto-create + DT_MOUNT put +
        # i_links_count bump + idempotency) — Rust owns this.
        self._py_mgr.mount(
            parent_zone_id,
            mount_path,
            target_zone_id,
            increment_links=increment_links,
        )

        # Python-only DLC bookkeeping: the coordinator tracks per-mount
        # backend references and kernel routing state that has no Rust
        # equivalent yet. Always wired (even on idempotent raft replays)
        # so follower nodes pick up the mount for local syscall routing.
        target_store = self.get_store(target_zone_id)
        if self._coordinator and target_store is not None:
            dlc_path = global_path or mount_path
            root_backend = self._coordinator.get_root_backend()
            if root_backend is not None:
                self._mount_via_kernel(dlc_path, root_backend, target_store)

        # Invalidate proxy dcache — entries resolved through this mount
        # point are now stale. Clear entire dcache because mount_path is
        # zone-relative but dcache keys are global paths; mounts are rare
        # so the full clear is fine.
        if self._dcache_proxy is not None:
            self._dcache_proxy._dcache.clear()

        # R20.5: register for cascade-unmount on remove_zone. Idempotent
        # under catch-up replays because tuple insertion into a set dedupes.
        dlc_global = global_path or mount_path
        self._mounts_by_target.setdefault(target_zone_id, set()).add(
            (parent_zone_id, mount_path, dlc_global)
        )

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

        R16.1b: raft-replicated state (restore DT_DIR + decrement target
        zone's i_links_count) is handled by ``PyZoneManager.unmount``,
        which returns the former ``target_zone_id``. This shim only
        unwires the DLC bookkeeping + dcache.

        Raises ``ValueError`` if the path is not a mount point.
        """
        target_zone_id = self._py_mgr.unmount(parent_zone_id, mount_path)

        # Remove from local MountTable via DLC (runtime routing).
        if self._coordinator:
            self._coordinator.unmount(global_path or mount_path)

        # Invalidate proxy dcache — entries cached through this mount
        # point would still resolve into the now-unmounted zone.
        if self._dcache_proxy is not None:
            self._dcache_proxy._dcache.clear()

        # R20.5: drop the cascade registration. ``target_zone_id`` is
        # the one returned by Rust ``unmount`` (authoritative).
        dlc_global = global_path or mount_path
        bucket = self._mounts_by_target.get(target_zone_id)
        if bucket is not None:
            bucket.discard((parent_zone_id, mount_path, dlc_global))
            if not bucket:
                self._mounts_by_target.pop(target_zone_id, None)

        logger.info(
            "Unmounted '%s' from zone '%s' (target=%s)",
            mount_path,
            parent_zone_id,
            target_zone_id,
        )

    # =========================================================================
    # i_links_count helpers (POSIX i_nlink semantics)
    # =========================================================================

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

        R16.3: the copy+rebase loop, nested-DT_MOUNT link-count bumps,
        and synthetic root-"/" fallback live in Rust
        (``PyZoneManager.share_subtree_core``). This shim handles the
        outer orchestration: precondition checks, UUID generation,
        creating the Python ``RaftMetadataStore`` wrapper, and flipping
        the parent mount point to DT_MOUNT via the R16.1b mount shim.

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

        existing = parent_store.get(path)
        if existing is not None and existing.is_mount:
            raise ValueError(f"'{path}' is already a DT_MOUNT in zone '{parent_zone_id}'")

        new_zone_id = zone_id or str(uuid.uuid4())

        # Create the new zone (registers Python RaftMetadataStore wrapper
        # in self._stores so get_store(new_zone_id) works below).
        self.create_zone(new_zone_id, peers=peers)

        # Rust copy + rebase + nested link-count bumps. Returns the
        # count of entries copied for the log line below.
        copied = self._py_mgr.share_subtree_core(parent_zone_id, path, new_zone_id)

        # Ensure the parent mount point exists as an explicit DT_DIR —
        # NFS compliance requires it so the R16.1b mount shim's
        # idempotency check sees the expected entry_type. (Rust-side
        # share_subtree_core deliberately leaves this to Python because
        # the Rust mount() path already handles auto-create DT_DIR.)
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

        # Flip parent DT_DIR → DT_MOUNT. The mount shim handles DLC
        # wiring + dcache invalidation; the apply-event hook (R16.2)
        # reconciles peer nodes on replication.
        self.mount(parent_zone_id, path, new_zone_id)

        logger.info(
            "Shared subtree '%s' from zone '%s' → new zone '%s' (%d entries copied)",
            path,
            parent_zone_id,
            new_zone_id,
            copied,
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
        """Check if all pending mounts are present on this node as DT_MOUNT.

        R16.4: switched from the i_links_count-based readiness probe (which
        needed the Phase-B leader write to have replicated) to a direct
        DT_MOUNT presence check. The R16.1b ``mount()`` shim writes
        DT_MOUNT + adjusts links_count as one leader-forwarded proposal,
        so once the DT_MOUNT has replicated to this node the topology is
        locally ready.
        """
        if not self._pending_mounts:
            return True

        active_mounts: dict[str, str] = {}
        assert self._root_zone_id is not None
        for global_path, target_zone_id in sorted(
            self._pending_mounts.items(), key=lambda x: x[0].count("/")
        ):
            parent_zone, local_path = self._resolve_mount_parent(global_path, active_mounts)
            parent_store = self.get_store(parent_zone)
            if parent_store is None:
                return False
            existing = parent_store.get(local_path)
            if existing is None or not existing.is_mount:
                return False
            active_mounts[global_path] = target_zone_id
        return True

    def _resolve_mount_parent(
        self, global_path: str, active_mounts: dict[str, str]
    ) -> tuple[str, str]:
        """Compute ``(parent_zone_id, zone_relative_path)`` for a global mount path.

        Longest-prefix match against the already-resolved mounts so nested
        federation mounts (``/corp/eng`` under ``/corp``) end up with
        ``parent_zone = "corp"`` and ``local_path = "/eng"``. Falls back
        to the root zone when no containing mount is present.
        """
        assert self._root_zone_id is not None
        for mount_path in sorted(active_mounts, key=len, reverse=True):
            if global_path.startswith(mount_path + "/"):
                return active_mounts[mount_path], global_path[len(mount_path) :]
        return self._root_zone_id, global_path

    def _apply_topology(self) -> bool:
        """Apply pending topology entries through the R16.1b mount shim.

        R16.4: the old Phase A / Phase B per-mount handwritten raft writes
        collapse into a single ``self.mount()`` call per entry. The Rust
        mount path (``PyZoneManager.mount``) handles auto-create DT_DIR,
        idempotent DT_MOUNT put, ``AdjustCounter(+1)`` on the target's
        ``__i_links_count__``, leader forwarding, and the apply-event
        hook wires DLC on peer nodes automatically. Failures (leader
        unreachable, target not yet opened locally) raise ``RuntimeError``
        and the entry stays in ``_pending_mounts`` for the next call.

        Returns True when every pending mount has applied on this node;
        False while still converging.
        """
        assert self._root_zone_id is not None
        root_store = self.get_store(self._root_zone_id)
        if root_store is None:
            logger.debug("Root zone store not ready yet (zone=%s)", self._root_zone_id)
            return False

        # Root "/" goes through raft propose; forwards to leader
        # transparently. If no leader is up yet, the propose RuntimeErrors
        # and we retry on the next ensure_topology() tick.
        if root_store.get("/") is None:
            try:
                root_store.put(
                    FileMetadata(
                        path="/",
                        backend_name="virtual",
                        physical_path="",
                        size=0,
                        entry_type=DT_DIR,
                        zone_id=self._root_zone_id,
                    )
                )
                logger.info("Created root '/' in zone '%s'", self._root_zone_id)
            except RuntimeError as e:
                logger.debug("Root creation deferred: %s", e)
                return False

        if not self._pending_mounts:
            self._topology_initialized = True
            return True

        # Process mounts in path-depth order (parents before children)
        # so nested-mount resolution picks up the freshly applied parent.
        sorted_mounts = sorted(self._pending_mounts.items(), key=lambda x: x[0].count("/"))
        active_mounts: dict[str, str] = {}
        remaining: dict[str, str] = {}

        for global_path, target_zone_id in sorted_mounts:
            parent_zone, local_path = self._resolve_mount_parent(global_path, active_mounts)
            if self.get_store(target_zone_id) is None or self.get_store(parent_zone) is None:
                logger.warning(
                    "Zone store missing for mount '%s' (parent=%s, target=%s)",
                    global_path,
                    parent_zone,
                    target_zone_id,
                )
                remaining[global_path] = target_zone_id
                continue

            try:
                self.mount(
                    parent_zone,
                    local_path,
                    target_zone_id,
                    global_path=global_path,
                )
                active_mounts[global_path] = target_zone_id
                logger.debug(
                    "Topology: mounted '%s' → zone '%s' (parent=%s, local=%s)",
                    global_path,
                    target_zone_id,
                    parent_zone,
                    local_path,
                )
            except RuntimeError as e:
                # Leader unreachable, proposal rejected, or a transient
                # raft error. Keep it in remaining + active so nested
                # children can still resolve against this parent on
                # the next attempt (the DT_MOUNT will land eventually).
                logger.debug(
                    "Topology: mount('%s' → '%s') deferred: %s",
                    global_path,
                    target_zone_id,
                    e,
                )
                remaining[global_path] = target_zone_id
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
