"""FederationRPCService — gRPC RPC surface for federation / zone ops.

Phase H of the rust-workspace restructure: this service no longer
crosses kernel layering.  Federation reads go through the
``/__sys__/zones/`` procfs namespace via ``sys_stat`` / ``sys_readdir``
(read-only, Linux /proc-style).  Federation writes go through:

  * ``kernel.sys_setattr(path, DT_MOUNT, target_zone_id=…)`` for
    mount-tied lifecycle (auto-creates the zone via the kernel's
    `FederationProvider` HAL trait).
  * ``kernel.sys_unlink(<mount_path>)`` for unmount.
  * ``nexus_runtime.federation_create_zone / remove_zone / join_zone``
    module-level functions for standalone zone-control operations
    that do not involve a mount path — analogous to Linux userspace
    utilities like ``mkfs`` / ``zfs``: control-plane bridges that
    reach kernel internals via the FederationProvider trait without
    being kernel methods themselves.

Registered in ``fastapi_server.create_app`` when federation is active.
"""

from __future__ import annotations

import contextlib
from typing import Any

from nexus.contracts.exceptions import NexusPermissionError
from nexus.contracts.rpc import rpc_expose


class FederationRPCService:
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
    ) -> dict[str, Any]:
        """Export a raft-backed zone to a .nexus bundle on the server's
        filesystem.

        Runs server-side so the exporter reaches the real metastore +
        backend rather than a remote proxy. CLI (``nexus zone export``)
        and the docker E2E suite call this RPC when the caller isn't
        the local owner of the redb file. The shared docker volume
        makes ``output_path`` visible to the CLI container; production
        deployments typically write to a mounted backup volume.
        """
        from pathlib import Path

        from nexus.bricks.portability import ZoneExportOptions, ZoneExportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_export_zone: no NexusFS attached")
        service = ZoneExportService(nx)
        options = ZoneExportOptions(
            output_path=Path(output_path),
            include_content=include_content,
            include_permissions=include_permissions,
            include_embeddings=include_embeddings,
            include_deleted=include_deleted,
            path_prefix=path_prefix,
        )
        manifest = service.export_zone(zone_id, options)
        return {
            "zone_id": zone_id,
            "output_path": str(options.output_path),
            "file_count": manifest.file_count,
            "total_size_bytes": manifest.total_size_bytes,
            "bundle_id": manifest.bundle_id,
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
    ) -> dict[str, Any]:
        """Import a zone bundle from the server's filesystem.

        Server-side counterpart of ``federation_export_zone``. See that
        method's docstring for the shared-volume / mounted-backup
        deployment model.
        """
        from pathlib import Path

        from nexus.bricks.portability import ConflictMode, ZoneImportOptions, ZoneImportService

        nx = self._nexus_fs
        if nx is None:
            raise RuntimeError("federation_import_zone: no NexusFS attached")
        # Create the raft zone before loading data into it. Import is a
        # restore-into-zone operation, not a create+restore, so the
        # target zone must exist on the raft side. Idempotent: a
        # pre-existing zone surfaces as an "already exists" error we
        # swallow, keeping the RPC callable twice with the same args.
        if target_zone:
            try:
                import nexus_runtime as _nr

                _nr.federation_create_zone(self._kernel, target_zone)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
        service = ZoneImportService(nx)
        options = ZoneImportOptions(
            bundle_path=Path(bundle_path),
            target_zone_id=target_zone,
            conflict_mode=ConflictMode(conflict),
            preserve_timestamps=preserve_timestamps,
            import_permissions=import_permissions,
            path_prefix_remap=path_prefix_remap or {},
        )
        result = service.import_zone(options)
        return {
            "target_zone": target_zone,
            "files_created": result.files_created,
            "files_updated": result.files_updated,
            "files_skipped": result.files_skipped,
            "files_failed": result.files_failed,
            "permissions_imported": result.permissions_imported,
        }

    # ── Zone lifecycle ─────────────────────────────────────────────

    @rpc_expose(admin_only=True)
    def federation_create_zone(self, zone_id: str) -> dict[str, Any]:
        # Standalone create (no mount): use the federation control-plane
        # helper.  Mount-tied creation flows through sys_setattr DT_MOUNT
        # which auto-creates via the same FederationProvider trait.
        import nexus_runtime as _nr

        created = _nr.federation_create_zone(self._kernel, zone_id)
        return {"zone_id": created}

    @rpc_expose(admin_only=True)
    def federation_remove_zone(self, zone_id: str, force: bool = False) -> dict[str, Any]:
        # Cascade-unmount happens inside the FederationProvider impl.
        # `force=true` honors the POSIX-style `unlink while i_links > 0`
        # bypass for replication races on followers.
        import nexus_runtime as _nr

        _nr.federation_remove_zone(self._kernel, zone_id, force)
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
        import nexus_runtime as _nr

        _nr.federation_join_zone(self._kernel, zone_id, False)

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
    ) -> dict[str, Any]:
        """Mount ``target_zone`` at ``path`` (global VFS) inside
        ``parent_zone`` via ``sys_setattr(DT_MOUNT)`` — the standard
        mount syscall.  The kernel's `FederationProvider` HAL trait
        auto-creates the target zone if it does not yet exist on this
        node and registers the apply-cb so peers see the mount via
        raft.
        """
        # DT_MOUNT entry_type=2 (see rust/kernel/src/core/dcache.rs).
        # Backend params unused for federation mounts — the kernel
        # resolves the metastore via FederationProvider::metastore_for_zone.
        self._kernel.sys_setattr(
            path,
            entry_type=2,
            backend_name="federation",
            zone_id=target_zone,
        )
        self._register_mount_in_python_dlc(path, parent_zone)
        return {
            "parent_zone": parent_zone,
            "path": path,
            "target_zone": target_zone,
        }

    @rpc_expose(admin_only=True)
    def federation_unmount(
        self,
        parent_zone: str,
        path: str,
    ) -> dict[str, Any]:
        # Standard sys_unlink on a DT_MOUNT entry → unmount.
        ctx = (
            self._kernel.context_for_zone(parent_zone)
            if hasattr(self._kernel, "context_for_zone")
            else None
        )
        try:
            self._kernel.sys_unlink(path, ctx) if ctx is not None else self._kernel.sys_unlink(path)
        except Exception:
            # Already unmounted / never mounted — surface as no-op,
            # matching POSIX `umount` of a non-mounted path.
            pass
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
            ``{"zone_id", "parent_zone_id", "prefix", "entries_copied"}``.
        """
        import uuid

        import nexus_runtime as _nr

        # Derive parent_zone_id and prefix via sys_stat.
        _path_stat = self._kernel.sys_stat(local_path, "root")
        parent_zone_id = (
            (_path_stat.get("zone_id") or "root")
            if _path_stat and isinstance(_path_stat, dict)
            else "root"
        )
        # Find mount point by walking up to nearest DT_MOUNT ancestor.
        _mp = local_path
        while _mp != "/":
            _mp_stat = self._kernel.sys_stat(_mp, "root")
            if _mp_stat and isinstance(_mp_stat, dict) and _mp_stat.get("entry_type") == 2:
                break
            _mp = _mp.rsplit("/", 1)[0] or "/"
        _tail = local_path[len(_mp) :].lstrip("/") if _mp != local_path else ""
        prefix = "/" + _tail if _tail else "/"
        new_zone_id = zone_id or f"share-{uuid.uuid4().hex[:8]}"
        # Federation control-plane helpers — analogous to Linux
        # userspace utilities (mkfs, zfs); call kernel internals
        # (FederationProvider trait) without bypassing layering.
        _nr.federation_create_zone(self._kernel, new_zone_id)
        copied = _nr.federation_zone_share(self._kernel, parent_zone_id, prefix, new_zone_id)
        _nr.federation_register_share(self._kernel, local_path, new_zone_id)
        return {
            "zone_id": new_zone_id,
            "parent_zone_id": parent_zone_id,
            "prefix": prefix,
            "entries_copied": copied,
        }

    # ── Introspection ──────────────────────────────────────────────

    def _links_count(self, zone_id: str) -> int:
        try:
            import nexus_runtime as _nr

            return int(_nr.federation_zone_links_count(self._kernel, zone_id))
        except Exception:
            return 0

    @rpc_expose(admin_only=False)
    def federation_list_zones(self) -> dict[str, Any]:
        # /__sys__/zones/ procfs view — read-only, kernel-internal
        # synthesised entries.
        zone_ids: list[str] = list(self._kernel.sys_readdir_backend("/__sys__/zones/", "root"))
        zones = [{"zone_id": zid, "links_count": self._links_count(zid)} for zid in zone_ids]
        return {"zones": zones, "node_id": zone_ids}

    @rpc_expose(admin_only=False)
    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        # Rich cluster status (term, commit_index, voters, …) does not
        # fit StatResult's struct shape — read it through the
        # federation control-plane helper.  For mere existence /
        # zone_id checks, ``sys_stat("/__sys__/zones/<id>")`` is the
        # cheaper read path.
        import nexus_runtime as _nr

        status: dict[str, Any] = dict(_nr.federation_zone_cluster_info(self._kernel, zone_id))
        status["links_count"] = self._links_count(zone_id)
        return status
