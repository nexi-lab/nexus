"""FederationRPCService — gRPC RPC surface for federation / zone ops.

Phase H of the rust-workspace restructure: this service no longer
crosses kernel layering.  Federation reads go through the
``/__sys__/zones/`` procfs namespace via ``sys_stat`` / ``sys_readdir``
(read-only, Linux /proc-style).  Federation writes go through:

  * ``kernel.sys_setattr(path, DT_MOUNT, target_zone_id=…)`` for
    mount-tied lifecycle (auto-creates the zone via the kernel's
    `DistributedCoordinator` HAL trait).
  * ``kernel.sys_unlink(<mount_path>)`` for unmount.
  * ``kernel._call("federation_*", {...})`` gRPC Call dispatches for
    standalone zone-control operations that do not involve a mount
    path — analogous to Linux userspace utilities like ``mkfs`` /
    ``zfs``: control-plane bridges that reach kernel internals via the
    DistributedCoordinator trait without being kernel methods themselves.

Registered in ``fastapi_server.create_app`` when federation is active.
"""

from __future__ import annotations

import contextlib
from typing import Any

from nexus.contracts.exceptions import NexusPermissionError
from nexus.contracts.rpc import rpc_expose


class FederationRPCMixin:
    """Mixin providing federation RPC methods with context-aware authentication."""

    @rpc_expose(admin_only=False)
    def federation_client_whoami(self, _context: Any = None) -> dict[str, Any]:
        """Return the caller's zone grants for federation handshake.

        Called by thin clients during SandboxBootstrapper startup to discover
        which zones their bearer token can access and with what permissions.
        Returns a list of {zone_id, permission} dicts from the caller's context.

        Issue #3786: federation handshake for thin client.
        """
        if _context is None:
            raise NexusPermissionError("federation_client_whoami requires authentication")

        # P3-2 multi-zone tokens carry zone_perms: tuple[tuple[str, str], ...]
        zone_perms = getattr(_context, "zone_perms", None)
        if zone_perms:
            zones = [{"zone_id": zid, "permission": perm} for zid, perm in zone_perms]
            return {"zones": zones}

        # P3-1 single-zone tokens carry zone_id; look up actual permission if available.
        # Re-read zone_perms here: OperationContext.__post_init__ always populates it
        # from zone_id (defaulting to "rw"), so a real OperationContext will have a
        # non-empty zone_perms even when zone_id is set without an explicit zone_perms.
        # On raw/mock contexts without __post_init__, default to "rw" (legacy single-zone
        # tokens are write-capable per OperationContext.__post_init__ policy, #3785 F3c).
        zone_id = getattr(_context, "zone_id", None)
        if zone_id:
            perm = "rw"
            _zone_perms = getattr(_context, "zone_perms", None)
            if _zone_perms:
                for zid, p in _zone_perms:
                    if zid == zone_id:
                        perm = p
                        break
            return {"zones": [{"zone_id": zone_id, "permission": perm}]}

        raise NexusPermissionError("token carries no zone grants")


class FederationRPCService(FederationRPCMixin):
    """Federation CRUD RPCs backed by syscalls + module-level
    federation control-plane helpers."""

    def __init__(self, kernel: Any, nexus_fs: Any = None) -> None:
        self._kernel = kernel
        self._nexus_fs = nexus_fs

    def _register_mount_in_python_dlc(self, mount_path: str, parent_zone: str) -> None:
        """Rust kernel handles federation mount registration natively."""
        pass

    # ── Zone snapshot (export / import — R20.17b) ──────────────────

    @rpc_expose(admin_only=True)
    def federation_export_zone(
        self,
        zone_id: str,
        output_path: str,
        include_content: bool = True,
        include_permissions: bool = True,
        include_embeddings: bool = False,
        include_deleted: bool = False,
        path_prefix: str | None = None,
        sign: bool = False,
        strip_credentials: bool = False,
        after_time: str | None = None,
        before_time: str | None = None,
        include_mounts: bool = False,
    ) -> dict[str, Any]:
        """Export a raft-backed zone to a .nexus bundle on the server's
        filesystem.

        Runs server-side so the exporter reaches the real metastore +
        backend rather than a remote proxy. CLI (``nexus zone export``,
        ``nexus archive create``) and the docker E2E suite call this RPC
        when the caller isn't the local owner of the redb file. The
        shared docker volume makes ``output_path`` visible to the CLI
        container; production deployments typically write to a mounted
        backup volume.

        The ``sign``/``strip_credentials``/``after_time``/``before_time``
        parameters drive the v2 archive features used by
        ``nexus archive create`` (#3793). Signing key is loaded from the
        server's ``~/.nexus/archive_signing_key`` (auto-generated on
        first use, TOFU trust model).

        ``include_mounts`` (Issue #4083) opt-in serializes mount configs
        into ``mounts.jsonl``; secret fields (per ConnectionArg.secret)
        are replaced with ``${MOUNT_<id>_<FIELD>}`` placeholders.
        """
        from datetime import datetime
        from pathlib import Path

        from nexus.bricks.portability import ZoneExportOptions, ZoneExportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_export_zone: no NexusFS attached")
        mount_manager = self._build_mount_manager(nx) if include_mounts else None
        service = ZoneExportService(nx, mount_manager=mount_manager)
        options = ZoneExportOptions(
            output_path=Path(output_path),
            include_content=include_content,
            include_permissions=include_permissions,
            include_embeddings=include_embeddings,
            include_deleted=include_deleted,
            path_prefix=path_prefix,
            sign=sign,
            strip_credentials=strip_credentials,
            after_time=datetime.fromisoformat(after_time) if after_time else None,
            before_time=datetime.fromisoformat(before_time) if before_time else None,
            include_mounts=include_mounts,
        )
        manifest = service.export_zone(zone_id, options)
        return {
            "zone_id": zone_id,
            "output_path": str(options.output_path),
            "file_count": manifest.file_count,
            "total_size_bytes": manifest.total_size_bytes,
            "bundle_id": manifest.bundle_id,
            "mount_count": manifest.mount_count,
        }

    @rpc_expose(admin_only=True)
    def federation_import_zone(
        self,
        bundle_path: str,
        target_zone: str | None = None,
        conflict: str = "skip",
        preserve_timestamps: bool = True,
        import_permissions: bool = True,
        path_prefix_remap: dict[str, str] | None = None,
        force: bool = False,
        rebuild_embeddings: bool = False,
        injections: dict[str, str] | None = None,
        require_no_placeholders: bool = True,
        restore_mounts: bool = True,
        mount_overrides: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Import a zone bundle from the server's filesystem.

        Server-side counterpart of ``federation_export_zone``. See that
        method's docstring for the shared-volume / mounted-backup
        deployment model.

        The ``force``/``rebuild_embeddings``/``injections``/
        ``require_no_placeholders`` parameters drive the v2 archive
        restore features used by ``nexus archive restore`` (#3793).

        ``restore_mounts``/``mount_overrides`` (Issue #4083): when the
        bundle contains ``mounts.jsonl``, every redacted field requires
        a value in ``mount_overrides[mount_id][field_name]`` or the
        import raises ``MissingCredentialsError`` before any side effect.
        """
        from pathlib import Path

        from nexus.bricks.portability import ConflictMode, ZoneImportOptions, ZoneImportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_import_zone: no NexusFS attached")
        # Create the raft zone before loading data into it. Import is
        # a restore-into-zone operation, not a create+restore, so the
        # target zone must exist on the raft side.  Routes through
        # the standard syscall surface (sys_setattr DT_MOUNT
        # auto-creates the underlying raft group when the federation
        # provider is initialised) — no PyO3 shortcut to the HAL
        # trait.  The synthetic mount entry under
        # ``/__fed_import__/{target_zone}`` is segregated with a
        # double-underscore prefix so it doesn't collide with normal
        # paths; idempotent on already-exists.
        if target_zone:
            DT_MOUNT = 2
            with contextlib.suppress(Exception):
                # mkdir parent directory; ignore "already exists" via
                # contextlib.suppress (fall through to setattr below).
                self._kernel.sys_setattr(
                    "/__fed_import__",
                    1,  # DT_DIR
                    "",  # backend_name (unused for DT_DIR)
                )
            try:
                self._kernel.sys_setattr(
                    f"/__fed_import__/{target_zone}",
                    DT_MOUNT,
                    backend_name="",
                    zone_id=target_zone,
                )
            except Exception as e:
                if "already" not in str(e).lower() and "exists" not in str(e).lower():
                    raise
        mount_manager = self._build_mount_manager(nx) if restore_mounts else None

        # Server-side audit trail for mount credential injection
        # (Issue #4083 rounds 2/4). Log only mount IDs and field names —
        # never values — so the operator action is captured for
        # forensics without writing secrets to disk. Round-4 reviewer
        # noted the previous "nexus.audit.federation" logger was an
        # orphan with no configured handler. Use this module's logger
        # at WARNING so the event lands in the normal server log
        # (always captured). If a future durable audit sink is wired,
        # this is the obvious callsite to redirect.
        if mount_overrides:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "AUDIT federation_import_zone: mount_overrides supplied for %d mount(s): %s",
                len(mount_overrides),
                {mid: sorted(fields.keys()) for mid, fields in mount_overrides.items()},
            )

        service = ZoneImportService(nx, mount_manager=mount_manager)
        options = ZoneImportOptions(
            bundle_path=Path(bundle_path),
            target_zone_id=target_zone,
            conflict_mode=ConflictMode(conflict),
            preserve_timestamps=preserve_timestamps,
            import_permissions=import_permissions,
            path_prefix_remap=path_prefix_remap or {},
            force=force,
            rebuild_embeddings=rebuild_embeddings,
            injections=injections or {},
            require_no_placeholders=require_no_placeholders,
            restore_mounts=restore_mounts,
            mount_overrides=mount_overrides,
        )
        result = service.import_zone(options)
        # Surface errors and warnings over the wire so the CLI can
        # exit non-zero (Issue #4083 reviewer finding: mount restore
        # failures otherwise look like 'Import Complete'). Each
        # ImportError is a small dataclass; serialize the relevant
        # fields, not the object itself, to keep the RPC payload
        # JSON-friendly.
        return {
            "target_zone": target_zone,
            "files_created": result.files_created,
            "files_updated": result.files_updated,
            "files_skipped": result.files_skipped,
            "files_failed": result.files_failed,
            "permissions_imported": result.permissions_imported,
            "errors": [
                {
                    "path": e.path,
                    "error_type": e.error_type,
                    "message": e.message,
                }
                for e in (result.errors or [])
            ],
            "warnings": list(result.warnings or []),
        }

    @staticmethod
    def _build_mount_manager(nx: Any) -> Any:
        """Construct a MountManager from a NexusFS handle.

        Mirrors the wiring in factory/_wired.py — MetastoreMountStore
        writes through public VFS syscalls, so a live NexusFS is the only
        dependency. Built lazily because the federation RPC service does
        not normally hold a MountManager handle (it doesn't take one in
        __init__), and we only need it when the caller opted into mount
        export / import.
        """
        from nexus.bricks.mount.metastore_mount_store import MetastoreMountStore
        from nexus.bricks.mount.mount_manager import MountManager

        return MountManager(MetastoreMountStore(nx))

    # ── Zone lifecycle ─────────────────────────────────────────────

    @rpc_expose(admin_only=True)
    def federation_create_zone(self, zone_id: str) -> dict[str, Any]:
        # Standalone-create RPC retained for back-compat (CLI / older
        # agents).  Body routes through the syscall surface — no PyO3
        # shortcut to the HAL trait.  ``sys_setattr DT_MOUNT``
        # auto-creates the underlying raft group when the federation
        # provider is initialised; the synthetic mount entry lives
        # under ``/__fed_zones__/{zone_id}`` so it's segregated from
        # the normal namespace and clean for ``federation_remove_zone``
        # to cascade-unmount.
        DT_MOUNT = 2
        with contextlib.suppress(Exception):
            self._kernel.sys_setattr(
                "/__fed_zones__",
                1,  # DT_DIR
                "",  # backend_name (unused for DT_DIR)
            )
        synthetic_path = f"/__fed_zones__/{zone_id}"
        try:
            self._kernel.sys_setattr(
                synthetic_path,
                DT_MOUNT,
                backend_name="",
                zone_id=zone_id,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "already" not in msg and "exists" not in msg and "dt_mount" not in msg:
                raise
        return {"zone_id": zone_id}

    @rpc_expose(admin_only=True)
    def federation_remove_zone(self, zone_id: str, force: bool = False) -> dict[str, Any]:
        # Cascade-unmount happens inside the DistributedCoordinator impl.
        # `force=true` honors the POSIX-style `unlink while i_links > 0`
        # bypass for replication races on followers.
        self._kernel._call("federation_remove_zone", {"zone_id": zone_id, "force": force})
        return {"zone_id": zone_id, "removed": True}

    @rpc_expose(admin_only=True)
    def federation_join(
        self,
        peer_addr: str,
        remote_path: str,
        local_path: str,
    ) -> dict[str, Any]:
        """Join a zone advertised by a peer at `remote_path` and mount
        it locally at `local_path`.

        Discovery uses the raft-replicated share registry in the root
        zone — ``remote_path → zone_id`` is already on this node once
        the sharing node's ``federation_share`` commits. `peer_addr`
        is informational (accepted for CLI parity with ``nexusd
        join``); it's not contacted here because raft already mirrors
        the registry row to every cluster member.
        """
        del peer_addr  # reserved for out-of-cluster bootstrap; unused in raft mode

        # Lookup share via /__sys__/shares/ procfs view (read-only
        # namespace — write happens via federation_share which
        # publishes to the raft-replicated registry).
        share_stat = self._kernel.sys_stat(f"/__sys__/shares{remote_path}", "root")
        zone_id = share_stat.get("zone_id") if share_stat and isinstance(share_stat, dict) else None
        if not zone_id:
            raise LookupError(
                f"No share registered for '{remote_path}'. "
                "The sharing node must call federation_share before federation_join."
            )

        # Join the zone's raft group via the federation control-plane.
        self._kernel._call("federation_join_zone", {"zone_id": zone_id, "as_learner": False})

        # Mount the shared zone at `local_path` so VFS routing reaches
        # it. Derive the local parent zone via sys_stat on the parent
        # directory (§12d: no route() exposure to service tier).
        _parent_dir = local_path.rsplit("/", 1)[0] or "/"
        _parent_stat = self._kernel.sys_stat(_parent_dir, "root")
        parent_zone = (
            (_parent_stat.get("zone_id") or "root")
            if _parent_stat and isinstance(_parent_stat, dict)
            else "root"
        )
        mount_result = self.federation_mount(
            parent_zone=parent_zone,
            path=local_path,
            target_zone=zone_id,
        )
        return {
            "zone_id": zone_id,
            "remote_path": remote_path,
            "local_path": local_path,
            "parent_zone": parent_zone,
            "mount": mount_result,
        }

    # ── Mount topology ─────────────────────────────────────────────

    @rpc_expose(admin_only=True)
    def federation_mount(
        self,
        parent_zone: str,
        path: str,
        target_zone: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Mount ``target_zone`` at ``path`` (global VFS) inside
        ``parent_zone`` via ``sys_setattr(DT_MOUNT)`` — the standard
        mount syscall.  The kernel's `DistributedCoordinator` HAL trait
        auto-creates the target zone if it does not yet exist on this
        node and registers the apply-cb so peers see the mount via
        raft.

        Mount-direction model (NFS / sshfs convention) selects creator
        vs joiner semantics:

        * ``source=None``: ``mount /local-path remote-zone-id`` —
          caller contributes a fresh 1-voter zone (creator semantics).
          Kernel's auto-create branch instantiates the raft group
          locally; subsequent peers join via the source-given path.
        * ``source="grpc://leader-addr:2126"``: ``mount
          remote-addr:/zone-id /local-path`` — caller picks up remote
          metadata via the joiner-side path: kernel sets up a local
          raft replica with ``skip_bootstrap=true``, sends the
          ``JoinZone`` RPC to ``source``, leader proposes
          ConfChangeV2 AddNode + pushes a snapshot.  Bridges the
          dynamic-bootstrap multi-node onboarding workflow into the
          standard mount API.
        """
        # DT_MOUNT entry_type=2 (see rust/kernel/src/core/dcache.rs).
        # Backend params unused for federation mounts — the kernel
        # resolves the metastore via DistributedCoordinator::metastore_for_zone.
        self._kernel.sys_setattr(
            path,
            entry_type=2,
            backend_name="federation",
            zone_id=target_zone,
            source=source,
        )
        self._register_mount_in_python_dlc(path, parent_zone)
        return {
            "parent_zone": parent_zone,
            "path": path,
            "target_zone": target_zone,
            "source": source,
        }

    @rpc_expose(admin_only=True)
    def federation_unmount(
        self,
        parent_zone: str,
        path: str,
    ) -> dict[str, Any]:
        # Standard sys_unlink on a DT_MOUNT entry → unmount.
        # Already-unmounted / never-mounted is a no-op (matches POSIX
        # `umount` of a non-mounted path).
        ctx = {
            "user_id": "federation-rpc",
            "zone_id": parent_zone or "root",
            "is_admin": True,
            "agent_id": None,
            "is_system": True,
            "groups": [],
            "admin_capabilities": [],
            "subject_type": "user",
            "subject_id": None,
            "request_id": "",
            "context_zone_id": None,
        }
        with contextlib.suppress(Exception):
            self._kernel.sys_unlink(path, ctx)
        # Mirror into Python DLC removal: without this the
        # unmounted path stays reachable via the mount-registered path.
        nx = self._nexus_fs
        if nx is not None:
            coord = getattr(nx, "_driver_coordinator", None)
            if coord is not None:
                with contextlib.suppress(Exception):
                    coord.unmount(path, zone_id=parent_zone)
        return {"parent_zone": parent_zone, "path": path, "target_zone": None}

    @rpc_expose(admin_only=True)
    def federation_share(
        self,
        local_path: str,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish `local_path`'s subtree as a standalone federation zone.

        Server derives the parent zone + zone-relative prefix via
        ``sys_stat`` (§12d: no route() exposure to service tier).
        On success, the new zone id is also written into the root
        zone's share registry under ``/__sys__/shares/<local_path>``
        via the federation control-plane helper so peers can resolve
        ``local_path → zone_id`` from their replicated state.

        Args:
            local_path: VFS-global path of the subtree to share.
            zone_id: Name for the newly-created zone. When omitted, a
                `share-{8-hex}` id is generated.

        Returns:
            ``{"zone_id", "copied_entries"}`` from the atomic
            ``federation_share_zone`` control-plane helper.
        """
        import uuid

        new_zone_id = zone_id or f"share-{uuid.uuid4().hex[:8]}"
        # Atomic create + copy + register through the
        # DistributedCoordinator trait. Path decomposition (parent
        # zone, prefix) happens inside the impl via VFSRouter.
        info: dict[str, Any] = dict(
            self._kernel._call(
                "federation_share_zone",
                {"local_path": local_path, "zone_id": new_zone_id},
            )
        )
        return info

    # ── Introspection ──────────────────────────────────────────────

    def _cluster_info(self, zone_id: str) -> dict[str, Any]:
        try:
            return dict(self._kernel._call("federation_cluster_info", {"zone_id": zone_id}))
        except Exception:
            return {}

    @rpc_expose(admin_only=False)
    def federation_list_zones(self) -> dict[str, Any]:
        # /__sys__/zones/ procfs view — read-only, kernel-internal
        # synthesised entries.
        zone_ids: list[str] = [
            p.rstrip("/").rsplit("/", 1)[-1]
            for p, _etype in self._kernel.sys_readdir("/__sys__/zones/", "root")
        ]
        zones = [
            {
                "zone_id": zid,
                "links_count": int(self._cluster_info(zid).get("links_count", 0)),
            }
            for zid in zone_ids
        ]
        return {"zones": zones, "node_id": zone_ids}

    @rpc_expose(admin_only=False)
    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        # Bundled cluster status (term, commit_index, voters, links_count …)
        # — single round-trip through the federation control-plane helper.
        return dict(self._kernel._call("federation_cluster_info", {"zone_id": zone_id}))
